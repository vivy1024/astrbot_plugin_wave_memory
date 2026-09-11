"""Backfill the `familiarity` relationship dimension from real per-user chat volume.

设计：
- 证据只认真人发言：memories.source='chat' AND memory_type='message'，排除 bot/空 sender；AI 生成的 core 摘要不重复计数。
- familiarity = clamp(25 * log10(1 + messages), 0, 100)，四舍五入到 1 位小数（对数饱和，避免大 V 线性碾压长尾）。
- 所有关系证据/AI 摘要统一写客观句「此人在当前群真实发言 N 条」，不写主观人设总结。
- 走权威仓储 upsert_relationship：仅设 familiarity，保留已有 formal 维度并重算 affinity；首次建立关系时也按正式五维权重计算，不再写死 0；幂等（按当前发言量重设，不累加）。
- 默认 dry-run，只读统计与预览；--apply 才写生产库。

用法（在容器内、插件根目录）：
    python -m scripts.backfill_familiarity                 # 所有群 dry-run
    python -m scripts.backfill_familiarity --group 398291136 --apply
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from typing import Any

# 允许脚本独立运行（不依赖 AstrBot 包上下文）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.relationship_policy import compute_affinity  # noqa: E402
from domain.scope import RuntimeScope, SessionRef  # noqa: E402
from engine.db.connection import ConnectionManager  # noqa: E402
from engine.db.scoped_soul_repo import ScopedSoulRepository  # noqa: E402

DEFAULT_DB = os.environ.get(
    "WAVE_MEMORY_DB",
    "/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db",
)
# 本群最活跃真人约 6430 条 → 95；1 条 → 7.5；每多一个数量级 +25。
FAMILIARITY_SCALE = 25.0
EVIDENCE_CAP = 200


def familiarity_for(messages: int) -> float:
    raw = FAMILIARITY_SCALE * math.log10(1.0 + max(0, int(messages)))
    return round(max(0.0, min(100.0, raw)), 1)


def _parse_session_id(session_id: str) -> SessionRef:
    platform_id, kind, conversation_id = session_id.split(":", 2)
    return SessionRef(id=session_id, platform_id=platform_id, kind=kind, conversation_id=conversation_id)


def collect_counts(conn: sqlite3.Connection, group: str | None) -> dict[tuple[str, str, str], dict[str, int]]:
    """(bot_id, session_id, group_id) -> {sender_id: messages}"""
    sql = (
        "SELECT bot_id, session_id, group_id, sender_id, COUNT(*) AS n "
        "FROM memories "
        "WHERE visibility='group' AND source='chat' AND memory_type='message' "
        "  AND sender_id IS NOT NULL AND sender_id NOT IN ('', 'bot') "
    )
    params: tuple[Any, ...] = ()
    if group:
        sql += "AND group_id=? "
        params = (group,)
    sql += "GROUP BY bot_id, session_id, group_id, sender_id"
    out: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in conn.execute(sql, params):
        key = (str(row["bot_id"]), str(row["session_id"]), str(row["group_id"]))
        out.setdefault(key, {})[str(row["sender_id"])] = int(row["n"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--group", default="", help="仅处理该 group_id（QQ 群号）；缺省处理所有群")
    parser.add_argument("--apply", action="store_true", help="真正写入；默认 dry-run")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    counts = collect_counts(conn, args.group or None)

    if not args.apply:
        total_users = sum(len(v) for v in counts.values())
        print(f"[dry-run] 群作用域 {len(counts)} 个，覆盖真人 {total_users} 名（未写入）")
        for (bot_id, session_id, group_id), senders in sorted(counts.items(), key=lambda kv: -len(kv[1]))[:5]:
            preview = sorted(senders.items(), key=lambda kv: -kv[1])[:5]
            print(f"  {bot_id} / {group_id}（{len(senders)} 人） 例:",
                  [(s[-6:], n, familiarity_for(n)) for s, n in preview])
        print("[dry-run] 加 --apply 落库")
        conn.close()
        return 0

    # apply：走权威仓储
    conn.close()
    cm = ConnectionManager(args.db)
    repo = ScopedSoulRepository(cm)
    written = 0
    for (bot_id, session_id, group_id), senders in counts.items():
        session = _parse_session_id(session_id)
        for sender_id, messages in senders.items():
            value = familiarity_for(messages)
            subject = f"{session.platform_id}:user:{sender_id}"
            scope = RuntimeScope(bot_id=bot_id, visibility="group", session=session, subject_principal_id=subject)
            evidence = [{
                "kind": "familiarity_backfill",
                "summary": f"此人在当前群真实发言 {messages} 条",
                "messages": messages,
                "source": "scripts.backfill_familiarity",
            }]
            try:
                repo.upsert_relationship(
                    scope,
                    subject_principal_id=subject,
                    affinity=compute_affinity({"familiarity": value}),
                    dimensions={"familiarity": value},
                    evidence=evidence,
                )
                written += 1
            except Exception as exc:  # 单个失败不影响整体
                print(f"  [skip] {subject}: {exc}")
    cm.close()
    print(f"[apply] 已写入 familiarity：{written} 人")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
