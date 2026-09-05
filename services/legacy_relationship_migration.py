"""High-fidelity, staged-only migration for legacy relationship data.

This module intentionally does not use ``scope_recovery_migration``: relationship
snapshots are per ``(bot_id, group_id)`` and retain five independent dimensions.
It never writes to the source database and legacy events are copied to a dedicated
audit stream rather than replayed through the live relationship calculation path.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from ..domain.relationship_policy import (
        DIMENSION_RANGES,
        attitude_level,
        clamp_dimension,
        compute_affinity,
    )
    from ..engine.db.migrations.scoped_relationship_calibration import (
        ensure_scoped_relationship_calibration_schema_connection,
    )
except ImportError:  # pragma: no cover - direct service imports in focused tests
    from domain.relationship_policy import (
        DIMENSION_RANGES,
        attitude_level,
        clamp_dimension,
        compute_affinity,
    )
    from engine.db.migrations.scoped_relationship_calibration import (
        ensure_scoped_relationship_calibration_schema_connection,
    )


RULE_VERSION = "legacy-relationship-high-fidelity/3"
CONFIRMATION = "migrate"
DIMENSIONS = ("familiarity", "trust", "fun", "hostility", "depth")
_DIMENSION_WEIGHTS = {"familiarity": 0.25, "trust": 0.30, "fun": 0.20, "depth": 0.25}


class LegacyRelationshipMigrationError(RuntimeError):
    """Raised when the staged migration cannot preserve legacy relationship data."""


def compute_legacy_affection(dimensions: Mapping[str, Any]) -> int:
    """Return the legacy five-dimension affection score with legacy truncation.

    This public compatibility helper preserves the old score clamp behavior.  The
    staged migrator separately rejects out-of-range persisted snapshots rather than
    silently normalizing stored evidence.
    """
    values = _normalise_dimensions(dimensions, enforce_ranges=False)
    score = sum(values[name] * weight for name, weight in _DIMENSION_WEIGHTS.items())
    score -= values["hostility"] * 0.5
    return int(max(-100.0, min(100.0, score)))


def preview(connection: sqlite3.Connection, target_scopes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Inspect a database without writing it and return migration/review decisions."""
    scopes = _target_scopes(target_scopes)
    scope_index, ambiguous = _scope_index(scopes)
    tables = _tables(connection)
    profile_items: list[dict[str, Any]] = []
    event_items: list[dict[str, Any]] = []

    if "user_profiles" not in tables:
        profile_items.append({"disposition": "review", "reason": "legacy_profiles_table_missing"})
    else:
        columns = _columns(connection, "user_profiles")
        required = {"user_id", "group_id", "bot_id", "affection", "metadata"}
        if not required <= columns:
            profile_items.append({
                "disposition": "review", "reason": "legacy_profiles_schema_incomplete",
                "missing_columns": sorted(required - columns),
            })
        else:
            selected = [name for name in ("id", "user_id", "group_id", "bot_id", "affection", "metadata", "last_seen") if name in columns]
            for row in _rows(connection, "user_profiles", selected):
                item = _preview_profile(connection, row, scope_index, ambiguous, tables)
                profile_items.append(item)

    if "relationship_events" not in tables:
        event_items.append({"disposition": "review", "reason": "legacy_events_table_missing"})
    else:
        columns = _columns(connection, "relationship_events")
        required = {"id", "bot_id", "group_id", "user_id", "event_type", "dimension", "delta", "reason"}
        if not required <= columns:
            event_items.append({
                "disposition": "review", "reason": "legacy_events_schema_incomplete",
                "missing_columns": sorted(required - columns),
            })
        else:
            selected = [name for name in (
                "id", "bot_id", "group_id", "user_id", "event_type", "dimension", "delta", "reason",
                "timestamp", "created_at", "source_episode_id", "source_memory_id",
            ) if name in columns]
            for row in _rows(connection, "relationship_events", selected):
                event_items.append(_preview_event(row, scope_index, ambiguous))

    return {
        "rule_version": RULE_VERSION,
        "target_scopes": [dict(scope) for scope in scopes],
        "profiles": profile_items,
        "events": event_items,
        "summary": {
            "profiles_migratable": sum(item.get("disposition") == "migrate" for item in profile_items),
            "profiles_keep_formal": sum(item.get("disposition") == "keep_formal" for item in profile_items),
            "profiles_review": sum(item.get("disposition") == "review" for item in profile_items),
            "events_auditable": sum(item.get("disposition") == "audit" for item in event_items),
            "events_review": sum(item.get("disposition") == "review" for item in event_items),
        },
    }


