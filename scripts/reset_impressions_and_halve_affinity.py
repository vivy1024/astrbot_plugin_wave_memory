#!/usr/bin/env python3
"""Clear current Bot impressions; optionally halve formal relationship values.

Safety:
- Default is dry-run and impression-only.
- --halve-affinity is required to touch relationship values.
- --apply writes the given db file only.
- A production-like path also needs --allow-production plus the confirmation token.
- Never forges group memories. Calibration evidence is an explicit maintenance marker.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain.relationship_policy import DIMENSION_RANGES, attitude_level, clamp_dimension, compute_affinity
from services.impression_timeline import clear_impression

CONFIRMATION = "reset-impressions-and-halve-affinity"
BATCH_ID = "impression-affinity-reset-v1"
IMPRESSION_REASON = "批量清理旧印象，重新积累"
AFFINITY_REASON = "批量将正式关系五维生效值减半，重新积累"
DIMENSIONS = ("familiarity", "trust", "fun", "hostility", "depth")
ACTOR = "maintenance"


def _is_prod_like(path: Path) -> bool:
    text = path.as_posix()
    return path.name == "wave_memory.db" and "plugin_data" in text and "backups" not in text


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=120)
        conn.execute("PRAGMA query_only=ON")
        return conn
    conn = sqlite3.connect(path.as_posix(), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return loaded if isinstance(loaded, type(default)) else default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _percentile(sorted_values: list[int], ratio: float) -> int | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * ratio))))
    return int(sorted_values[index])


def _affinity_stats(values: list[int]) -> dict[str, int | None]:
    ordered = sorted(int(item) for item in values)
    return {
        "count": len(ordered),
        "min": ordered[0] if ordered else None,
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "max": ordered[-1] if ordered else None,
    }


def _current_impression(metadata: Any) -> str:
    payload = _json(metadata, {})
    return str(payload.get("impression") or "").strip()


def plan_impression_clears(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if "user_profiles" not in _tables(conn):
        return []
    rows = conn.execute(
        "SELECT user_id, group_id, bot_id, metadata FROM user_profiles"
    ).fetchall()
    planned: list[dict[str, Any]] = []
    for user_id, group_id, bot_id, metadata in rows:
        current = _current_impression(metadata)
        if not current:
            continue
        planned.append({
            "user_id": str(user_id or ""),
            "group_id": str(group_id or ""),
            "bot_id": str(bot_id or ""),
            "previous_impression": current,
        })
    return planned


def plan_affinity_halves(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = _tables(conn)
    required = {
        "scoped_soul_relationships",
        "scoped_soul_relationship_values",
        "scoped_soul_revisions",
        "scoped_soul_relationship_calibration_events",
        "scoped_soul_timeline",
    }
    if not required.issubset(tables):
        return []
    relationships = conn.execute(
        """SELECT bot_id, session_id, visibility, subject_principal_id,
                  affinity, state, dimensions, revision, evidence
             FROM scoped_soul_relationships
            WHERE visibility='group'
            ORDER BY bot_id, session_id, subject_principal_id"""
    ).fetchall()
    planned: list[dict[str, Any]] = []
    for bot_id, session_id, visibility, subject, affinity, state, dimensions_raw, revision, evidence_raw in relationships:
        values = conn.execute(
            """SELECT dimension, automatic_value, manual_adjustment, manual_override, effective_value
                 FROM scoped_soul_relationship_values
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            (bot_id, session_id, visibility, subject),
        ).fetchall()
        by_dim = {str(row[0]): row for row in values}
        after_dimensions: dict[str, float] = {}
        before_dimensions = _json(dimensions_raw, {})
        dimension_changes: list[dict[str, Any]] = []
        skipped_missing = []
        for dimension in DIMENSIONS:
            row = by_dim.get(dimension)
            if row is None:
                skipped_missing.append(dimension)
                continue
            before_effective = float(row[4])
            after_effective = clamp_dimension(dimension, before_effective * 0.5)
            after_dimensions[dimension] = after_effective
            dimension_changes.append({
                "dimension": dimension,
                "automatic_value": None if row[1] is None else float(row[1]),
                "manual_adjustment": None if row[2] is None else float(row[2]),
                "manual_override_before": None if row[3] is None else float(row[3]),
                "effective_before": before_effective,
                "effective_after": after_effective,
            })
        if not dimension_changes:
            continue
        after_affinity = compute_affinity(after_dimensions)
        planned.append({
            "bot_id": str(bot_id),
            "session_id": str(session_id),
            "visibility": str(visibility),
            "subject_principal_id": str(subject),
            "affinity_before": int(affinity or 0),
            "affinity_after": int(after_affinity),
            "state_before": str(state or ""),
            "state_after": attitude_level(after_affinity),
            "revision_before": int(revision or 0),
            "revision_after": int(revision or 0) + 1,
            "before_dimensions": before_dimensions,
            "after_dimensions": after_dimensions,
            "existing_evidence": _json(evidence_raw, []),
            "dimension_changes": dimension_changes,
            "missing_dimensions": skipped_missing,
        })
    return planned


