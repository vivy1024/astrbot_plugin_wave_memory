"""增量 scoped relationship calibration schema；不读取或回填 legacy 关系数据。"""

from __future__ import annotations

import json
import math
import sqlite3

from ..connection import ConnectionManager
from .scoped_soul_private_visibility import apply_scoped_soul_private_visibility


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_soul_relationship_values (
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    subject_principal_id TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN ('familiarity', 'trust', 'fun', 'hostility', 'depth')),
    automatic_value REAL NOT NULL,
    manual_adjustment REAL,
    manual_override REAL,
    effective_value REAL NOT NULL,
    relationship_revision INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, session_id, visibility, subject_principal_id, dimension)
);
CREATE TABLE IF NOT EXISTS scoped_soul_relationship_calibration_events (
    calibration_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    subject_principal_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('adjust', 'override', 'clear_override', 'restore_auto')),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL,
    actor TEXT NOT NULL,
    relationship_revision INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scoped_relationship_values_scope_subject
    ON scoped_soul_relationship_values (bot_id, session_id, visibility, subject_principal_id, relationship_revision);
CREATE INDEX IF NOT EXISTS idx_scoped_relationship_calibration_scope_time
    ON scoped_soul_relationship_calibration_events (bot_id, session_id, visibility, subject_principal_id, created_at DESC);
