"""关系小传的纯增量 schema：Bot 为每个人写的一段「关系小传」与最近若干个历史版本。

- ``person_relationship_portraits``：按 (bot_id, user_id) 一行。人物键与
  ``person_timeline_events`` 相同（本地账号 ID）；管理员确认的关联账号在读取时合并。
  正文只由非私聊的时间线事件生成，因此在任何场合注入都不外泄私聊内容。
- ``person_relationship_portrait_history``：正文被替换前的旧版本（自动生成或管理员编辑都会留档），
  每人只保留最近若干条，供管理台回看。

本迁移不读取、不改写任何旧表；全部 ``IF NOT EXISTS``，缺列时只补列，重复执行无副作用。
"""

from __future__ import annotations

from ..connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_relationship_portraits (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'auto' CHECK (status IN ('auto', 'edited')),
    source_max_event_id INTEGER NOT NULL DEFAULT 0,
    source_event_count INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT '',
    edited_by TEXT NOT NULL DEFAULT '',
    generated_at REAL,
    reviewed_through_event_id INTEGER NOT NULL DEFAULT 0,
    reviewed_at REAL,
    pending_update INTEGER NOT NULL DEFAULT 0,
    pending_since REAL,
    last_error TEXT NOT NULL DEFAULT '',
    last_error_at REAL,
    last_attempt_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_person_relationship_portraits_bot_updated
    ON person_relationship_portraits (bot_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS person_relationship_portrait_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    source_max_event_id INTEGER NOT NULL DEFAULT 0,
    source_event_count INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT '',
    edited_by TEXT NOT NULL DEFAULT '',
    generated_at REAL,
    replaced_at REAL NOT NULL,
    replaced_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_person_relationship_portrait_history_person
    ON person_relationship_portrait_history (bot_id, user_id, version DESC)
"""

# 之后新增的列在这里登记：旧库缺列时补上（ALTER TABLE ... ADD COLUMN），不改已有数据。
_COLUMNS: dict[str, dict[str, str]] = {
    "person_relationship_portraits": {
        "content": "TEXT NOT NULL DEFAULT ''",
        "version": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'auto'",
        "source_max_event_id": "INTEGER NOT NULL DEFAULT 0",
        "source_event_count": "INTEGER NOT NULL DEFAULT 0",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "edited_by": "TEXT NOT NULL DEFAULT ''",
        "generated_at": "REAL",
        "reviewed_through_event_id": "INTEGER NOT NULL DEFAULT 0",
        "reviewed_at": "REAL",
        "pending_update": "INTEGER NOT NULL DEFAULT 0",
        "pending_since": "REAL",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "last_error_at": "REAL",
        "last_attempt_at": "REAL",
    },
    "person_relationship_portrait_history": {
        "source_max_event_id": "INTEGER NOT NULL DEFAULT 0",
        "source_event_count": "INTEGER NOT NULL DEFAULT 0",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "edited_by": "TEXT NOT NULL DEFAULT ''",
        "generated_at": "REAL",
        "replaced_by": "TEXT NOT NULL DEFAULT ''",
    },
}


def ensure_relationship_portrait_schema(cm: ConnectionManager) -> None:
    if not hasattr(cm, "migration_transaction"):
        raise TypeError("cm must provide migration_transaction")
    statements = tuple(s.strip() for s in _SCHEMA.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)
        for table, columns in _COLUMNS.items():
            existing = {str(row[1]) for row in tx.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    tx.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


__all__ = ["ensure_relationship_portrait_schema"]
