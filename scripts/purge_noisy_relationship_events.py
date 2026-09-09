#!/usr/bin/env python3
"""Remove passing-by relationship noise and rebuild formal five-dimension values.

Safety:
- Default is dry-run.
- --apply writes the given db file only after a sibling backup.
- A production-like path also needs --allow-production plus the confirmation token.
- Real social events (direct_reply / real jokes / etc.) are kept.
- Passing-by ``message_seen`` and keyword-empty reasons are deleted.
- Subjects left with no formal events are removed from the formal relationship tables.
- user_profiles impressions / affection are not wiped; leftover noise familiarity is subtracted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain.relationship_policy import (
    DIMENSION_RANGES,
    NOISY_EVENT_REASONS,
    NOISY_EVENT_TYPES,
    attitude_level,
    clamp_dimension,
    compute_affinity,
)

CONFIRMATION = "purge-noisy-relationship-events"
BATCH_ID = "purge-noisy-relationship-events-v1"


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


def _noise_sql(event_type_col: str = "event_type", reason_col: str = "reason") -> tuple[str, list[str]]:
    types = tuple(sorted(NOISY_EVENT_TYPES))
    reasons = tuple(sorted(NOISY_EVENT_REASONS))
    type_placeholders = ",".join("?" for _ in types) or "NULL"
    reason_placeholders = ",".join("?" for _ in reasons) or "NULL"
    sql = (
        f"({event_type_col} IN ({type_placeholders})"
        f" OR COALESCE({reason_col}, '') IN ({reason_placeholders}))"
    )
    return sql, [*types, *reasons]


def _principal_user_id(subject: str) -> str:
    marker = ":user:"
    if marker in subject:
        return subject.split(marker, 1)[1]
    return subject


def _session_group_id(session_id: str) -> str:
    marker = ":group:"
    if marker in session_id:
        return session_id.split(marker, 1)[1]
    return session_id


def plan_purge(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    noise_sql, noise_params = _noise_sql()
    scoped_noise = 0
    legacy_noise = 0
    remaining_by_subject: dict[tuple[str, str, str, str], dict[str, float]] = {}
    current_subjects: list[tuple[str, str, str, str]] = []

    if "scoped_soul_relationship_events" in tables:
        scoped_noise = int(
            conn.execute(
                f"SELECT COUNT(*) FROM scoped_soul_relationship_events WHERE {noise_sql}",
                noise_params,
            ).fetchone()[0]
        )
        for bot_id, session_id, visibility, subject, dimension, total in conn.execute(
            f"""SELECT bot_id, session_id, visibility, subject_principal_id, dimension, SUM(delta)
                  FROM scoped_soul_relationship_events
                 WHERE NOT {noise_sql}
                 GROUP BY bot_id, session_id, visibility, subject_principal_id, dimension""",
            noise_params,
        ):
            key = (str(bot_id), str(session_id), str(visibility), str(subject))
            remaining_by_subject.setdefault(key, {})[str(dimension)] = float(total or 0.0)

    if "relationship_events" in tables:
        legacy_noise = int(
            conn.execute(
                f"SELECT COUNT(*) FROM relationship_events WHERE {noise_sql}",
                noise_params,
            ).fetchone()[0]
        )

    if "scoped_soul_relationships" in tables:
        current_subjects = [
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in conn.execute(
                """SELECT bot_id, session_id, visibility, subject_principal_id
                     FROM scoped_soul_relationships"""
            )
        ]

    rebuild: list[dict[str, Any]] = []
    delete_subjects: list[dict[str, str]] = []
    for bot_id, session_id, visibility, subject in current_subjects:
        key = (bot_id, session_id, visibility, subject)
        remaining = remaining_by_subject.get(key, {})
        if remaining:
            rebuild.append({
                "bot_id": bot_id,
                "session_id": session_id,
                "visibility": visibility,
                "subject_principal_id": subject,
                "remaining_dimensions": {dim: round(value, 2) for dim, value in remaining.items()},
            })
        else:
            delete_subjects.append({
                "bot_id": bot_id,
                "session_id": session_id,
                "visibility": visibility,
                "subject_principal_id": subject,
            })

    profile_updates = 0
    if "user_profiles" in tables and "scoped_soul_relationship_events" in tables:
        for bot_id, session_id, subject, total in conn.execute(
            f"""SELECT bot_id, session_id, subject_principal_id, SUM(delta)
                  FROM scoped_soul_relationship_events
                 WHERE {noise_sql} AND dimension='familiarity'
                 GROUP BY bot_id, session_id, subject_principal_id""",
            noise_params,
        ):
            if abs(float(total or 0.0)) < 1e-9:
                continue
            profile_updates += 1

    return {
        "scoped_noise_events": scoped_noise,
        "legacy_noise_events": legacy_noise,
        "subjects_rebuilt": len(rebuild),
        "subjects_deleted": len(delete_subjects),
        "profile_familiarity_updates": profile_updates,
        "rebuild_preview": rebuild[:20],
        "delete_preview": delete_subjects[:20],
        "kept_event_types": [
            {"event_type": str(row[0]), "count": int(row[1])}
            for row in (
                conn.execute(
                    f"""SELECT event_type, COUNT(*)
                          FROM scoped_soul_relationship_events
                         WHERE NOT {noise_sql}
                         GROUP BY event_type
                         ORDER BY COUNT(*) DESC""",
                    noise_params,
                ).fetchall()
                if "scoped_soul_relationship_events" in tables
                else []
            )
        ],
    }


def _bump_revision(conn: sqlite3.Connection, *, bot_id: str, session_id: str, visibility: str, subject: str, now: float) -> int:
    conn.execute(
        """INSERT INTO scoped_soul_revisions(
               bot_id, session_id, visibility, component, subject_principal_id, revision, updated_at)
           VALUES (?, ?, ?, 'relationship', ?, 1, ?)
           ON CONFLICT(bot_id, session_id, visibility, component, subject_principal_id)
           DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at""",
        (bot_id, session_id, visibility, subject, now),
    )
    row = conn.execute(
        """SELECT revision FROM scoped_soul_revisions
            WHERE bot_id=? AND session_id=? AND visibility=? AND component='relationship'
              AND subject_principal_id=?""",
        (bot_id, session_id, visibility, subject),
    ).fetchone()
    return int(row[0] if row else 1)


def apply_purge(conn: sqlite3.Connection, *, now: float) -> dict[str, int]:
    tables = _tables(conn)
    noise_sql, noise_params = _noise_sql()
    deleted_scoped = 0
    deleted_legacy = 0
    rebuilt = 0
    deleted_subjects = 0
    profile_updates = 0
    profile_noise = _capture_profile_noise(conn)

    remaining_by_subject: dict[tuple[str, str, str, str], dict[str, float]] = {}
    if "scoped_soul_relationship_events" in tables:
        for bot_id, session_id, visibility, subject, dimension, total in conn.execute(
            f"""SELECT bot_id, session_id, visibility, subject_principal_id, dimension, SUM(delta)
                  FROM scoped_soul_relationship_events
                 WHERE NOT {noise_sql}
                 GROUP BY bot_id, session_id, visibility, subject_principal_id, dimension""",
            noise_params,
        ):
            key = (str(bot_id), str(session_id), str(visibility), str(subject))
            remaining_by_subject.setdefault(key, {})[str(dimension)] = float(total or 0.0)
        cur = conn.execute(
            f"DELETE FROM scoped_soul_relationship_events WHERE {noise_sql}",
            noise_params,
        )
        deleted_scoped = int(cur.rowcount or 0)

    if "relationship_events" in tables:
        cur = conn.execute(
            f"DELETE FROM relationship_events WHERE {noise_sql}",
            noise_params,
        )
        deleted_legacy = int(cur.rowcount or 0)

    if "scoped_soul_relationships" in tables:
        subjects = list(conn.execute(
            """SELECT bot_id, session_id, visibility, subject_principal_id, revision, evidence
                 FROM scoped_soul_relationships"""
        ))
        for bot_id, session_id, visibility, subject, _revision, evidence_raw in subjects:
            key = (str(bot_id), str(session_id), str(visibility), str(subject))
            remaining = remaining_by_subject.get(key, {})
            if not remaining:
                conn.execute(
                    """DELETE FROM scoped_soul_relationship_values
                        WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
                    key,
                )
                conn.execute(
                    """DELETE FROM scoped_soul_relationships
                        WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
                    key,
                )
                deleted_subjects += 1
                continue

            revision = _bump_revision(
                conn,
                bot_id=str(bot_id),
                session_id=str(session_id),
                visibility=str(visibility),
                subject=str(subject),
                now=now,
            )
            existing_values = {
                str(row[0]): row
                for row in conn.execute(
                    """SELECT dimension, automatic_value, manual_adjustment, manual_override, effective_value
                         FROM scoped_soul_relationship_values
                        WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
                    key,
                )
            }
            keep_dimensions = set(remaining) | {
                dim for dim, row in existing_values.items()
                if row[2] is not None or row[3] is not None
            }
            conn.execute(
                """DELETE FROM scoped_soul_relationship_values
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                      AND dimension NOT IN ({})""".format(
                    ",".join("?" for _ in keep_dimensions) or "NULL"
                ),
                (*key, *keep_dimensions) if keep_dimensions else key,
            )
            dimensions: dict[str, float] = {}
            for dimension in keep_dimensions:
                current = existing_values.get(dimension)
                automatic = clamp_dimension(dimension, remaining.get(dimension, 0.0))
                adjustment = None if current is None else current[2]
                override = None if current is None else current[3]
                if override is not None:
                    effective = clamp_dimension(dimension, float(override))
                else:
                    effective = clamp_dimension(dimension, automatic + float(adjustment or 0.0))
                evidence = [{"kind": "maintenance_batch", "id": BATCH_ID, "dimension": dimension}]
                conn.execute(
                    """INSERT INTO scoped_soul_relationship_values(
                           bot_id, session_id, visibility, subject_principal_id, dimension,
                           automatic_value, manual_adjustment, manual_override, effective_value,
                           relationship_revision, evidence, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bot_id, session_id, visibility, subject_principal_id, dimension)
                       DO UPDATE SET automatic_value=excluded.automatic_value,
                           effective_value=excluded.effective_value,
                           relationship_revision=excluded.relationship_revision,
                           evidence=excluded.evidence, updated_at=excluded.updated_at""",
                    (
                        *key,
                        dimension,
                        automatic,
                        adjustment,
                        override,
                        effective,
                        revision,
                        _dump(evidence),
                        now,
                    ),
                )
                dimensions[dimension] = effective
            affinity = compute_affinity(dimensions)
            conn.execute(
                """UPDATE scoped_soul_relationships
                      SET affinity=?, state=?, dimensions=?, revision=?, updated_at=?
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
                (
                    affinity,
                    attitude_level(affinity),
                    _dump(dimensions),
                    revision,
                    now,
                    *key,
                ),
            )
            rebuilt += 1

    if "user_profiles" in tables:
        for bot_id, group_id, user_id, noise_familiarity in profile_noise:
            row = conn.execute(
                "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                (user_id, group_id, bot_id),
            ).fetchone()
            if row is None:
                continue
            metadata = _json(row[0], {})
            dims = _json(metadata.get("dimensions"), {})
            if not isinstance(dims, dict):
                continue
            before = float(dims.get("familiarity") or 0.0)
            after = clamp_dimension("familiarity", before - float(noise_familiarity))
            if abs(after - before) < 1e-9:
                continue
            dims["familiarity"] = round(after, 2)
            metadata["dimensions"] = dims
            metadata["noise_purged_at"] = now
            metadata["noise_purge_batch"] = BATCH_ID
            conn.execute(
                "UPDATE user_profiles SET metadata=? WHERE user_id=? AND group_id=? AND bot_id=?",
                (_dump(metadata), user_id, group_id, bot_id),
            )
            profile_updates += 1

    return {
        "deleted_scoped_events": deleted_scoped,
        "deleted_legacy_events": deleted_legacy,
        "subjects_rebuilt": rebuilt,
        "subjects_deleted": deleted_subjects,
        "profile_familiarity_updates": profile_updates,
    }


def _capture_profile_noise(conn: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
    tables = _tables(conn)
    if "scoped_soul_relationship_events" not in tables:
        return []
    noise_sql, noise_params = _noise_sql()
    pending: list[tuple[str, str, str, float]] = []
    for bot_id, session_id, subject, total in conn.execute(
        f"""SELECT bot_id, session_id, subject_principal_id, SUM(delta)
              FROM scoped_soul_relationship_events
             WHERE {noise_sql} AND dimension='familiarity'
             GROUP BY bot_id, session_id, subject_principal_id""",
        noise_params,
    ):
        pending.append((
            str(bot_id),
            _session_group_id(str(session_id)),
            _principal_user_id(str(subject)),
            float(total or 0.0),
        ))
    return pending


def run(db_path: Path, *, apply: bool, now: float | None = None) -> dict[str, Any]:
    stamp = float(now if now is not None else time.time())
    conn = _connect(db_path, readonly=not apply)
    try:
        report = plan_purge(conn)
        report["db"] = str(db_path)
        report["mode"] = "apply" if apply else "dry-run"
        report["batch_id"] = BATCH_ID
        if not apply:
            return report
        backup_path = db_path.with_name(f"{db_path.stem}_before_noise_purge_{int(stamp)}.db")
        if backup_path.exists():
            raise SystemExit(f"backup already exists: {backup_path}")
        backup_conn = sqlite3.connect(backup_path.as_posix())
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        report["backup_path"] = str(backup_path)
        try:
            report["applied"] = apply_purge(conn, now=stamp)
            leftover = conn.execute(
                f"SELECT COUNT(*) FROM scoped_soul_relationship_events WHERE {_noise_sql()[0]}",
                _noise_sql()[1],
            ).fetchone()[0] if "scoped_soul_relationship_events" in _tables(conn) else 0
            if leftover:
                raise RuntimeError(f"noise_events_remain:{leftover}")
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
    report = run(db_path, apply=bool(args.apply))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
