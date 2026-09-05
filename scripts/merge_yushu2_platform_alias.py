#!/usr/bin/env python3
"""Merge leftover 羽书2 session rows into 羽书 for the same bot/group.

Default is dry-run. Live apply requires --apply and confirmation
merge-yushu2-platform-alias.

Rewrite-safe rows (memories, events, facts, tags, timeline, private memories)
keep their ids and only change session_id / subject_principal_id.

Primary-key collisions (relationships, values, mood, revisions, cursors,
concerns that already exist under 羽书) are dropped after the 羽书 row is kept.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

CONFIRMATION = "merge-yushu2-platform-alias"
OLD_PLATFORM = "羽书2"
NEW_PLATFORM = "羽书"
GROUP_ID = "398291136"
OLD_GROUP_SESSION = f"{OLD_PLATFORM}:group:{GROUP_ID}"
NEW_GROUP_SESSION = f"{NEW_PLATFORM}:group:{GROUP_ID}"
OLD_PREFIX = f"{OLD_PLATFORM}:"
NEW_PREFIX = f"{NEW_PLATFORM}:"
OLD_USER_PREFIX = f"{OLD_PLATFORM}:user:"
NEW_USER_PREFIX = f"{NEW_PLATFORM}:user:"

REWRITE_SESSION_TABLES = (
    "memories",
    "scoped_facts",
    "scoped_memory_tags",
    "scoped_soul_timeline",
    "scoped_soul_relationship_events",
    "scoped_fact_history",
)

DROP_IF_CONFLICT = (
    ("scoped_soul_relationships", ("bot_id", "session_id", "visibility", "subject_principal_id")),
    ("scoped_soul_relationship_values", ("bot_id", "session_id", "visibility", "subject_principal_id", "dimension")),
    ("scoped_soul_mood", ("bot_id", "session_id", "visibility")),
    ("scoped_soul_revisions", ("bot_id", "session_id", "visibility", "component", "subject_principal_id")),
    ("scoped_consolidation_cursors", ("bot_id", "session_id", "visibility", "cursor_name")),
    ("scoped_soul_concerns", ("bot_id", "session_id", "visibility", "topic")),
    ("scoped_tags", ("bot_id", "session_id", "visibility", "name")),
    ("scoped_tag_relations", ("bot_id", "session_id", "visibility", "source_tag_id", "target_tag_id", "relation_type")),
)


def _connect(path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=120)
        conn.execute("PRAGMA query_only=ON")
        return conn
    conn = sqlite3.connect(path, timeout=120)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _rewrite_session_id(value: str) -> str:
    if value.startswith(OLD_PREFIX):
        return NEW_PREFIX + value[len(OLD_PREFIX):]
    return value


def _rewrite_principal(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(OLD_USER_PREFIX):
        return NEW_USER_PREFIX + value[len(OLD_USER_PREFIX):]
    if value.startswith(OLD_PREFIX):
        return NEW_PREFIX + value[len(OLD_PREFIX):]
    return value


def inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    leftover: dict[str, Any] = {}
    for table in sorted(tables):
        cols = _columns(conn, table)
        item: dict[str, Any] = {}
        if "session_id" in cols:
            sessions = conn.execute(
                f"SELECT session_id, COUNT(*) FROM {table} WHERE session_id LIKE ? GROUP BY session_id",
                (f"{OLD_PLATFORM}:%",),
            ).fetchall()
            if sessions:
                item["session_id"] = {str(session): int(count) for session, count in sessions}
        if "subject_principal_id" in cols:
            n = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE subject_principal_id LIKE ?", (f"{OLD_PLATFORM}:%",))
            if n:
                item["subject_principal_id"] = n
        if "metadata_json" in cols:
            n = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE metadata_json LIKE ?", (f"%{OLD_PLATFORM}%",))
            if n:
                item["metadata_json"] = n
        if item:
            leftover[table] = item
    return leftover


def plan(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    actions: list[dict[str, Any]] = []
    for table in REWRITE_SESSION_TABLES:
        if table not in tables:
            continue
        cols = _columns(conn, table)
        n = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE session_id LIKE ?", (f"{OLD_PLATFORM}:%",))
        if not n:
            continue
        actions.append({"table": table, "op": "rewrite_session", "rows": n, "rewrite_principal": "subject_principal_id" in cols})
    for table, key in DROP_IF_CONFLICT:
        if table not in tables:
            continue
        n = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE session_id LIKE ?", (f"{OLD_PLATFORM}:%",))
        if not n:
            continue
        actions.append({"table": table, "op": "rewrite_or_drop", "rows": n, "conflict_key": list(key)})
    traces = 0
    if "injection_traces" in tables and "metadata_json" in _columns(conn, "injection_traces"):
        traces = _count(conn, "SELECT COUNT(*) FROM injection_traces WHERE metadata_json LIKE ?", (f"%{OLD_PLATFORM}%",))
        if traces:
            actions.append({"table": "injection_traces", "op": "rewrite_metadata_json", "rows": traces})
    return {
        "old_platform": OLD_PLATFORM,
        "new_platform": NEW_PLATFORM,
        "group_id": GROUP_ID,
        "old_group_session": OLD_GROUP_SESSION,
        "new_group_session": NEW_GROUP_SESSION,
        "inventory": inventory(conn),
        "actions": actions,
    }


def _conflict_exists(conn: sqlite3.Connection, table: str, key: tuple[str, ...], row: sqlite3.Row) -> bool:
    rewritten = []
    values = []
    for column in key:
        value = row[column]
        if column == "session_id":
            value = _rewrite_session_id(str(value))
        elif column == "subject_principal_id":
            value = _rewrite_principal(value)
        rewritten.append(f"{column}=?")
        values.append(value)
    sql = f"SELECT 1 FROM {table} WHERE " + " AND ".join(rewritten) + " LIMIT 1"
    return conn.execute(sql, tuple(values)).fetchone() is not None


def apply(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    counts: Counter[str] = Counter()
    conn.row_factory = sqlite3.Row
    for table in REWRITE_SESSION_TABLES:
        if table not in tables:
            continue
        cols = _columns(conn, table)
        rows = conn.execute(
            f"SELECT rowid AS _rid, session_id FROM {table} WHERE session_id LIKE ?",
            (f"{OLD_PLATFORM}:%",),
        ).fetchall()
        for row in rows:
            new_session = _rewrite_session_id(str(row["session_id"]))
            if "subject_principal_id" in cols:
                principal = conn.execute(
                    f"SELECT subject_principal_id FROM {table} WHERE rowid=?",
                    (row["_rid"],),
                ).fetchone()[0]
                conn.execute(
                    f"UPDATE {table} SET session_id=?, subject_principal_id=? WHERE rowid=?",
                    (new_session, _rewrite_principal(principal), row["_rid"]),
                )
            else:
                conn.execute(f"UPDATE {table} SET session_id=? WHERE rowid=?", (new_session, row["_rid"]))
            counts[f"{table}.rewritten"] += 1
    for table, key in DROP_IF_CONFLICT:
        if table not in tables:
            continue
        cols = _columns(conn, table)
        rows = conn.execute(
            f"SELECT rowid AS _rid, * FROM {table} WHERE session_id LIKE ?",
            (f"{OLD_PLATFORM}:%",),
        ).fetchall()
        for row in rows:
            if _conflict_exists(conn, table, key, row):
                conn.execute(f"DELETE FROM {table} WHERE rowid=?", (row["_rid"],))
                counts[f"{table}.dropped_conflict"] += 1
                continue
            new_session = _rewrite_session_id(str(row["session_id"]))
            if "subject_principal_id" in cols:
                conn.execute(
                    f"UPDATE {table} SET session_id=?, subject_principal_id=? WHERE rowid=?",
                    (new_session, _rewrite_principal(row["subject_principal_id"]), row["_rid"]),
                )
            else:
                conn.execute(f"UPDATE {table} SET session_id=? WHERE rowid=?", (new_session, row["_rid"]))
            counts[f"{table}.rewritten"] += 1
    if "injection_traces" in tables and "metadata_json" in _columns(conn, "injection_traces"):
        rows = conn.execute(
            "SELECT rowid AS _rid, metadata_json FROM injection_traces WHERE metadata_json LIKE ?",
            (f"%{OLD_PLATFORM}%",),
        ).fetchall()
        for row in rows:
            raw = str(row["metadata_json"] or "")
            updated = (
                raw.replace(OLD_GROUP_SESSION, NEW_GROUP_SESSION)
                .replace(OLD_USER_PREFIX, NEW_USER_PREFIX)
                .replace(f'"platform_id": "{OLD_PLATFORM}"', f'"platform_id": "{NEW_PLATFORM}"')
                .replace(f'"platform_id":"{OLD_PLATFORM}"', f'"platform_id":"{NEW_PLATFORM}"')
                .replace(OLD_PREFIX, NEW_PREFIX)
            )
            if updated != raw:
                conn.execute("UPDATE injection_traces SET metadata_json=? WHERE rowid=?", (updated, row["_rid"]))
                counts["injection_traces.rewritten"] += 1
    leftover = inventory(conn)
    return {"applied": dict(counts), "leftover": leftover}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    db = args.db.expanduser().resolve()
    if not db.is_file():
        raise SystemExit(f"database not found: {db}")
    if args.apply and args.confirmation != CONFIRMATION:
        raise SystemExit(f"live apply requires --confirmation {CONFIRMATION}")
    readonly = not args.apply
    conn = _connect(db, readonly=readonly)
    try:
        payload = {"db": str(db), "apply": bool(args.apply), "planned": plan(conn)}
        if args.apply:
            payload["result"] = apply(conn)
            conn.commit()
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        leftover = payload.get("result", {}).get("leftover") if args.apply else payload["planned"]["inventory"]
        blocking = {
            table: info
            for table, info in (leftover or {}).items()
            if table != "injection_traces" or info.get("session_id")
        }
        if args.apply and blocking:
            raise SystemExit("merge left 羽书2 rows behind")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
