"""纯增量 Scoped Soul schema；不读取、不推断、不回填 legacy Soul 数据。"""

from __future__ import annotations

from ..connection import ConnectionManager


_SCOPED_SOUL_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_soul_revisions (
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    component TEXT NOT NULL,
    subject_principal_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, session_id, visibility, component, subject_principal_id)
);

CREATE TABLE IF NOT EXISTS scoped_soul_mood (
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    valence REAL NOT NULL,
    arousal REAL NOT NULL,
    cause TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT 'scoped-mood/v1',
    revision INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    observed_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, session_id, visibility)
);

CREATE TABLE IF NOT EXISTS scoped_soul_concerns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    topic TEXT NOT NULL,
    intensity REAL NOT NULL,
    origin_memory_id INTEGER,
    origin_episode_id INTEGER,
    concern_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dormant', 'progressing', 'resolved', 'expired', 'archived')),
    urgency REAL NOT NULL DEFAULT 0,
    last_progress_at REAL,
    expected_resolution_at REAL,
    resolution_note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_triggered REAL NOT NULL,
    revision INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    UNIQUE (bot_id, session_id, visibility, topic)
);

CREATE TABLE IF NOT EXISTS scoped_soul_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    subject_principal_id TEXT,
    event_summary TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'time_anchor',
    emotional_weight REAL NOT NULL,
    occurred_at REAL NOT NULL,
    revision INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scoped_soul_relationships (
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    subject_principal_id TEXT NOT NULL,
    affinity INTEGER NOT NULL,
    state TEXT NOT NULL,
    dimensions TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, session_id, visibility, subject_principal_id)
);

CREATE TABLE IF NOT EXISTS scoped_soul_relationship_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    subject_principal_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    dimension TEXT NOT NULL,
    delta REAL NOT NULL,
    reason TEXT NOT NULL,
    source_episode_id INTEGER,
    source_memory_id INTEGER,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    before_json TEXT,
    after_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_scoped_soul_concerns_scope
    ON scoped_soul_concerns (bot_id, session_id, visibility, intensity DESC);
CREATE INDEX IF NOT EXISTS idx_scoped_soul_timeline_scope_time
    ON scoped_soul_timeline (bot_id, session_id, visibility, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_scoped_soul_relationship_events_scope_subject
    ON scoped_soul_relationship_events (
        bot_id, session_id, visibility, subject_principal_id, created_at DESC
    );
"""


def _upgrade_scoped_soul_concerns(tx) -> None:
    columns = {str(row[1]) for row in tx.execute("PRAGMA table_info(scoped_soul_concerns)").fetchall()}
    if not columns:
        return
    additions = (
        ("origin_episode_id", "INTEGER"),
        ("concern_type", "TEXT NOT NULL DEFAULT ''"),
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ("urgency", "REAL NOT NULL DEFAULT 0"),
        ("last_progress_at", "REAL"),
        ("expected_resolution_at", "REAL"),
        ("resolution_note", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, definition in additions:
        if name not in columns:
            tx.execute(f"ALTER TABLE scoped_soul_concerns ADD COLUMN {name} {definition}")


def ensure_scoped_soul_schema(cm: ConnectionManager) -> None:
    """建立全新的正式 Soul 数据面，绝不猜测 legacy 行的 Scope。"""
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    statements = tuple(
        statement.strip()
        for statement in _SCOPED_SOUL_SCHEMA.split(";")
        if statement.strip()
    )
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)
        _upgrade_scoped_soul_concerns(tx)


__all__ = ["ensure_scoped_soul_schema"]
