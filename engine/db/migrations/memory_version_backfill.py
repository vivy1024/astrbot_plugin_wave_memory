"""v5.1 一次性数据修复：把旧记忆为空的 ``memories.version`` 补成 1。

所有读取方都把空版本当作 1（``COALESCE(version, 1)``、``int(row[0] or 1)``），但 WebUI 签发记忆
对象引用时要求版本是整数，线上约 6.9 万条旧记忆因此打不开详情与上下文、只能只读查看。
补成 1 与现有语义完全一致，不改变任何召回、投影或乐观锁行为。

``memories`` 上只有 ``AFTER UPDATE OF content, sender_name, group_id`` 的全文索引触发器，
只改 version 不会触发它。标记表存在即视为已执行。
"""

from __future__ import annotations

from typing import Any

MARKER_TABLE = "memory_version_backfill_v51"


def backfill_null_memory_versions(cm: Any) -> int:
    """返回补齐的行数；已执行过或缺列时返回 0。"""
    with cm.migration_transaction() as tx:
        tables = {row[0] for row in tx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if MARKER_TABLE in tables or "memories" not in tables:
            return 0
        columns = {str(row[1]) for row in tx.execute("PRAGMA table_info(memories)")}
        if "version" not in columns:
            return 0
        cursor = tx.execute("UPDATE memories SET version = 1 WHERE version IS NULL")
        changed = int(cursor.rowcount or 0)
        tx.execute(f"CREATE TABLE {MARKER_TABLE} (applied_at REAL NOT NULL, rows_updated INTEGER NOT NULL)")
        tx.execute(f"INSERT INTO {MARKER_TABLE}(applied_at, rows_updated) VALUES (strftime('%s','now'), ?)", (changed,))
        return changed


__all__ = ["MARKER_TABLE", "backfill_null_memory_versions"]
