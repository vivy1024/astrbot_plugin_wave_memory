"""Staged Legacy -> formal shared knowledge migration.

This module is deliberately offline-first.  It copies a source SQLite database to a
staged target, migrates only the explicitly selected generic/affinity domains, verifies
row mappings, and deletes the selected low-quality Legacy rows only after migration
checks pass.  It never mutates the source database in place.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping

try:
    from ..scope_recovery import (
        PURGE_TABLES_BY_DOMAIN,
        SCOPE_RECOVERY_RULE_VERSION,
        _valid_bot_id,
        normalize_target_scopes,
    )
except ImportError:  # pragma: no cover - direct repository imports
    from services.scope_recovery import (
        PURGE_TABLES_BY_DOMAIN,
        SCOPE_RECOVERY_RULE_VERSION,
        _valid_bot_id,
        normalize_target_scopes,
    )


MIGRATION_RULE_VERSION = "scope-recovery-migration/1"
PURGE_TABLES = tuple(table for tables in PURGE_TABLES_BY_DOMAIN.values() for table in tables)
INDEX_REBUILD_STEPS = (
    "memory_hnsw",
    "tag_catalog_hnsw",
    "scoped_memory_effective_tags",
    "cooccurrence",
)


class ScopeRecoveryMigrationError(ValueError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes_sha256__": hashlib.sha256(bytes(value)).hexdigest()}
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in _tables(conn):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    missing = required - _columns(conn, table)
    if missing:
        raise ScopeRecoveryMigrationError(f"{table}_missing_columns:{','.join(sorted(missing))}")


def _scope_key(scope: Mapping[str, str]) -> str:
    return f"{scope['bot_id']}|{scope['session_id']}|{scope['visibility']}"


def _scope_values(scope: Mapping[str, str]) -> tuple[str, str, str]:
    return scope["bot_id"], scope["session_id"], scope["visibility"]


def _target_scopes(value: Any) -> tuple[dict[str, str], ...]:
    scopes = normalize_target_scopes(value)
    if len(scopes) != 2:
        raise ScopeRecoveryMigrationError("exactly_two_target_scopes_required")
    return scopes


def _scope_group_matches(scope: Mapping[str, str], group_id: Any, bot_id: Any = None, *, require_bot: bool = False) -> bool:
    normalized_bot = _text(bot_id)
    return (
        _text(group_id) == scope["group_id"]
        and (normalized_bot == scope["bot_id"] if normalized_bot else not require_bot)
    )


def _attitude(affinity: int) -> str:
    if affinity >= 60:
        return "intimate"
    if affinity >= 30:
        return "friendly"
    if affinity >= 0:
        return "neutral"
    if affinity >= -30:
        return "cold"
    return "hostile"


def _ensure_migration_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scope_recovery_migrations (
            run_id TEXT PRIMARY KEY,
            rule_version TEXT NOT NULL,
            source_snapshot_hash TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            target_scopes_json TEXT NOT NULL,
            status TEXT NOT NULL,
            indexes_status TEXT NOT NULL,
            created_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS scope_recovery_items (
            source_table TEXT NOT NULL,
            legacy_id TEXT NOT NULL,
            target_scope_key TEXT NOT NULL,
            disposition TEXT NOT NULL,
            target_id INTEGER,
            source_hash TEXT NOT NULL,
            target_hash TEXT,
            run_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(source_table, legacy_id, target_scope_key)
        );
        CREATE TABLE IF NOT EXISTS scope_recovery_memory_map (
            legacy_memory_id INTEGER NOT NULL,
            target_scope_key TEXT NOT NULL,
            target_memory_id INTEGER NOT NULL,
            origin_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            PRIMARY KEY(legacy_memory_id, target_scope_key)
        );
        """
    )