def build_report(*, impressions: list[dict[str, Any]], relationships: list[dict[str, Any]]) -> dict[str, Any]:
    by_scope: Counter[str] = Counter()
    for item in impressions:
        by_scope[f"{item['bot_id']}|{item['group_id']}"] += 1
    return {
        "batch_id": BATCH_ID,
        "impressions": {
            "clear_count": len(impressions),
            "by_bot_group": dict(sorted(by_scope.items())),
            "preview": impressions[:20],
        },
        "relationships": {
            "count": len(relationships),
            "dimension_count": sum(len(item["dimension_changes"]) for item in relationships),
            "missing_dimension_rows": sum(len(item["missing_dimensions"]) for item in relationships),
            "affinity_before": _affinity_stats([item["affinity_before"] for item in relationships]),
            "affinity_after": _affinity_stats([item["affinity_after"] for item in relationships]),
            "preview": [
                {
                    "bot_id": item["bot_id"],
                    "session_id": item["session_id"],
                    "subject_principal_id": item["subject_principal_id"],
                    "affinity_before": item["affinity_before"],
                    "affinity_after": item["affinity_after"],
                    "missing_dimensions": item["missing_dimensions"],
                }
                for item in relationships[:20]
            ],
        },
    }


def apply_impression_clears(conn: sqlite3.Connection, planned: list[dict[str, Any]], *, now: float) -> int:
    updated = 0
    for item in planned:
        row = conn.execute(
            "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
            (item["user_id"], item["group_id"], item["bot_id"]),
        ).fetchone()
        if row is None:
            continue
        metadata = _json(row[0], {})
        if not str(metadata.get("impression") or "").strip():
            continue
        cleared = clear_impression(metadata, reason=IMPRESSION_REASON, now=now, actor=ACTOR)
        conn.execute(
            "UPDATE user_profiles SET metadata=? WHERE user_id=? AND group_id=? AND bot_id=?",
            (_dump(cleared), item["user_id"], item["group_id"], item["bot_id"]),
        )
        updated += 1
    return updated


def _maintenance_evidence(*, now: float, subject: str, dimension: str, before: float, after: float) -> list[dict[str, Any]]:
    return [{
        "kind": "maintenance_batch",
        "id": BATCH_ID,
        "content_hash": f"sha256:{BATCH_ID}:{dimension}",
        "captured_at": now,
        "available": True,
        "summary": f"{subject} {dimension} {before:g}->{after:g}",
    }]


