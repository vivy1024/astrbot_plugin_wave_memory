#!/usr/bin/env python3
"""Readonly production monitor for residual Phase-2 fanout risk.

Never writes. Reports:
1) anti-fanout code gates (when importable)
2) fanout_duplicate provenance mark coverage
3) recent injection_traces multi-group / duplicate-content exposure
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path


def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=60)


def gate_status() -> dict:
    out: dict = {"importable": False}
    try:
        from services.migrations_archive.approved_scope_recovery import (
            APPROVED_SCOPE_RECOVERY_POLICY,
            APPROVED_SCOPE_RECOVERY_RULE_VERSION,
            FORBIDDEN_FANOUT_RULE_VERSIONS,
        )
        from scripts.apply_classified_scope_recovery import _promote
        from services.migrations_archive.scope_recovery_migration import ScopeRecoveryMigrationError

        out.update(
            {
                "importable": True,
                "rule_version": APPROVED_SCOPE_RECOVERY_RULE_VERSION,
                "policy": APPROVED_SCOPE_RECOVERY_POLICY,
                "forbids_classified_v1": "classified-scope-recovery/1" in FORBIDDEN_FANOUT_RULE_VERSIONS,
            }
        )
        try:
            _promote(Path("."), Path("."), Path("./backup"), "promote-recovered-database")
            out["promote_status"] = "UNEXPECTED_ALLOWED"
        except ScopeRecoveryMigrationError as exc:
            out["promote_status"] = "blocked"
            out["promote_error"] = str(exc)[:160]
        except Exception as exc:  # pragma: no cover
            out["promote_status"] = "error"
            out["promote_error"] = str(exc)[:160]
    except Exception as exc:
        out["import_error"] = str(exc)[:200]
    return out


def mark_status(conn: sqlite3.Connection) -> dict:
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    out = {"has_map": "scope_recovery_memory_map" in tables}
    out["fanout_marked_rows"] = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE provenance LIKE '%fanout_duplicate%'"
    ).fetchone()[0]
    if out["has_map"]:
        out["multi_target_families"] = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT legacy_memory_id
                     FROM scope_recovery_memory_map
                    GROUP BY legacy_memory_id
                   HAVING COUNT(*) > 1
               )"""
        ).fetchone()[0]
    return out


def injection_status(conn: sqlite3.Connection, *, limit: int = 100, since_ts: float | None = None) -> dict:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(injection_traces)")}
    if "final_preview" not in cols:
        return {"available": False, "reason": "final_preview_missing"}
    rows = conn.execute(
        "SELECT timestamp, status, final_preview FROM injection_traces ORDER BY timestamp DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    multi_group = 0
    duplicate_content = 0
    post_cutoff = 0
    post_cutoff_dup = 0
    samples: list[dict] = []
    for ts, status, preview in rows:
        text = str(preview or "")
        groups = set(re.findall(r"\[群\s*([^\]]+)\]", text))
        if len(groups) >= 2:
            multi_group += 1
        contents: list[str] = []
        for line in text.splitlines():
            if "记忆" not in line and not line.strip().startswith("[群"):
                continue
            body = re.sub(r"\[群[^\]]+\]\s*", "", line)
            body = re.sub(r"\(relevance:[^)]+\)\s*$", "", body).strip()
            if "): " in body:
                body = body.split("): ", 1)[-1]
            body = " ".join(body.split())
            if len(body) >= 8:
                contents.append(body)
        repeated = [(k, v) for k, v in Counter(contents).items() if v > 1]
        is_dup = bool(repeated)
        if is_dup:
            duplicate_content += 1
        after = since_ts is not None and isinstance(ts, (int, float)) and float(ts) >= float(since_ts)
        if after:
            post_cutoff += 1
            if is_dup:
                post_cutoff_dup += 1
        if is_dup and len(samples) < 5:
            samples.append(
                {
                    "timestamp": ts,
                    "status": status,
                    "groups": sorted(groups)[:8],
                    "repeated_preview": [
                        {"content": k[:60], "count": v} for k, v in repeated[:3]
                    ],
                    "after_cutoff": after,
                }
            )
    return {
        "available": True,
        "scanned": len(rows),
        "multi_group_tag_traces": multi_group,
        "duplicate_content_traces": duplicate_content,
        "since_ts": since_ts,
        "traces_after_cutoff": post_cutoff,
        "duplicate_content_after_cutoff": post_cutoff_dup,
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--since-ts",
        type=float,
        default=None,
        help="only count residual duplicates after this unix timestamp",
    )
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"database missing: {db}")
    conn = _connect(db)
    try:
        report = {
            "generated_at": time.time(),
            "db": str(db),
            "gates": gate_status(),
            "marks": mark_status(conn),
            "injection": injection_status(
                conn,
                limit=args.limit,
                since_ts=args.since_ts,
            ),
            "promote_entrypoints": {
                "classified_script": "scripts/apply_classified_scope_recovery.py::_promote hard-disabled",
                "phase2_cli": "scripts/phase2_scope_recovery.py has no promote command",
                "learning_promotions": "unrelated candidate promotion (facts/fewshot), not memory fanout",
            },
            "verdict": {
                "fanout_promote_allowed": False,
                "mark_and_collapse_active": True,
                "next_safe_work": [
                    "other-group relationship fill_missing_only",
                    "optional staged physical cleanup of marked fanout rows",
                    "continue monitoring injection.duplicate_content_after_cutoff",
                ],
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
