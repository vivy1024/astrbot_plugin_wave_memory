"""Purge legacy junk, temporary migration tables, and low-quality mock assets from production wave_memory.db.

Retains all core infrastructure:
- memories (286k rows)
- fts_memories (fulltext search)
- scoped_tags, scoped_memory_tags, tag_catalog
- person_timeline_events (6476 person facts)
- user_profiles, write_operations, domain_outbox, outbox_deliveries

Purges:
- 106.7w temporary migration tables (scope_recovery_items, migration_actions, etc.)
- Legacy jargon junk (jargon 745, jargon_candidates 300, scoped_jargon 17)
- Fake soul timeline / time anchors (scoped_soul_timeline 3564, time_anchors 2673)
- Legacy mood & concerns (bot_mood, mood_snapshots, concerns)
- Legacy tag transient tables (tag_extraction_status, memory_tags, tags, etc.)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path


TABLES_TO_TRUNCATE = [
    # 1. 历史迁移/恢复施工中间表 (~106.7万行)
    "scope_recovery_items",
    "scope_recovery_memory_map",
    "migration_actions",
    "migration_quarantine",
    "learning_legacy_migration_runs",

    # 2. 黑话系垃圾数据
    "jargon",
    "jargon_candidates",
    "jargon_examples",
    "jargon_concepts",
    "scoped_jargon",

    # 3. 假时间线与时间锚点聊天切片
    "scoped_soul_timeline",
    "time_anchors",
    "bot_mood",
    "mood_snapshots",
    "concerns",

    # 4. 旧标签中间状态与旧标签关系表 (已被 tag_catalog + scoped_tags 替代)
    "tag_extraction_status",
    "tag_intrinsic_residuals",
    "tag_pair_similarity",
    "memory_tags",
    "tag_relations",
    "tags",
]


def purge_junk(db_path: str | Path, run_vacuum: bool = True) -> dict:
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    try:
        existing_tables = set(r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        
        purged_counts: dict[str, int] = {}
        total_rows_purged = 0

        for tbl in TABLES_TO_TRUNCATE:
            if tbl in existing_tables:
                cnt = cursor.execute(f'SELECT count(*) FROM "{tbl}"').fetchone()[0]
                if cnt > 0:
                    cursor.execute(f'DELETE FROM "{tbl}"')
                    purged_counts[tbl] = cnt
                    total_rows_purged += cnt
                else:
                    purged_counts[tbl] = 0

        conn.commit()

        # 检查核心关键表行数
        core_checks = {}
        for tbl in ["memories", "fts_memories", "person_timeline_events", "tag_catalog", "scoped_tags", "scoped_memory_tags"]:
            if tbl in existing_tables:
                core_checks[tbl] = cursor.execute(f'SELECT count(*) FROM "{tbl}"').fetchone()[0]

        vacuum_done = False
        if run_vacuum:
            cursor.execute("VACUUM")
            vacuum_done = True

        return {
            "total_rows_purged": total_rows_purged,
            "purged_tables_detail": purged_counts,
            "core_tables_verified": core_checks,
            "vacuum_executed": vacuum_done,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge legacy junk tables from wave_memory.db")
    parser.add_argument("db_path", nargs="?", default="/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db", help="Path to wave_memory.db")
    parser.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM step")
    args = parser.parse_args()

    print(f"Purging junk from {args.db_path}...")
    res = purge_junk(args.db_path, run_vacuum=not args.no_vacuum)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
