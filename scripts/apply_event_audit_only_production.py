#!/usr/bin/env python3
"""Apply event_audit_only history into production under explicit confirmation.

Safety:
- confirmation must be import-event-audit-only
- only INSERT OR IGNORE into scoped_soul_relationship_legacy_events
- aborts if formal relationship fingerprint changes
- never touches affinity / values / live relationship_events
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from services.migrations_archive.legacy_relationship_migration import (
    CONFIRMATION as STAGE_CONFIRMATION,
    stage,
    _ensure_audit_tables,
    _formal_fingerprint,
    _target_scopes,
)

PROD_CONFIRMATION = "import-event-audit-only"

SCOPES = [
    {
        "bot_id": "yushu",
        "group_id": "398291136",
        "session_id": "羽书:group:398291136",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "576588284",
        "session_id": "羽书:group:576588284",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "1151238916",
        "session_id": "羽书:group:1151238916",
        "visibility": "group",
    },
    {
        "bot_id": "baizz",
        "group_id": "398291136",
        "session_id": "白真真:group:398291136",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "150727649",
        "session_id": "羽书:group:150727649",
        "visibility": "group",
    },
    {
        "bot_id": "baizz",
        "group_id": "150727649",
        "session_id": "白真真:group:150727649",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "871953949",
        "session_id": "羽书:group:871953949",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "28781957",
        "session_id": "羽书:group:28781957",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "1018722649",
        "session_id": "羽书:group:1018722649",
        "visibility": "group",
    },
    {
        "bot_id": "yushu",
        "group_id": "286691404",
        "session_id": "羽书:group:286691404",
        "visibility": "group",
    },
]

COLS = [
    "legacy_event_id",
    "scope_key",
    "bot_id",
    "session_id",
    "visibility",
    "group_id",
    "subject_principal_id",
    "event_type",
    "dimension",
    "delta",
    "reason",
    "occurred_at",
    "source_episode_id",
    "source_memory_id",
    "source_hash",
    "event_hash",
    "run_id",
    "created_at",
]


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    prod = Path("/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db")
    out_dir = Path(
        "/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/backups/"
        "relationship_event_audit_only_prod_apply"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = out_dir / "wave_memory.event-audit-only.sqlite3"
    if staged.exists():
        staged.unlink()

    scopes = _target_scopes(SCOPES)
    source_hash = _file_hash(prod)

    # Pre formal snapshot from production
    pre_conn = sqlite3.connect(f"file:{prod.as_posix()}?mode=ro", uri=True, timeout=120)
    pre_conn.row_factory = sqlite3.Row
    pre_fp = _formal_fingerprint(pre_conn, scopes)
    pre_aff = pre_conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(affinity),0) FROM scoped_soul_relationships"
    ).fetchone()
    pre_conn.close()

    t0 = time.time()
    stage_report = stage(
        source_db_path=prod,
        output_db_path=staged,
        run_dir=out_dir / "run",
        target_scopes=SCOPES,
        expected_source_hash=source_hash,
        confirmation=STAGE_CONFIRMATION,
        mode="event_audit_only",
    )
    stage_s = round(time.time() - t0, 1)
    if stage_report.get("formal_fingerprint_before") != stage_report.get(
        "formal_fingerprint_after"
    ):
        raise SystemExit("staged formal fingerprint changed; abort production apply")

    # Import audit rows into production
    t1 = time.time()
    prod_conn = sqlite3.connect(prod.as_posix(), timeout=600)
    prod_conn.execute("PRAGMA busy_timeout=120000")
    prod_conn.execute("PRAGMA journal_mode=WAL")
    try:
        before_fp = _formal_fingerprint(prod_conn, scopes)
        _ensure_audit_tables(prod_conn)
        before_audit = prod_conn.execute(
            "SELECT COUNT(*) FROM scoped_soul_relationship_legacy_events"
        ).fetchone()[0]

        stg = sqlite3.connect(f"file:{staged.as_posix()}?mode=ro", uri=True, timeout=600)
        try:
            wanted = {(s["bot_id"], s["session_id"]) for s in scopes}
            rows = stg.execute(
                f"SELECT {','.join(COLS)} FROM scoped_soul_relationship_legacy_events"
            ).fetchall()
            payload = [tuple(r) for r in rows if (r[2], r[3]) in wanted]
            # batch insert
            inserted = 0
            batch = 2000
            sql = (
                f"INSERT OR IGNORE INTO scoped_soul_relationship_legacy_events("
                f"{','.join(COLS)}) VALUES ({','.join('?' for _ in COLS)})"
            )
            for i in range(0, len(payload), batch):
                cur = prod_conn.executemany(sql, payload[i : i + batch])
                inserted += int(cur.rowcount or 0)
        finally:
            stg.close()

        after_fp = _formal_fingerprint(prod_conn, scopes)
        if before_fp != after_fp:
            prod_conn.rollback()
            raise SystemExit("aborted: formal fingerprint changed during production import")

        # also ensure global formal counts/sum unchanged for all relationships
        after_aff = prod_conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(affinity),0) FROM scoped_soul_relationships"
        ).fetchone()
        if list(after_aff) != list(pre_aff):
            prod_conn.rollback()
            raise SystemExit(
                f"aborted: formal affinity totals changed {list(pre_aff)} -> {list(after_aff)}"
            )

        prod_conn.commit()
        after_audit = prod_conn.execute(
            "SELECT COUNT(*) FROM scoped_soul_relationship_legacy_events"
        ).fetchone()[0]
        by_scope = prod_conn.execute(
            """SELECT bot_id, session_id, COUNT(*) n, COUNT(DISTINCT subject_principal_id) subjects
                 FROM scoped_soul_relationship_legacy_events
                GROUP BY bot_id, session_id
                ORDER BY n DESC"""
        ).fetchall()
    except Exception:
        prod_conn.rollback()
        raise
    finally:
        prod_conn.close()

    result = {
        "confirmation": PROD_CONFIRMATION,
        "authorized": True,
        "stage_seconds": stage_s,
        "apply_seconds": round(time.time() - t1, 1),
        "stage_event_result": stage_report.get("event_result"),
        "stage_profile_result": stage_report.get("profile_result"),
        "attempted_rows": len(payload),
        "sqlite_rowcount_sum": inserted,
        "audit_before": before_audit,
        "audit_after": after_audit,
        "audit_net_increase": after_audit - before_audit,
        "formal_fingerprint_equal": True,
        "formal_count_sum_before": list(pre_aff),
        "formal_count_sum_after": list(after_aff),
        "by_scope": [
            {"bot_id": r[0], "session_id": r[1], "rows": r[2], "subjects": r[3]}
            for r in by_scope
        ],
        "staged_path": str(staged),
        "phase2_promote": False,
        "fanout_cutover": False,
    }
    report_path = out_dir / "production_apply_report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
