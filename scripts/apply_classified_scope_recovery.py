from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.db.outbox_repo import OutboxRepository
from engine.index_manifest import read_index_manifest, validate_index_manifest
from engine.vector_index import VectorIndex
from services.migrations_archive.scope_recovery_migration import (
    CLASSIFIED_RECOVERY_RULE_VERSION,
    ScopeRecoveryMigrationError,
    apply_classified_scope_recovery,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _quick_check(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()


def _promote(staged: Path, production: Path, backup: Path, confirmation: str) -> dict[str, Any]:
    """Hard-disabled: classified recovery promote implements multi-group fanout.

    Shared memory is now cross-group read authorization + collapse, not a
    production DB swap of 1→N physical copies.
    """
    del staged, production, backup  # arguments retained for CLI compatibility
    if confirmation != "promote-recovered-database":
        raise ScopeRecoveryMigrationError("promotion_confirmation_required")
    raise ScopeRecoveryMigrationError(
        "classified_fanout_promote_forbidden:"
        "use owned-scope recovery / read-share policy; never re-promote classified-scope-recovery/1 fanout"
    )


def _valid_vectors(conn: sqlite3.Connection, query: str, dimension: int) -> tuple[list[int], list[bytes], int]:
    ids: list[int] = []
    vectors: list[bytes] = []
    invalid = 0
    expected_bytes = int(dimension) * 4
    for row_id, vector in conn.execute(query):
        raw = bytes(vector)
        if len(raw) != expected_bytes:
            invalid += 1
            continue
        ids.append(int(row_id))
        vectors.append(raw)
    return ids, vectors, invalid


def _save_index(
    *,
    ids: list[int],
    vectors: list[bytes],
    dimension: int,
    base_path: Path,
    kind: str,
    watermark: int,
) -> dict[str, Any]:
    index = VectorIndex(
        dimension=dimension,
        max_elements=max(len(ids) + 1, 1000),
        index_path=None,
        kind=kind,
    )
    for offset in range(0, len(ids), 5000):
        batch_ids = ids[offset:offset + 5000]
        batch_vectors = np.asarray(
            [np.frombuffer(raw, dtype=np.float32) for raw in vectors[offset:offset + 5000]],
            dtype=np.float32,
        )
        index.add(batch_ids, batch_vectors)
    index.index_path = str(base_path)
    manifest = index.save(db_watermark=int(watermark))
    if manifest is None:
        raise ScopeRecoveryMigrationError(f"{kind}_manifest_not_written")
    validate_index_manifest(
        manifest,
        base_path,
        expected_kind=kind,
        expected_dimension=dimension,
        verify_checksum=True,
    )
    return manifest.to_dict()


def _rebuild_indexes(
    database: Path,
    data_dir: Path,
    dimension: int,
    confirmation: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    if confirmation != "rebuild-recovery-indexes":
        raise ScopeRecoveryMigrationError("index_rebuild_confirmation_required")
    database = database.resolve()
    data_dir = data_dir.resolve()
    if run_id:
        run_conn = sqlite3.connect(database)
        try:
            run = run_conn.execute(
                "SELECT rule_version,status FROM scope_recovery_migrations WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            run_conn.close()
        if run is None:
            raise ScopeRecoveryMigrationError("recovery_run_missing")
        if str(run[0]).startswith("approved-group-scope-recovery/"):
            raise ScopeRecoveryMigrationError("approved_recovery_requires_phase2_index_tool")
        if str(run[1]) != "staged":
            raise ScopeRecoveryMigrationError("recovery_run_not_staged")
    conn = sqlite3.connect(database)
    try:
        memory_ids, memory_vectors, invalid_memory_vectors = _valid_vectors(
            conn,
            """SELECT id,vector FROM memories
                 WHERE vector IS NOT NULL AND resolution_state='resolved'
                   AND COALESCE(quarantine,0)=0
                   AND COALESCE(memory_type,'message') NOT IN ('archived','evicted','deleted')
                 ORDER BY id""",
            dimension,
        )
        tag_ids, tag_vectors, invalid_tag_vectors = _valid_vectors(
            conn,
            """SELECT id,embedding FROM tag_catalog
                 WHERE embedding IS NOT NULL AND status='active' ORDER BY id""",
            dimension,
        )
        watermark = OutboxRepository.committed_watermark(conn)
    finally:
        conn.close()
    memory_manifest = _save_index(
        ids=memory_ids,
        vectors=memory_vectors,
        dimension=dimension,
        base_path=data_dir / "memory.hnsw",
        kind="memory",
        watermark=watermark,
    )
    tag_manifest = _save_index(
        ids=tag_ids,
        vectors=tag_vectors,
        dimension=dimension,
        base_path=data_dir / "tags.hnsw",
        kind="tag",
        watermark=watermark,
    )
    conn = sqlite3.connect(database)
    try:
        if run_id:
            existing = conn.execute(
                "SELECT run_id FROM scope_recovery_migrations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is None:
                raise ScopeRecoveryMigrationError("recovery_run_missing")
            cursor = conn.execute(
                """UPDATE scope_recovery_migrations
                      SET indexes_status='ready:memory_hnsw,tag_catalog_hnsw'
                    WHERE run_id=?""",
                (run_id,),
            )
        else:
            cursor = conn.execute(
                """UPDATE scope_recovery_migrations
                      SET indexes_status='ready:memory_hnsw,tag_catalog_hnsw'
                    WHERE run_id=(
                        SELECT run_id FROM scope_recovery_migrations
                         WHERE rule_version=? ORDER BY created_at DESC LIMIT 1
                    )""",
                (CLASSIFIED_RECOVERY_RULE_VERSION,),
            )
        if cursor.rowcount != 1:
            raise ScopeRecoveryMigrationError("recovery_run_missing")
        conn.commit()
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "memory": memory_manifest,
        "tag": tag_manifest,
        "invalid_memory_vectors": invalid_memory_vectors,
        "invalid_tag_vectors": invalid_tag_vectors,
    }


def _verify(database: Path, data_dir: Path, dimension: int, *, run_id: str | None = None) -> dict[str, Any]:
    database = database.resolve()
    data_dir = data_dir.resolve()
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        if run_id:
            run = conn.execute(
                """SELECT run_id,rule_version,status,indexes_status,created_at,completed_at
                     FROM scope_recovery_migrations WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        else:
            run = conn.execute(
                """SELECT run_id,rule_version,status,indexes_status,created_at,completed_at
                     FROM scope_recovery_migrations WHERE rule_version=?
                     ORDER BY created_at DESC LIMIT 1""",
                (CLASSIFIED_RECOVERY_RULE_VERSION,),
            ).fetchone()
        if run is None:
            raise ScopeRecoveryMigrationError("recovery_run_missing")
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        approved_mapped = 0
        if "scope_recovery_approved_memory_map" in tables:
            approved_mapped = int(conn.execute(
                "SELECT COUNT(*) FROM scope_recovery_approved_memory_map WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0])
        if approved_mapped:
            mapped = approved_mapped
            projected = int(conn.execute(
                """SELECT COUNT(*) FROM scope_recovery_approved_memory_map rm
                     JOIN memories m ON m.id=rm.target_memory_id
                    WHERE rm.run_id=? AND m.resolution_state='resolved'
                      AND COALESCE(m.quarantine,0)=0""",
                (run["run_id"],),
            ).fetchone()[0])
            evicted_reactivated = int(conn.execute(
                """SELECT COUNT(*) FROM scope_recovery_approved_memory_map rm
                     JOIN memories m ON m.id=rm.target_memory_id
                    WHERE rm.run_id=? AND m.memory_type='evicted'""",
                (run["run_id"],),
            ).fetchone()[0])
        else:
            mapped = int(conn.execute(
                "SELECT COUNT(*) FROM scope_recovery_memory_map WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0])
            projected = int(conn.execute(
                """SELECT COUNT(*) FROM memories
                     WHERE provenance LIKE '%classified_legacy_recovery%' AND resolution_state='resolved'
                       AND COALESCE(quarantine,0)=0"""
            ).fetchone()[0])
            evicted_reactivated = int(conn.execute(
                """SELECT COUNT(*) FROM memories
                     WHERE provenance LIKE '%classified_legacy_recovery%'
                       AND memory_type='evicted'"""
            ).fetchone()[0])
        scoped_tag_links = int(conn.execute(
            """SELECT COUNT(*) FROM scope_recovery_items
                 WHERE run_id=? AND source_table='memory_tags' AND disposition='migrated'""",
            (run[0],),
        ).fetchone()[0])
        scoped_facts = int(conn.execute(
            """SELECT COUNT(*) FROM scope_recovery_items
                 WHERE run_id=? AND source_table='facts' AND disposition='migrated'""",
            (run[0],),
        ).fetchone()[0])
        memory_vector_count = int(conn.execute(
            """SELECT COUNT(*) FROM memories
                 WHERE vector IS NOT NULL AND resolution_state='resolved'
                   AND COALESCE(quarantine,0)=0
                   AND COALESCE(memory_type,'message') NOT IN ('archived','evicted','deleted')"""
        ).fetchone()[0])
        tag_vector_count = int(conn.execute(
            "SELECT COUNT(*) FROM tag_catalog WHERE embedding IS NOT NULL AND status='active'"
        ).fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()
    memory_manifest = read_index_manifest(
        data_dir / "memory.hnsw",
        expected_kind="memory",
        expected_dimension=dimension,
        verify_checksum=True,
    )
    tag_manifest = read_index_manifest(
        data_dir / "tags.hnsw",
        expected_kind="tag",
        expected_dimension=dimension,
        verify_checksum=True,
    )
    return {
        "run": {
            "run_id": run["run_id"],
            "rule_version": run["rule_version"],
            "status": run["status"],
            "indexes_status": run["indexes_status"],
            "created_at": run["created_at"],
            "completed_at": run["completed_at"],
        },
        "database": {
            "quick_check": quick_check,
            "mapped_memories": mapped,
            "projected_memories": projected,
            "evicted_reactivated": evicted_reactivated,
            "scoped_tag_links": scoped_tag_links,
            "scoped_facts": scoped_facts,
            "memory_vector_count": memory_vector_count,
            "tag_vector_count": tag_vector_count,
        },
        "memory_manifest": None if memory_manifest is None else memory_manifest.to_dict(),
        "tag_manifest": None if tag_manifest is None else tag_manifest.to_dict(),
        "memory_manifest_matches_db": memory_manifest is not None and memory_manifest.count == memory_vector_count,
        "tag_manifest_matches_db": tag_manifest is not None and tag_manifest.count == tag_vector_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply and verify classified formal Scope recovery")
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage = subparsers.add_parser("stage")
    stage.add_argument("--source-db", required=True)
    stage.add_argument("--classification-report", required=True)
    stage.add_argument("--output-db", required=True)
    stage.add_argument("--run-dir", required=True)
    stage.add_argument("--confirmation", choices=("recover",), required=True)

    promote = subparsers.add_parser("promote")
    promote.add_argument("--staged-db", required=True)
    promote.add_argument("--production-db", required=True)
    promote.add_argument("--backup-db", required=True)
    promote.add_argument("--confirmation", choices=("promote-recovered-database",), required=True)

    rebuild = subparsers.add_parser("rebuild-indexes")
    rebuild.add_argument("--database", required=True)
    rebuild.add_argument("--data-dir", required=True)
    rebuild.add_argument("--dimension", type=int, required=True)
    rebuild.add_argument("--run-id", help="bind index readiness to this exact staged run")
    rebuild.add_argument("--confirmation", choices=("rebuild-recovery-indexes",), required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--database", required=True)
    verify.add_argument("--data-dir", required=True)
    verify.add_argument("--dimension", type=int, required=True)
    verify.add_argument("--run-id", help="verify this exact staged run")

    args = parser.parse_args()
    if args.command == "stage":
        result = apply_classified_scope_recovery(
            args.source_db,
            args.classification_report,
            args.output_db,
            args.run_dir,
            confirmation=args.confirmation,
        )
    elif args.command == "promote":
        result = _promote(Path(args.staged_db), Path(args.production_db), Path(args.backup_db), args.confirmation)
    elif args.command == "rebuild-indexes":
        result = _rebuild_indexes(
            Path(args.database),
            Path(args.data_dir),
            args.dimension,
            args.confirmation,
            run_id=args.run_id,
        )
    else:
        result = _verify(Path(args.database), Path(args.data_dir), args.dimension, run_id=args.run_id)
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