"""

_RANGES = {
    "familiarity": (0.0, 100.0),
    "trust": (-50.0, 100.0),
    "fun": (0.0, 80.0),
    "hostility": (0.0, 100.0),
    "depth": (0.0, 80.0),
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _clamp(dimension: str, value: float) -> float:
    lo, hi = _RANGES[dimension]
    return max(lo, min(hi, value))


def _relationship_values_support_hostility(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scoped_soul_relationship_values'"
    ).fetchone()
    return bool(row and "hostility" in str(row[0] or "").casefold())


def _upgrade_relationship_values_to_five_dimensions(connection: sqlite3.Connection) -> None:
    """Rebuild the value table when a prior four-dimension CHECK is present.

    SQLite cannot add a CHECK alternative in place.  The caller already owns the
    migration transaction, so copying into a temporary table and renaming it is
    atomic from the plugin's point of view.
    """
    if not _columns(connection, "scoped_soul_relationship_values") or _relationship_values_support_hostility(connection):
        return
    connection.execute(
        """CREATE TABLE scoped_soul_relationship_values__five_dimensions (
               bot_id TEXT NOT NULL,
               session_id TEXT NOT NULL,
               visibility TEXT NOT NULL CHECK (visibility = 'group'),
               subject_principal_id TEXT NOT NULL,
               dimension TEXT NOT NULL CHECK (dimension IN ('familiarity', 'trust', 'fun', 'hostility', 'depth')),
               automatic_value REAL NOT NULL,
               manual_adjustment REAL,
               manual_override REAL,
               effective_value REAL NOT NULL,
               relationship_revision INTEGER NOT NULL,
               evidence TEXT NOT NULL DEFAULT '[]',
               updated_at REAL NOT NULL,
               PRIMARY KEY (bot_id, session_id, visibility, subject_principal_id, dimension)
           )"""
    )
    connection.execute(
        """INSERT INTO scoped_soul_relationship_values__five_dimensions(
               bot_id, session_id, visibility, subject_principal_id, dimension,
               automatic_value, manual_adjustment, manual_override, effective_value,
               relationship_revision, evidence, updated_at)
           SELECT bot_id, session_id, visibility, subject_principal_id, dimension,
                  automatic_value, manual_adjustment, manual_override, effective_value,
                  relationship_revision, evidence, updated_at
             FROM scoped_soul_relationship_values"""
    )
    connection.execute("DROP TABLE scoped_soul_relationship_values")
    connection.execute(
        "ALTER TABLE scoped_soul_relationship_values__five_dimensions RENAME TO scoped_soul_relationship_values"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoped_relationship_values_scope_subject "
        "ON scoped_soul_relationship_values (bot_id, session_id, visibility, subject_principal_id, relationship_revision)"
    )


def _initialize_formal_values(connection: sqlite3.Connection) -> None:
    """Only project valid dimensions from the existing formal relationship table."""
    if not _columns(connection, "scoped_soul_relationships"):
        return
    rows = connection.execute(
        """SELECT bot_id, session_id, visibility, subject_principal_id,
                  dimensions, revision, evidence, updated_at
             FROM scoped_soul_relationships"""
    ).fetchall()
    for row in rows:
        try:
            dimensions = json.loads(str(row[4] or "{}"))
            evidence = json.loads(str(row[6] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        connection.execute(
            """INSERT INTO scoped_soul_revisions(
                   bot_id, session_id, visibility, component, subject_principal_id, revision, updated_at)
               VALUES (?, ?, ?, 'relationship', ?, ?, ?)
               ON CONFLICT(bot_id, session_id, visibility, component, subject_principal_id)
               DO UPDATE SET revision=MAX(scoped_soul_revisions.revision, excluded.revision),
                   updated_at=MAX(scoped_soul_revisions.updated_at, excluded.updated_at)""",
            (row[0], row[1], row[2], row[3], int(row[5] or 0), float(row[7] or 0)),
        )
        if not isinstance(dimensions, dict):
            continue
        for dimension, raw_value in dimensions.items():
            if dimension not in _RANGES or isinstance(raw_value, bool):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            value = _clamp(dimension, value)
            connection.execute(
                """INSERT OR IGNORE INTO scoped_soul_relationship_values(
                       bot_id, session_id, visibility, subject_principal_id, dimension,
                       automatic_value, manual_adjustment, manual_override, effective_value,
                       relationship_revision, evidence, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)""",
                (row[0], row[1], row[2], row[3], dimension, value, value,
                 int(row[5] or 0), json.dumps(evidence, ensure_ascii=False, sort_keys=True), float(row[7] or 0)),
            )


def _apply(connection: sqlite3.Connection) -> None:
    for statement in (part.strip() for part in _SCHEMA.split(";") if part.strip()):
        connection.execute(statement)
    _upgrade_relationship_values_to_five_dimensions(connection)
    columns = _columns(connection, "scoped_soul_relationship_events")
    if columns:
        for name, definition in (
            ("operation_id", "TEXT"),
            ("evidence", "TEXT NOT NULL DEFAULT '[]'"),
            ("value_layer", "TEXT NOT NULL DEFAULT 'automatic'"),
            ("before_json", "TEXT"),
            ("after_json", "TEXT"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE scoped_soul_relationship_events ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scoped_relationship_events_operation "
            "ON scoped_soul_relationship_events (operation_id)"
        )
    # Historical audit table (may be absent on fresh DBs). Indexes serve affinity
    # read-path summaries without replaying scores.
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "scoped_soul_relationship_legacy_events" in tables:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_legacy_rel_events_subject "
            "ON scoped_soul_relationship_legacy_events("
            "bot_id, session_id, visibility, subject_principal_id, occurred_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_legacy_rel_events_scope_type "
            "ON scoped_soul_relationship_legacy_events("
            "bot_id, session_id, visibility, event_type)"
        )
    _initialize_formal_values(connection)
    # Soul 表同时接受群聊与私聊行（羽书在私聊里同样形成关系与状态）。
    apply_scoped_soul_private_visibility(connection)


def ensure_scoped_relationship_calibration_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    with cm.migration_transaction() as transaction:
        _apply(transaction)


def ensure_scoped_relationship_calibration_schema_connection(connection: sqlite3.Connection) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    _apply(connection)


__all__ = [
    "ensure_scoped_relationship_calibration_schema",
    "ensure_scoped_relationship_calibration_schema_connection",
]
