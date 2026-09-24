from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("hnswlib")

from services.migrations_archive.approved_scope_recovery import APPROVED_SCOPE_RECOVERY_RULE_VERSION
from services.migrations_archive.approved_scope_recovery_indexes import (
    rebuild_approved_scope_recovery_indexes,
    verify_approved_scope_recovery_indexes,
)


def _vector(*values: float) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


def test_rebuild_uses_runtime_memory_policy_and_catalog_index_path(tmp_path):
    database = tmp_path / "staged.sqlite3"
    index_dir = tmp_path / "approved-indexes"
    run_id = "approved-group-scope-recovery:test"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, vector BLOB, bot_id TEXT, session_id TEXT,
            visibility TEXT, resolution_state TEXT, quarantine INTEGER, source TEXT,
            memory_type TEXT, importance REAL, access_count INTEGER, timestamp REAL
        );
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE memory_tags (memory_id INTEGER, tag_id INTEGER, relevance REAL);
        CREATE TABLE tag_catalog (id INTEGER PRIMARY KEY, embedding BLOB, status TEXT);
        CREATE TABLE scope_recovery_migrations (
            run_id TEXT PRIMARY KEY, rule_version TEXT, source_snapshot_hash TEXT,
            plan_hash TEXT, target_scopes_json TEXT, status TEXT, indexes_status TEXT,
            created_at REAL, completed_at REAL
        );
        """
    )
    connection.execute(
        """INSERT INTO memories VALUES
           (1,'g1',?,NULL,NULL,NULL,'',0,'chat','message',1,0,9999999999)""",
        (_vector(1, 0, 0, 0),),
    )
    connection.execute("INSERT INTO tags VALUES (1, 'alpha')")
    connection.execute("INSERT INTO memory_tags VALUES (1, 1, 1.0)")
    connection.execute("INSERT INTO tag_catalog VALUES (1, ?, 'active')", (_vector(0, 1, 0, 0),))
    connection.execute(
        """INSERT INTO scope_recovery_migrations VALUES
           (?, ?, 'sha256:snapshot', 'sha256:plan', '[]', 'staged',
            'pending:memory_hnsw,tag_catalog_hnsw', 1, 1)""",
        (run_id, APPROVED_SCOPE_RECOVERY_RULE_VERSION),
    )
    connection.commit()
    connection.close()

    result = rebuild_approved_scope_recovery_indexes(
        database,
        index_dir,
        run_id,
        dimension=4,
        memory_index_settings={"hot_max_vectors": 1, "tag_index_max_vectors": 1},
        confirmation="rebuild-approved-recovery-indexes",
    )

    assert result["memory_manifest"]["kind"] == "memory"
    assert result["memory_manifest"]["count"] == 1
    assert result["tag_catalog_manifest"]["kind"] == "tag_catalog"
    assert result["tag_catalog_manifest"]["count"] == 1
    assert (index_dir / "memory.hnsw.manifest.json").is_file()
    assert (index_dir / "tag_catalog.hnsw.manifest.json").is_file()
    assert not (index_dir / "tags.hnsw.manifest.json").exists()

    verification = verify_approved_scope_recovery_indexes(
        database,
        index_dir,
        run_id,
        dimension=4,
        memory_index_settings={"hot_max_vectors": 1, "tag_index_max_vectors": 1},
    )
    assert verification["memory_candidate_count"] == 1
    assert verification["tag_catalog_candidate_count"] == 1

    check = sqlite3.connect(database)
    try:
        assert check.execute(
            "SELECT indexes_status FROM scope_recovery_migrations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == "ready:memory_hnsw,tag_catalog_hnsw"
    finally:
        check.close()
