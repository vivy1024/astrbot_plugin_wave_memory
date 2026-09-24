"""Offline, snapshot-bound recovery for legacy memories with explicit Scope mappings.

This is intentionally separate from the historical classified recovery path.  The old
path inferred targets from every formal Scope and therefore duplicated group-bound
``core`` rows across unrelated groups.  This module consumes an immutable snapshot,
constructs an auditable exact plan from explicit group -> Scope mappings, and only
writes a separate staged database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

try:
    from ..data_governance_jobs import open_readonly_snapshot
    from ..scope_recovery import normalize_scope_mappings
    from .scope_recovery_migration import (
        ScopeRecoveryMigrationError,
        _canonical,
        _columns,
        _ensure_migration_tables,
        _formal_group_scopes,
        _migrate_classified_facts,
        _migrate_classified_tags,
        _row_hash,
        _scope_key,
        _source_hash,
        _text,
        _insert_shared_memory,
    )
except ImportError:  # pragma: no cover - direct repository imports
    from services.data_governance_jobs import open_readonly_snapshot
    from services.scope_recovery import normalize_scope_mappings
    from services.migrations_archive.scope_recovery_migration import (
        ScopeRecoveryMigrationError,
        _canonical,
        _columns,
        _ensure_migration_tables,
        _formal_group_scopes,
        _migrate_classified_facts,
        _migrate_classified_tags,
        _row_hash,
        _scope_key,
        _source_hash,
        _text,
        _insert_shared_memory,
    )


# v4 deliberately fails closed on every v3 multi-Scope fanout plan.
# Shared memory is cross-group *read* authorization, not physical 1→N copies.
# Historical classified fanout (1 legacy → many group memories) is forbidden.
APPROVED_SCOPE_RECOVERY_RULE_VERSION = "approved-group-scope-recovery/4"
APPROVED_SCOPE_RECOVERY_POLICY = "owned-group-scope-recover-no-fanout/v4"
FORBIDDEN_FANOUT_RULE_VERSIONS = frozenset({
    "classified-scope-recovery/1",
    "approved-group-scope-recovery/1",
    "approved-group-scope-recovery/2",
    "approved-group-scope-recovery/3",
})
_PLAN_SCHEMA_VERSION = 1

# The projection carries these source attributes unchanged.  Hashing only these fields
# makes the plan stable across harmless runtime-only counters while binding every datum
# relevant to eligibility and recovered content.
_SOURCE_SIGNATURE_FIELDS = (
    "id",
    "group_id",
    "sender_id",
    "sender_name",
    "content",
    "vector",
    "timestamp",
    "importance",
    "memory_type",
    "source",
    "summary",
    "bot_id",
    "session_id",
    "visibility",
    "quarantine",
    "resolution_state",
)

# These are formal, derived dependents of a projected memory.  They belong to the
# projection's Scope and therefore must not survive after a verified cross-Scope target
# is removed.  Historical feedback is deliberately handled separately: it is retained
# as audit evidence but detached from the removed memory id.
_MEMORY_DEPENDENCIES = (
    ("scoped_memory_tags", "memory_id"),
    ("scoped_memory_effective_tags", "memory_id"),
    ("scoped_memory_tag_corrections", "memory_id"),
    ("scoped_facts", "source_memory_id"),
    ("scoped_fact_history", "source_memory_id"),
    ("scoped_jargon", "source_memory_id"),
    ("scoped_beliefs", "source_memory_id"),
    ("scoped_soul_concerns", "origin_memory_id"),
    ("scoped_soul_relationship_events", "source_memory_id"),
    ("memory_mentions", "memory_id"),
)

# Legacy-domain records are retained for later Phase 3 migration, but their evidence
# pointer cannot continue to name a removed formal projection.
_LEGACY_REFERENCE_DETACHMENTS = (
    ("jargon", "source_memory_id"),
    ("concerns", "origin_memory_id"),
    ("relationship_events", "source_memory_id"),
)


class ApprovedScopeRecoveryError(ScopeRecoveryMigrationError):
    """The approved recovery plan or its staged application is unsafe."""


def _require_single_file_snapshot(path: Path) -> None:
    """Reject a SQLite main file whose WAL state cannot be hash-bound safely."""

    if not path.is_file():
        raise ApprovedScopeRecoveryError("source_snapshot_missing")
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    present = [sidecar.name for sidecar in sidecars if sidecar.exists()]
    if present:
        raise ApprovedScopeRecoveryError("source_snapshot_sidecar_present:" + ",".join(sorted(present)))
    connection = open_readonly_snapshot(path)
    try:
        journal_mode = _text(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    finally:
        connection.close()
    if journal_mode != "delete":
        raise ApprovedScopeRecoveryError("source_snapshot_journal_mode_not_delete:" + journal_mode)


def _reserve_new_file(path: Path, *, error: str) -> None:
    """Atomically reserve an output path without an overwrite window."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb"):
            pass
    except FileExistsError as exc:
        raise ApprovedScopeRecoveryError(error) from exc


