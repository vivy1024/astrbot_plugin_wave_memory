"""v2.2 experience/relationship/jargon schema migration.

Additive only: creates new evidence tables and lifecycle columns without dropping data.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _add_column(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    if column not in _columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        logger.info("[Migration v2.2] Added %s.%s", table, column)


def run_migration(db_path: str) -> bool:
    """Run v2.2 additive schema migration.

    Args:
        db_path: path to wave_memory.db

    Returns:
        True on success, False on rollback.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS experience_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT,
                episode_type TEXT NOT NULL,
                trigger_text TEXT,
                bot_inner_thought TEXT,
                bot_action TEXT,
                bot_reply TEXT,
                user_reaction TEXT,
                outcome TEXT,
                source_memory_ids TEXT DEFAULT '[]',
                emotional_weight REAL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_experience_bot_time
                ON experience_episodes(bot_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_experience_user
                ON experience_episodes(bot_id, user_id, group_id);

            CREATE TABLE IF NOT EXISTS relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dimension TEXT NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                source_episode_id INTEGER,
                source_memory_id INTEGER,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rel_events_user_time
                ON relationship_events(bot_id, user_id, group_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_rel_events_type
                ON relationship_events(event_type);
            """
        )

        if _table_exists(cur, "experience_episodes"):
            _add_column(cur, "experience_episodes", "idempotency_key", "TEXT")
            _add_column(cur, "experience_episodes", "updated_at", "REAL")
            cur.execute("UPDATE experience_episodes SET updated_at=COALESCE(updated_at, created_at)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_episode_idempotency ON experience_episodes(bot_id, group_id, idempotency_key) WHERE idempotency_key IS NOT NULL")

        if _table_exists(cur, "jargon"):
            _add_column(cur, "jargon", "status", "TEXT DEFAULT 'pending'")
            _add_column(cur, "jargon", "scope", "TEXT DEFAULT 'local'")
            _add_column(cur, "jargon", "source", "TEXT DEFAULT 'wave_memory'")
            _add_column(cur, "jargon", "last_infer_freq", "INTEGER DEFAULT 0")
            _add_column(cur, "jargon", "reject_reason", "TEXT")

        if _table_exists(cur, "facts"):
            fact_columns = _columns(cur, "facts")
            _add_column(cur, "facts", "last_reinforced", "REAL")
            _add_column(cur, "facts", "fact_type", "TEXT DEFAULT 'FACTUAL'")
            if "created_at" in fact_columns:
                cur.execute("UPDATE facts SET last_reinforced = COALESCE(last_reinforced, created_at) WHERE last_reinforced IS NULL")

        if _table_exists(cur, "beliefs"):
            belief_columns = _columns(cur, "beliefs")
            _add_column(cur, "beliefs", "conflicts", "TEXT DEFAULT '[]'")
            _add_column(cur, "beliefs", "last_reinforced", "REAL")
            _add_column(cur, "beliefs", "archived_reason", "TEXT")
            _add_column(cur, "beliefs", "evidence_type", "TEXT DEFAULT 'memory'")
            _add_column(cur, "beliefs", "evidence_ids", "TEXT DEFAULT '[]'")
            if "created_at" in belief_columns:
                cur.execute("UPDATE beliefs SET last_reinforced = COALESCE(last_reinforced, created_at) WHERE last_reinforced IS NULL")

        conn.commit()
        logger.info("[Migration v2.2] Experience rework schema migration completed")
        return True
    except Exception as e:
        conn.rollback()
        logger.error("[Migration v2.2] Failed: %s", e)
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python v2_2_experience_rework.py <path_to_wave_memory.db>")
        raise SystemExit(2)
    raise SystemExit(0 if run_migration(sys.argv[1]) else 1)