def stage(
    source_db_path: str | os.PathLike[str],
    output_db_path: str | os.PathLike[str],
    run_dir: str | os.PathLike[str],
    target_scopes: Sequence[Mapping[str, Any]],
    expected_source_hash: str,
    confirmation: str,
    *,
    mode: str = "full",
) -> dict[str, Any]:
    """Create a staged SQLite copy and migrate only high-fidelity relationship data.

    ``source_db_path`` is copied before any SQLite operation on the staged database;
    the source is never opened writable.  ``confirmation`` must equal
    :data:`CONFIRMATION` (currently ``"migrate"``).

    ``mode``:
      - ``full``: profiles + event audit (default; may merge formal snapshots on staged copy)
      - ``event_audit_only``: only insert ``scoped_soul_relationship_legacy_events``;
        never rewrite formal relationships/values/affinity
    """
    source = Path(source_db_path).resolve()
    output = Path(output_db_path).resolve()
    run_path = Path(run_dir).resolve()
    mode_norm = str(mode or "full").strip().lower()
    if mode_norm not in {"full", "event_audit_only"}:
        raise LegacyRelationshipMigrationError("legacy_relationship_migration_mode_invalid")
    if source == output:
        raise LegacyRelationshipMigrationError("source_and_output_db_must_differ")
    if confirmation != CONFIRMATION:
        raise LegacyRelationshipMigrationError("legacy_relationship_migration_confirmation_required")
    if not source.is_file():
        raise LegacyRelationshipMigrationError("source_database_missing")
    if not expected_source_hash:
        raise LegacyRelationshipMigrationError("source_snapshot_hash_required")
    scopes = _target_scopes(target_scopes)
    source_hash = _file_hash(source)
    if expected_source_hash != source_hash:
        raise LegacyRelationshipMigrationError("source_snapshot_hash_mismatch")

    run_path.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    run_id = "legacy-relationship-migration:" + uuid.uuid4().hex
    source_backup = run_path / f"{run_id.replace(':', '-')}-source-before.sqlite3"
    staging = run_path / f".legacy-relationship-stage-{uuid.uuid4().hex}.sqlite3"
    shutil.copy2(source, source_backup)
    if _file_hash(source_backup) != source_hash:
        raise LegacyRelationshipMigrationError("source_changed_during_backup")
    _make_read_only(source_backup)

    conn: sqlite3.Connection | None = None
    try:
        # Copying the immutable backup preserves the exact SQLite snapshot and never touches source.
        shutil.copy2(source_backup, staging)
        # copy2 preserves the backup's read-only mode; the disposable stage must be writable.
        staging.chmod(stat.S_IREAD | stat.S_IWRITE)
        conn = sqlite3.connect(staging)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # The staged copy must be capable of representing all five legacy dimensions
        # before preview decisions are converted into writes.  This never touches source.
        ensure_scoped_relationship_calibration_schema_connection(conn)
        before_legacy_state = _legacy_table_state(conn)
        before_formal_fingerprint = _formal_fingerprint(conn, scopes)
        plan = preview(conn, scopes)
        _ensure_audit_tables(conn)
        now = time.time()
        conn.execute(
            """INSERT INTO legacy_relationship_migration_runs(
                   run_id, rule_version, source_hash, target_scopes_json, status, created_at
               ) VALUES (?, ?, ?, ?, 'running', ?)""",
            (run_id, RULE_VERSION, source_hash, _canonical(list(scopes)), now),
        )

        if mode_norm == "event_audit_only":
            profile_result = {
                "migrated": 0,
                "review": 0,
                "already_migrated": 0,
                "merged_existing_formal": 0,
                "skipped": True,
                "reason": "event_audit_only",
            }
        else:
            profile_result = _stage_profiles(conn, plan["profiles"], run_id, source_hash)
        event_result = _stage_events(conn, plan["events"], run_id, source_hash)
        after_legacy_state = _legacy_table_state(conn)
        if before_legacy_state != after_legacy_state:
            raise LegacyRelationshipMigrationError("legacy_tables_must_not_be_changed")
        after_formal_fingerprint = _formal_fingerprint(conn, scopes)
        if mode_norm == "event_audit_only" and before_formal_fingerprint != after_formal_fingerprint:
            raise LegacyRelationshipMigrationError("event_audit_only_must_not_change_formal_relationships")
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise LegacyRelationshipMigrationError(f"staged_sqlite_quick_check_failed:{quick_check}")

        report = {
            "run_id": run_id,
            "rule_version": RULE_VERSION,
            "mode": mode_norm,
            "source_hash": source_hash,
            "source_backup_path": str(source_backup),
            "target_scopes": [dict(scope) for scope in scopes],
            "profile_result": profile_result,
            "event_result": event_result,
            "preview_summary": plan["summary"],
            "legacy_table_state": before_legacy_state,
            "formal_fingerprint_before": before_formal_fingerprint,
            "formal_fingerprint_after": after_formal_fingerprint,
            "legacy_rows_deleted": 0,
            "quick_check": quick_check,
        }
        report_path = run_path / f"{run_id.replace(':', '-')}.json"
        report["report_path"] = str(report_path)
        report_json = _canonical(report)
        conn.execute(
            "UPDATE legacy_relationship_migration_runs SET status='staged', report_json=?, completed_at=? WHERE run_id=?",
            (report_json, time.time(), run_id),
        )
        conn.commit()
        report_path.write_text(report_json + "\n", encoding="utf-8")
        conn.close()
        conn = None
        os.replace(staging, output)
        return report
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()
        try:
            staging.unlink()
        except OSError:
            pass
        raise


