"""Additive schema for scoped reply signals and reviewed human expressions.

Startup owns migration invocation. These tables never update memories or the
existing tool-owned scoped_few_shot_examples projection.
"""

from __future__ import annotations

from .scoped_learning_projections import _transaction


_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS reply_feedback_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK(visibility='group'),
        reply_memory_id INTEGER NOT NULL,
        response_memory_id INTEGER NOT NULL DEFAULT 0,
        speaker_id TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL CHECK(outcome IN ('accepted','corrected','rejected','unknown')),
        situation TEXT NOT NULL DEFAULT '',
        source_quote TEXT NOT NULL DEFAULT '',
        artifacts_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(bot_id, session_id, visibility, reply_memory_id, response_memory_id)
    )""",
    """CREATE TABLE IF NOT EXISTS reply_expression_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK(visibility='group'),
        speaker_id TEXT NOT NULL,
        source_memory_id INTEGER NOT NULL,
        content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 20),
        situation TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        artifact_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        evidence_bindings_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
        reviewer TEXT NOT NULL DEFAULT '',
        review_note TEXT NOT NULL DEFAULT '',
        reviewed_at REAL,
        created_at REAL NOT NULL,
        UNIQUE(bot_id, session_id, visibility, source_memory_id)
    )""",
    """CREATE TABLE IF NOT EXISTS reply_expression_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        example_id INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL,
        expected_revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('approved','rejected')),
        reviewer TEXT NOT NULL,
        note TEXT NOT NULL,
        style_only INTEGER NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(example_id, idempotency_key),
        FOREIGN KEY(example_id) REFERENCES reply_expression_candidates(id)
    )""",
    """CREATE TABLE IF NOT EXISTS reply_expression_usage (
        bot_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK(visibility='group'),
        reply_memory_id INTEGER NOT NULL,
        example_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY(bot_id, session_id, visibility, reply_memory_id, example_id),
        FOREIGN KEY(example_id) REFERENCES reply_expression_candidates(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reply_feedback_scope ON "
    "reply_feedback_events(bot_id, session_id, visibility, reply_memory_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_reply_expression_recall ON "
    "reply_expression_candidates(bot_id, session_id, visibility, status, situation, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_reply_expression_quality ON "
    "reply_expression_usage(bot_id, session_id, visibility, example_id, reply_memory_id DESC)",
)


def ensure_reply_feedback_learning_schema(connection) -> None:
    """Idempotent; participate in a caller-owned migration transaction if present."""
    def apply(tx):
        for statement in _STATEMENTS:
            tx.execute(statement)

    if bool(getattr(connection, "in_transaction", False)):
        apply(connection)
    else:
        with _transaction(connection) as tx:
            apply(tx)
    # 内容级去重的来源表是这套 schema 的一部分：`observe` 会往里写来源，
    # 只建候选表会让每次采集在建来源行时报 no such table。放这里一起建，
    # 调用方只调这一支也不会漏表。
    from .expression_candidate_dedup import ensure_expression_candidate_dedup_schema

    ensure_expression_candidate_dedup_schema(connection)


__all__ = ["ensure_reply_feedback_learning_schema"]