def _item_exists(conn: sqlite3.Connection, table: str, legacy_id: Any, scope_key: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT source_table, legacy_id, target_scope_key, disposition, target_id
             FROM scope_recovery_items
            WHERE source_table=? AND legacy_id=? AND target_scope_key=?""",
        (table, str(legacy_id), scope_key),
    ).fetchone()


def _record_item(
    conn: sqlite3.Connection,
    *,
    source_table: str,
    legacy_id: Any,
    scope_key: str,
    disposition: str,
    target_id: int | None,
    source_hash: str,
    target_hash: str | None,
    run_id: str,
) -> None:
    conn.execute(
        """INSERT INTO scope_recovery_items(
               source_table, legacy_id, target_scope_key, disposition, target_id,
               source_hash, target_hash, run_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_table, legacy_id, target_scope_key) DO UPDATE SET
               disposition=excluded.disposition, target_id=excluded.target_id,
               source_hash=excluded.source_hash, target_hash=excluded.target_hash,
               run_id=excluded.run_id""",
        (source_table, str(legacy_id), scope_key, disposition, target_id, source_hash, target_hash, run_id, time.time()),
    )


def _row_hash(row: Mapping[str, Any]) -> str:
    return _sha256({str(key): row[key] for key in sorted(row)})


def _memory_select(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "memories")
    _require_columns(conn, "memories", {"id", "group_id", "content", "bot_id", "session_id", "visibility"})
    selected = [
        name for name in (
            "id", "group_id", "sender_id", "sender_name", "content", "vector", "timestamp",
            "importance", "memory_type", "source", "summary", "bot_id", "session_id", "visibility",
            "origin_fingerprint", "provenance", "version", "quarantine", "resolution_state",
        ) if name in columns
    ]
    where = "(bot_id IS NULL OR bot_id='' OR session_id IS NULL OR session_id='' OR visibility IS NULL OR visibility='')"
    rows = conn.execute(f"SELECT {', '.join(selected)} FROM memories WHERE {where} ORDER BY id").fetchall()
    return [{name: row[index] for index, name in enumerate(selected)} for row in rows]


def _insert_shared_memory(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    scope: Mapping[str, str],
    *,
    run_id: str,
    origin_prefix: str = "legacy_shared",
    source_override: str | None = "legacy_shared",
    provenance_extra: Mapping[str, Any] | None = None,
    memory_columns: set[str] | None = None,
    check_origin_fingerprint: bool = True,
    source_hash_override: str | None = None,
) -> int:
    scope_key = _scope_key(scope)
    legacy_id = int(row["id"])
    origin_key = f"{origin_prefix}|memories|{legacy_id}|{scope_key}"
    existing = conn.execute(
        "SELECT target_memory_id FROM scope_recovery_memory_map WHERE legacy_memory_id=? AND target_scope_key=?",
        (legacy_id, scope_key),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    fingerprint = _sha256(origin_key)
    columns = memory_columns or _columns(conn, "memories")
    existing = None
    if check_origin_fingerprint and "origin_fingerprint" in columns:
        existing = conn.execute("SELECT id FROM memories WHERE origin_fingerprint=?", (fingerprint,)).fetchone()
    if existing is not None:
        target_id = int(existing[0])
    else:
        provenance = {
            "kind": "wave_memory_provenance",
            "version": f"{origin_prefix}/v1",
            "migration_rule": MIGRATION_RULE_VERSION,
            "legacy_source_table": "memories",
            "legacy_id": legacy_id,
            "legacy_shared": origin_prefix == "legacy_shared",
            "origin_key": origin_key,
            "source_group_id": _text(row.get("group_id")),
            "target_scope": dict(scope),
            **dict(provenance_extra or {}),
        }
        values: dict[str, Any] = {
            "group_id": scope["group_id"],
            "sender_id": row.get("sender_id") or "",
            "sender_name": row.get("sender_name") or "",
            "content": row.get("content") or "",
            "vector": row.get("vector"),
            "timestamp": row.get("timestamp") or time.time(),
            "importance": row.get("importance") if row.get("importance") is not None else 1.0,
            "access_count": row.get("access_count") if row.get("access_count") is not None else 0,
            "last_accessed": row.get("last_accessed"),
            "memory_type": row.get("memory_type") or "message",
            "source": row.get("source") if source_override is None else source_override,
            "summary": row.get("summary"),
            "bot_id": scope["bot_id"],
            "session_id": scope["session_id"],
            "visibility": "group",
            "origin_fingerprint": fingerprint,
            "provenance": _canonical(provenance),
            "version": "2",
            "quarantine": 0,
            "resolution_state": "resolved",
        }
        usable = {key: value for key, value in values.items() if key in columns}
        if not {"group_id", "content", "bot_id", "session_id", "visibility"} <= set(usable):
            raise ScopeRecoveryMigrationError("memories_formal_scope_columns_required")
        names = list(usable)
        cursor = conn.execute(
            f"INSERT INTO memories ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            [usable[name] for name in names],
        )
        target_id = int(cursor.lastrowid)
    conn.execute(
        """INSERT INTO scope_recovery_memory_map(
               legacy_memory_id, target_scope_key, target_memory_id, origin_key, run_id
           ) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(legacy_memory_id, target_scope_key) DO UPDATE SET target_memory_id=excluded.target_memory_id, run_id=excluded.run_id""",
        (legacy_id, scope_key, target_id, origin_key, run_id),
    )
    _record_item(
        conn,
        source_table="memories",
        legacy_id=legacy_id,
        scope_key=scope_key,
        disposition="migrated",
        target_id=target_id,
        source_hash=source_hash_override or _row_hash(row),
        target_hash=_sha256({"target_id": target_id, "origin_key": origin_key}),
        run_id=run_id,
    )
    return target_id


def _target_memory_id(conn: sqlite3.Connection, legacy_memory_id: Any, scope_key: str) -> int | None:
    row = conn.execute(
        "SELECT target_memory_id FROM scope_recovery_memory_map WHERE legacy_memory_id=? AND target_scope_key=?",
        (int(legacy_memory_id), scope_key),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _migrate_facts(conn: sqlite3.Connection, scopes: tuple[dict[str, str], ...], run_id: str) -> dict[str, int]:
    if not _columns(conn, "facts"):
        return {"migrated": 0, "review": 0}
    _require_columns(conn, "scoped_facts", {"bot_id", "session_id", "visibility", "subject", "predicate", "object", "status"})
    columns = _columns(conn, "facts")
    selected = [name for name in ("id", "subject", "predicate", "object", "confidence", "status", "source_memory_id", "created_at") if name in columns]
    rows = conn.execute(f"SELECT {', '.join(selected)} FROM facts ORDER BY id").fetchall()
    migrated = review = 0
    for raw in rows:
        row = {name: raw[index] for index, name in enumerate(selected)}
        if not all(_text(row.get(name)) for name in ("subject", "predicate", "object")):
            review += 1
            continue
        for scope in scopes:
            scope_key = _scope_key(scope)
            legacy_id = row.get("id")
            if _item_exists(conn, "facts", legacy_id, scope_key):
                migrated += 1
                continue
            source_memory_id = row.get("source_memory_id")
            target_memory_id = _target_memory_id(conn, source_memory_id, scope_key) if source_memory_id is not None else None
            if source_memory_id is not None and target_memory_id is None:
                review += 1
                _record_item(conn, source_table="facts", legacy_id=legacy_id, scope_key=scope_key, disposition="review", target_id=None, source_hash=_row_hash(row), target_hash=None, run_id=run_id)
                continue
            existing = conn.execute(
                """SELECT id FROM scoped_facts WHERE bot_id=? AND session_id=? AND visibility=?
                   AND subject=? AND predicate=? AND object=?""",
                (*_scope_values(scope), _text(row["subject"]), _text(row["predicate"]), _text(row["object"])),
            ).fetchone()
            if existing is not None:
                target_id = int(existing[0])
            else:
                fact_columns = _columns(conn, "scoped_facts")
                now = time.time()
                values = {
                    "bot_id": scope["bot_id"], "session_id": scope["session_id"], "visibility": "group",
                    "subject": _text(row["subject"]), "predicate": _text(row["predicate"]), "object": _text(row["object"]),
                    "confidence": float(row.get("confidence") or 0.0), "status": str(row.get("status") or "pending"),
                    "source_memory_id": target_memory_id,
                    "provenance": _canonical({"legacy_shared": True, "source_table": "facts", "legacy_id": legacy_id, "migration_rule": MIGRATION_RULE_VERSION}),
                    "valid_from": None, "valid_until": None, "created_at": row.get("created_at") or now, "updated_at": now, "revision": 1,
                }
                usable = {key: value for key, value in values.items() if key in fact_columns}
                names = list(usable)
                cursor = conn.execute(f"INSERT INTO scoped_facts ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", [usable[name] for name in names])
                target_id = int(cursor.lastrowid)
            _record_item(conn, source_table="facts", legacy_id=legacy_id, scope_key=scope_key, disposition="migrated", target_id=target_id, source_hash=_row_hash(row), target_hash=_sha256({"target_id": target_id}), run_id=run_id)
            migrated += 1
    return {"migrated": migrated, "review": review}


def _upsert_catalog(conn: sqlite3.Connection, name: str, tag_type: str, embedding: Any = None) -> int | None:
    if "tag_catalog" not in _tables(conn):
        return None
    normalized = unicodedata.normalize("NFKC", _text(name))
    if not normalized:
        return None
    normalized_type = _text(tag_type) or "keyword"
    now = time.time()
    try:
        conn.execute(
            """INSERT INTO tag_catalog(normalized_name, display_name, tag_type, description, status, created_at, updated_at)
               VALUES (?, ?, ?, '', 'active', ?, ?)
               ON CONFLICT(normalized_name, tag_type) DO UPDATE SET
                   display_name=CASE WHEN tag_catalog.display_name='' THEN excluded.display_name ELSE tag_catalog.display_name END,
                   updated_at=excluded.updated_at""",
            (normalized, _text(name), normalized_type, now, now),
        )
    except sqlite3.OperationalError:
        conn.execute(
            "INSERT OR IGNORE INTO tag_catalog(normalized_name, display_name, tag_type, description, status, created_at, updated_at) VALUES (?, ?, ?, '', 'active', ?, ?)",
            (normalized, _text(name), normalized_type, now, now),
        )
    row = conn.execute(
        "SELECT id FROM tag_catalog WHERE normalized_name=? AND tag_type=?",
        (normalized, normalized_type),
    ).fetchone()
    if row is None:
        return None
    catalog_id = int(row[0])
    catalog_columns = _columns(conn, "tag_catalog")
    if embedding is not None and "embedding" in catalog_columns:
        raw = bytes(embedding)
        assignments = ["embedding=COALESCE(embedding, ?)"]
        params: list[Any] = [raw]
        if "embedding_model" in catalog_columns:
            assignments.append("embedding_model=COALESCE(embedding_model, ?)")
            params.append("legacy-tag-vector")
        if "embedding_dim" in catalog_columns:
            assignments.append("embedding_dim=COALESCE(embedding_dim, ?)")
            params.append(len(raw) // 4 if len(raw) % 4 == 0 else None)
        assignments.append("updated_at=?")
        params.extend((now, catalog_id))
        conn.execute(
            f"UPDATE tag_catalog SET {', '.join(assignments)} WHERE id=?",
            params,
        )
    return catalog_id


def _upsert_scoped_tag(conn: sqlite3.Connection, scope: Mapping[str, str], tag: Mapping[str, Any]) -> int:
    _require_columns(conn, "scoped_tags", {"bot_id", "session_id", "visibility", "name", "tag_type"})
    columns = _columns(conn, "scoped_tags")
    name = _text(tag.get("name"))
    tag_type = _text(tag.get("tag_type") or tag.get("type") or "keyword")
    catalog_id = _upsert_catalog(conn, name, tag_type, tag.get("vector"))
    existing = conn.execute("SELECT id FROM scoped_tags WHERE bot_id=? AND session_id=? AND visibility=? AND name=?", (*_scope_values(scope), name)).fetchone()
    if existing is not None:
        return int(existing[0])
    values = {
        "catalog_id": catalog_id,
        "bot_id": scope["bot_id"], "session_id": scope["session_id"], "visibility": "group",
        "name": name, "tag_type": tag_type, "description": _text(tag.get("description")),
        "confidence": float(tag.get("confidence") or 0.0), "metadata": _canonical({"legacy_shared": True, "legacy_tag_id": tag.get("id")}),
        "created_at": tag.get("created_at") or time.time(), "updated_at": time.time(),
    }
    usable = {key: value for key, value in values.items() if key in columns}
    names = list(usable)
    cursor = conn.execute(f"INSERT INTO scoped_tags ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", [usable[name] for name in names])
    return int(cursor.lastrowid)


def _migrate_tags(conn: sqlite3.Connection, scopes: tuple[dict[str, str], ...], run_id: str) -> dict[str, int]:
    if not {"tags", "memory_tags"} <= _tables(conn):
        return {"migrated": 0, "review": 0}
    _require_columns(conn, "scoped_tags", {"bot_id", "session_id", "visibility", "name"})
    _require_columns(conn, "scoped_memory_tags", {"bot_id", "session_id", "visibility", "memory_id", "tag_id"})
    tag_columns = _columns(conn, "tags")
    tag_selected = [name for name in ("id", "name", "tag_type", "description", "confidence", "vector", "created_at") if name in tag_columns]
    tags = {int(row[0]): {name: row[index] for index, name in enumerate(tag_selected)} for row in conn.execute(f"SELECT {', '.join(tag_selected)} FROM tags").fetchall()}
    mt_rows = conn.execute("SELECT rowid, memory_id, tag_id, position, relevance FROM memory_tags").fetchall()
    migrated = review = 0
    for legacy_row_id, legacy_memory_id, legacy_tag_id, position, relevance in mt_rows:
        tag = tags.get(int(legacy_tag_id))
        if tag is None:
            review += 1
            continue
        for scope in scopes:
            scope_key = _scope_key(scope)
            if _item_exists(conn, "memory_tags", legacy_row_id, scope_key):
                migrated += 1
                continue
            target_memory_id = _target_memory_id(conn, legacy_memory_id, scope_key)
            if target_memory_id is None:
                review += 1
                _record_item(conn, source_table="memory_tags", legacy_id=legacy_row_id, scope_key=scope_key, disposition="review", target_id=None, source_hash=_sha256({"memory_id": legacy_memory_id, "tag_id": legacy_tag_id}), target_hash=None, run_id=run_id)
                continue
            target_tag_id = _upsert_scoped_tag(conn, scope, tag)
            conn.execute(
                """INSERT OR IGNORE INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (*_scope_values(scope), target_memory_id, target_tag_id, int(position or 0), float(relevance or 1.0), time.time()),
            )
            _record_item(conn, source_table="memory_tags", legacy_id=legacy_row_id, scope_key=scope_key, disposition="migrated", target_id=target_tag_id, source_hash=_sha256({"memory_id": legacy_memory_id, "tag_id": legacy_tag_id}), target_hash=_sha256({"memory_id": target_memory_id, "tag_id": target_tag_id}), run_id=run_id)
            migrated += 1
    if "tag_relations" in _tables(conn):
        review += _row_count(conn, "tag_relations")
    return {"migrated": migrated, "review": review}