def _backup_to_reserved_database(source: Path, destination: Path) -> None:
    """Use SQLite backup instead of a filesystem copy for the staged image."""

    source_connection = open_readonly_snapshot(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection, pages=256)
        destination_connection.commit()
        journal_mode = _text(destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).casefold()
        if journal_mode != "delete":
            raise ApprovedScopeRecoveryError("approved_snapshot_journal_mode_not_delete:" + journal_mode)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def create_approved_scope_snapshot(
    source_db_path: str | os.PathLike[str],
    output_snapshot_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create one immutable single-file snapshot through SQLite's backup API.

    The source may be a live WAL database; only the output is accepted by plan/stage.
    It is atomically reserved before copying so an existing approved artifact can never
    be overwritten by a concurrent operator.
    """

    source = Path(source_db_path).resolve()
    output = Path(output_snapshot_path).resolve()
    if source == output:
        raise ApprovedScopeRecoveryError("source_and_snapshot_must_differ")
    if not source.is_file():
        raise ApprovedScopeRecoveryError("source_database_missing")
    _reserve_new_file(output, error="approved_snapshot_already_exists")
    try:
        _backup_to_reserved_database(source, output)
        _require_single_file_snapshot(output)
        connection = open_readonly_snapshot(output)
        try:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            connection.close()
        if quick_check != "ok":
            raise ApprovedScopeRecoveryError("approved_snapshot_integrity_failed:" + quick_check)
        return {
            "snapshot_path": str(output),
            "snapshot_hash": _source_hash(output),
            "quick_check": quick_check,
        }
    except Exception:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise


def _source_signature(row: Mapping[str, Any]) -> str:
    return _row_hash({field: row.get(field) for field in _SOURCE_SIGNATURE_FIELDS})


def _chunked(values: list[int], size: int = 800):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _candidate_snapshot_rows(connection: sqlite3.Connection):
    """Stream only fully unscoped, otherwise eligible source rows.

    Vectors can be several KiB each.  Filtering in SQLite and yielding one row at a
    time avoids loading every legacy BLOB merely to classify it as review-only.
    """

    columns = _columns(connection, "memories")
    required = {
        "id", "group_id", "content", "bot_id", "session_id", "visibility",
        "memory_type", "source", "quarantine", "resolution_state",
    }
    missing = required - columns
    if missing:
        raise ApprovedScopeRecoveryError("memories_missing_columns:" + ",".join(sorted(missing)))
    selected = [name for name in _SOURCE_SIGNATURE_FIELDS if name in columns]
    where = """
        COALESCE(TRIM(bot_id),'')='' AND COALESCE(TRIM(session_id),'')=''
        AND COALESCE(TRIM(visibility),'')='' AND COALESCE(TRIM(group_id),'')!=''
        AND memory_type='message' AND source IN ('core','chat')
        AND COALESCE(quarantine,0)=0 AND COALESCE(resolution_state,'') IN ('','resolved')
    """
    cursor = connection.execute(
        f"SELECT {', '.join(selected)} FROM memories WHERE {where} ORDER BY id"
    )
    for raw in cursor:
        yield {name: raw[index] for index, name in enumerate(selected)}


def _legacy_disposition_counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    """Return total incomplete Scope rows, partial Scope rows, and candidates."""

    incomplete = "(COALESCE(TRIM(bot_id),'')='' OR COALESCE(TRIM(session_id),'')='' OR COALESCE(TRIM(visibility),'')='')"
    fully_unscoped = "COALESCE(TRIM(bot_id),'')='' AND COALESCE(TRIM(session_id),'')='' AND COALESCE(TRIM(visibility),'')=''"
    total = int(connection.execute(f"SELECT COUNT(*) FROM memories WHERE {incomplete}").fetchone()[0])
    partial = int(connection.execute(f"SELECT COUNT(*) FROM memories WHERE {incomplete} AND NOT ({fully_unscoped})").fetchone()[0])
    candidates = int(connection.execute(
        f"""SELECT COUNT(*) FROM memories WHERE {fully_unscoped}
              AND COALESCE(TRIM(group_id),'')!='' AND memory_type='message'
              AND source IN ('core','chat') AND COALESCE(quarantine,0)=0
              AND COALESCE(resolution_state,'') IN ('','resolved')"""
    ).fetchone()[0])
    return total, partial, candidates


def _candidate_category(row: Mapping[str, Any]) -> str | None:
    """Return a conservative group-bound category, never inventing a Scope."""

    # Any persisted Scope evidence makes the record ambiguous for group-wide fanout.
    # Phase 2 never discards that evidence by duplicating it to every Bot.
    if any(_text(row.get(name)) for name in ("bot_id", "session_id", "visibility")):
        return None
    try:
        quarantined = int(row.get("quarantine") or 0) != 0
    except (TypeError, ValueError):
        quarantined = bool(row.get("quarantine"))
    if quarantined:
        return None
    if _text(row.get("resolution_state")) not in {"", "resolved"}:
        return None
    if _text(row.get("memory_type")) != "message":
        return None
    if not _text(row.get("group_id")):
        return None
    source = _text(row.get("source"))
    if source == "core":
        return "group_core_candidate"
    if source == "chat":
        return "group_chat_candidate"
    return None


def _flatten_scopes(mappings: Mapping[str, tuple[dict[str, str], ...]]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for scopes in mappings.values():
        for scope in scopes:
            unique[_scope_key(scope)] = dict(scope)
    return [unique[key] for key in sorted(unique)]


def _plan_without_hash(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "plan_hash"}


def _plan_hash(plan: Mapping[str, Any]) -> str:
    # Keep the SHA encoding consistent with scope_recovery_migration._sha256 without
    # importing another private helper into the plan format.
    import hashlib

    return "sha256:" + hashlib.sha256(_canonical(_plan_without_hash(plan)).encode("utf-8")).hexdigest()


def _read_json(path: Path, *, error: str) -> dict[str, Any]:
    if not path.is_file():
        raise ApprovedScopeRecoveryError(error + "_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovedScopeRecoveryError(error + "_invalid") from exc
    if not isinstance(value, Mapping):
        raise ApprovedScopeRecoveryError(error + "_invalid")
    return dict(value)


def _validate_explicit_scopes(
    connection: sqlite3.Connection,
    scope_mappings: Any,
) -> dict[str, tuple[dict[str, str], ...]]:
    try:
        mappings = normalize_scope_mappings(scope_mappings)
    except ValueError as exc:
        raise ApprovedScopeRecoveryError(str(exc)) from exc
    if not mappings:
        raise ApprovedScopeRecoveryError("approved_scope_mappings_required")

    formal = {_scope_key(scope): scope for scope in _formal_group_scopes(connection)}
    if not formal:
        raise ApprovedScopeRecoveryError("formal_target_scopes_missing")
    normalized: dict[str, tuple[dict[str, str], ...]] = {}
    for group_id, scopes in mappings.items():
        if not scopes:
            raise ApprovedScopeRecoveryError("approved_scope_mapping_empty:" + group_id)
        unique: dict[str, dict[str, str]] = {}
        for scope in scopes:
            scope_key = _scope_key(scope)
            if scope_key in unique:
                raise ApprovedScopeRecoveryError("approved_scope_mapping_duplicate:" + scope_key)
            persisted = formal.get(scope_key)
            if persisted is None:
                raise ApprovedScopeRecoveryError("approved_scope_not_formal:" + scope_key)
            if persisted["group_id"] != group_id:
                raise ApprovedScopeRecoveryError("approved_scope_group_mismatch:" + scope_key)
            unique[scope_key] = dict(scope)
        normalized[group_id] = tuple(unique[key] for key in sorted(unique))
    return normalized


def build_approved_scope_recovery_plan(
    source_snapshot_path: str | os.PathLike[str],
    scope_mappings: Any,
) -> dict[str, Any]:
    """Build an immutable, exact recovery plan from a SQLite snapshot.

    Only explicitly mapped, group-bound ``core`` and ``chat`` message rows are
    included.  The resulting document pins both source bytes and each source row's
    recoverable content, so it cannot be applied to a later live database by mistake.
    """

    snapshot = Path(source_snapshot_path).resolve()
    _require_single_file_snapshot(snapshot)
    source_hash = _source_hash(snapshot)
    selected: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    selected_by_category: Counter[str] = Counter()
    selected_by_group_scope: Counter[str] = Counter()
    connection = open_readonly_snapshot(snapshot)
    try:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if integrity != "ok":
            raise ApprovedScopeRecoveryError("source_snapshot_integrity_failed:" + integrity)
        mappings = _validate_explicit_scopes(connection, scope_mappings)
        legacy_total, partial_scope_rows, eligible_candidates = _legacy_disposition_counts(connection)
        if partial_scope_rows:
            skipped["partial_scope_requires_review"] = partial_scope_rows
        skipped["ineligible_or_unsupported"] = max(0, legacy_total - partial_scope_rows - eligible_candidates)
        for row in _candidate_snapshot_rows(connection):
            category = _candidate_category(row)
            if category is None:  # Defensive: SQL and Python eligibility must agree.
                raise ApprovedScopeRecoveryError("candidate_query_eligibility_mismatch:" + str(row["id"]))
            group_id = _text(row.get("group_id"))
            scopes = mappings.get(group_id, ())
            if not scopes:
                skipped["approved_mapping_required"] += 1
                continue
            # One owned formal Scope only. Multi-Scope projection is the failed
            # Phase-2 fanout model and must never be planned again.
            if len(scopes) != 1:
                skipped["multi_scope_fanout_forbidden"] += 1
                continue
            target_keys = sorted(_scope_key(scope) for scope in scopes)
            if len(target_keys) != 1:
                skipped["multi_scope_fanout_forbidden"] += 1
                continue
            selected.append(
                {
                    "legacy_memory_id": int(row["id"]),
                    "source_group_id": group_id,
                    "category": category,
                    "source_signature": _source_signature(row),
                    "target_scope_keys": target_keys,
                }
            )
            selected_by_category[category] += 1
            for key in target_keys:
                selected_by_group_scope[f"{group_id} -> {key}"] += 1
    finally:
        connection.close()

    if not selected:
        raise ApprovedScopeRecoveryError("no_approved_recoverable_memories")

    target_scopes = _flatten_scopes(mappings)
    plan: dict[str, Any] = {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "rule_version": APPROVED_SCOPE_RECOVERY_RULE_VERSION,
        "policy": APPROVED_SCOPE_RECOVERY_POLICY,
        "source_snapshot_hash": source_hash,
        "scope_mappings": [
            scope
            for group_id in sorted(mappings)
            for scope in sorted(mappings[group_id], key=_scope_key)
        ],
        "target_scopes": target_scopes,
        "selected_memories": selected,
        "summary": {
            "legacy_rows_scanned": legacy_total,
            "selected_source_memories": len(selected),
            "projected_memory_rows": sum(len(item["target_scope_keys"]) for item in selected),
            "selected_by_category": dict(sorted(selected_by_category.items())),
            "selected_by_group_scope": dict(sorted(selected_by_group_scope.items())),
            "skipped": dict(sorted(skipped.items())),
        },
    }
    plan["plan_hash"] = _plan_hash(plan)
    return plan


def write_approved_scope_recovery_plan(plan: Mapping[str, Any], output_path: str | os.PathLike[str]) -> Path:
    """Persist a complete plan atomically; never overwrite an existing approval."""

    path = Path(output_path).resolve()
    if _plan_hash(plan) != _text(plan.get("plan_hash")):
        raise ApprovedScopeRecoveryError("approved_plan_hash_invalid")
    _reserve_new_file(path, error="approved_plan_already_exists")
    try:
        path.write_text(_canonical(plan) + "\n", encoding="utf-8")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _load_approved_plan(plan_path: Path) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, dict[str, str]]]:
    plan = _read_json(plan_path, error="approved_plan")
    if plan.get("schema_version") != _PLAN_SCHEMA_VERSION:
        raise ApprovedScopeRecoveryError("approved_plan_schema_unsupported")
    if plan.get("rule_version") != APPROVED_SCOPE_RECOVERY_RULE_VERSION:
        raise ApprovedScopeRecoveryError("approved_plan_rule_unsupported")
    if plan.get("policy") != APPROVED_SCOPE_RECOVERY_POLICY:
        raise ApprovedScopeRecoveryError("approved_plan_policy_unsupported")
    if _plan_hash(plan) != _text(plan.get("plan_hash")):
        raise ApprovedScopeRecoveryError("approved_plan_hash_invalid")
    source_hash = _text(plan.get("source_snapshot_hash"))
    if not source_hash.startswith("sha256:"):
        raise ApprovedScopeRecoveryError("approved_plan_snapshot_hash_required")

    scope_by_key: dict[str, dict[str, str]] = {}
    for raw in plan.get("target_scopes") or []:
        if not isinstance(raw, Mapping):
            raise ApprovedScopeRecoveryError("approved_plan_scope_invalid")
        scope = {name: _text(raw.get(name)) for name in ("group_id", "bot_id", "session_id", "visibility")}
        key = _scope_key(scope)
        session_parts = scope["session_id"].split(":", 2)
        if not all(scope.values()) or scope["visibility"] != "group":
            raise ApprovedScopeRecoveryError("approved_plan_scope_invalid")
        if len(session_parts) != 3 or not session_parts[0] or session_parts[1] != "group" or session_parts[2] != scope["group_id"]:
            raise ApprovedScopeRecoveryError("approved_plan_scope_invalid")
        if key in scope_by_key:
            raise ApprovedScopeRecoveryError("approved_plan_scope_duplicate")
        scope_by_key[key] = scope

    selected: dict[int, dict[str, Any]] = {}
    for raw in plan.get("selected_memories") or []:
        if not isinstance(raw, Mapping):
            raise ApprovedScopeRecoveryError("approved_plan_item_invalid")
        try:
            legacy_id = int(raw.get("legacy_memory_id"))
        except (TypeError, ValueError) as exc:
            raise ApprovedScopeRecoveryError("approved_plan_item_invalid") from exc
        source_group_id = _text(raw.get("source_group_id"))
        category = _text(raw.get("category"))
        source_signature = _text(raw.get("source_signature"))
        target_keys = raw.get("target_scope_keys")
        if (
            legacy_id in selected
            or not source_group_id
            or category not in {"group_core_candidate", "group_chat_candidate"}
            or not source_signature.startswith("sha256:")
            or not isinstance(target_keys, list)
            or not target_keys
        ):
            raise ApprovedScopeRecoveryError("approved_plan_item_invalid")
        normalized_keys = sorted({_text(value) for value in target_keys})
        if len(normalized_keys) != len(target_keys):
            raise ApprovedScopeRecoveryError("approved_plan_target_duplicate")
        if len(normalized_keys) != 1:
            # Shared memory is read authorization, never 1→N physical fanout.
            raise ApprovedScopeRecoveryError("multi_scope_fanout_forbidden")
        for key in normalized_keys:
            scope = scope_by_key.get(key)
            if scope is None or scope["group_id"] != source_group_id:
                raise ApprovedScopeRecoveryError("approved_plan_target_invalid")
        selected[legacy_id] = {
            "legacy_memory_id": legacy_id,
            "source_group_id": source_group_id,
            "category": category,
            "source_signature": source_signature,
            "target_scope_keys": normalized_keys,
        }
    if not selected:
        raise ApprovedScopeRecoveryError("approved_plan_has_no_items")
    return plan, selected, scope_by_key


def _source_rows_for_ids(connection: sqlite3.Connection, memory_ids: list[int]) -> dict[int, dict[str, Any]]:
    columns = _columns(connection, "memories")
    selected = [field for field in _SOURCE_SIGNATURE_FIELDS if field in columns]
    # Provenance is not part of a source signature, but it is mandatory when
    # deciding whether a historical target can be safely retained or removed.
    if "provenance" in columns:
        selected.append("provenance")
    result: dict[int, dict[str, Any]] = {}
    for chunk in _chunked(memory_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM memories WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        for raw in rows:
            row = {name: raw[index] for index, name in enumerate(selected)}
            result[int(row["id"])] = row
    return result


def _ensure_approved_recovery_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scope_recovery_approved_memory_map (
            source_snapshot_hash TEXT NOT NULL,
            legacy_memory_id INTEGER NOT NULL,
            target_scope_key TEXT NOT NULL,
            target_memory_id INTEGER NOT NULL,
            source_signature TEXT NOT NULL,
            run_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(source_snapshot_hash, legacy_memory_id, target_scope_key)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recovery_approved_memory_map_run
            ON scope_recovery_approved_memory_map(run_id);
        CREATE INDEX IF NOT EXISTS idx_scope_recovery_memory_map_target_memory_id
            ON scope_recovery_memory_map(target_memory_id);
        CREATE TABLE IF NOT EXISTS scope_recovery_projection_corrections (
            run_id TEXT NOT NULL,
            legacy_memory_id INTEGER NOT NULL,
            target_scope_key TEXT NOT NULL,
            target_memory_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(run_id, legacy_memory_id, target_scope_key, target_memory_id, action)
        );
        """
    )


