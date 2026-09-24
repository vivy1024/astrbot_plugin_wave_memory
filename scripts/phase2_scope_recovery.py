"""Build, stage, and verify the approved Phase 2 Scope recovery artifact.

This command intentionally has no promotion mode.  It operates on an immutable source
snapshot and writes a separate staged database; production DB/index switching remains a
separate maintenance-window operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.migrations_archive.approved_scope_recovery import (
    apply_approved_scope_recovery,
    create_approved_scope_snapshot,
    build_approved_scope_recovery_plan,
    verify_approved_scope_recovery,
    write_approved_scope_recovery_plan,
)
from services.migrations_archive.approved_scope_recovery_indexes import (
    rebuild_approved_scope_recovery_indexes,
    verify_approved_scope_recovery_indexes,
)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _load_scope_mappings(path: str) -> Any:
    source = Path(path).resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"scope mappings invalid: {source}") from exc
    if isinstance(value, dict):
        value = value.get("scope_mappings")
    if not isinstance(value, list):
        raise SystemExit("scope mappings must be a JSON array or {\"scope_mappings\": [...]} object")
    return value


def _load_memory_index_settings(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    source = Path(path).resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"index settings invalid: {source}") from exc
    if not isinstance(value, dict):
        raise SystemExit("index settings must be a JSON object")
    section = value.get("Memory_Index_Settings", value)
    if not isinstance(section, dict):
        raise SystemExit("Memory_Index_Settings must be a JSON object")
    return section


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="create an immutable single-file SQLite backup")
    snapshot.add_argument("--source-db", required=True, help="live or offline SQLite source; never mutated")
    snapshot.add_argument("--output-snapshot", required=True)

    plan = subparsers.add_parser("plan", help="create a snapshot-bound explicit recovery plan")
    plan.add_argument("--source-snapshot", required=True)
    plan.add_argument("--scope-mappings", required=True, help="JSON mapping file; one group may list multiple Bot Scopes")
    plan.add_argument("--output-plan", required=True)

    stage = subparsers.add_parser(
        "stage",
        help="retain-only: add/update plan targets and audit external projections; never deletes or mutates the source snapshot",
    )
    stage.add_argument("--source-snapshot", required=True)
    stage.add_argument("--plan", required=True)
    stage.add_argument("--output-db", required=True)
    stage.add_argument("--run-dir", required=True)
    stage.add_argument(
        "--confirmation",
        choices=("retain-approved-group-scopes-non-destructive",),
        required=True,
        help="explicitly acknowledge that this retain-only stage has no deletion or production promotion",
    )

    verify = subparsers.add_parser("verify", help="verify one exact staged recovery run")
    verify.add_argument("--database", required=True)
    verify.add_argument("--plan", required=True)
    verify.add_argument("--run-id", required=True)

    rebuild_indexes = subparsers.add_parser(
        "rebuild-indexes",
        help="build new bounded memory.hnsw and tag_catalog.hnsw artifacts for one staged run",
    )
    rebuild_indexes.add_argument("--database", required=True)
    rebuild_indexes.add_argument("--index-directory", required=True, help="must not already exist")
    rebuild_indexes.add_argument("--run-id", required=True)
    rebuild_indexes.add_argument("--dimension", type=int, required=True)
    rebuild_indexes.add_argument("--index-settings", help="plugin config or Memory_Index_Settings JSON")
    rebuild_indexes.add_argument("--confirmation", choices=("rebuild-approved-recovery-indexes",), required=True)

    verify_indexes = subparsers.add_parser("verify-indexes", help="verify exact staged HNSW artifacts")
    verify_indexes.add_argument("--database", required=True)
    verify_indexes.add_argument("--index-directory", required=True)
    verify_indexes.add_argument("--run-id", required=True)
    verify_indexes.add_argument("--dimension", type=int, required=True)
    verify_indexes.add_argument("--index-settings", help="plugin config or Memory_Index_Settings JSON")

    args = parser.parse_args()
    if args.command == "snapshot":
        _print(create_approved_scope_snapshot(args.source_db, args.output_snapshot))
    elif args.command == "plan":
        result = build_approved_scope_recovery_plan(
            args.source_snapshot,
            _load_scope_mappings(args.scope_mappings),
        )
        output = write_approved_scope_recovery_plan(result, args.output_plan)
        _print({"plan_path": str(output), "plan_hash": result["plan_hash"], "summary": result["summary"]})
    elif args.command == "stage":
        _print(
            apply_approved_scope_recovery(
                args.source_snapshot,
                args.plan,
                args.output_db,
                args.run_dir,
                confirmation=args.confirmation,
            )
        )
    elif args.command == "verify":
        _print(verify_approved_scope_recovery(args.database, args.plan, args.run_id))
    elif args.command == "rebuild-indexes":
        _print(
            rebuild_approved_scope_recovery_indexes(
                args.database,
                args.index_directory,
                args.run_id,
                dimension=args.dimension,
                memory_index_settings=_load_memory_index_settings(args.index_settings),
                confirmation=args.confirmation,
            )
        )
    else:
        _print(
            verify_approved_scope_recovery_indexes(
                args.database,
                args.index_directory,
                args.run_id,
                dimension=args.dimension,
                memory_index_settings=_load_memory_index_settings(args.index_settings),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
