"""Bot 身体（激素）的纯增量 schema：一个 Bot 一行状态，另有每个人的事件计数用于习惯化。

全部 ``IF NOT EXISTS``，重复执行无副作用；不读取、不改写任何旧表。
"""

from __future__ import annotations

from ..connection import ConnectionManager

_BOT_BODY_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_body_state (
    bot_id TEXT PRIMARY KEY,
    hormones TEXT NOT NULL DEFAULT '{}',
    last_tick_at REAL NOT NULL DEFAULT 0,
    last_active_at REAL NOT NULL DEFAULT 0,
    last_human_at REAL NOT NULL DEFAULT 0,
    offline_since REAL NOT NULL DEFAULT 0,
    edge_memo TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL DEFAULT 'bot-body/v1',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_body_person_seen (
    bot_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (bot_id, principal_id)
);

CREATE TABLE IF NOT EXISTS bot_body_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    deltas TEXT NOT NULL DEFAULT '{}',
    scene TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_body_events_bot_time ON bot_body_events (bot_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_body_events_habit ON bot_body_events (bot_id, principal_id, kind, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_body_events_source_ref
    ON bot_body_events (bot_id, source_ref) WHERE source_ref <> '';
"""


def ensure_bot_body_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    statements = tuple(s.strip() for s in _BOT_BODY_SCHEMA.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)


__all__ = ["ensure_bot_body_schema"]