def _existing_maps(
    connection: sqlite3.Connection,
    memory_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunked(memory_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""SELECT legacy_memory_id,target_scope_key,target_memory_id
                  FROM scope_recovery_memory_map
                 WHERE legacy_memory_id IN ({placeholders})""",
            chunk,
        ).fetchall()
        for legacy_id, scope_key, target_id in rows:
            result[int(legacy_id)].append(
                {
                    "legacy_memory_id": int(legacy_id),
                    "target_scope_key": _text(scope_key),
                    "target_memory_id": int(target_id),
                }
            )
    return result


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (bytes, bytearray, memoryview)) or isinstance(right, (bytes, bytearray, memoryview)):
        return bytes(left or b"") == bytes(right or b"")
    if left is None or right is None:
        return left is None and right is None
    return left == right


def _verified_legacy_projection(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    scope: Mapping[str, str],
) -> tuple[bool, str]:
    if (
        _text(target.get("group_id")) != scope["group_id"]
        or _text(target.get("bot_id")) != scope["bot_id"]
        or _text(target.get("session_id")) != scope["session_id"]
        or _text(target.get("visibility")) != "group"
    ):
        return False, "target_scope_mismatch"
    try:
        provenance = json.loads(_text(target.get("provenance")) or "{}")
    except json.JSONDecodeError:
        return False, "target_provenance_invalid"
    if not isinstance(provenance, Mapping):
        return False, "target_provenance_invalid"
    try:
        provenance_id = int(provenance.get("legacy_id"))
    except (TypeError, ValueError):
        return False, "target_provenance_missing"
    if (
        provenance.get("legacy_source_table") != "memories"
        or provenance_id != int(source["id"])
        or _text(provenance.get("source_group_id")) != _text(source.get("group_id"))
    ):
        return False, "target_provenance_mismatch"
    for field in ("sender_id", "sender_name", "content", "vector", "timestamp", "memory_type", "source", "summary"):
        if field in source and field in target and not _same_value(source.get(field), target.get(field)):
            return False, "target_source_content_stale"
    return True, "verified"


def _available_memory_dependencies(connection: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    return tuple(
        (table, column)
        for table, column in _MEMORY_DEPENDENCIES
        if column in _columns(connection, table)
    )


def _detach_projection_maps(connection: sqlite3.Connection, target_ids: list[int]) -> int:
    """Remove historical map entries before creating replacements.

    The target rows remain until replacement IDs have been allocated.  SQLite may reuse
    a just-deleted rowid, which would make old audit references point at a different
    recovered memory; detaching first avoids that ambiguity.
    """

    deleted = 0
    for target_chunk in _chunked(target_ids):
        placeholders = ",".join("?" for _ in target_chunk)
        cursor = connection.execute(
            f"DELETE FROM scope_recovery_memory_map WHERE target_memory_id IN ({placeholders})",
            target_chunk,
        )
        deleted += int(cursor.rowcount)
    return deleted


def _supersede_historical_memory_items(
    connection: sqlite3.Connection,
    removals: list[tuple[int, str, int, str]],
    run_id: str,
) -> int:
    """Keep old audit rows truthful after their projected target is removed."""

    if "target_id" not in _columns(connection, "scope_recovery_items"):
        return 0
    updated = 0
    for legacy_id, scope_key, target_id, _ in removals:
        cursor = connection.execute(
            """UPDATE scope_recovery_items
                  SET disposition='superseded', target_id=NULL, target_hash=NULL, run_id=?
                WHERE source_table='memories' AND legacy_id=? AND target_scope_key=?
                  AND target_id=?""",
            (run_id, str(legacy_id), scope_key, target_id),
        )
        updated += int(cursor.rowcount)
    return updated


def _detach_memory_feedback(connection: sqlite3.Connection, target_ids: list[int]) -> int:
    """Preserve feedback evidence while removing its now-invalid target reference."""

    if "memory_id" not in _columns(connection, "memory_feedback"):
        return 0
    detached = 0
    for target_chunk in _chunked(target_ids):
        placeholders = ",".join("?" for _ in target_chunk)
        cursor = connection.execute(
            f"UPDATE memory_feedback SET memory_id=NULL WHERE memory_id IN ({placeholders})",
            target_chunk,
        )
        detached += int(cursor.rowcount)
    return detached


def _detach_legacy_source_references(connection: sqlite3.Connection, target_ids: list[int]) -> dict[str, int]:
    """Retain Phase 3 source records but detach evidence to deleted projections."""

    detached: Counter[str] = Counter()
    available = tuple(
        (table, column)
        for table, column in _LEGACY_REFERENCE_DETACHMENTS
        if column in _columns(connection, table)
    )
    for target_chunk in _chunked(target_ids):
        placeholders = ",".join("?" for _ in target_chunk)
        for table, column in available:
            cursor = connection.execute(
                f"UPDATE {table} SET {column}=NULL WHERE {column} IN ({placeholders})",
                target_chunk,
            )
            detached[table] += int(cursor.rowcount)
    return dict(sorted(detached.items()))


def _detach_experience_episode_references(connection: sqlite3.Connection, target_ids: list[int]) -> int:
    """Remove deleted target ids from legacy episode JSON evidence without deleting episodes."""

    columns = _columns(connection, "experience_episodes")
    if not {"id", "source_memory_ids"} <= columns:
        return 0
    removed_ids = {int(value) for value in target_ids}
    changed = 0
    for episode_id, raw_ids in connection.execute(
        "SELECT id,source_memory_ids FROM experience_episodes WHERE source_memory_ids IS NOT NULL"
    ):
        raw_text = _text(raw_ids)
        if not raw_text:
            continue
        try:
            values = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ApprovedScopeRecoveryError("experience_episode_memory_ids_invalid:" + str(episode_id)) from exc
        if not isinstance(values, list):
            raise ApprovedScopeRecoveryError("experience_episode_memory_ids_invalid:" + str(episode_id))
        retained: list[Any] = []
        removed = False
        for value in values:
            try:
                is_removed = int(value) in removed_ids
            except (TypeError, ValueError):
                is_removed = False
            if is_removed:
                removed = True
            else:
                retained.append(value)
        if removed:
            connection.execute(
                "UPDATE experience_episodes SET source_memory_ids=? WHERE id=?",
                (json.dumps(retained, ensure_ascii=False, separators=(",", ":")), episode_id),
            )
            changed += 1
    return changed


def _delete_projection_dependencies(
    connection: sqlite3.Connection,
    target_ids: list[int],
    dependencies: tuple[tuple[str, str], ...],
) -> dict[str, int]:
    deleted: Counter[str] = Counter()
    for target_chunk in _chunked(target_ids):
        placeholders = ",".join("?" for _ in target_chunk)
        for table, column in dependencies:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                target_chunk,
            )
            deleted[table] += int(cursor.rowcount)
        cursor = connection.execute(
            f"DELETE FROM scope_recovery_memory_map WHERE target_memory_id IN ({placeholders})",
            target_chunk,
        )
        deleted["scope_recovery_memory_map"] += int(cursor.rowcount)
        cursor = connection.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})",
            target_chunk,
        )
        deleted["memories"] += int(cursor.rowcount)
    return dict(sorted(deleted.items()))


def _experience_episode_reference_count(connection: sqlite3.Connection, target_ids: set[int]) -> int:
    columns = _columns(connection, "experience_episodes")
    if not {"id", "source_memory_ids"} <= columns:
        return 0
    references = 0
    for episode_id, raw_ids in connection.execute(
        "SELECT id,source_memory_ids FROM experience_episodes WHERE source_memory_ids IS NOT NULL"
    ):
        raw_text = _text(raw_ids)
        if not raw_text:
            continue
        try:
            values = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ApprovedScopeRecoveryError("experience_episode_memory_ids_invalid:" + str(episode_id)) from exc
        if not isinstance(values, list):
            raise ApprovedScopeRecoveryError("experience_episode_memory_ids_invalid:" + str(episode_id))
        for value in values:
            try:
                if int(value) in target_ids:
                    references += 1
                    break
            except (TypeError, ValueError):
                continue
    return references


def _removed_projection_reference_counts(
    connection: sqlite3.Connection,
    run_id: str,
) -> dict[str, int]:
    """Prove removed targets no longer back formal runtime rows after staging."""

    target_ids = [
        int(row[0])
        for row in connection.execute(
            """SELECT DISTINCT target_memory_id FROM scope_recovery_projection_corrections
                 WHERE run_id=? AND action='removed'""",
            (run_id,),
        ).fetchall()
    ]
    if not target_ids:
        return {}
    counts: Counter[str] = Counter()
    target_id_set = set(target_ids)
    episode_references = _experience_episode_reference_count(connection, target_id_set)
    if episode_references:
        counts["experience_episodes"] = episode_references
    dependencies = _available_memory_dependencies(connection)
    for target_chunk in _chunked(target_ids):
        placeholders = ",".join("?" for _ in target_chunk)
        for table, column in dependencies:
            counts[table] += int(connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})",
                target_chunk,
            ).fetchone()[0])
        if "memory_id" in _columns(connection, "memory_feedback"):
            counts["memory_feedback"] += int(connection.execute(
                f"SELECT COUNT(*) FROM memory_feedback WHERE memory_id IN ({placeholders})",
                target_chunk,
            ).fetchone()[0])
        for table, column in _LEGACY_REFERENCE_DETACHMENTS:
            if column in _columns(connection, table):
                counts[f"legacy:{table}"] += int(connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})",
                    target_chunk,
                ).fetchone()[0])
        counts["scope_recovery_memory_map"] += int(connection.execute(
            f"SELECT COUNT(*) FROM scope_recovery_memory_map WHERE target_memory_id IN ({placeholders})",
            target_chunk,
        ).fetchone()[0])
        counts["memories"] += int(connection.execute(
            f"SELECT COUNT(*) FROM memories WHERE id IN ({placeholders})",
            target_chunk,
        ).fetchone()[0])
    return {key: value for key, value in sorted(counts.items()) if value}


def _record_correction(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    legacy_memory_id: int,
    target_scope_key: str,
    target_memory_id: int,
    action: str,
    reason: str,
) -> None:
    connection.execute(
        """INSERT INTO scope_recovery_projection_corrections(
               run_id,legacy_memory_id,target_scope_key,target_memory_id,action,reason,created_at
           ) VALUES (?,?,?,?,?,?,?)""",
        (run_id, legacy_memory_id, target_scope_key, target_memory_id, action, reason, time.time()),
    )


def _record_approved_map(
    connection: sqlite3.Connection,
    *,
    source_snapshot_hash: str,
    legacy_memory_id: int,
    target_scope_key: str,
    target_memory_id: int,
    source_signature: str,
    run_id: str,
) -> None:
    connection.execute(
        """INSERT INTO scope_recovery_approved_memory_map(
               source_snapshot_hash,legacy_memory_id,target_scope_key,target_memory_id,
               source_signature,run_id,created_at
           ) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(source_snapshot_hash,legacy_memory_id,target_scope_key) DO UPDATE SET
               target_memory_id=excluded.target_memory_id,
               source_signature=excluded.source_signature,
               run_id=excluded.run_id,
               created_at=excluded.created_at""",
        (
            source_snapshot_hash,
            legacy_memory_id,
            target_scope_key,
            target_memory_id,
            source_signature,
            run_id,
            time.time(),
        ),
    )


def _validate_plan_source_rows(
    rows: Mapping[int, Mapping[str, Any]],
    selected: Mapping[int, Mapping[str, Any]],
) -> None:
    for legacy_id, item in selected.items():
        row = rows.get(legacy_id)
        if row is None:
            raise ApprovedScopeRecoveryError("approved_source_row_missing:" + str(legacy_id))
        if _candidate_category(row) is None:
            raise ApprovedScopeRecoveryError("approved_source_row_ineligible:" + str(legacy_id))
        if _text(row.get("group_id")) != item["source_group_id"]:
            raise ApprovedScopeRecoveryError("approved_source_group_changed:" + str(legacy_id))
        if _source_signature(row) != item["source_signature"]:
            raise ApprovedScopeRecoveryError("approved_source_row_changed:" + str(legacy_id))


def _validate_staged_recovery(
    connection: sqlite3.Connection,
    *,
    source_hash: str,
    selected: Mapping[int, Mapping[str, Any]],
    scope_by_key: Mapping[str, Mapping[str, str]],
    run_id: str,
) -> dict[str, Any]:
    expected = sum(len(item["target_scope_keys"]) for item in selected.values())
    rows = connection.execute(
        """SELECT legacy_memory_id,target_scope_key,target_memory_id
              FROM scope_recovery_approved_memory_map
             WHERE source_snapshot_hash=? AND run_id=?""",
        (source_hash, run_id),
    ).fetchall()
    if len(rows) != expected:
        raise ApprovedScopeRecoveryError(f"approved_mapping_incomplete:{len(rows)}/{expected}")
    actual_by_legacy: dict[int, set[str]] = defaultdict(set)
    target_ids: list[int] = []
    for legacy_id, scope_key, target_id in rows:
        legacy_id = int(legacy_id)
        key = _text(scope_key)
        if legacy_id not in selected or key not in selected[legacy_id]["target_scope_keys"]:
            raise ApprovedScopeRecoveryError("approved_mapping_unplanned")
        actual_by_legacy[legacy_id].add(key)
        target_ids.append(int(target_id))
    for legacy_id, item in selected.items():
        if actual_by_legacy.get(legacy_id, set()) != set(item["target_scope_keys"]):
            raise ApprovedScopeRecoveryError("approved_mapping_target_incomplete:" + str(legacy_id))

    targets = _source_rows_for_ids(connection, target_ids)
    invalid = 0
    for legacy_id, scope_key, target_id in rows:
        scope = scope_by_key[_text(scope_key)]
        target = targets.get(int(target_id))
        if target is None or (
            _text(target.get("group_id")) != scope["group_id"]
            or _text(target.get("bot_id")) != scope["bot_id"]
            or _text(target.get("session_id")) != scope["session_id"]
            or _text(target.get("visibility")) != "group"
            or _text(target.get("resolution_state")) != "resolved"
            or int(target.get("quarantine") or 0) != 0
        ):
            invalid += 1
    if invalid:
        raise ApprovedScopeRecoveryError("approved_target_invalid:" + str(invalid))

    # Historical maps outside this exact plan can be valid cross-group sharing
    # projections.  They are audited as retained during staging, but are not an
    # error merely because this plan does not target their Scope.
    removed_references = _removed_projection_reference_counts(connection, run_id)
    if removed_references:
        raise ApprovedScopeRecoveryError("approved_removed_projection_references:" + _canonical(removed_references))

    quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if quick_check != "ok":
        raise ApprovedScopeRecoveryError("approved_staged_integrity_failed:" + quick_check)
    if foreign_key_rows:
        raise ApprovedScopeRecoveryError("approved_staged_foreign_keys_failed:" + str(len(foreign_key_rows)))
    return {
        "expected_mappings": expected,
        "actual_mappings": len(rows),
        "invalid_targets": invalid,
        "removed_projection_references": removed_references,
        "quick_check": quick_check,
        "foreign_key_violations": len(foreign_key_rows),
    }


def apply_approved_scope_recovery(
    source_snapshot_path: str | os.PathLike[str],
    plan_path: str | os.PathLike[str],
    output_db_path: str | os.PathLike[str],
    run_dir: str | os.PathLike[str],
    *,
    confirmation: str = "",
) -> dict[str, Any]:
    """Apply an approved recovery plan to a new staged database only.

    The source must be the exact snapshot used to build the plan.  This retain-only
    stage adds or updates plan targets and audits verified plan-external group-visible
    projections without deleting them or any of their dependents.
    """

    if confirmation != "retain-approved-group-scopes-non-destructive":
        raise ApprovedScopeRecoveryError("approved_retain_confirmation_required")
    source = Path(source_snapshot_path).resolve()
    plan_file = Path(plan_path).resolve()
    output = Path(output_db_path).resolve()
    artifacts = Path(run_dir).resolve()
    if source == output:
        raise ApprovedScopeRecoveryError("source_and_output_db_must_differ")
    _require_single_file_snapshot(source)

    plan, selected, scope_by_key = _load_approved_plan(plan_file)
    if str(plan.get("rule_version") or "") in FORBIDDEN_FANOUT_RULE_VERSIONS:
        raise ApprovedScopeRecoveryError("fanout_recovery_rule_forbidden:" + str(plan.get("rule_version")))
    if any(len(item.get("target_scope_keys") or ()) != 1 for item in selected.values()):
        raise ApprovedScopeRecoveryError("multi_scope_fanout_forbidden")
    source_hash = _source_hash(source)
    if source_hash != plan["source_snapshot_hash"]:
        raise ApprovedScopeRecoveryError("approved_snapshot_hash_mismatch")

    artifacts.mkdir(parents=True, exist_ok=True)
    run_id = "approved-group-scope-recovery:" + uuid.uuid4().hex
    connection: sqlite3.Connection | None = None
    output_reserved = False
    try:
        _reserve_new_file(output, error="approved_output_already_exists")
        output_reserved = True
        _backup_to_reserved_database(source, output)
        connection = sqlite3.connect(output)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        _ensure_migration_tables(connection)
        _ensure_approved_recovery_tables(connection)

        formal = {_scope_key(scope): scope for scope in _formal_group_scopes(connection)}
        for scope_key, scope in scope_by_key.items():
            if formal.get(scope_key) != scope:
                raise ApprovedScopeRecoveryError("approved_target_scope_changed:" + scope_key)

        source_rows = _source_rows_for_ids(connection, sorted(selected))
        _validate_plan_source_rows(source_rows, selected)
        historical = _existing_maps(connection, sorted(selected))
        target_ids = {
            entry["target_memory_id"]
            for entries in historical.values()
            for entry in entries
        }
        existing_targets = _source_rows_for_ids(connection, sorted(target_ids))

        preserved: dict[tuple[int, str], int] = {}
        retained_external_projections = 0
        for legacy_id, item in selected.items():
            expected_keys = set(item["target_scope_keys"])
            for entry in historical.get(legacy_id, ()):
                scope_key = entry["target_scope_key"]
                target_id = entry["target_memory_id"]
                target = existing_targets.get(target_id)
                if target is None:
                    raise ApprovedScopeRecoveryError("historical_projection_missing:" + str(target_id))
                scope = scope_by_key.get(scope_key)
                if scope is None:
                    # A plan is intentionally not a statement that every other Scope
                    # is wrong: cross_group_enabled histories may validly share this
                    # source with another group.  Retain only a fully verified,
                    # group-visible projection and make that decision auditable.
                    target_scope = {
                        "group_id": _text(target.get("group_id")),
                        "bot_id": _text(target.get("bot_id")),
                        "session_id": _text(target.get("session_id")),
                        "visibility": _text(target.get("visibility")),
                    }
                    verified, reason = _verified_legacy_projection(source_rows[legacy_id], target, target_scope)
                    if not verified:
                        raise ApprovedScopeRecoveryError(
                            "historical_projection_unverified:" + str(target_id) + ":" + reason
                        )
                    _record_correction(
                        connection,
                        run_id=run_id,
                        legacy_memory_id=legacy_id,
                        target_scope_key=scope_key,
                        target_memory_id=target_id,
                        action="retained",
                        reason="verified_plan_external_group_visible",
                    )
                    retained_external_projections += 1
                    continue
                verified, reason = _verified_legacy_projection(source_rows[legacy_id], target, scope)
                if not verified and reason != "target_source_content_stale":
                    raise ApprovedScopeRecoveryError("historical_projection_unverified:" + str(target_id) + ":" + reason)
                if scope_key in expected_keys and verified:
                    preserved[(legacy_id, scope_key)] = target_id
                elif scope_key not in expected_keys:
                    # The Scope is known to the overall plan but is not a target for
                    # this source row.  It is still an external sharing projection,
                    # not a deletion candidate.  Record the retain decision just as
                    # we do for a Scope absent from the plan altogether.
                    _record_correction(
                        connection,
                        run_id=run_id,
                        legacy_memory_id=legacy_id,
                        target_scope_key=scope_key,
                        target_memory_id=target_id,
                        action="retained",
                        reason=(
                            "verified_plan_external_group_visible"
                            if verified
                            else "stale_plan_external_projection_retained"
                        ),
                    )
                    retained_external_projections += 1

        # Retain-only is deliberately the normal path.  The legacy deletion helpers
        # remain available for a separately approved future workflow, but this stage
        # never detaches maps, deletes projections, or touches their dependencies.

        selected_scopes: dict[int, tuple[dict[str, str], ...]] = {
            legacy_id: tuple(scope_by_key[key] for key in item["target_scope_keys"])
            for legacy_id, item in selected.items()
        }
        target_memory_ids: dict[tuple[int, str], int] = {}
        memory_columns = _columns(connection, "memories")
        created = preserved_count = 0
        for legacy_id, scopes in selected_scopes.items():
            item = selected[legacy_id]
            row = source_rows[legacy_id]
            for scope in scopes:
                scope_key = _scope_key(scope)
                prior_target = preserved.get((legacy_id, scope_key))
                target_id = _insert_shared_memory(
                    connection,
                    row,
                    scope,
                    run_id=run_id,
                    origin_prefix="approved_group_scope_recovery",
                    source_override=None,
                    provenance_extra={
                        "recovery_rule": APPROVED_SCOPE_RECOVERY_RULE_VERSION,
                        "recovery_policy": APPROVED_SCOPE_RECOVERY_POLICY,
                        "approved_plan_hash": plan["plan_hash"],
                        "source_snapshot_hash": source_hash,
                        "classification_category": item["category"],
                    },
                    memory_columns=memory_columns,
                    check_origin_fingerprint=True,
                    source_hash_override=item["source_signature"],
                )
                target_memory_ids[(legacy_id, scope_key)] = target_id
                _record_approved_map(
                    connection,
                    source_snapshot_hash=source_hash,
                    legacy_memory_id=legacy_id,
                    target_scope_key=scope_key,
                    target_memory_id=target_id,
                    source_signature=item["source_signature"],
                    run_id=run_id,
                )
                if prior_target is None:
                    created += 1
                    _record_correction(
                        connection,
                        run_id=run_id,
                        legacy_memory_id=legacy_id,
                        target_scope_key=scope_key,
                        target_memory_id=target_id,
                        action="created",
                        reason="approved_scope_missing_or_stale",
                    )
                else:
                    preserved_count += 1
                    _record_correction(
                        connection,
                        run_id=run_id,
                        legacy_memory_id=legacy_id,
                        target_scope_key=scope_key,
                        target_memory_id=target_id,
                        action="preserved",
                        reason="approved_scope_verified",
                    )

        target_scopes = [scope_by_key[key] for key in sorted(scope_by_key)]
        connection.execute(
            """INSERT INTO scope_recovery_migrations(
                   run_id,rule_version,source_snapshot_hash,plan_hash,target_scopes_json,
                   status,indexes_status,created_at
               ) VALUES (?,?,?,?,?,'running','pending:memory_hnsw,tag_catalog_hnsw',?)""",
            (
                run_id,
                APPROVED_SCOPE_RECOVERY_RULE_VERSION,
                source_hash,
                plan["plan_hash"],
                _canonical(target_scopes),
                time.time(),
            ),
        )
        tags = _migrate_classified_tags(
            connection,
            targets_by_memory=selected_scopes,
            target_memory_ids=target_memory_ids,
            run_id=run_id,
        )
        facts = _migrate_classified_facts(
            connection,
            targets_by_memory=selected_scopes,
            target_memory_ids=target_memory_ids,
            run_id=run_id,
        )
        dependent_deletions: dict[str, int] = {}
        verification = _validate_staged_recovery(
            connection,
            source_hash=source_hash,
            selected=selected,
            scope_by_key=scope_by_key,
            run_id=run_id,
        )
        connection.execute(
            "UPDATE scope_recovery_migrations SET status='staged',completed_at=? WHERE run_id=?",
            (time.time(), run_id),
        )
        connection.commit()
        report = {
            "run_id": run_id,
            "rule_version": APPROVED_SCOPE_RECOVERY_RULE_VERSION,
            "policy": APPROVED_SCOPE_RECOVERY_POLICY,
            "source_snapshot_path": str(source),
            "source_snapshot_hash": source_hash,
            "approved_plan_path": str(plan_file),
            "approved_plan_hash": plan["plan_hash"],
            "selected_source_memories": len(selected),
            "projected_memory_rows": len(target_memory_ids),
            "created_memory_rows": created,
            "preserved_memory_rows": preserved_count,
            "retained_plan_external_projection_rows": retained_external_projections,
            "removed_cross_scope_memory_rows": 0,
            "removal_dependency_deletions": dependent_deletions,
            "tags": tags,
            "facts": facts,
            "verification": verification,
            "legacy_rows_deleted": 0,
            "indexes_status": "pending:memory_hnsw,tag_catalog_hnsw",
            "index_rebuild_required": True,
        }
        report_path = artifacts / f"{run_id.replace(':', '-')}.json"
        _reserve_new_file(report_path, error="approved_run_report_already_exists")
        try:
            report_path.write_text(_canonical(report) + "\n", encoding="utf-8")
        except Exception:
            report_path.unlink(missing_ok=True)
            raise
        report["report_path"] = str(report_path)
        connection.close()
        connection = None
        return report
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if output_reserved:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        raise


def verify_approved_scope_recovery(
    database_path: str | os.PathLike[str],
    plan_path: str | os.PathLike[str],
    run_id: str,
) -> dict[str, Any]:
    """Re-run the database-only safety gates for one exact staged run."""

    database = Path(database_path).resolve()
    if not database.is_file():
        raise ApprovedScopeRecoveryError("staged_database_missing")
    plan, selected, scope_by_key = _load_approved_plan(Path(plan_path).resolve())
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """SELECT rule_version,source_snapshot_hash,plan_hash,status,indexes_status
                  FROM scope_recovery_migrations WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ApprovedScopeRecoveryError("approved_staged_run_missing")
        if row["rule_version"] != APPROVED_SCOPE_RECOVERY_RULE_VERSION:
            raise ApprovedScopeRecoveryError("approved_staged_run_rule_mismatch")
        if row["source_snapshot_hash"] != plan["source_snapshot_hash"] or row["plan_hash"] != plan["plan_hash"]:
            raise ApprovedScopeRecoveryError("approved_staged_run_plan_mismatch")
        verification = _validate_staged_recovery(
            connection,
            source_hash=plan["source_snapshot_hash"],
            selected=selected,
            scope_by_key=scope_by_key,
            run_id=run_id,
        )
        corrections = {
            action: int(count)
            for action, count in connection.execute(
                "SELECT action,COUNT(*) FROM scope_recovery_projection_corrections WHERE run_id=? GROUP BY action",
                (run_id,),
            ).fetchall()
        }
        return {
            "run_id": run_id,
            "status": row["status"],
            "indexes_status": row["indexes_status"],
            "verification": verification,
            "corrections": corrections,
        }
    finally:
        connection.close()


__all__ = [
    "APPROVED_SCOPE_RECOVERY_POLICY",
    "APPROVED_SCOPE_RECOVERY_RULE_VERSION",
    "ApprovedScopeRecoveryError",
    "apply_approved_scope_recovery",
    "build_approved_scope_recovery_plan",
    "create_approved_scope_snapshot",
    "verify_approved_scope_recovery",
    "write_approved_scope_recovery_plan",
]
