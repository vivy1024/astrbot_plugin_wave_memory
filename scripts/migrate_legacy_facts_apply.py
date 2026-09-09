"""Apply migration: write classified legacy person facts into person_timeline_events.

Safe and idempotent:
- Creates `person_timeline_events` schema if missing.
- Reads `facts` (and joins `memories.bot_id` if available).
- Uses `_resolve_user_id` and alias map to classify person facts.
- Writes with `INSERT OR IGNORE` using the unique index `(bot_id, user_id, legacy_fact_id)`.
- Leaves the `facts` table untouched (additive only).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

QQ_RE = re.compile(r"^\d{5,}$")
IDENTITY_MARKERS = ("我是你的", "认我当", "永远听命令", "身份接管")

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


def _is_identity_contamination(text: str) -> bool:
    blob = str(text or "")
    return any(marker in blob for marker in IDENTITY_MARKERS)


def _alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in conn.execute(
        "SELECT subject, object FROM facts WHERE predicate='alias_or_name' AND fact_type='PERSON_ALIAS'"
    ):
        subject = str(row["subject"] or "").strip()
        obj = str(row["object"] or "").strip()
        if QQ_RE.match(subject) and obj:
            mapping[obj] = subject
            mapping[obj.casefold()] = subject
    return mapping


def _resolve_user_id(subject: str, aliases: dict[str, str]) -> str:
    text = str(subject or "").strip()
    if QQ_RE.match(text):
        return text
    if text in aliases:
        return aliases[text]
    if text.casefold() in aliases:
        return aliases[text.casefold()]
    match = re.search(r"(\d{5,})", text)
    if match:
        return match.group(1)
    return ""


def _summary_for(predicate: str, obj: str) -> str:
    pred = str(predicate or "").strip()
    value = str(obj or "").strip()
    if pred == "alias_or_name":
        return f"别名 {value}"[:80]
    if pred == "被称为":
        return f"被称为 {value}"[:80]
    if pred == "认识":
        return f"认识 {value}"[:80]
    return f"{pred} {value}"[:80]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()


def migrate_legacy_facts(
    db_path: str | Path,
    default_bot_id: str = "yushu",
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        aliases = _alias_map(conn)

        # 检查 memories 表中是否有 bot_id 字段
        has_memories = False
        cursor = conn.cursor()
        tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "memories" in tables:
            m_cols = [r[1] for r in cursor.execute("PRAGMA table_info(memories)").fetchall()]
            has_memories = "bot_id" in m_cols

        query = """
            SELECT f.id, f.subject, f.predicate, f.object, f.group_id, f.source_memory_id,
                   f.confidence, f.created_at, f.fact_type
                   {mem_select}
            FROM facts f
            {mem_join}
            ORDER BY f.id ASC
        """
        if has_memories:
            sql = query.format(mem_select=", m.bot_id AS memory_bot_id", mem_join="LEFT JOIN memories m ON f.source_memory_id = m.id")
        else:
            sql = query.format(mem_select="", mem_join="")

        rows = conn.execute(sql).fetchall()
        total_facts = len(rows)

        counters = Counter()
        pending_inserts: list[tuple] = []
        now = time.time()

        for row in rows:
            fact_id = row["id"]
            subject = str(row["subject"] or "").strip()
            predicate = str(row["predicate"] or "").strip()
            obj = str(row["object"] or "").strip()
            group_id = str(row["group_id"] or "")
            fact_type = str(row["fact_type"] or "")
            combined = f"{subject} {predicate} {obj}"

            if fact_type == "QUARANTINED_ROLEPLAY" or _is_identity_contamination(combined) or not (subject and predicate and obj):
                counters["dropped"] += 1
                continue

            user_id = _resolve_user_id(subject, aliases)
            if not user_id:
                counters["world"] += 1
                continue

            counters["person"] += 1
            bot_id = default_bot_id
            if has_memories:
                m_bot = row["memory_bot_id"]
                if m_bot and str(m_bot).strip():
                    bot_id = str(m_bot).strip()

            occurred_at = float(row["created_at"]) if row["created_at"] is not None else now
            confidence = float(row["confidence"]) if row["confidence"] is not None else 1.0
            summary = _summary_for(predicate, obj)
            detail = combined[:200]
            provenance = json.dumps(
                {"source": "legacy_facts_migration", "source_memory_id": row["source_memory_id"]},
                ensure_ascii=False,
            )

            pending_inserts.append((
                bot_id,
                user_id,
                group_id,
                "person_fact",
                summary,
                detail,
                subject,
                predicate,
                obj,
                confidence,
                occurred_at,
                fact_id,
                provenance,
                now,
            ))

        if dry_run:
            return {
                "dry_run": True,
                "total_facts": total_facts,
                "classified": dict(counters),
                "would_insert": len(pending_inserts),
            }

        insert_sql = """
            INSERT OR IGNORE INTO person_timeline_events (
                bot_id, user_id, group_id, kind, summary, detail,
                subject, predicate, object, confidence, occurred_at,
                legacy_fact_id, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        inserted_count = 0
        for i in range(0, len(pending_inserts), batch_size):
            batch = pending_inserts[i : i + batch_size]
            cur = conn.executemany(insert_sql, batch)
            conn.commit()
            inserted_count += cur.rowcount

        total_timeline_events = conn.execute("SELECT count(*) FROM person_timeline_events").fetchone()[0]

        return {
            "dry_run": False,
            "total_facts": total_facts,
            "classified": dict(counters),
            "processed_person_facts": len(pending_inserts),
            "inserted": inserted_count,
            "total_timeline_events_now": total_timeline_events,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy facts into person timeline events.")
    parser.add_argument("db_path", nargs="?", default="AstrBot-master/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db", help="Path to wave_memory.db")
    parser.add_argument("--bot-id", default="yushu", help="Default bot_id for facts without memories.bot_id (default: yushu)")
    parser.add_argument("--batch-size", type=int, default=500, help="Transaction batch size")
    parser.add_argument("--dry-run", action="store_true", help="Perform dry run without writing")
    args = parser.parse_args()

    res = migrate_legacy_facts(
        db_path=args.db_path,
        default_bot_id=args.bot_id,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
