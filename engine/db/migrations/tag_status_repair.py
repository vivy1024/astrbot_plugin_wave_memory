"""v5.1 一次性数据修复：「提取完成但一个标签都没挂上」的记忆改记为 skipped。

v5.1 之前 TagWorker 按模型有没有返回标签决定写 done / skipped，但标签在入库前还要过准入
（停用词、低置信短语等）。模型给了标签、却被准入全部拒掉时，状态仍记为 done，而记忆上
没有任何标签——覆盖率统计把它们算成「标签丢失」，一键重新提取也只会再被拒一遍。线上
13,299 次提取属于这种情况（outbox ``memory.tags_applied`` 里 status=done、tag_ids 为空、
rejected_count>0）。v5.1 起按实际挂上的标签判定，这里把存量改过来一次。

改动的 memory_id 与旧状态写进 ``tag_status_repair_v51``；该表存在即视为已修复，不重复执行，
之后再出现的「done 但无标签」才是真正被清理掉的关联。
"""

from __future__ import annotations

from typing import Any

BACKUP_TABLE = "tag_status_repair_v51"
REASON = "admission_rejected_all"


def _tables(tx: Any) -> set[str]:
    return {row[0] for row in tx.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def repair_done_without_tags(cm: Any) -> int:
    """返回改为 skipped 的行数；已修复过或缺表时返回 0。"""
    with cm.migration_transaction() as tx:
        tables = _tables(tx)
        if BACKUP_TABLE in tables or not {"tag_extraction_status", "memories"} <= tables:
            return 0
        columns = {row[1] for row in tx.execute("PRAGMA table_info(memories)")}
        linked = []
        if "scoped_memory_tags" in tables and {"bot_id", "session_id", "visibility"} <= columns:
            linked.append(
                "EXISTS (SELECT 1 FROM scoped_memory_tags t WHERE t.bot_id=m.bot_id AND t.session_id=m.session_id "
                "AND t.visibility=m.visibility AND t.memory_id=m.id)"
            )
        if "memory_tags" in tables:
            linked.append("EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id=m.id)")
        has_tags = " OR ".join(linked) or "0"
        tx.execute(
            f"CREATE TABLE {BACKUP_TABLE} (memory_id INTEGER PRIMARY KEY, old_status TEXT, repaired_at REAL)"
        )
        tx.execute(
            f"""INSERT INTO {BACKUP_TABLE}(memory_id, old_status, repaired_at)
                SELECT s.memory_id, s.status, strftime('%s','now')
                  FROM tag_extraction_status s JOIN memories m ON m.id = s.memory_id
                 WHERE s.status = 'done' AND NOT ({has_tags})"""
        )
        cursor = tx.execute(
            f"""UPDATE tag_extraction_status SET status='skipped', last_error=?
                 WHERE memory_id IN (SELECT memory_id FROM {BACKUP_TABLE})""",
            (REASON,),
        )
        return int(cursor.rowcount or 0)


__all__ = ["BACKUP_TABLE", "REASON", "repair_done_without_tags"]
