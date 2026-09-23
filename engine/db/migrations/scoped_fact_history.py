"""增量创建 scoped fact observation history。"""
from __future__ import annotations
from ..connection import ConnectionManager
from .scoped_soul_private_visibility import apply_scoped_fact_private_visibility

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_fact_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL CHECK (visibility='group'),
 candidate_fact_id INTEGER,
 existing_fact_id INTEGER,
subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
 relation TEXT NOT NULL CHECK (relation IN ('compatible','scoped','conflicts','supersedes')),
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected')),
 confidence REAL NOT NULL DEFAULT 0.0,
 candidate_snapshot TEXT NOT NULL DEFAULT '{}', existing_snapshot TEXT NOT NULL DEFAULT '{}',
 evidence TEXT NOT NULL DEFAULT '{}', source_tags TEXT NOT NULL DEFAULT '[]', query_trace_id TEXT NOT NULL DEFAULT '',
 source_memory_id INTEGER, provenance TEXT NOT NULL DEFAULT '{}',
 valid_from REAL, valid_until REAL, supersedes_id INTEGER,
 idempotency_key TEXT NOT NULL, observed_at REAL NOT NULL, reviewed_at REAL,
 UNIQUE(bot_id,session_id,visibility,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_scoped_fact_history_scope_fact ON scoped_fact_history(bot_id,session_id,visibility,subject,predicate,observed_at);
CREATE INDEX IF NOT EXISTS idx_scoped_fact_history_review ON scoped_fact_history(bot_id,session_id,visibility,review_status,observed_at);
"""

def ensure_scoped_fact_history_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    with cm.migration_transaction() as tx:
        for statement in _SCHEMA.split(';'):
            if statement.strip():
                tx.execute(statement)
        # 事实表同时接受私聊行（私聊得知的事实只在该私聊可见）。
        apply_scoped_fact_private_visibility(tx)

__all__ = ['ensure_scoped_fact_history_schema']