def _migrate_relationship_events(conn: sqlite3.Connection, scopes: tuple[dict[str, str], ...], run_id: str) -> dict[str, int]:
    if "relationship_events" not in _tables(conn) or "scoped_soul_relationship_events" not in _tables(conn):
        return {"migrated": 0, "review": 0}
    columns = _columns(conn, "relationship_events")
    required = {"id", "bot_id", "group_id", "user_id", "event_type", "dimension", "delta", "reason"}
    if not required <= columns:
        return {"migrated": 0, "review": _row_count(conn, "relationship_events")}
    selected = [name for name in ("id", "bot_id", "group_id", "user_id", "event_type", "dimension", "delta", "reason", "source_episode_id", "source_memory_id", "created_at") if name in columns]
    migrated = review = 0
    for raw in conn.execute(f"SELECT {', '.join(selected)} FROM relationship_events ORDER BY id").fetchall():
        row = {name: raw[index] for index, name in enumerate(selected)}
        targets = [scope for scope in scopes if _scope_group_matches(scope, row.get("group_id"), row.get("bot_id"), require_bot=True)]
        if not targets or not _text(row.get("user_id")):
            review += 1
            continue
        for scope in targets:
            scope_key = _scope_key(scope)
            legacy_id = row["id"]
            if _item_exists(conn, "relationship_events", legacy_id, scope_key):
                migrated += 1
                continue
            subject = f"{scope['session_id'].split(':', 1)[0]}:user:{_text(row['user_id'])}"
            source_memory_id = row.get("source_memory_id")
            target_memory_id = _target_memory_id(conn, source_memory_id, scope_key) if source_memory_id is not None else None
            if source_memory_id is not None and target_memory_id is None:
                review += 1
                _record_item(conn, source_table="relationship_events", legacy_id=legacy_id, scope_key=scope_key, disposition="review", target_id=None, source_hash=_row_hash(row), target_hash=None, run_id=run_id)
                continue
            now = row.get("created_at") or time.time()
            conn.execute(
                """INSERT INTO scoped_soul_relationship_events(
                       bot_id, session_id, visibility, subject_principal_id, event_type, dimension,
                       delta, reason, source_episode_id, source_memory_id, revision, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (*_scope_values(scope), subject, _text(row.get("event_type")), _text(row.get("dimension")), float(row.get("delta") or 0.0), _text(row.get("reason")), row.get("source_episode_id"), target_memory_id, now),
            )
            target_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            _record_item(conn, source_table="relationship_events", legacy_id=legacy_id, scope_key=scope_key, disposition="migrated", target_id=target_id, source_hash=_row_hash(row), target_hash=_sha256({"target_id": target_id}), run_id=run_id)
            migrated += 1
    return {"migrated": migrated, "review": review}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) if table in _tables(conn) else 0


def _migrate_affinity(conn: sqlite3.Connection, scopes: tuple[dict[str, str], ...], run_id: str) -> dict[str, int]:
    migrated = review = 0
    if "user_profiles" in _tables(conn):
        columns = _columns(conn, "user_profiles")
        if {"user_id", "group_id", "bot_id"} <= columns and "scoped_soul_relationships" in _tables(conn):
            selected = [name for name in ("id", "user_id", "group_id", "bot_id", "affection", "metadata", "last_seen") if name in columns]
            for raw in conn.execute(f"SELECT {', '.join(selected)} FROM user_profiles").fetchall():
                row = {name: raw[index] for index, name in enumerate(selected)}
                target_scopes = [scope for scope in scopes if _scope_group_matches(scope, row.get("group_id"), row.get("bot_id"), require_bot=True)]
                if not target_scopes or not _text(row.get("user_id")):
                    review += 1
                    continue
                for scope in target_scopes:
                    scope_key = _scope_key(scope)
                    legacy_id = row.get("id") or f"{row.get('user_id')}:{row.get('group_id')}:{row.get('bot_id')}"
                    if _item_exists(conn, "user_profiles", legacy_id, scope_key):
                        migrated += 1
                        continue
                    subject = f"{scope['session_id'].split(':', 1)[0]}:user:{_text(row['user_id'])}"
                    try:
                        affinity = max(-100, min(100, int(row.get("affection") or 0)))
                    except (TypeError, ValueError):
                        review += 1
                        continue
                    existing = conn.execute("SELECT revision FROM scoped_soul_relationships WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?", (*_scope_values(scope), subject)).fetchone()
                    revision = int(existing[0] or 0) + 1 if existing else 1
                    now = time.time()
                    evidence = _canonical([{"source_table": "user_profiles", "legacy_id": legacy_id, "legacy_shared": False, "bot_id": scope["bot_id"]}])
                    if existing:
                        conn.execute("UPDATE scoped_soul_relationships SET affinity=?, state=?, dimensions=?, revision=?, evidence=?, updated_at=? WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?", (affinity, _attitude(affinity), "{}", revision, evidence, now, *_scope_values(scope), subject))
                    else:
                        conn.execute("INSERT INTO scoped_soul_relationships(bot_id, session_id, visibility, subject_principal_id, affinity, state, dimensions, revision, evidence, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*_scope_values(scope), subject, affinity, _attitude(affinity), "{}", revision, evidence, now))
                    _record_item(conn, source_table="user_profiles", legacy_id=legacy_id, scope_key=scope_key, disposition="migrated", target_id=None, source_hash=_row_hash(row), target_hash=_sha256({"subject": subject, "affinity": affinity}), run_id=run_id)
                    migrated += 1
    return {"migrated": migrated, "review": review}


def _validate_staged_migration(conn: sqlite3.Connection, memory_rows: list[dict[str, Any]], scopes: tuple[dict[str, str], ...]) -> None:
    expected = len(memory_rows) * len(scopes)
    actual = int(conn.execute("SELECT COUNT(*) FROM scope_recovery_memory_map").fetchone()[0])
    if actual != expected:
        raise ScopeRecoveryMigrationError(f"memory_target_mapping_incomplete:{actual}/{expected}")
    for scope in scopes:
        count = int(conn.execute("SELECT COUNT(*) FROM scope_recovery_memory_map WHERE target_scope_key=?", (_scope_key(scope),)).fetchone()[0])
        if count != len(memory_rows):
            raise ScopeRecoveryMigrationError(f"memory_target_scope_mapping_incomplete:{_scope_key(scope)}:{count}/{len(memory_rows)}")


def _delete_purge_rows(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for table in PURGE_TABLES:
        if table not in _tables(conn):
            continue
        count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        if count:
            conn.execute(f'DELETE FROM "{table}"')
        deleted[table] = count
    return deleted


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def apply_staged_migration(
    source_db_path: str | os.PathLike[str],
    output_db_path: str | os.PathLike[str],
    run_dir: str | os.PathLike[str],
    target_scopes: Any,
    *,
    plan_hash: str = "",
    expected_source_hash: str = "",
    confirmation: str = "",
) -> dict[str, Any]:
    """Apply migration to a staged copy; source DB is never replaced in place."""
    source = Path(source_db_path).resolve()
    output = Path(output_db_path).resolve()
    if source == output:
        raise ScopeRecoveryMigrationError("source_and_output_db_must_differ")
    if not source.is_file():
        raise ScopeRecoveryMigrationError("source_database_missing")
    if not plan_hash:
        raise ScopeRecoveryMigrationError("plan_hash_required")
    if not expected_source_hash:
        raise ScopeRecoveryMigrationError("source_snapshot_hash_required")
    if confirmation != "migrate":
        raise ScopeRecoveryMigrationError("migration_confirmation_required")
    scopes = _target_scopes(target_scopes)
    source_hash = _source_hash(source)
    if expected_source_hash and expected_source_hash != source_hash:
        raise ScopeRecoveryMigrationError("source_snapshot_hash_mismatch")
    target_scopes_json = _canonical(list(scopes))
    run_id = "scope-recovery-run:" + uuid.uuid4().hex
    run_path = Path(run_dir).resolve()
    run_path.mkdir(parents=True, exist_ok=True)
    source_backup = run_path / f"{run_id.replace(':', '-')}-source-before.sqlite3"
    shutil.copy2(source, source_backup)
    if _source_hash(source_backup) != source_hash:
        raise ScopeRecoveryMigrationError("source_changed_during_backup")
    staging = run_path / f".target-{uuid.uuid4().hex}.sqlite3"
    output.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source_backup)
    target_conn = sqlite3.connect(staging)
    target_conn.row_factory = sqlite3.Row
    try:
        source_conn.backup(target_conn)
        target_conn.execute("PRAGMA foreign_keys=ON")
        _ensure_migration_tables(target_conn)
        target_conn.execute(
            "INSERT INTO scope_recovery_migrations(run_id, rule_version, source_snapshot_hash, plan_hash, target_scopes_json, status, indexes_status, created_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (run_id, SCOPE_RECOVERY_RULE_VERSION, source_hash, plan_hash, target_scopes_json, "pending:" + ",".join(INDEX_REBUILD_STEPS), time.time()),
        )
        memory_rows = _memory_select(target_conn)
        for row in memory_rows:
            for scope in scopes:
                _insert_shared_memory(target_conn, row, scope, run_id=run_id)
        fact_result = _migrate_facts(target_conn, scopes, run_id)
        tag_result = _migrate_tags(target_conn, scopes, run_id)
        relationship_event_result = _migrate_relationship_events(target_conn, scopes, run_id)
        affinity_result = _migrate_affinity(target_conn, scopes, run_id)
        _validate_staged_migration(target_conn, memory_rows, scopes)
        deleted = _delete_purge_rows(target_conn, run_id)
        migration_report = {
            "run_id": run_id,
            "rule_version": MIGRATION_RULE_VERSION,
            "source_snapshot_hash": source_hash,
            "source_backup_path": str(source_backup),
            "plan_hash": plan_hash,
            "target_scopes": list(scopes),
            "migrated": {
                "memories": len(memory_rows) * len(scopes),
                "facts": fact_result["migrated"],
                "tags": tag_result["migrated"],
                "relationship_events": relationship_event_result["migrated"],
                "affinity": affinity_result["migrated"],
            },
            "review": {
                "facts": fact_result["review"],
                "tags_and_tag_relations": tag_result["review"],
                "relationship_events": relationship_event_result["review"],
                "experience_episodes": _row_count(target_conn, "experience_episodes"),
                "affinity": affinity_result["review"],
            },
            "deleted": deleted,
            "skipped": {"time_anchors": True, "raw_book_tables": True, "curated_holyman": True, "experience_episodes": "review_only"},
            "indexes_status": "pending",
            "index_rebuild_steps": list(INDEX_REBUILD_STEPS),
        }
        target_conn.execute("UPDATE scope_recovery_migrations SET status='staged', completed_at=? WHERE run_id=?", (time.time(), run_id))
        target_conn.commit()
        (run_path / f"{run_id.replace(':', '-')}.json").write_text(_canonical(migration_report) + "\n", encoding="utf-8")
        target_conn.close()
        source_conn.close()
        os.replace(staging, output)
        return migration_report
    except Exception:
        target_conn.rollback()
        target_conn.close()
        source_conn.close()
        try:
            staging.unlink()
        except OSError:
            pass
        raise


CLASSIFIED_RECOVERY_RULE_VERSION = "classified-scope-recovery/1"
_RECOVERABLE_CATEGORIES = frozenset({"generic_shared_candidate", "group_chat_candidate"})
_FORMAL_SCOPE_TABLES = (
    "memories",
    "scoped_facts",
    "scoped_tags",
    "scoped_beliefs",
    "scoped_jargon",
    "scoped_soul_mood",
    "scoped_soul_concerns",
    "scoped_soul_revisions",
    "scoped_soul_timeline",
    "scoped_soul_relationships",
    "scoped_soul_relationship_events",
)


def _chunked(values: list[int], size: int = 800):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _formal_group_scopes(conn: sqlite3.Connection) -> tuple[dict[str, str], ...]:
    """Discover only already-persisted canonical group Scopes."""
    scopes: dict[tuple[str, str, str], dict[str, str]] = {}
    tables = _tables(conn)
    for table in _FORMAL_SCOPE_TABLES:
        columns = _columns(conn, table)
        if table not in tables or not {"bot_id", "session_id", "visibility"} <= columns:
            continue
        where = "bot_id IS NOT NULL AND bot_id!='' AND session_id IS NOT NULL AND session_id!='' AND visibility='group'"
        if table == "memories":
            where += " AND resolution_state='resolved' AND COALESCE(quarantine,0)=0 AND COALESCE(memory_type,'message') NOT IN ('archived','evicted','deleted')"
        for bot_id, session_id, visibility in conn.execute(
            f"SELECT DISTINCT bot_id,session_id,visibility FROM {table} WHERE {where}"
        ).fetchall():
            bot = _text(bot_id)
            session = _text(session_id)
            parts = session.split(":", 2)
            if not _valid_bot_id(bot) or len(parts) != 3 or not parts[0] or parts[1] != "group" or not parts[2]:
                continue
            key = (bot, session, "group")
            scopes[key] = {
                "bot_id": bot,
                "session_id": session,
                "visibility": "group",
                "group_id": parts[2],
            }
    return tuple(scopes[key] for key in sorted(scopes))


def _load_classified_items(report_path: Path) -> tuple[dict[str, Any], dict[int, dict[str, Any]], str]:
    if not report_path.is_file():
        raise ScopeRecoveryMigrationError("classification_report_missing")
    raw = report_path.read_bytes()
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScopeRecoveryMigrationError("classification_report_invalid") from exc
    if not isinstance(report, Mapping) or not isinstance(report.get("items"), list):
        raise ScopeRecoveryMigrationError("classification_report_items_required")
    items: dict[int, dict[str, Any]] = {}
    for value in report["items"]:
        if not isinstance(value, Mapping):
            continue
        try:
            memory_id = int(value.get("memory_id"))
        except (TypeError, ValueError):
            continue
        category = _text(value.get("category"))
        if category in _RECOVERABLE_CATEGORIES:
            items[memory_id] = dict(value)
    return dict(report), items, "sha256:" + hashlib.sha256(raw).hexdigest()


def _memory_rows_for_ids(conn: sqlite3.Connection, memory_ids: list[int]) -> dict[int, dict[str, Any]]:
    columns = _columns(conn, "memories")
    selected = [
        name for name in (
            "id", "group_id", "sender_id", "sender_name", "content", "vector", "timestamp",
            "importance", "access_count", "last_accessed", "memory_type", "source", "summary",
            "bot_id", "session_id", "visibility", "origin_fingerprint", "provenance", "version",
            "quarantine", "resolution_state",
        ) if name in columns
    ]
    result: dict[int, dict[str, Any]] = {}
    for chunk in _chunked(memory_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM memories WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        for raw in rows:
            row = {name: raw[index] for index, name in enumerate(selected)}
            result[int(row["id"])] = row
    return result


def _row_is_recoverable(row: Mapping[str, Any], category: str) -> bool:
    if int(row.get("quarantine") or 0) != 0:
        return False
    resolution = _text(row.get("resolution_state"))
    if resolution not in {"", "resolved"}:
        return False
    if all(_text(row.get(name)) for name in ("bot_id", "session_id", "visibility")):
        return False
    if _text(row.get("memory_type")) != "message":
        return False
    source = _text(row.get("source"))
    return (
        category == "generic_shared_candidate" and source == "core"
    ) or (
        category == "group_chat_candidate" and source == "chat"
    )


def _legacy_tag_links(
    conn: sqlite3.Connection,
    memory_ids: list[int],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, dict[str, Any]]]:
    if not memory_ids or not {"memory_tags", "tags"} <= _tables(conn):
        return {}, {}
    links: dict[int, list[dict[str, Any]]] = {}
    tag_ids: set[int] = set()
    memory_tag_columns = _columns(conn, "memory_tags")
    position_expr = "position" if "position" in memory_tag_columns else "0"
    relevance_expr = "relevance" if "relevance" in memory_tag_columns else "1.0"
    for chunk in _chunked(memory_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT rowid,memory_id,tag_id,{position_expr},{relevance_expr} FROM memory_tags WHERE memory_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for rowid, memory_id, tag_id, position, relevance in rows:
            tag_key = int(tag_id)
            links.setdefault(int(memory_id), []).append(
                {
                    "rowid": int(rowid),
                    "tag_id": tag_key,
                    "position": int(position or 0),
                    "relevance": float(relevance if relevance is not None else 1.0),
                }
            )
            tag_ids.add(tag_key)
    if not tag_ids:
        return links, {}
    tag_columns = _columns(conn, "tags")
    selected = [
        name for name in (
            "id", "name", "tag_type", "description", "confidence", "vector", "created_at",
            "aliases", "metadata", "updated_at",
        ) if name in tag_columns
    ]
    tags: dict[int, dict[str, Any]] = {}
    for chunk in _chunked(sorted(tag_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for raw in conn.execute(
            f"SELECT {', '.join(selected)} FROM tags WHERE id IN ({placeholders})",
            chunk,
        ).fetchall():
            tag = {name: raw[index] for index, name in enumerate(selected)}
            tags[int(tag["id"])] = tag
    return links, tags


def _migrate_classified_tags(
    conn: sqlite3.Connection,
    *,
    targets_by_memory: Mapping[int, tuple[dict[str, str], ...]],
    target_memory_ids: Mapping[tuple[int, str], int],
    run_id: str,
) -> dict[str, int]:
    links, tags = _legacy_tag_links(conn, sorted(targets_by_memory))
    if not links:
        return {"legacy_links": 0, "scoped_links": 0, "relations": 0, "missing_tags": 0}
    scoped_tag_map: dict[tuple[str, int], int] = {}
    scoped_links = missing_tags = 0
    now = time.time()
    effective_available = "scoped_memory_effective_tags" in _tables(conn)
    for legacy_memory_id, memory_links in links.items():
        for scope in targets_by_memory.get(legacy_memory_id, ()):
            scope_key = _scope_key(scope)
            target_memory_id = target_memory_ids[(legacy_memory_id, scope_key)]
            for link in memory_links:
                tag = tags.get(int(link["tag_id"]))
                if tag is None or not _text(tag.get("name")):
                    missing_tags += 1
                    continue
                map_key = (scope_key, int(tag["id"]))
                target_tag_id = scoped_tag_map.get(map_key)
                if target_tag_id is None:
                    target_tag_id = _upsert_scoped_tag(conn, scope, tag)
                    scoped_tag_map[map_key] = target_tag_id
                conn.execute(
                    """INSERT OR IGNORE INTO scoped_memory_tags(
                           bot_id,session_id,visibility,memory_id,tag_id,position,relevance,created_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    (*_scope_values(scope), target_memory_id, target_tag_id, link["position"], link["relevance"], now),
                )
                if effective_available:
                    conn.execute(
                        """INSERT OR REPLACE INTO scoped_memory_effective_tags(
                               bot_id,session_id,visibility,memory_id,tag_id,position,relevance,
                               source,correction_id,projection_revision,updated_at
                           ) VALUES (?,?,?,?,?,?,?,'automatic',NULL,1,?)""",
                        (*_scope_values(scope), target_memory_id, target_tag_id, link["position"], link["relevance"], now),
                    )
                _record_item(
                    conn,
                    source_table="memory_tags",
                    legacy_id=link["rowid"],
                    scope_key=scope_key,
                    disposition="migrated",
                    target_id=target_tag_id,
                    source_hash=_sha256({"memory_id": legacy_memory_id, "tag_id": tag["id"]}),
                    target_hash=_sha256({"memory_id": target_memory_id, "tag_id": target_tag_id}),
                    run_id=run_id,
                )
                scoped_links += 1
    relations = 0
    if "tag_relations" in _tables(conn) and "scoped_tag_relations" in _tables(conn):
        relation_columns = _columns(conn, "tag_relations")
        selected = [name for name in ("id", "source_tag_id", "target_tag_id", "relation_type", "weight", "confidence", "metadata", "created_at") if name in relation_columns]
        for raw in conn.execute(f"SELECT {', '.join(selected)} FROM tag_relations").fetchall():
            relation = {name: raw[index] for index, name in enumerate(selected)}
            for scope_key in {_scope_key(scope) for scopes in targets_by_memory.values() for scope in scopes}:
                source_tag_id = scoped_tag_map.get((scope_key, int(relation["source_tag_id"])))
                target_tag_id = scoped_tag_map.get((scope_key, int(relation["target_tag_id"])))
                if source_tag_id is None or target_tag_id is None:
                    continue
                bot_id, session_id, visibility = scope_key.split("|", 2)
                conn.execute(
                    """INSERT INTO scoped_tag_relations(
                           bot_id,session_id,visibility,source_tag_id,target_tag_id,relation_type,
                           weight,confidence,metadata,created_at,updated_at,status,valid_until,revision
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',NULL,1)
                       ON CONFLICT(bot_id,session_id,visibility,source_tag_id,target_tag_id,relation_type)
                       DO UPDATE SET weight=MAX(scoped_tag_relations.weight,excluded.weight),
                                     confidence=MAX(scoped_tag_relations.confidence,excluded.confidence),
                                     updated_at=excluded.updated_at""",
                    (
                        bot_id, session_id, visibility, source_tag_id, target_tag_id,
                        _text(relation.get("relation_type")) or "related",
                        float(relation.get("weight") or 1.0),
                        float(relation.get("confidence") or 0.0),
                        relation.get("metadata") or "{}",
                        relation.get("created_at") or now,
                        now,
                    ),
                )
                relations += 1
    if "scoped_tag_projection_state" in _tables(conn):
        for scope_key in {_scope_key(scope) for scopes in targets_by_memory.values() for scope in scopes}:
            bot_id, session_id, visibility = scope_key.split("|", 2)
            cursor = max(
                (target_id for (legacy_id, key), target_id in target_memory_ids.items() if key == scope_key),
                default=None,
            )
            conn.execute(
                """INSERT INTO scoped_tag_projection_state(
                       bot_id,session_id,visibility,state,projection_revision,cursor_memory_id,last_error,updated_at
                   ) VALUES (?,?,?,'ready',1,?,NULL,?)
                   ON CONFLICT(bot_id,session_id,visibility) DO UPDATE SET
                       state='ready', projection_revision=MAX(scoped_tag_projection_state.projection_revision,1),
                       cursor_memory_id=MAX(COALESCE(scoped_tag_projection_state.cursor_memory_id,0),COALESCE(excluded.cursor_memory_id,0)),
                       last_error=NULL, updated_at=excluded.updated_at""",
                (bot_id, session_id, visibility, cursor, now),
            )
    return {
        "legacy_links": sum(len(value) for value in links.values()),
        "scoped_links": scoped_links,
        "relations": relations,
        "missing_tags": missing_tags,
    }


def _migrate_classified_facts(
    conn: sqlite3.Connection,
    *,
    targets_by_memory: Mapping[int, tuple[dict[str, str], ...]],
    target_memory_ids: Mapping[tuple[int, str], int],
    run_id: str,
) -> dict[str, int]:
    if "facts" not in _tables(conn) or "scoped_facts" not in _tables(conn):
        return {"source_rows": 0, "scoped_rows": 0}
    fact_columns = _columns(conn, "facts")
    selected = [name for name in ("id", "subject", "predicate", "object", "source_memory_id", "confidence", "valid_from", "valid_until", "created_at", "last_reinforced", "fact_type") if name in fact_columns]
    memory_ids = sorted(targets_by_memory)
    source_rows = scoped_rows = 0
    now = time.time()
    for chunk in _chunked(memory_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM facts WHERE source_memory_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for raw in rows:
            fact = {name: raw[index] for index, name in enumerate(selected)}
            source_rows += 1
            legacy_memory_id = int(fact["source_memory_id"])
            if not all(_text(fact.get(name)) for name in ("subject", "predicate", "object")):
                continue
            for scope in targets_by_memory.get(legacy_memory_id, ()):
                scope_key = _scope_key(scope)
                target_memory_id = target_memory_ids[(legacy_memory_id, scope_key)]
                provenance = _canonical(
                    {
                        "kind": "legacy_fact_projection",
                        "migration_rule": CLASSIFIED_RECOVERY_RULE_VERSION,
                        "legacy_fact_id": int(fact["id"]),
                        "legacy_source_memory_id": legacy_memory_id,
                        "fact_type": _text(fact.get("fact_type")),
                    }
                )
                conn.execute(
                    """INSERT INTO scoped_facts(
                           bot_id,session_id,visibility,subject,predicate,object,confidence,status,
                           source_memory_id,provenance,valid_from,valid_until,created_at,updated_at,revision
                       ) VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,1)
                       ON CONFLICT(bot_id,session_id,visibility,subject,predicate,object) DO UPDATE SET
                           confidence=MAX(scoped_facts.confidence,excluded.confidence),
                           source_memory_id=COALESCE(scoped_facts.source_memory_id,excluded.source_memory_id),
                           updated_at=excluded.updated_at""",
                    (
                        *_scope_values(scope),
                        _text(fact["subject"]), _text(fact["predicate"]), _text(fact["object"]),
                        float(fact.get("confidence") or 0.0), target_memory_id, provenance,
                        fact.get("valid_from"), fact.get("valid_until"), fact.get("created_at") or now, now,
                    ),
                )
                target = conn.execute(
                    """SELECT id FROM scoped_facts WHERE bot_id=? AND session_id=? AND visibility=?
                         AND subject=? AND predicate=? AND object=?""",
                    (*_scope_values(scope), _text(fact["subject"]), _text(fact["predicate"]), _text(fact["object"])),
                ).fetchone()
                _record_item(
                    conn,
                    source_table="facts",
                    legacy_id=fact["id"],
                    scope_key=scope_key,
                    disposition="migrated",
                    target_id=int(target[0]) if target else None,
                    source_hash=_row_hash(fact),
                    target_hash=_sha256({"source_memory_id": target_memory_id}),
                    run_id=run_id,
                )
                scoped_rows += 1
    return {"source_rows": source_rows, "scoped_rows": scoped_rows}


def _validate_classified_recovery(
    conn: sqlite3.Connection,
    *,
    targets_by_memory: Mapping[int, tuple[dict[str, str], ...]],
    run_id: str,
) -> dict[str, Any]:
    expected = sum(len(scopes) for scopes in targets_by_memory.values())
    mapped = int(conn.execute(
        "SELECT COUNT(*) FROM scope_recovery_memory_map WHERE run_id=?",
        (run_id,),
    ).fetchone()[0])
    if mapped != expected:
        raise ScopeRecoveryMigrationError(f"classified_memory_mapping_incomplete:{mapped}/{expected}")
    invalid = int(conn.execute(
        """SELECT COUNT(*) FROM scope_recovery_memory_map rm
             JOIN memories m ON m.id=rm.target_memory_id
            WHERE rm.run_id=? AND (
                m.resolution_state!='resolved' OR COALESCE(m.quarantine,0)!=0 OR
                COALESCE(m.memory_type,'message') IN ('archived','evicted','deleted') OR
                m.bot_id IS NULL OR m.session_id IS NULL OR m.visibility!='group'
            )""",
        (run_id,),
    ).fetchone()[0])
    if invalid:
        raise ScopeRecoveryMigrationError(f"classified_recovery_invalid_targets:{invalid}")
    integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    if integrity != "ok":
        raise ScopeRecoveryMigrationError(f"classified_recovery_integrity_failed:{integrity}")
    return {"expected_mappings": expected, "actual_mappings": mapped, "invalid_targets": invalid, "quick_check": integrity}


def apply_classified_scope_recovery(
    source_db_path: str | os.PathLike[str],
    classification_report_path: str | os.PathLike[str],
    output_db_path: str | os.PathLike[str],
    run_dir: str | os.PathLike[str],
    *,
    confirmation: str = "",
) -> dict[str, Any]:
    """Recover classified non-evicted memories into a staged formal-Scope database.

    Generic ``core/message`` rows are projected to every already-existing formal group
    Scope.  ``chat/message`` rows are projected only when their group currently maps to
    exactly one Bot Scope.  Missing and multi-Bot mappings remain review-only.
    """
    if confirmation != "recover":
        raise ScopeRecoveryMigrationError("classified_recovery_confirmation_required")
    source = Path(source_db_path).resolve()
    report_path = Path(classification_report_path).resolve()
    output = Path(output_db_path).resolve()
    run_path = Path(run_dir).resolve()
    if source == output:
        raise ScopeRecoveryMigrationError("source_and_output_db_must_differ")
    if not source.is_file():
        raise ScopeRecoveryMigrationError("source_database_missing")
    report, classified_items, report_hash = _load_classified_items(report_path)
    if not classified_items:
        raise ScopeRecoveryMigrationError("classification_report_has_no_recoverable_items")
    run_path.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    run_id = "classified-scope-recovery:" + uuid.uuid4().hex
    source_backup = run_path / f"{run_id.replace(':', '-')}-source-before.sqlite3"
    staging = run_path / f".target-{uuid.uuid4().hex}.sqlite3"

    source_conn = sqlite3.connect(source)
    backup_conn = sqlite3.connect(source_backup)
    try:
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()
    source_hash = _source_hash(source_backup)
    shutil.copy2(source_backup, staging)

    conn = sqlite3.connect(staging)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        _ensure_migration_tables(conn)
        formal_scopes = _formal_group_scopes(conn)
        if not formal_scopes:
            raise ScopeRecoveryMigrationError("formal_target_scopes_missing")
        scopes_by_group: dict[str, list[dict[str, str]]] = {}
        for scope in formal_scopes:
            scopes_by_group.setdefault(scope["group_id"], []).append(scope)

        report_ids = sorted(classified_items)
        rows = _memory_rows_for_ids(conn, report_ids)
        targets_by_memory: dict[int, tuple[dict[str, str], ...]] = {}
        skipped = {
            "missing_source_row": 0,
            "live_row_no_longer_eligible": 0,
            "multi_bot_group_review": 0,
            "no_current_scope_review": 0,
        }
        by_category = {"generic_shared_candidate": 0, "group_chat_candidate": 0}
        for memory_id in report_ids:
            item = classified_items[memory_id]
            category = _text(item.get("category"))
            row = rows.get(memory_id)
            if row is None:
                skipped["missing_source_row"] += 1
                continue
            if not _row_is_recoverable(row, category):
                skipped["live_row_no_longer_eligible"] += 1
                continue
            if category == "generic_shared_candidate":
                targets = formal_scopes
            else:
                candidates = tuple(scopes_by_group.get(_text(row.get("group_id")), ()))
                if len(candidates) > 1:
                    skipped["multi_bot_group_review"] += 1
                    continue
                if not candidates:
                    skipped["no_current_scope_review"] += 1
                    continue
                targets = candidates
            targets_by_memory[memory_id] = tuple(dict(scope) for scope in targets)
            by_category[category] += 1

        if not targets_by_memory:
            raise ScopeRecoveryMigrationError("no_live_recoverable_memories")
        plan_hash = _sha256(
            {
                "rule_version": CLASSIFIED_RECOVERY_RULE_VERSION,
                "report_hash": report_hash,
                "formal_scopes": formal_scopes,
                "memory_targets": {str(key): [_scope_key(scope) for scope in value] for key, value in targets_by_memory.items()},
            }
        )
        conn.execute(
            """INSERT INTO scope_recovery_migrations(
                   run_id,rule_version,source_snapshot_hash,plan_hash,target_scopes_json,
                   status,indexes_status,created_at
               ) VALUES (?,?,?,?,?,'running','pending:memory_hnsw,tag_catalog_hnsw',?)""",
            (run_id, CLASSIFIED_RECOVERY_RULE_VERSION, source_hash, plan_hash, _canonical(list(formal_scopes)), time.time()),
        )
        target_memory_ids: dict[tuple[int, str], int] = {}
        memory_columns = _columns(conn, "memories")
        for legacy_memory_id, scopes in targets_by_memory.items():
            row = rows[legacy_memory_id]
            category = _text(classified_items[legacy_memory_id].get("category"))
            source_hash_for_row = _row_hash(row)
            for scope in scopes:
                scope_key = _scope_key(scope)
                target_memory_ids[(legacy_memory_id, scope_key)] = _insert_shared_memory(
                    conn,
                    row,
                    scope,
                    run_id=run_id,
                    origin_prefix="classified_legacy_recovery",
                    source_override=None,
                    provenance_extra={
                        "classification_category": category,
                        "classification_report_hash": report_hash,
                        "recovery_rule": CLASSIFIED_RECOVERY_RULE_VERSION,
                    },
                    memory_columns=memory_columns,
                    check_origin_fingerprint=False,
                    source_hash_override=source_hash_for_row,
                )
        tag_result = _migrate_classified_tags(
            conn,
            targets_by_memory=targets_by_memory,
            target_memory_ids=target_memory_ids,
            run_id=run_id,
        )
        fact_result = _migrate_classified_facts(
            conn,
            targets_by_memory=targets_by_memory,
            target_memory_ids=target_memory_ids,
            run_id=run_id,
        )
        verification = _validate_classified_recovery(conn, targets_by_memory=targets_by_memory, run_id=run_id)
        report_summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
        migration_report = {
            "run_id": run_id,
            "rule_version": CLASSIFIED_RECOVERY_RULE_VERSION,
            "source_snapshot_hash": source_hash,
            "source_backup_path": str(source_backup),
            "classification_report_path": str(report_path),
            "classification_report_hash": report_hash,
            "classification_source_hash": report.get("source_sha256"),
            "classification_snapshot_hash": report.get("snapshot_sha256"),
            "classification_summary": dict(report_summary),
            "plan_hash": plan_hash,
            "target_scopes": list(formal_scopes),
            "selected_source_memories": len(targets_by_memory),
            "selected_by_category": by_category,
            "projected_memory_rows": len(target_memory_ids),
            "skipped": skipped,
            "tags": tag_result,
            "facts": fact_result,
            "verification": verification,
            "legacy_rows_deleted": 0,
            "evicted_rows_reactivated": 0,
            "indexes_status": "pending",
            "index_rebuild_steps": ["memory_hnsw", "tag_catalog_hnsw"],
        }
        conn.execute(
            "UPDATE scope_recovery_migrations SET status='staged',completed_at=? WHERE run_id=?",
            (time.time(), run_id),
        )
        conn.commit()
        report_file = run_path / f"{run_id.replace(':', '-')}.json"
        report_file.write_text(_canonical(migration_report) + "\n", encoding="utf-8")
        migration_report["report_path"] = str(report_file)
        conn.close()
        os.replace(staging, output)
        return migration_report
    except Exception:
        try:
            conn.rollback()
        except sqlite3.ProgrammingError:
            pass
        try:
            conn.close()
        except sqlite3.ProgrammingError:
            pass
        try:
            staging.unlink()
        except OSError:
            pass
        raise


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Apply staged generic Scope recovery migration")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--output-db", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--target-scope", action="append", required=True, help="JSON canonical group Scope; pass exactly twice")
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--source-hash", required=True)
    parser.add_argument("--confirmation", choices=("migrate",), required=True)
    args = parser.parse_args()
    scopes = [json.loads(value) for value in args.target_scope]
    result = apply_staged_migration(args.source_db, args.output_db, args.run_dir, scopes, plan_hash=args.plan_hash, expected_source_hash=args.source_hash, confirmation=args.confirmation)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(main())


__all__ = [
    "CLASSIFIED_RECOVERY_RULE_VERSION",
    "INDEX_REBUILD_STEPS",
    "MIGRATION_RULE_VERSION",
    "PURGE_TABLES",
    "ScopeRecoveryMigrationError",
    "apply_classified_scope_recovery",
    "apply_staged_migration",
]