def apply_affinity_halves(conn: sqlite3.Connection, planned: list[dict[str, Any]], *, now: float) -> dict[str, int]:
    changed_relationships = 0
    changed_dimensions = 0
    for item in planned:
        scope = (item["bot_id"], item["session_id"], item["visibility"], item["subject_principal_id"])
        current = conn.execute(
            """SELECT revision FROM scoped_soul_relationships
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            scope,
        ).fetchone()
        if current is None or int(current[0] or 0) != int(item["revision_before"]):
            raise RuntimeError(f"relationship_revision_conflict:{scope}")
        revision = int(item["revision_after"])
        conn.execute(
            """INSERT INTO scoped_soul_revisions(
                   bot_id, session_id, visibility, component, subject_principal_id, revision, updated_at)
               VALUES (?, ?, ?, 'relationship', ?, ?, ?)
               ON CONFLICT(bot_id, session_id, visibility, component, subject_principal_id)
               DO UPDATE SET revision=excluded.revision, updated_at=excluded.updated_at""",
            (item["bot_id"], item["session_id"], item["visibility"], item["subject_principal_id"], revision, now),
        )
        conn.execute(
            """UPDATE scoped_soul_relationship_values
                  SET relationship_revision=?
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            (revision, *scope),
        )
        all_evidence: list[dict[str, Any]] = []
        for change in item["dimension_changes"]:
            evidence = _maintenance_evidence(
                now=now,
                subject=item["subject_principal_id"],
                dimension=change["dimension"],
                before=change["effective_before"],
                after=change["effective_after"],
            )
            all_evidence.extend(evidence)
            before_payload = {
                "dimension": change["dimension"],
                "automatic_value": change["automatic_value"],
                "manual_adjustment": change["manual_adjustment"],
                "manual_override": change["manual_override_before"],
                "effective_value": change["effective_before"],
            }
            after_payload = {
                "dimension": change["dimension"],
                "automatic_value": change["automatic_value"],
                "manual_adjustment": change["manual_adjustment"],
                "manual_override": change["effective_after"],
                "effective_value": change["effective_after"],
            }
            conn.execute(
                """UPDATE scoped_soul_relationship_values
                      SET manual_override=?, effective_value=?, evidence=?, updated_at=?, relationship_revision=?
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=? AND dimension=?""",
                (
                    change["effective_after"],
                    change["effective_after"],
                    _dump(evidence),
                    now,
                    revision,
                    *scope,
                    change["dimension"],
                ),
            )
            operation_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"wave-memory:{BATCH_ID}:{item['bot_id']}:{item['session_id']}:{item['subject_principal_id']}:{change['dimension']}:{revision}",
            ).hex
            calibration_id = f"relationship-calibration:{operation_id}"
            conn.execute(
                """INSERT INTO scoped_soul_relationship_calibration_events(
                       calibration_id, operation_id, bot_id, session_id, visibility,
                       subject_principal_id, dimension, action, before_json, after_json,
                       reason, evidence, actor, relationship_revision, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'override', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    calibration_id,
                    operation_id,
                    item["bot_id"],
                    item["session_id"],
                    item["visibility"],
                    item["subject_principal_id"],
                    change["dimension"],
                    _dump(before_payload),
                    _dump(after_payload),
                    AFFINITY_REASON,
                    _dump(evidence),
                    ACTOR,
                    revision,
                    now,
                ),
            )
            changed_dimensions += 1
        merged_evidence = list(all_evidence) + [
            item for item in item["existing_evidence"]
            if isinstance(item, dict) and item.get("kind") == "historical_audit_summary"
        ]
        conn.execute(
            """UPDATE scoped_soul_relationships
                  SET affinity=?, state=?, dimensions=?, revision=?, evidence=?, updated_at=?
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=? AND revision=?""",
            (
                item["affinity_after"],
                item["state_after"],
                _dump(item["after_dimensions"]),
                revision,
                _dump(merged_evidence),
                now,
                *scope,
                item["revision_before"],
            ),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError(f"relationship_update_missed:{scope}")
        conn.execute(
            """INSERT INTO scoped_soul_timeline(
                   bot_id, session_id, visibility, subject_principal_id, event_summary,
                   event_type, emotional_weight, occurred_at, revision, evidence, created_at)
               VALUES (?, ?, ?, ?, ?, 'relationship.manual_calibration', 0.5, ?, ?, ?, ?)""",
            (
                item["bot_id"],
                item["session_id"],
                item["visibility"],
                item["subject_principal_id"],
                f"维护校准关系：五维生效值减半；理由：{AFFINITY_REASON}",
                now,
                revision,
                _dump(all_evidence),
                now,
            ),
        )
        changed_relationships += 1
    return {"relationships": changed_relationships, "dimensions": changed_dimensions}


def run(
    db_path: Path,
    *,
    apply: bool,
    now: float | None = None,
    clear_impressions: bool = True,
    halve_affinity: bool = False,
) -> dict[str, Any]:
    stamp = float(now if now is not None else time.time())
    conn = _connect(db_path, readonly=not apply)
    try:
        impressions = plan_impression_clears(conn) if clear_impressions else []
        relationships = plan_affinity_halves(conn) if halve_affinity else []
        report = build_report(impressions=impressions, relationships=relationships)
        report["db"] = str(db_path)
        report["mode"] = "apply" if apply else "dry-run"
        report["clear_impressions"] = bool(clear_impressions)
        report["halve_affinity"] = bool(halve_affinity)
        if not apply:
            return report
        backup_path = db_path.with_name(f"{db_path.name}.bak-{int(stamp)}")
        if backup_path.exists():
            raise SystemExit(f"backup already exists: {backup_path}")
        shutil.copy2(db_path, backup_path)
        report["backup_path"] = str(backup_path)
        try:
            report["applied_impressions"] = apply_impression_clears(conn, impressions, now=stamp) if clear_impressions else 0
            if halve_affinity:
                applied = apply_affinity_halves(conn, relationships, now=stamp)
                report["applied_relationships"] = applied["relationships"]
                report["applied_dimensions"] = applied["dimensions"]
            else:
                report["applied_relationships"] = 0
                report["applied_dimensions"] = 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--halve-affinity", action="store_true", help="Also halve formal relationship effective values")
    parser.add_argument("--skip-impressions", action="store_true", help="Do not clear current impressions")
    parser.add_argument("--confirmation", type=str, default="")
    parser.add_argument("--allow-production", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"db not found: {db_path}")
    if args.apply and _is_prod_like(db_path):
        if args.confirmation != CONFIRMATION or not args.allow_production:
            raise SystemExit(
                "production-like apply requires --allow-production "
                f"and --confirmation {CONFIRMATION}"
            )
    report = run(
        db_path,
        apply=bool(args.apply),
        clear_impressions=not bool(args.skip_impressions),
        halve_affinity=bool(args.halve_affinity),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
