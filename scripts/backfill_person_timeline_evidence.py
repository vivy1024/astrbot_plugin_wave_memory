"""批量回填 person_timeline_events 缺失的原话证据 (source_quote 与 source_memory_id)。

利用每条印象/好感事件的 (bot_id, group_id, user_id, occurred_at)，
在同一 Bot、同一群的 memories 中反查当时群友的发言：
- detail/provenance 已记录的原话与按 ID 查到的原话视为确证，写入 detail；
- 按时间窗口推测的原话只写入 provenance 并标记 source_quote_inferred，不混入 detail。

用法：
    python scripts/backfill_person_timeline_evidence.py [--db PATH] [--limit N] [--dry-run]
非 dry-run 时先把数据库完整备份到同目录 ``*.bak-before-evidence-<时间戳>``，
全部更新在单个事务内完成，任何错误都会整体回滚。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def find_db_path() -> Path:
    candidates = [
        Path("/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db"),
        Path("AstrBot-master/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db"),
        Path("wave_memory.db"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    raise FileNotFoundError("未找到 wave_memory.db")


def backup_database(db_path: Path) -> Path:
    """用 SQLite 在线备份 API 复制完整数据库（含 WAL 中尚未检查点的内容）。"""
    target = db_path.with_name(f"{db_path.name}.bak-before-evidence-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def backfill_evidence(db_path: Path, limit: int = 2000, dry_run: bool = False) -> int:
    if not dry_run:
        logger.info(f"已备份数据库到 {backup_database(db_path)}")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=10000")
    cursor = conn.cursor()
    if not dry_run:
        cursor.execute("BEGIN IMMEDIATE")

    logger.info(f"开始扫描最近 {limit} 条 person_timeline_events (dry_run={dry_run})...")
    rows = cursor.execute("""
        SELECT id, bot_id, group_id, user_id, kind, summary, detail, provenance, occurred_at
        FROM person_timeline_events
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()

    updated_count = 0

    for r in rows:
        eid, bot_id, group_id, user_id, kind, summary, detail, prov_str, occurred_at = r
        prov = {}
        if prov_str:
            try:
                prov = json.loads(prov_str)
            except Exception:
                prov = {}

        existing_quote = str(prov.get("source_quote") or "").strip()
        existing_mid = prov.get("source_memory_id")
        if existing_quote and existing_mid:
            continue

        target_quote = existing_quote
        target_mid = existing_mid
        inferred = False

        # 1. 优先从 detail / summary 中正则提取
        if not target_quote and detail:
            m = re.search(r'原话(?:证据)?[:：]\s*[“"\'「](.*?)[”"\'」]', detail)
            if m:
                target_quote = m.group(1).strip()
        if not target_quote and "原话" in summary:
            m = re.search(r'原话(?:证据)?[:：]\s*[“"\'「](.*?)[”"\'」]', summary)
            if m:
                target_quote = m.group(1).strip()

        # 2. 如果有 mid 查 quote
        if not target_quote and target_mid:
            row_mem = cursor.execute(
                "SELECT content FROM memories WHERE id=? AND bot_id=? AND group_id=?",
                (target_mid, bot_id, group_id),
            ).fetchone()
            if row_mem and row_mem[0]:
                target_quote = str(row_mem[0]).strip()[:150]

        # 3. 如果依然没有，从发生时间点的前后对话窗口中反查当时该群友说的消息
        if not target_quote and bot_id and user_id and group_id and occurred_at:
            msg = cursor.execute("""
                SELECT id, content FROM memories
                WHERE bot_id=? AND group_id=? AND visibility='group'
                  AND (sender_id=? OR sender_name=?)
                  AND timestamp <= ? + 15
                  AND timestamp >= ? - 300
                  AND resolution_state='resolved' AND COALESCE(quarantine,0)=0
                  AND COALESCE(memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')
                ORDER BY timestamp DESC LIMIT 1
            """, (bot_id, group_id, user_id, user_id, occurred_at, occurred_at)).fetchone()
            if msg and msg[1]:
                target_mid = msg[0]
                target_quote = str(msg[1]).strip()[:150]
                inferred = True

        if not target_quote and not target_mid:
            continue

        # 准备更新
        prov["source_quote"] = target_quote
        if target_mid:
            prov["source_memory_id"] = target_mid
        if inferred:
            # 时间窗口推测的原话只写入 provenance 并标记，不混入 detail 冒充确证
            prov["source_quote_inferred"] = True

        new_detail = detail or summary
        if target_quote and not inferred and "原话证据" not in new_detail:
            new_detail = f"{new_detail}\n原话证据：“{target_quote}”"

        updated_count += 1
        if not dry_run:
            cursor.execute("""
                UPDATE person_timeline_events
                SET detail=?, provenance=?
                WHERE id=?
            """, (new_detail, json.dumps(prov, ensure_ascii=False), eid))

    try:
        if not dry_run:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(f"处理完成！成功回填原话证据记录数: {updated_count} / {len(rows)}")
    return updated_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help="wave_memory.db 路径（缺省时自动查找）")
    parser.add_argument("--limit", type=int, default=2000, help="扫描最近多少条事件")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()
    db_file = args.db or find_db_path()
    backfill_evidence(db_file, limit=max(1, args.limit), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