def _stage_profiles(conn: sqlite3.Connection, items: Sequence[Mapping[str, Any]], run_id: str, source_hash: str) -> dict[str, int]:
    migrated = review = already_migrated = merged_existing_formal = 0
    for item in items:
        if item.get("disposition") not in {"migrate", "keep_formal"}:
            _record_item(conn, item, run_id, source_hash, "review")
            review += 1
            continue
        profile = item["profile"]
        scope = item["scope"]
        scope_key = _scope_key(scope)
        legacy_id = str(item["legacy_id"])
        item_hash = _object_hash(profile)
        existing_item = conn.execute(
            "SELECT disposition FROM legacy_relationship_migration_items WHERE source_table='user_profiles' AND legacy_id=? AND scope_key=? AND source_row_hash=?",
            (legacy_id, scope_key, item_hash),
        ).fetchone()
        if existing_item and str(existing_item[0]) in {"migrated", "keep_formal"}:
            already_migrated += 1
            continue
        original = _formal_snapshot(conn, scope, item["subject"])
        if item.get("disposition") == "keep_formal":
            _record_item(conn, item, run_id, source_hash, "keep_formal", original)
            already_migrated += 1
            continue
        _write_profile_baseline(conn, item, original)
        _record_item(conn, item, run_id, source_hash, "migrated", original)
        migrated += 1
        if isinstance(original, Mapping) and isinstance(original.get("relationship"), Mapping):
            merged_existing_formal += 1
    return {
        "migrated": migrated,
        "review": review,
        "already_migrated": already_migrated,
        "merged_existing_formal": merged_existing_formal,
    }


