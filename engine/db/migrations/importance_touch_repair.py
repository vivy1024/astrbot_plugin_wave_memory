"""v5.1 一次性数据修复：扣回「每次被召回 +0.01」累加到 ``memories.importance`` 上的部分。

v5.1 之前召回（touch）除了访问次数 +1，还给重要度 +0.01（封顶 3.0）。常被召回的记忆重要度
被一路推到 3.0，打分又按重要度相乘，形成「越被召回越重要」的正反馈：线上被召回最多的
是「@某人」这类空消息（访问 1449 次、重要度 3.0）。v5.1 起召回不再改重要度，这里把存量
扣回去一次：

- 未封顶的行：``importance - 0.01 × access_count``，下限 0.1（与原累加严格互逆；做梦强化
  每次 +0.05，扣回 0.01 后仍保留 0.04 的强化）；
- 已封顶到 3.0 的行无法反推原值，回到默认 1.0（与上面的结果取大）。

修改前把 (id, 旧重要度) 写进 ``memory_importance_repair_v51``，需要时可原样还原：
``UPDATE memories SET importance = (SELECT importance FROM memory_importance_repair_v51 b WHERE b.id = memories.id)
WHERE id IN (SELECT id FROM memory_importance_repair_v51)``。备份表存在即视为已修复，不会重复执行。
"""

from __future__ import annotations

from typing import Any

BACKUP_TABLE = "memory_importance_repair_v51"
TOUCH_STEP = 0.01
SATURATED = 2.99


def repair_touch_inflated_importance(cm: Any) -> int:
    """返回修复的行数；已修复过返回 0。"""
    with cm.migration_transaction() as tx:
        exists = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (BACKUP_TABLE,)
        ).fetchone()
        if exists:
            return 0
        columns = {row[1] for row in tx.execute("PRAGMA table_info(memories)")}
        if not {"importance", "access_count"} <= columns:
            return 0
        tx.execute(
            f"CREATE TABLE {BACKUP_TABLE} (id INTEGER PRIMARY KEY, importance REAL, access_count INTEGER, repaired_at REAL)"
        )
        tx.execute(
            f"""INSERT INTO {BACKUP_TABLE}(id, importance, access_count, repaired_at)
                SELECT id, importance, access_count, strftime('%s','now')
                  FROM memories WHERE COALESCE(access_count, 0) > 0"""
        )
        cursor = tx.execute(
            f"""UPDATE memories
                   SET importance = CASE
                       WHEN importance >= {SATURATED} THEN MAX(1.0, importance - {TOUCH_STEP} * access_count)
                       ELSE MAX(0.1, importance - {TOUCH_STEP} * access_count)
                   END
                 WHERE id IN (SELECT id FROM {BACKUP_TABLE})"""
        )
        return int(cursor.rowcount or 0)


__all__ = ["BACKUP_TABLE", "repair_touch_inflated_importance"]
