"""Append-only person timeline: summary + detail, keyed by bot and user across groups."""

from __future__ import annotations

from ..connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('affinity', 'impression', 'person_fact')),
    summary TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    predicate TEXT NOT NULL DEFAULT '',
    object TEXT NOT NULL DEFAULT '',
    confidence REAL,
    occurred_at REAL NOT NULL,
    legacy_fact_id INTEGER,
    provenance TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_person_timeline_person_time
    ON person_timeline_events (bot_id, user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_person_timeline_kind
    ON person_timeline_events (bot_id, user_id, kind, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_person_timeline_group_time
    ON person_timeline_events (bot_id, group_id, occurred_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_person_timeline_legacy_fact
    ON person_timeline_events (bot_id, user_id, legacy_fact_id)
    WHERE legacy_fact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS person_unsettled_state (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    energy REAL NOT NULL DEFAULT 0,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    traces TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, user_id, group_id)
);
"""


def ensure_person_timeline_schema(cm: ConnectionManager) -> None:
    if not hasattr(cm, "migration_transaction"):
        raise TypeError("cm must provide migration_transaction")
    with cm.migration_transaction() as tx:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                tx.execute(statement)


__all__ = ["ensure_person_timeline_schema"]