def _stage_events(conn: sqlite3.Connection, items: Sequence[Mapping[str, Any]], run_id: str, source_hash: str) -> dict[str, int]:
    audited = review = already_audited = 0
    for item in items:
        if item.get("disposition") != "audit":
            _record_item(conn, item, run_id, source_hash, "review")
            review += 1
            continue
        event = item["event"]
        scope = item["scope"]
        scope_key = _scope_key(scope)
        event_hash = _object_hash(event)
        cur = conn.execute(
            """INSERT OR IGNORE INTO scoped_soul_relationship_legacy_events(
                   legacy_event_id, scope_key, bot_id, session_id, visibility, group_id,
                   subject_principal_id, event_type, dimension, delta, reason, occurred_at,
                   source_episode_id, source_memory_id, source_hash, event_hash, run_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(item["legacy_id"]), scope_key, scope["bot_id"], scope["session_id"], scope["visibility"],
                scope["group_id"], item["subject"], _text(event.get("event_type")), _text(event.get("dimension")),
                float(event.get("delta") or 0.0), _text(event.get("reason")), _event_time(event),
                event.get("source_episode_id"), event.get("source_memory_id"), source_hash, event_hash, run_id, time.time(),
            ),
        )
        if cur.rowcount:
            _record_item(conn, item, run_id, source_hash, "audited")
            audited += 1
        else:
            already_audited += 1
    return {"audited": audited, "review": review, "already_audited": already_audited}


def _formal_is_shallow_versus_legacy(
    original: Mapping[str, Any] | None,
    legacy_dimensions: Mapping[str, float],
) -> bool:
    """Keep a lived but shallower formal snapshot instead of inflating it with legacy."""
    if not isinstance(original, Mapping):
        return False
    relationship = original.get("relationship")
    if not isinstance(relationship, Mapping):
        return False
    try:
        formal_dimensions = _normalise_formal_dimensions(
            relationship.get("dimensions"),
            reason="formal_relationship_dimensions_invalid",
        )
    except ValueError:
        return False
    if not any(abs(value) > 1e-9 for value in formal_dimensions.values()):
        return False
    comparable = [name for name in DIMENSIONS if name in formal_dimensions]
    if not comparable:
        return False
    return all(formal_dimensions[name] + 1e-9 < float(legacy_dimensions.get(name) or 0.0) for name in comparable)


def _merge_legacy_snapshot_with_formal_overlay(
    legacy_dimensions: Mapping[str, Any],
    original: Mapping[str, Any] | None,
) -> tuple[dict[str, float], dict[str, Any] | None]:
    """Put a verified legacy baseline beneath an existing formal automatic state.

    Formal relationships created after the legacy era contain live deltas from an
    empty formal baseline.  Replacing them with a legacy snapshot would silently
    discard those newer events.  Manual layers are rejected earlier, so this helper
    can safely preserve automatic values as an overlay in the same exact Scope.
    """
    legacy = _normalise_dimensions(legacy_dimensions)
    if original is None:
        return legacy, None

    relationship = original.get("relationship") if isinstance(original, Mapping) else None
    if not isinstance(relationship, Mapping):
        raise ValueError("formal_relationship_values_without_relation")
    relationship_dimensions = _normalise_formal_dimensions(
        relationship.get("dimensions"),
        reason="formal_relationship_dimensions_invalid",
    )
    relationship_evidence = _normalise_formal_evidence(relationship.get("evidence"))
    value_dimensions = _formal_value_dimensions(original.get("values"))
    for name in set(relationship_dimensions) & set(value_dimensions):
        if not math.isclose(relationship_dimensions[name], value_dimensions[name], rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("formal_relationship_projection_mismatch")

    overlay = dict(relationship_dimensions)
    overlay.update(value_dimensions)
    effective: dict[str, float] = {}
    clamped_dimensions: list[dict[str, float | str]] = []
    for name in DIMENSIONS:
        requested = legacy[name] + overlay.get(name, 0.0)
        value = clamp_dimension(name, requested)
        effective[name] = value
        if not math.isclose(value, requested, rel_tol=0.0, abs_tol=1e-9):
            clamped_dimensions.append({"dimension": name, "requested": requested, "effective": value})

    raw_revision = relationship.get("revision")
    if isinstance(raw_revision, bool):
        raise ValueError("formal_relationship_revision_invalid")
    try:
        revision = int(raw_revision or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("formal_relationship_revision_invalid") from exc
    return effective, {
        "dimensions": overlay,
        "evidence": relationship_evidence,
        "relationship_hash": _object_hash(dict(relationship)),
        "revision": revision,
        "clamped_dimensions": clamped_dimensions,
    }


def _normalise_formal_dimensions(value: Any, *, reason: str) -> dict[str, float]:
    try:
        raw = _json_object(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(reason) from exc
    dimensions: dict[str, float] = {}
    for name, raw_value in raw.items():
        if name not in DIMENSION_RANGES:
            raise ValueError("formal_relationship_dimension_unknown")
        if isinstance(raw_value, bool):
            raise ValueError(reason)
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(reason) from exc
        if not math.isfinite(numeric):
            raise ValueError(reason)
        lo, hi = DIMENSION_RANGES[name]
        if numeric < lo or numeric > hi:
            raise ValueError("formal_relationship_dimension_out_of_range")
        dimensions[name] = numeric
    return dimensions


def _normalise_formal_evidence(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("formal_relationship_evidence_invalid") from exc
    if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
        raise ValueError("formal_relationship_evidence_invalid")
    return [dict(item) for item in raw]


def _formal_value_dimensions(value: Any) -> dict[str, float]:
    if value in (None, []):
        return {}
    if not isinstance(value, list):
        raise ValueError("formal_relationship_values_invalid")
    dimensions: dict[str, float] = {}
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError("formal_relationship_values_invalid")
        name = row.get("dimension")
        if not isinstance(name, str) or name not in DIMENSION_RANGES or name in dimensions:
            raise ValueError("formal_relationship_values_invalid")
        if row.get("manual_adjustment") is not None or row.get("manual_override") is not None:
            raise ValueError("manual_relationship_calibration_conflict")
        try:
            automatic = float(row.get("automatic_value"))
            effective = float(row.get("effective_value"))
        except (TypeError, ValueError) as exc:
            raise ValueError("formal_relationship_values_invalid") from exc
        if not math.isfinite(automatic) or not math.isfinite(effective):
            raise ValueError("formal_relationship_values_invalid")
        lo, hi = DIMENSION_RANGES[name]
        if automatic < lo or automatic > hi or effective < lo or effective > hi:
            raise ValueError("formal_relationship_dimension_out_of_range")
        if not math.isclose(automatic, effective, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("formal_relationship_projection_mismatch")
        dimensions[name] = automatic
    return dimensions


def _write_profile_baseline(conn: sqlite3.Connection, item: Mapping[str, Any], original: Mapping[str, Any] | None) -> None:
    scope = item["scope"]
    subject = item["subject"]
    legacy_dimensions = item["dimensions"]
    dimensions, formal_overlay = _merge_legacy_snapshot_with_formal_overlay(legacy_dimensions, original)
    affinity = compute_affinity(dimensions)
    tables = _tables(conn)
    if "scoped_soul_relationships" not in tables:
        raise LegacyRelationshipMigrationError("formal_relationships_table_missing")
    relationship_columns = _columns(conn, "scoped_soul_relationships")
    required = {"bot_id", "session_id", "visibility", "subject_principal_id", "affinity", "dimensions"}
    if not required <= relationship_columns:
        raise LegacyRelationshipMigrationError("formal_relationships_schema_incomplete")
    value_status = _formal_values_status(conn)
    if value_status == "incomplete":
        raise LegacyRelationshipMigrationError("formal_relationship_values_schema_incomplete")
    now = time.time()
    evidence = [*formal_overlay["evidence"]] if formal_overlay else []
    evidence.append({
        "kind": "legacy_relationship_snapshot_baseline", "legacy_profile_id": item["legacy_id"],
        "source_row_hash": _object_hash(item["profile"]), "dimensions": legacy_dimensions,
        "effective_dimensions": dimensions,
    })
    if formal_overlay:
        evidence.append({
            "kind": "formal_relationship_live_overlay",
            "original_relationship_hash": formal_overlay["relationship_hash"],
            "original_revision": formal_overlay["revision"],
            "dimensions": formal_overlay["dimensions"],
            "clamped_dimensions": formal_overlay["clamped_dimensions"],
        })

    revision = _next_revision(original)
    if value_status == "available":
        manual = _manual_value_rows(conn, scope, subject)
        if manual:
            # Preview normally catches this; keep the no-overwrite invariant if the DB changed.
            raise LegacyRelationshipMigrationError("manual_relationship_calibration_conflict")
        try:
            _upsert_values(conn, scope, subject, dimensions, evidence, now, revision)
        except sqlite3.DatabaseError as exc:
            # A current schema that rejects hostility cannot receive a lossy four-value projection.
            raise LegacyRelationshipMigrationError("formal_values_reject_legacy_hostility") from exc

    row_values: dict[str, Any] = {
        "bot_id": scope["bot_id"], "session_id": scope["session_id"], "visibility": scope["visibility"],
        "subject_principal_id": subject, "affinity": affinity, "dimensions": _canonical(dimensions),
    }
    optional = {
        "state": _state_for_affinity(affinity), "revision": revision,
        "evidence": _canonical(evidence), "updated_at": now,
    }
    row_values.update({name: value for name, value in optional.items() if name in relationship_columns})
    names = list(row_values)
    assignments = ", ".join(f'"{name}"=excluded."{name}"' for name in names if name not in {"bot_id", "session_id", "visibility", "subject_principal_id"})
    conn.execute(
        f"INSERT INTO scoped_soul_relationships ({', '.join(_quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)}) "
        f"ON CONFLICT(bot_id,session_id,visibility,subject_principal_id) DO UPDATE SET {assignments}",
        tuple(row_values[name] for name in names),
    )
    _sync_relationship_revision(conn, scope, subject, revision, now)


def _sync_relationship_revision(
    conn: sqlite3.Connection,
    scope: Mapping[str, str],
    subject: str,
    revision: int,
    now: float,
) -> None:
    """Keep the repository's optimistic-concurrency revision source in sync."""
    if "scoped_soul_revisions" not in _tables(conn):
        return
    required = {"bot_id", "session_id", "visibility", "component", "subject_principal_id", "revision", "updated_at"}
    if not required <= _columns(conn, "scoped_soul_revisions"):
        raise LegacyRelationshipMigrationError("formal_relationship_revision_schema_incomplete")
    conn.execute(
        """INSERT INTO scoped_soul_revisions(
               bot_id, session_id, visibility, component, subject_principal_id, revision, updated_at)
           VALUES (?, ?, ?, 'relationship', ?, ?, ?)
           ON CONFLICT(bot_id, session_id, visibility, component, subject_principal_id)
           DO UPDATE SET revision=excluded.revision, updated_at=excluded.updated_at""",
        (scope["bot_id"], scope["session_id"], scope["visibility"], subject, revision, now),
    )


def _upsert_values(
    conn: sqlite3.Connection,
    scope: Mapping[str, str],
    subject: str,
    dimensions: Mapping[str, float],
    evidence: list[dict[str, Any]],
    now: float,
    revision: int,
) -> None:
    columns = _columns(conn, "scoped_soul_relationship_values")
    for dimension in DIMENSIONS:
        values: dict[str, Any] = {
            "bot_id": scope["bot_id"], "session_id": scope["session_id"], "visibility": scope["visibility"],
            "subject_principal_id": subject, "dimension": dimension, "automatic_value": dimensions[dimension],
        }
        optional = {
            "manual_adjustment": None, "manual_override": None, "effective_value": dimensions[dimension],
            "relationship_revision": revision, "evidence": _canonical(evidence), "updated_at": now,
        }
        values.update({name: value for name, value in optional.items() if name in columns})
        names = list(values)
        immutable = {"bot_id", "session_id", "visibility", "subject_principal_id", "dimension", "manual_adjustment", "manual_override"}
        assignments = ", ".join(f'"{name}"=excluded."{name}"' for name in names if name not in immutable)
        conn.execute(
            f"INSERT INTO scoped_soul_relationship_values ({', '.join(_quote(name) for name in names)}) "
            f"VALUES ({', '.join('?' for _ in names)}) "
            f"ON CONFLICT(bot_id,session_id,visibility,subject_principal_id,dimension) DO UPDATE SET {assignments}",
            tuple(values[name] for name in names),
        )


def _preview_profile(conn: sqlite3.Connection, row: Mapping[str, Any], scope_index: Mapping[tuple[str, str], Mapping[str, str]], ambiguous: set[tuple[str, str]], tables: set[str]) -> dict[str, Any]:
    item: dict[str, Any] = {"source_table": "user_profiles", "legacy_id": _legacy_id(row), "profile": dict(row)}
    key = (_text(row.get("bot_id")), _text(row.get("group_id")))
    if not _text(row.get("user_id")):
        return {**item, "disposition": "review", "reason": "legacy_profile_subject_missing"}
    if key in ambiguous:
        return {**item, "disposition": "review", "reason": "ambiguous_target_scope"}
    scope = scope_index.get(key)
    if scope is None:
        return {**item, "disposition": "review", "reason": "target_scope_missing"}
    try:
        metadata = _json_object(row.get("metadata"))
        dimensions = _normalise_dimensions(metadata.get("dimensions"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {**item, "disposition": "review", "reason": "legacy_five_dimensions_incomplete"}
    if not any(value != 0 for value in dimensions.values()):
        return {**item, "disposition": "review", "reason": "legacy_five_dimensions_zero"}
    try:
        affection = int(float(row.get("affection")))
    except (TypeError, ValueError):
        return {**item, "disposition": "review", "reason": "legacy_affection_invalid"}
    computed = compute_legacy_affection(dimensions)
    if affection != computed:
        return {**item, "disposition": "review", "reason": "legacy_affection_formula_mismatch", "computed_affection": computed}
    subject = _subject(scope, _text(row["user_id"]))
    if "scoped_soul_relationships" not in tables:
        return {**item, "disposition": "review", "reason": "formal_relationships_table_missing", "scope": dict(scope), "subject": subject}
    relationship_required = {"bot_id", "session_id", "visibility", "subject_principal_id", "affinity", "dimensions"}
    if not relationship_required <= _columns(conn, "scoped_soul_relationships"):
        return {**item, "disposition": "review", "reason": "formal_relationships_schema_incomplete", "scope": dict(scope), "subject": subject}
    values_status = _formal_values_status(conn)
    if values_status == "incomplete":
        return {**item, "disposition": "review", "reason": "formal_relationship_values_schema_incomplete", "scope": dict(scope), "subject": subject}
    manual_reason = _manual_conflict_reason(conn, tables, scope, subject)
    if manual_reason:
        return {**item, "disposition": "review", "reason": manual_reason, "scope": dict(scope), "subject": subject}
    formal = _formal_snapshot(conn, scope, subject)
    try:
        _merge_legacy_snapshot_with_formal_overlay(dimensions, formal)
    except ValueError as exc:
        return {**item, "disposition": "review", "reason": str(exc), "scope": dict(scope), "subject": subject}
    if _formal_is_shallow_versus_legacy(formal, dimensions):
        return {
            **item,
            "disposition": "keep_formal",
            "reason": "formal_shallow_relationship_preferred",
            "scope": dict(scope),
            "subject": subject,
            "dimensions": dimensions,
            "computed_affection": computed,
        }
    return {
        **item, "disposition": "migrate", "scope": dict(scope), "subject": subject,
        "dimensions": dimensions, "computed_affection": computed,
    }


def _preview_event(row: Mapping[str, Any], scope_index: Mapping[tuple[str, str], Mapping[str, str]], ambiguous: set[tuple[str, str]]) -> dict[str, Any]:
    item: dict[str, Any] = {"source_table": "relationship_events", "legacy_id": _legacy_id(row), "event": dict(row)}
    key = (_text(row.get("bot_id")), _text(row.get("group_id")))
    if key in ambiguous:
        return {**item, "disposition": "review", "reason": "ambiguous_target_scope"}
    scope = scope_index.get(key)
    if scope is None:
        return {**item, "disposition": "review", "reason": "target_scope_missing"}
    if not _text(row.get("user_id")):
        return {**item, "disposition": "review", "reason": "legacy_event_subject_missing"}
    return {**item, "disposition": "audit", "scope": dict(scope), "subject": _subject(scope, _text(row["user_id"]))}


def _manual_conflict_reason(conn: sqlite3.Connection, tables: set[str], scope: Mapping[str, str], subject: str) -> str | None:
    if "scoped_soul_relationship_values" not in tables:
        return None
    columns = _columns(conn, "scoped_soul_relationship_values")
    manual_columns = {"manual_adjustment", "manual_override"}
    identity = {"bot_id", "session_id", "visibility", "subject_principal_id"}
    if not identity <= columns:
        return "formal_values_schema_incomplete"
    if not manual_columns <= columns:
        return "formal_values_schema_incomplete"
    return "manual_relationship_calibration_conflict" if _manual_value_rows(conn, scope, subject) else None


def _manual_value_rows(conn: sqlite3.Connection, scope: Mapping[str, str], subject: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT dimension FROM scoped_soul_relationship_values
             WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
               AND (manual_adjustment IS NOT NULL OR manual_override IS NOT NULL)""",
        (scope["bot_id"], scope["session_id"], scope["visibility"], subject),
    ).fetchall()


def _formal_values_status(conn: sqlite3.Connection) -> str:
    if "scoped_soul_relationship_values" not in _tables(conn):
        return "missing"
    required = {"bot_id", "session_id", "visibility", "subject_principal_id", "dimension", "automatic_value", "manual_adjustment", "manual_override"}
    return "available" if required <= _columns(conn, "scoped_soul_relationship_values") else "incomplete"


def _formal_snapshot(conn: sqlite3.Connection, scope: Mapping[str, str], subject: str) -> dict[str, Any] | None:
    tables = _tables(conn)
    snapshot: dict[str, Any] = {}
    if "scoped_soul_relationships" in tables:
        cursor = conn.execute(
            "SELECT * FROM scoped_soul_relationships WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?",
            (scope["bot_id"], scope["session_id"], scope["visibility"], subject),
        )
        row = cursor.fetchone()
        if row:
            snapshot["relationship"] = _cursor_row_mapping(cursor, row)
    if "scoped_soul_relationship_values" in tables:
        cursor = conn.execute(
            "SELECT * FROM scoped_soul_relationship_values WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=? ORDER BY dimension",
            (scope["bot_id"], scope["session_id"], scope["visibility"], subject),
        )
        rows = cursor.fetchall()
        if rows:
            snapshot["values"] = [_cursor_row_mapping(cursor, row) for row in rows]
    return snapshot or None


def _cursor_row_mapping(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    """Support both sqlite3.Row production connections and tuple test/preview connections."""
    try:
        return dict(row)
    except (TypeError, ValueError):
        columns = [str(column[0]) for column in (cursor.description or ())]
        return dict(zip(columns, row))


def _record_item(conn: sqlite3.Connection, item: Mapping[str, Any], run_id: str, source_hash: str, disposition: str, original: Mapping[str, Any] | None = None) -> None:
    scope = item.get("scope")
    scope_key = _scope_key(scope) if isinstance(scope, Mapping) else ""
    source_row = item.get("profile") or item.get("event") or {}
    source_row_hash = _object_hash(source_row)
    original_json = _canonical(original) if original else None
    original_hash = _object_hash(original) if original else None
    conn.execute(
        """INSERT INTO legacy_relationship_migration_items(
               run_id, source_table, legacy_id, scope_key, disposition, reason, source_hash,
               source_row_hash, original_formal_json, original_formal_hash, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_table,legacy_id,scope_key,source_row_hash) DO UPDATE SET
               run_id=excluded.run_id, disposition=excluded.disposition, reason=excluded.reason,
               source_hash=excluded.source_hash, original_formal_json=excluded.original_formal_json,
               original_formal_hash=excluded.original_formal_hash, created_at=excluded.created_at""",
        (run_id, _text(item.get("source_table")), str(item.get("legacy_id", "")), scope_key, disposition,
         _text(item.get("reason")), source_hash, source_row_hash, original_json, original_hash, time.time()),
    )


def _ensure_audit_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scoped_soul_relationship_legacy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_event_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            group_id TEXT NOT NULL,
            subject_principal_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            dimension TEXT NOT NULL,
            delta REAL NOT NULL,
            reason TEXT NOT NULL,
            occurred_at REAL,
            source_episode_id INTEGER,
            source_memory_id INTEGER,
            source_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            run_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(legacy_event_id, scope_key, event_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_rel_events_subject
            ON scoped_soul_relationship_legacy_events(
                bot_id, session_id, visibility, subject_principal_id, occurred_at DESC, id DESC
            );
        CREATE INDEX IF NOT EXISTS idx_legacy_rel_events_scope_type
            ON scoped_soul_relationship_legacy_events(
                bot_id, session_id, visibility, event_type
            );
        CREATE TABLE IF NOT EXISTS legacy_relationship_migration_runs (
            run_id TEXT PRIMARY KEY,
            rule_version TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            target_scopes_json TEXT NOT NULL,
            status TEXT NOT NULL,
            report_json TEXT,
            created_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS legacy_relationship_migration_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            legacy_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reason TEXT,
            source_hash TEXT NOT NULL,
            source_row_hash TEXT NOT NULL,
            original_formal_json TEXT,
            original_formal_hash TEXT,
            created_at REAL NOT NULL,
            UNIQUE(source_table, legacy_id, scope_key, source_row_hash)
        );
    """)


def _target_scopes(target_scopes: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    if not isinstance(target_scopes, Sequence) or isinstance(target_scopes, (str, bytes)) or not target_scopes:
        raise LegacyRelationshipMigrationError("canonical_target_scopes_required")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in target_scopes:
        if not isinstance(raw, Mapping):
            raise LegacyRelationshipMigrationError("canonical_target_scope_required")
        required = {"bot_id", "session_id", "visibility", "group_id"}
        if set(raw) != required:
            raise LegacyRelationshipMigrationError("target_scope_must_be_canonical_group_scope")
        scope = {name: _text(raw.get(name)) for name in required}
        parts = scope["session_id"].split(":", 2)
        if not scope["bot_id"] or not scope["group_id"] or scope["visibility"] != "group" or len(parts) != 3 or parts[1] != "group" or parts[2] != scope["group_id"]:
            raise LegacyRelationshipMigrationError("target_scope_must_be_canonical_group_scope")
        key = (scope["bot_id"], scope["session_id"], scope["visibility"], scope["group_id"])
        if key in seen:
            raise LegacyRelationshipMigrationError("duplicate_target_scope")
        seen.add(key)
        result.append(scope)
    return tuple(sorted(result, key=_scope_key))


def _scope_index(scopes: Sequence[Mapping[str, str]]) -> tuple[dict[tuple[str, str], Mapping[str, str]], set[tuple[str, str]]]:
    result: dict[tuple[str, str], Mapping[str, str]] = {}
    ambiguous: set[tuple[str, str]] = set()
    for scope in scopes:
        key = (scope["bot_id"], scope["group_id"])
        if key in result:
            ambiguous.add(key)
        else:
            result[key] = scope
    return result, ambiguous


def _normalise_dimensions(raw: Any, *, enforce_ranges: bool = True) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != set(DIMENSIONS):
        raise ValueError("five_dimensions_required")
    values: dict[str, float] = {}
    for name in DIMENSIONS:
        value = raw[name]
        if isinstance(value, bool):
            raise ValueError("dimension_boolean_invalid")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("dimension_not_finite")
        lo, hi = DIMENSION_RANGES[name]
        if enforce_ranges and (value < lo or value > hi):
            raise ValueError("dimension_out_of_range")
        values[name] = value
    return values


def _rows(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_quote(name) for name in columns)} FROM {_quote(table)} "
        f"ORDER BY {_quote('id') if 'id' in columns else 'rowid'}"
    ).fetchall()
    return [dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row)) for row in rows]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()}


