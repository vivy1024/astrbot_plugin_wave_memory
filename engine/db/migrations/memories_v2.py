"""纯增量、可重复的 memories v2 schema 迁移。

迁移只添加列和索引，绝不 UPDATE 或回填已有 memories：旧记录的 v2
字段必须保持 NULL，直到经过显式审核/迁移流程。
"""

from __future__ import annotations

from ..connection import ConnectionManager


# `memories.version` 是未来 ObjectRef/乐观并发的行版本，而不是 schema 编号。
# v2 新行从 1 开始；旧行保持 NULL，不能通过默认值伪装为已解析对象。
MEMORIES_V2_VERSION = 1

_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bot_id", "TEXT"),
    ("session_id", "TEXT"),
    ("visibility", "TEXT"),
    ("origin_fingerprint", "TEXT"),
    ("provenance", "TEXT"),
    ("version", "INTEGER"),
    ("quarantine", "INTEGER"),
    ("resolution_state", "TEXT"),
)

_SCOPED_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_memories_v2_scope
    ON memories(bot_id, session_id, visibility, resolution_state, quarantine, timestamp)
"""
# 按发言人取最近一条/最近名字（Bot 主页、人物解析、好感 flush）；线上库早年手工建过，代码里补上。
_SENDER_INDEX = "CREATE INDEX IF NOT EXISTS idx_memories_sender ON memories(sender_id)"
# 按昵称找人（人物解析工具、事后记账）：没有它要按 bot_id 回表扫这个 Bot 的全部行，
# 线上实测读 690MB；加上后 0 读盘，索引约 11MB。
_SENDER_NAME_INDEX = "CREATE INDEX IF NOT EXISTS idx_memories_bot_sender_name ON memories(bot_id, sender_name)"


def ensure_memories_v2_schema(cm: ConnectionManager) -> None:
    """在单一 ``ConnectionManager.migration_transaction`` 中增量建立 v2 列。

    ``memories`` 必须已由 ``MemoryRepo`` 建表。每个新列均保持 nullable，
    因而已有记录不会被赋默认 Scope、版本或审计信息。
    """
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")

    with cm.migration_transaction() as tx:
        columns = {row[1] for row in tx.execute("PRAGMA table_info(memories)")}
        if not columns:
            raise RuntimeError("memories table must exist before memories v2 migration")
        for name, definition in _V2_COLUMNS:
            if name not in columns:
                tx.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        tx.execute(_SCOPED_INDEX)
        tx.execute(_SENDER_INDEX)
        tx.execute(_SENDER_NAME_INDEX)


__all__ = ["MEMORIES_V2_VERSION", "ensure_memories_v2_schema"]
