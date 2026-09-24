"""Policy-compatible HNSW artifacts for an approved staged Scope recovery run.

The historical recovery CLI built a full-memory index and wrote the Catalog to the
legacy ``tags.hnsw`` file.  This module deliberately mirrors the live runtime instead:
``memory.hnsw`` is bounded by :class:`MemoryIndexPolicy`, while the semantic Catalog is
written to ``tag_catalog.hnsw`` with kind ``tag_catalog``.  All artifacts live in a new,
otherwise empty staging directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import numpy as np

try:
    from .approved_scope_recovery import APPROVED_SCOPE_RECOVERY_RULE_VERSION, ApprovedScopeRecoveryError
    from ..memory_index_policy import (
        MemoryIndexPolicy,
        decode_vector,
        memory_index_policy_from_settings,
        select_hot_memory_candidates,
    )
    from ...engine.db.outbox_repo import OutboxRepository
    from ...engine.index_manifest import read_index_manifest, validate_index_manifest
    from ...engine.vector_index import VectorIndex
except ImportError:  # pragma: no cover - direct repository imports
    from services.migrations_archive.approved_scope_recovery import APPROVED_SCOPE_RECOVERY_RULE_VERSION, ApprovedScopeRecoveryError
    from services.memory_index_policy import (
        MemoryIndexPolicy,
        decode_vector,
        memory_index_policy_from_settings,
        select_hot_memory_candidates,
    )
    from engine.db.outbox_repo import OutboxRepository
    from engine.index_manifest import read_index_manifest, validate_index_manifest
    from engine.vector_index import VectorIndex


_ARTIFACT_NAME = "approved-recovery-index-artifact.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bounded_int(settings: Mapping[str, Any], key: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(float(settings.get(key, default))))
    except (TypeError, ValueError):
        return default


def _settings(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT run_id,rule_version,source_snapshot_hash,plan_hash,status,indexes_status
              FROM scope_recovery_migrations WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ApprovedScopeRecoveryError("approved_index_run_missing")
    if row["rule_version"] != APPROVED_SCOPE_RECOVERY_RULE_VERSION:
        raise ApprovedScopeRecoveryError("approved_index_run_rule_mismatch")
    if row["status"] != "staged":
        raise ApprovedScopeRecoveryError("approved_index_run_not_staged")
    return row


def _watermark(connection: sqlite3.Connection) -> int:
    try:
        return int(OutboxRepository.committed_watermark(connection))
    except sqlite3.Error:
        return 0


def _reserve_empty_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise ApprovedScopeRecoveryError("approved_index_directory_already_exists") from exc


def _save_index(index: VectorIndex, *, path: Path, kind: str, dimension: int, watermark: int) -> dict[str, Any]:
    index.index_path = str(path)
    manifest = index.save(db_watermark=watermark)
    if manifest is None:
        raise ApprovedScopeRecoveryError("approved_index_manifest_missing:" + kind)
    validate_index_manifest(
        manifest,
        path,
        expected_kind=kind,
        expected_dimension=dimension,
        verify_checksum=True,
    )
    return manifest.to_dict()


def _memory_artifact(
    connection: sqlite3.Connection,
    *,
    directory: Path,
    dimension: int,
    policy: MemoryIndexPolicy,
    retention: int,
    watermark: int,
) -> tuple[dict[str, Any], list[int]]:
    candidates = select_hot_memory_candidates(connection, policy, int(dimension))
    index = VectorIndex(
        dimension=int(dimension),
        max_elements=int(policy.max_vectors),
        index_path=None,
        kind="memory",
        allow_resize=False,
        generation_retention=retention,
    )
    ids: list[int] = []
    for offset in range(0, len(candidates), 5000):
        batch = candidates[offset:offset + 5000]
        batch_ids = [int(candidate.memory_id) for candidate in batch if candidate.vector is not None]
        vectors = [candidate.vector for candidate in batch if candidate.vector is not None]
        if not batch_ids:
            continue
        index.add(batch_ids, np.asarray(vectors, dtype=np.float32))
        ids.extend(batch_ids)
    manifest = _save_index(
        index,
        path=directory / "memory.hnsw",
        kind="memory",
        dimension=int(dimension),
        watermark=watermark,
    )
    if len(ids) != len(candidates) or len(ids) > int(policy.max_vectors):
        raise ApprovedScopeRecoveryError("approved_memory_hot_policy_mismatch")
    return manifest, ids


def _tag_catalog_artifact(
    connection: sqlite3.Connection,
    *,
    directory: Path,
    dimension: int,
    max_vectors: int,
    retention: int,
    watermark: int,
) -> tuple[dict[str, Any], list[int], int]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'"
    ).fetchone()
    index = VectorIndex(
        dimension=int(dimension),
        max_elements=max_vectors,
        index_path=None,
        kind="tag_catalog",
        generation_retention=retention,
    )
    ids: list[int] = []
    invalid = 0
    if exists is not None:
        batch_ids: list[int] = []
        vectors: list[np.ndarray] = []
        cursor = connection.execute(
            "SELECT id,embedding FROM tag_catalog WHERE embedding IS NOT NULL AND status='active' ORDER BY id"
        )
        for tag_id, embedding in cursor:
            vector = decode_vector(embedding, int(dimension))
            if vector is None:
                invalid += 1
                continue
            batch_ids.append(int(tag_id))
            vectors.append(vector)
            if len(batch_ids) >= 5000:
                index.add(batch_ids, np.asarray(vectors, dtype=np.float32))
                ids.extend(batch_ids)
                batch_ids, vectors = [], []
        if batch_ids:
            index.add(batch_ids, np.asarray(vectors, dtype=np.float32))
            ids.extend(batch_ids)
    manifest = _save_index(
        index,
        path=directory / "tag_catalog.hnsw",
        kind="tag_catalog",
        dimension=int(dimension),
        watermark=watermark,
    )
    return manifest, ids, invalid


def _write_artifact(path: Path, value: Mapping[str, Any]) -> None:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
            handle.write("\n")
    except FileExistsError as exc:
        raise ApprovedScopeRecoveryError("approved_index_artifact_already_exists") from exc


def rebuild_approved_scope_recovery_indexes(
    database_path: str | os.PathLike[str],
    index_directory: str | os.PathLike[str],
    run_id: str,
    *,
    dimension: int,
    memory_index_settings: Mapping[str, Any] | None = None,
    confirmation: str = "",
) -> dict[str, Any]:
    """Build immutable, runtime-compatible HNSW artifacts for one staged run."""

    if confirmation != "rebuild-approved-recovery-indexes":
        raise ApprovedScopeRecoveryError("approved_index_confirmation_required")
    database = Path(database_path).resolve()
    directory = Path(index_directory).resolve()
    if not database.is_file():
        raise ApprovedScopeRecoveryError("staged_database_missing")
    if int(dimension) <= 0:
        raise ApprovedScopeRecoveryError("approved_index_dimension_invalid")
    _reserve_empty_directory(directory)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        run = _run_row(connection, run_id)
        if _text(run["indexes_status"]) not in {"pending:memory_hnsw,tag_catalog_hnsw", ""}:
            raise ApprovedScopeRecoveryError("approved_index_run_not_pending")
        settings = _settings(memory_index_settings)
        policy = memory_index_policy_from_settings(settings)
        tag_max_vectors = _bounded_int(settings, "tag_index_max_vectors", 40_000, 1)
        # Recovery artifacts must follow the same retention policy as the runtime
        # indexes.  Forcing a minimum of two generations here silently reintroduced
        # a second resident/disk copy after every approved recovery run.
        retention = _bounded_int(settings, "generation_retention", 1, 1)
        watermark = _watermark(connection)
        memory_manifest, memory_ids = _memory_artifact(
            connection,
            directory=directory,
            dimension=int(dimension),
            policy=policy,
            retention=retention,
            watermark=watermark,
        )
        tag_manifest, tag_ids, invalid_tag_vectors = _tag_catalog_artifact(
            connection,
            directory=directory,
            dimension=int(dimension),
            max_vectors=tag_max_vectors,
            retention=retention,
            watermark=watermark,
        )
        artifact = {
            "run_id": run_id,
            "rule_version": run["rule_version"],
            "source_snapshot_hash": run["source_snapshot_hash"],
            "plan_hash": run["plan_hash"],
            "dimension": int(dimension),
            "memory_policy": {
                "max_vectors": policy.max_vectors,
                "per_scope_max_vectors": policy.per_scope_max_vectors,
                "scoped_reserved_vectors": policy.scoped_reserved_vectors,
                "chat_hot_days": policy.chat_hot_days,
                "enforce_scope_hot_quota": policy.enforce_scope_hot_quota,
            },
            "memory_manifest": memory_manifest,
            "tag_catalog_manifest": tag_manifest,
            "memory_ids_hash": _sha256(memory_ids),
            "tag_catalog_ids_hash": _sha256(tag_ids),
            "invalid_tag_vectors": invalid_tag_vectors,
            "db_watermark": watermark,
        }
        _write_artifact(directory / _ARTIFACT_NAME, artifact)
        cursor = connection.execute(
            """UPDATE scope_recovery_migrations
                  SET indexes_status='ready:memory_hnsw,tag_catalog_hnsw'
                WHERE run_id=? AND status='staged'""",
            (run_id,),
        )
        if cursor.rowcount != 1:
            raise ApprovedScopeRecoveryError("approved_index_run_not_staged")
        connection.commit()
        return {**artifact, "index_directory": str(directory)}
    except Exception:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def verify_approved_scope_recovery_indexes(
    database_path: str | os.PathLike[str],
    index_directory: str | os.PathLike[str],
    run_id: str,
    *,
    dimension: int,
    memory_index_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate manifests and current policy membership for one exact staged run."""

    database = Path(database_path).resolve()
    directory = Path(index_directory).resolve()
    if not database.is_file() or not directory.is_dir():
        raise ApprovedScopeRecoveryError("approved_index_artifacts_missing")
    artifact_path = directory / _ARTIFACT_NAME
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovedScopeRecoveryError("approved_index_artifact_invalid") from exc
    if not isinstance(artifact, Mapping):
        raise ApprovedScopeRecoveryError("approved_index_artifact_invalid")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        run = _run_row(connection, run_id)
        if _text(run["indexes_status"]) != "ready:memory_hnsw,tag_catalog_hnsw":
            raise ApprovedScopeRecoveryError("approved_index_run_not_ready")
        for field in ("run_id", "rule_version", "source_snapshot_hash", "plan_hash"):
            if artifact.get(field) != run[field]:
                raise ApprovedScopeRecoveryError("approved_index_artifact_run_mismatch:" + field)
        if int(artifact.get("dimension") or 0) != int(dimension):
            raise ApprovedScopeRecoveryError("approved_index_artifact_dimension_mismatch")
        settings = _settings(memory_index_settings)
        policy = memory_index_policy_from_settings(settings)
        memory_candidates = select_hot_memory_candidates(connection, policy, int(dimension))
        memory_ids = [int(candidate.memory_id) for candidate in memory_candidates]
        tag_ids: list[int] = []
        invalid_tag_vectors = 0
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'").fetchone() is not None:
            for tag_id, embedding in connection.execute(
                "SELECT id,embedding FROM tag_catalog WHERE embedding IS NOT NULL AND status='active' ORDER BY id"
            ):
                if decode_vector(embedding, int(dimension)) is None:
                    invalid_tag_vectors += 1
                else:
                    tag_ids.append(int(tag_id))
        memory_manifest = read_index_manifest(
            directory / "memory.hnsw",
            expected_kind="memory",
            expected_dimension=int(dimension),
            verify_checksum=True,
        )
        tag_manifest = read_index_manifest(
            directory / "tag_catalog.hnsw",
            expected_kind="tag_catalog",
            expected_dimension=int(dimension),
            verify_checksum=True,
        )
        if memory_manifest is None or tag_manifest is None:
            raise ApprovedScopeRecoveryError("approved_index_manifest_missing")
        if (
            memory_manifest.count != len(memory_ids)
            or tag_manifest.count != len(tag_ids)
            or artifact.get("memory_ids_hash") != _sha256(memory_ids)
            or artifact.get("tag_catalog_ids_hash") != _sha256(tag_ids)
            or int(artifact.get("invalid_tag_vectors") or 0) != invalid_tag_vectors
            or int(artifact.get("db_watermark") or -1) != _watermark(connection)
        ):
            raise ApprovedScopeRecoveryError("approved_index_membership_mismatch")
        return {
            "run_id": run_id,
            "indexes_status": run["indexes_status"],
            "memory_manifest": memory_manifest.to_dict(),
            "tag_catalog_manifest": tag_manifest.to_dict(),
            "memory_candidate_count": len(memory_ids),
            "tag_catalog_candidate_count": len(tag_ids),
            "invalid_tag_vectors": invalid_tag_vectors,
        }
    finally:
        connection.close()


__all__ = [
    "rebuild_approved_scope_recovery_indexes",
    "verify_approved_scope_recovery_indexes",
]