def _legacy_table_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Fingerprint every legacy row so a staged write cannot silently alter it."""
    state: dict[str, dict[str, Any]] = {}
    tables = _tables(conn)
    for table in ("user_profiles", "relationship_events"):
        if table not in tables:
            continue
        columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()]
        order = _quote("id") if "id" in columns else "rowid"
        cursor = conn.execute(f"SELECT * FROM {_quote(table)} ORDER BY {order}")
        digest = hashlib.sha256()
        digest.update(_canonical(columns).encode("utf-8"))
        count = 0
        for row in cursor:
            digest.update(_canonical(list(row)).encode("utf-8"))
            digest.update(b"\n")
            count += 1
        state[table] = {"count": count, "sha256": "sha256:" + digest.hexdigest()}
    return state


def _formal_fingerprint(
    conn: sqlite3.Connection,
    scopes: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Fingerprint formal relationship rows/values for event_audit_only guards."""
    tables = _tables(conn)
    if "scoped_soul_relationships" not in tables:
        return {"relationships": {"count": 0, "sha256": "sha256:" + hashlib.sha256(b"").hexdigest()}}

    digest = hashlib.sha256()
    count = 0
    affinity_sum = 0
    for scope in scopes:
        rows = conn.execute(
            """SELECT bot_id, session_id, visibility, subject_principal_id,
                      affinity, state, dimensions, revision, evidence, updated_at
                 FROM scoped_soul_relationships
                WHERE bot_id=? AND session_id=? AND visibility=?
                ORDER BY subject_principal_id""",
            (scope["bot_id"], scope["session_id"], scope["visibility"]),
        ).fetchall()
        for row in rows:
            digest.update(_canonical(list(row)).encode("utf-8"))
            digest.update(b"\n")
            count += 1
            try:
                affinity_sum += int(row[4] or 0)
            except (TypeError, ValueError):
                pass
        if "scoped_soul_relationship_values" in tables:
            value_rows = conn.execute(
                """SELECT bot_id, session_id, visibility, subject_principal_id, dimension,
                          automatic_value, manual_adjustment, manual_override, effective_value,
                          relationship_revision, evidence, updated_at
                     FROM scoped_soul_relationship_values
                    WHERE bot_id=? AND session_id=? AND visibility=?
                    ORDER BY subject_principal_id, dimension""",
                (scope["bot_id"], scope["session_id"], scope["visibility"]),
            ).fetchall()
            for row in value_rows:
                digest.update(b"V")
                digest.update(_canonical(list(row)).encode("utf-8"))
                digest.update(b"\n")
    return {
        "relationships": {
            "count": count,
            "affinity_sum": affinity_sum,
            "sha256": "sha256:" + digest.hexdigest(),
        }
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    parsed = json.loads(_text(value) or "{}")
    if not isinstance(parsed, Mapping):
        raise ValueError("metadata_not_object")
    return dict(parsed)


def _event_time(event: Mapping[str, Any]) -> float | None:
    value = event.get("timestamp", event.get("created_at"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _next_revision(original: Mapping[str, Any] | None) -> int:
    if not original:
        return 1
    relationship = original.get("relationship") if isinstance(original, Mapping) else None
    try:
        return int(relationship.get("revision") or 0) + 1
    except (AttributeError, TypeError, ValueError):
        return 1


def _state_for_affinity(affinity: int) -> str:
    """Use the same state labels and thresholds as live formal relationships."""
    return attitude_level(affinity)


def _subject(scope: Mapping[str, str], user_id: str) -> str:
    return f"{scope['session_id'].split(':', 1)[0]}:user:{user_id}"


def _legacy_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") if row.get("id") is not None else _object_hash(dict(row)))


def _scope_key(scope: Mapping[str, Any]) -> str:
    return "|".join(_text(scope.get(name)) for name in ("bot_id", "session_id", "visibility", "group_id"))


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _object_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _make_read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD)


__all__ = [
    "CONFIRMATION", "DIMENSIONS", "LegacyRelationshipMigrationError", "RULE_VERSION",
    "compute_legacy_affection", "preview", "stage",
]
