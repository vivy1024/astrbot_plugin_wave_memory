"""Extract valid person facts (nicknames, aliases, confirmed actions) from 2026-09-06 backup scoped_facts
and backfill them into production person_timeline_events.

Strictly filters out drawing/image-generation junk and identity contamination.
Ensures idempotency via deduplication against existing (user_id, summary).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

QQ_RE = re.compile(r"(\d{5,})")
JUNK_MARKERS = ("生图", "任务ID", "参考图", "画图", "娘化", "壁纸", "视频", "生成图片", "文生图", "图生图")
IDENTITY_MARKERS = ("我是你的", "认我当", "永远听命令", "身份接管")


def _is_junk(text: str) -> bool:
    blob = str(text or "")
    if any(m in blob for m in JUNK_MARKERS):
        return True
    if any(m in blob for m in IDENTITY_MARKERS):
        return True
    return False


def _summary_for(predicate: str, obj: str) -> str:
    pred = str(predicate or "").strip()
    value = str(obj or "").strip()
    if pred == "alias_or_name":
        return f"别名 {value}"[:80]
    if pred == "被称为":
        return f"被称为 {value}"[:80]
    if pred == "认识":
        return f"认识 {value}"[:80]
    return f"{pred} {value}"[:80]


def backfill_facts(
    backup_db: str | Path,
    prod_db: str | Path,
    dry_run: bool = False,
) -> dict:
    conn_b = sqlite3.connect(f"file:{backup_db}?mode=ro", uri=True)
    conn_b.row_factory = sqlite3.Row
    conn_p = sqlite3.connect(str(prod_db))
    conn_p.row_factory = sqlite3.Row

    try:
        # 1. 取得生产库现有的 (user_id, summary) 集合
        existing = {
            (str(r[0]), str(r[1]).strip())
            for r in conn_p.execute("SELECT user_id, summary FROM person_timeline_events").fetchall()
        }

        # 2. 读取备份库 scoped_facts
        rows = conn_b.execute(
            "SELECT id, bot_id, session_id, subject, predicate, object, confidence, created_at FROM scoped_facts"
        ).fetchall()

        pending = []
        counters = Counter()
        now = time.time()

        for r in rows:
            sub = str(r["subject"] or "").strip()
            pred = str(r["predicate"] or "").strip()
            obj = str(r["object"] or "").strip()
            combined = f"{sub} {pred} {obj}"

            if not (sub and pred and obj):
                counters["empty"] += 1
                continue

            if _is_junk(combined):
                counters["junk_draw_or_identity"] += 1
                continue

            m = QQ_RE.search(sub)
            if not m:
                counters["non_qq_world"] += 1
                continue

            user_id = m.group(1)
            summary = _summary_for(pred, obj)

            if (user_id, summary) in existing:
                counters["already_in_prod"] += 1
                continue

            bot_id = str(r["bot_id"] or "yushu").strip()
            session_id = str(r["session_id"] or "").strip()
            group_id = session_id.split(":")[-1] if ":" in session_id else session_id
            confidence = float(r["confidence"] or 0.7)
            occurred_at = float(r["created_at"] or now)
            detail = combined[:200]
            provenance = json.dumps(
                {"source": "backup_20260906_scoped_facts", "scoped_fact_id": r["id"]},
                ensure_ascii=False,
            )

            pending.append((
                bot_id,
                user_id,
                group_id,
                "person_fact",
                summary,
                detail,
                sub,
                pred,
                obj,
                confidence,
                occurred_at,
                r["id"],  # legacy_fact_id 
                provenance,
                now,
            ))
            existing.add((user_id, summary))
            counters["to_insert"] += 1

        if dry_run:
            return {
                "dry_run": True,
                "total_backup_scoped_facts": len(rows),
                "stats": dict(counters),
                "would_insert": len(pending),
                "samples": [
                    {"user_id": p[1], "summary": p[4], "detail": p[5]} for p in pending[:10]
                ],
            }

        insert_sql = """
            INSERT OR IGNORE INTO person_timeline_events (
                bot_id, user_id, group_id, kind, summary, detail,
                subject, predicate, object, confidence, occurred_at,
                legacy_fact_id, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn_p.executemany(insert_sql, pending)
        conn_p.commit()

        total_now = conn_p.execute("SELECT count(*) FROM person_timeline_events").fetchone()[0]
        users_now = conn_p.execute("SELECT count(DISTINCT user_id) FROM person_timeline_events").fetchone()[0]

        return {
            "dry_run": False,
            "total_backup_scoped_facts": len(rows),
            "stats": dict(counters),
            "inserted_count": len(pending),
            "total_timeline_events_now": total_now,
            "total_unique_users_now": users_now,
        }
    finally:
        conn_b.close()
        conn_p.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill valid person facts from backup scoped_facts")
    parser.add_argument("--backup", default="/AstrBot/data/backups_host/wave_memory_before_cleanup_20260906_135411.db")
    parser.add_argument("--prod", default="/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    res = backfill_facts(args.backup, args.prod, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
