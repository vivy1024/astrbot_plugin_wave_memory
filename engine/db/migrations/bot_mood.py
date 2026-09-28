"""Bot 心情 v2 的纯增量 schema：一个 Bot 一份心情 + 带来源的情绪事件流水。

旧的 ``scoped_soul_mood``（按群一行）保留不动，继续作为「群氛围」写入；本迁移不读取、
不改写任何旧行，重复执行无副作用（全部 ``IF NOT EXISTS``）。
"""

from __future__ import annotations

from ..connection import ConnectionManager

_BOT_MOOD_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_mood_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    source TEXT NOT NULL,
    valence REAL NOT NULL,
    arousal REAL NOT NULL,
    intensity REAL NOT NULL DEFAULT 0,
    cause TEXT NOT NULL DEFAULT '',
    subject_principal_id TEXT NOT NULL DEFAULT '',
    subject_name TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'group',
    source_ref TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_mood_events_bot_time
    ON bot_mood_events (bot_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_mood_events_source_ref
    ON bot_mood_events (bot_id, source_ref) WHERE source_ref <> '';

CREATE TABLE IF NOT EXISTS bot_mood_state (
    bot_id TEXT PRIMARY KEY,
    valence REAL NOT NULL,
    arousal REAL NOT NULL,
    fatigue REAL NOT NULL DEFAULT 0,
    word TEXT NOT NULL DEFAULT '',
    relationship_cursor INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL DEFAULT 'bot-mood/v2',
    updated_at REAL NOT NULL
)
"""


def ensure_bot_mood_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    statements = tuple(s.strip() for s in _BOT_MOOD_SCHEMA.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)


__all__ = ["ensure_bot_mood_schema"]
