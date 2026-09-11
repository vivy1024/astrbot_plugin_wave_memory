from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_main_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY,
                content TEXT,
                vector BLOB
            );
            CREATE TABLE memory_vectors (
                memory_id INTEGER PRIMARY KEY,
                vector BLOB NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );
            CREATE VIRTUAL TABLE fts_memories USING fts5(content);
            CREATE TRIGGER fts_memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO fts_memories(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER fts_memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO fts_memories(fts_memories, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;
            CREATE TRIGGER fts_memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO fts_memories(fts_memories, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO fts_memories(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TABLE write_operations (
                operation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                write_sequence INTEGER NOT NULL
            );
            CREATE TABLE domain_outbox (
                event_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                aggregate_kind TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE outbox_deliveries (
                event_id TEXT NOT NULL,
                consumer_name TEXT NOT NULL,
                state TEXT NOT NULL,
                processed_at REAL,
                PRIMARY KEY(event_id, consumer_name)
            );
            CREATE TABLE derived_projection_state (
                consumer_name TEXT NOT NULL,
                aggregate_kind TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                applied_version INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(consumer_name, aggregate_kind, aggregate_id)
            );
            CREATE TABLE background_job_runs (
                run_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                cursor_generation INTEGER NOT NULL,
                error_code TEXT,
                updated_at REAL NOT NULL
            );
            """
        )
        connection.execute("DROP TRIGGER fts_memories_ai")
        connection.executemany(
            "INSERT INTO memories(id, content, vector) VALUES (?, ?, ?)",
            ((1, "first", b"1234"), (2, "second", b"5678")),
        )
        connection.execute("INSERT INTO fts_memories(rowid, content) VALUES (1, 'first')")
        connection.executemany(
            "INSERT INTO memory_vectors(memory_id, vector) VALUES (?, ?)",
            ((1, b"1234"), (99, b"orphan")),
        )
        connection.execute(
            "INSERT INTO write_operations(operation_id, status, write_sequence) VALUES ('op-1', 'committed', 1)"
        )
        connection.execute(
            """INSERT INTO domain_outbox(
                   event_id, operation_id, aggregate_kind, aggregate_id, aggregate_version, created_at
               ) VALUES ('event-1', 'op-1', 'memory', '1', 2, 100.0)"""
        )
        connection.executemany(
            "INSERT INTO outbox_deliveries(event_id, consumer_name, state, processed_at) VALUES (?, ?, ?, NULL)",
            (("event-1", "memory_index", "processing"), ("event-1", "tag_index", "pending")),
        )
        connection.execute(
            """INSERT INTO derived_projection_state(
                   consumer_name, aggregate_kind, aggregate_id, applied_version, generation, updated_at
               ) VALUES ('memory_index', 'memory', '1', 1, 1, 90.0)"""
        )
        connection.executemany(
            """INSERT INTO background_job_runs(
                   run_id, request_id, status, attempt, cursor_generation, error_code, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ("run-active", "request-1", "running", 1, 0, None, 101.0),
                ("run-failed", "request-2", "failed", 2, 1, "rebuild_failed", 102.0),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _create_book_lore_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for table in ("book_entities", "book_relations", "book_communities", "book_notes"):
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        connection.execute("INSERT INTO book_entities(id) VALUES (1)")
        connection.commit()
    finally:
        connection.close()


def _create_manifest(
    index_path: Path,
    *,
    kind: str = "memory",
    watermark: int = 1,
) -> Path:
    from engine.index_manifest import (
        IndexManifest,
        checksum_file,
        generation_path,
        manifest_path,
    )

    generation_file = generation_path(index_path, 1)
    generation_file.write_bytes(b"immutable-index-generation")
    payload = IndexManifest(
        kind=kind,
        generation=1,
        dimension=3,
        db_watermark=watermark,
        count=1,
        checksum=checksum_file(generation_file),
        created_at="2026-07-13T00:00:00Z",
    )
    manifest_path(index_path).write_text(
        json.dumps(payload.to_dict(), sort_keys=True),
        encoding="utf-8",
    )
    return generation_file


def test_diagnostics_collects_readonly_evidence_and_distinct_health(tmp_path):
    from services.diagnostics import DiagnosticsService, HEALTH_VALUES, IndexSource

    database_path = tmp_path / "wave_memory.db"
    book_lore_path = tmp_path / "book_lore.db"
    memory_index_path = tmp_path / "memory.hnsw"
    tag_index_path = tmp_path / "tags.hnsw"
    _create_main_database(database_path)
    _create_book_lore_database(book_lore_path)
    _create_manifest(memory_index_path)
    tag_index_path.write_bytes(b"legacy-index-without-manifest")
    before_hash = _sha256(database_path)

    service = DiagnosticsService(
        database_path=database_path,
        memory_index=IndexSource(memory_index_path, "memory", dimension=3, runtime_count=1),
        tag_index=IndexSource(tag_index_path, "tags", dimension=3, runtime_count=0),
        book_lore_path=book_lore_path,
        clock=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    payload = service.collect()

    assert _sha256(database_path) == before_hash
    assert payload["health"] == "drift"
    assert payload["source"] == "wave_memory_readonly_diagnostics"
    assert payload["checked_at"] == "2026-07-13T00:00:00Z"
    assert payload["evidence"]["read_only"] is True
    by_name = {item["name"]: item for item in payload["checks"]}
    assert set(by_name) == {
        "fts",
        "memory_vectors",
        "legacy_scope_debt",
        "outbox_consumer_lag",
        "job_runs",
        "derived_projection",
        "memory_manifest",
        "tag_manifest",
        "memory_index_shadow",
        "tag_index_shadow",
        "book_lore_source",
        "process_memory",
    }
    assert by_name["legacy_scope_debt"]["health"] in HEALTH_VALUES
    assert "legacy_null_scope" in by_name["legacy_scope_debt"]["evidence"]
    assert all(item["health"] in HEALTH_VALUES for item in by_name.values())
    assert all(item["source"] and item["checked_at"] and "evidence" in item for item in by_name.values())
    assert by_name["fts"]["health"] == "drift"
    assert by_name["fts"]["evidence"]["missing_rows"] == 1
    assert by_name["memory_vectors"]["health"] == "drift"
    assert by_name["memory_vectors"]["evidence"]["orphan_count"] == 1
    assert by_name["outbox_consumer_lag"]["health"] == "drift"
    assert by_name["outbox_consumer_lag"]["evidence"]["total_lag"] == 2
    assert by_name["job_runs"]["health"] == "drift"
    assert by_name["job_runs"]["evidence"]["failed_count"] == 1
    assert by_name["derived_projection"]["health"] == "drift"
    assert by_name["derived_projection"]["evidence"]["lagged_count"] == 2
    assert by_name["memory_manifest"]["health"] == "healthy"
    assert by_name["memory_manifest"]["evidence"]["generation"] == 1
    assert by_name["memory_manifest"]["evidence"]["checksum_verified"] is True
    assert by_name["memory_manifest"]["evidence"]["watermark_relation"] == "equal"
    assert by_name["tag_manifest"]["health"] == "drift"
    assert by_name["tag_manifest"]["evidence"]["reason"] == "generation_manifest_missing"
    assert by_name["book_lore_source"]["health"] == "healthy"
    assert by_name["book_lore_source"]["evidence"]["scope"] == "catalog"


@pytest.mark.parametrize(
    ("manifest_watermark", "relation", "health", "reason"),
    (
        (0, "lag", "drift", "db_watermark_lag"),
        (1, "equal", "healthy", None),
        (2, "ahead", "drift", "db_watermark_ahead"),
    ),
)
def test_manifest_watermark_is_compared_to_committed_write_sequence(
    tmp_path,
    manifest_watermark,
    relation,
    health,
    reason,
):
    from services.diagnostics import DiagnosticsService, IndexSource

    database_path = tmp_path / "watermark.db"
    index_path = tmp_path / "memory.hnsw"
    _create_main_database(database_path)
    _create_manifest(index_path, watermark=manifest_watermark)

    payload = DiagnosticsService(
        database_path=database_path,
        memory_index=IndexSource(index_path, "memory", dimension=3, runtime_count=1),
    ).collect()
    check = {item["name"]: item for item in payload["checks"]}["memory_manifest"]

    assert check["health"] == health
    assert check["evidence"]["watermark_relation"] == relation
    assert check["evidence"]["committed_write_sequence_watermark"] == 1
    assert check["evidence"]["watermark_delta"] == manifest_watermark - 1
    if reason is None:
        assert check["evidence"]["drift_reasons"] == []
    else:
        assert reason in check["evidence"]["drift_reasons"]


@pytest.mark.parametrize(("manifest_kind", "source_kind"), (("tag", "tags"), ("tags", "tag")))
def test_tag_manifest_accepts_singular_and_plural_kind_aliases(
    tmp_path,
    manifest_kind,
    source_kind,
):
    from services.diagnostics import DiagnosticsService, IndexSource

    index_path = tmp_path / f"{manifest_kind}.hnsw"
    _create_manifest(index_path, kind=manifest_kind, watermark=0)
    payload = DiagnosticsService(
        tag_index=IndexSource(index_path, source_kind, dimension=3, runtime_count=1),
    ).collect()
    check = {item["name"]: item for item in payload["checks"]}["tag_manifest"]

    assert check["health"] == "healthy"
    assert check["evidence"]["kind"] == manifest_kind
    assert check["evidence"]["checksum_verified"] is True


def test_manifest_checksum_is_verified_by_default(tmp_path):
    from services.diagnostics import DiagnosticsService, IndexSource

    index_path = tmp_path / "memory.hnsw"
    generation_file = _create_manifest(index_path, watermark=0)
    generation_file.write_bytes(b"tampered-generation")

    payload = DiagnosticsService(
        memory_index=IndexSource(index_path, "memory", dimension=3, runtime_count=1),
    ).collect()
    check = {item["name"]: item for item in payload["checks"]}["memory_manifest"]

    assert check["health"] == "drift"
    assert check["evidence"]["reason"] == "invalid_generation_manifest"
    assert "checksum mismatch" in check["evidence"]["error"]


def test_diagnostics_treats_retired_memory_vectors_table_as_healthy(tmp_path):
    from services.diagnostics import DiagnosticsService

    database_path = tmp_path / "canonical-vectors.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, vector BLOB)")
        connection.execute(
            "INSERT INTO memories(id, vector) VALUES (?, ?)",
            (1, np.asarray([1.0, 0.0], dtype=np.float32).tobytes()),
        )
        connection.commit()
    finally:
        connection.close()

    payload = DiagnosticsService(database_path=database_path).collect()
    vector_check = {item["name"]: item for item in payload["checks"]}["memory_vectors"]

    assert vector_check["health"] == "healthy"
    assert vector_check["evidence"]["storage_mode"] == "memories.vector"
    assert vector_check["evidence"]["legacy_table_present"] is False
    assert vector_check["evidence"]["canonical_vector_count"] == 1


def test_diagnostics_shadow_probe_detects_id_and_recall_drift(tmp_path):
    from services.diagnostics import DiagnosticsService, IndexSource

    database_path = tmp_path / "shadow.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY, vector BLOB)"
        )
        connection.executemany(
            "INSERT INTO memories(id, vector) VALUES (?, ?)",
            [
                (1, np.asarray([1.0, 0.0], dtype=np.float32).tobytes()),
                (2, np.asarray([0.0, 1.0], dtype=np.float32).tobytes()),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    def stale_search(vector, k):
        return [(1, 0.0)]

    payload = DiagnosticsService(
        database_path=database_path,
        memory_index=IndexSource(
            None,
            "memory",
            dimension=2,
            runtime_count=2,
            runtime_ids=(1, 99),
            search=stale_search,
        ),
    ).collect()
    shadow = {item["name"]: item for item in payload["checks"]}[
        "memory_index_shadow"
    ]

    assert shadow["health"] == "drift"
    assert shadow["evidence"]["missing_ids"] == [2]
    assert shadow["evidence"]["orphan_ids"] == [99]
    assert shadow["evidence"]["sample_recall"] == 0.5


def test_diagnostics_without_sources_is_not_configured_and_creates_nothing(tmp_path, monkeypatch):
    from services.diagnostics import DiagnosticsService

    monkeypatch.chdir(tmp_path)
    payload = DiagnosticsService(
        clock=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc)
    ).collect()

    assert payload["health"] == "not_configured"
    assert {item["health"] for item in payload["checks"]} == {"not_configured"}
    assert not (tmp_path / "wave_memory.db").exists()
    assert not (tmp_path / "book_lore.db").exists()


def test_diagnostics_distinguishes_empty_repairing_and_probe_error(tmp_path):
    from services.diagnostics import DiagnosticsService, IndexSource

    database_path = tmp_path / "active.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE write_operations (
                operation_id TEXT PRIMARY KEY, status TEXT, write_sequence INTEGER
            );
            CREATE TABLE domain_outbox (
                event_id TEXT PRIMARY KEY, operation_id TEXT, aggregate_kind TEXT,
                aggregate_id TEXT, aggregate_version INTEGER, created_at REAL
            );
            CREATE TABLE outbox_deliveries (
                event_id TEXT, consumer_name TEXT, state TEXT, processed_at REAL
            );
            CREATE TABLE derived_projection_state (
                consumer_name TEXT, aggregate_kind TEXT, aggregate_id TEXT,
                applied_version INTEGER, generation INTEGER, updated_at REAL
            );
            CREATE TABLE background_job_runs (
                run_id TEXT PRIMARY KEY, request_id TEXT, status TEXT, attempt INTEGER,
                cursor_generation INTEGER, error_code TEXT, updated_at REAL
            );
            INSERT INTO write_operations VALUES ('op', 'committed', 1);
            INSERT INTO domain_outbox VALUES ('event', 'op', 'memory', '1', 1, 10.0);
            INSERT INTO outbox_deliveries VALUES ('event', 'memory_index', 'processing', NULL);
            INSERT INTO background_job_runs VALUES ('run', 'request', 'running', 1, 0, NULL, 11.0);
            """
        )
        connection.commit()
    finally:
        connection.close()

    payload = DiagnosticsService(
        database_path=database_path,
        memory_index=IndexSource(tmp_path / "memory.hnsw", "memory"),
        tag_index=IndexSource(None, "tags"),
        book_lore_path=tmp_path / "missing-book-lore.db",
    ).collect()
    by_name = {item["name"]: item for item in payload["checks"]}

    assert by_name["outbox_consumer_lag"]["health"] == "repairing"
    assert by_name["job_runs"]["health"] == "repairing"
    assert by_name["derived_projection"]["health"] == "repairing"
    assert by_name["memory_manifest"]["health"] == "empty"
    assert by_name["tag_manifest"]["health"] == "not_configured"
    assert by_name["book_lore_source"]["health"] == "probe_error"


def test_diagnostics_api_is_safe_with_empty_container(tmp_path, monkeypatch):
    quart = __import__("quart")
    if not hasattr(quart.Quart, "test_client"):
        pytest.skip("full-suite Quart test double pollution; API test passes in isolation")

    from webui.app import create_app
    from webui.container import ServiceContainer

    ServiceContainer.reset()
    monkeypatch.chdir(tmp_path)
    app = create_app()

    async def exercise():
        response = await app.test_client().get("/api/diagnostics/indexes")
        return response.status_code, await response.get_json()

    try:
        status, payload = asyncio.run(exercise())
    finally:
        ServiceContainer.reset()

    assert status == 200
    assert payload["health"] == "not_configured"
    assert payload["evidence"]["read_only"] is True
    assert not (tmp_path / "wave_memory.db").exists()
    assert not (tmp_path / "book_lore.db").exists()


def test_blueprint_container_resolution_uses_only_live_paths(tmp_path):
    from webui.blueprints.diagnostics import build_diagnostics_service

    database_path = tmp_path / "configured.db"
    database_path.write_bytes(b"not-opened-by-construction")
    memory_index = SimpleNamespace(
        index_path=str(tmp_path / "memory.hnsw"),
        kind="memory",
        dimension=1024,
        count=7,
    )
    service = build_diagnostics_service(SimpleNamespace(
        db=SimpleNamespace(db_path=str(database_path)),
        memory_index=memory_index,
        tag_index=SimpleNamespace(
            index_path=str(tmp_path / "tags.hnsw"),
            kind="tags",
            dimension=1024,
            count=3,
        ),
        plugin_config={},
        learning_source_registry=None,
    ))

    assert service.database_path == database_path.resolve()
    assert service.memory_index.runtime_count == 7
    assert Path(service.tag_index.path) == (tmp_path / "tags.hnsw").resolve()
    assert service.tag_index.kind == "tag"
    assert service.tag_index.runtime_count == 3
    assert service.book_lore_path is None


def test_diagnostics_optimizations_handle_large_dataset_simulation(tmp_path):
    import sqlite3
    from services.diagnostics import DiagnosticsService

    db_file = tmp_path / "test_opt.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            vector BLOB
        );
        CREATE TABLE write_operations (
            operation_id TEXT PRIMARY KEY,
            write_sequence INTEGER UNIQUE,
            status TEXT NOT NULL
        );
        CREATE TABLE domain_outbox (
            event_id TEXT PRIMARY KEY,
            operation_id TEXT REFERENCES write_operations(operation_id),
            aggregate_kind TEXT,
            aggregate_id TEXT,
            aggregate_version INTEGER,
            created_at REAL
        );
        CREATE TABLE outbox_deliveries (
            event_id TEXT REFERENCES domain_outbox(event_id),
            consumer_name TEXT NOT NULL,
            state TEXT NOT NULL,
            processed_at REAL,
            PRIMARY KEY(event_id, consumer_name)
        );
        CREATE TABLE derived_projection_state (
            consumer_name TEXT NOT NULL,
            aggregate_kind TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            applied_version INTEGER NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY(consumer_name, aggregate_kind, aggregate_id)
        );
    """)

    # 模拟数据
    conn.execute("INSERT INTO write_operations VALUES ('op1', 1, 'committed')")
    conn.execute("INSERT INTO domain_outbox VALUES ('ev1', 'op1', 'memory', '1', 1, 100.0)")
    conn.execute("INSERT INTO outbox_deliveries VALUES ('ev1', 'consumer_a', 'completed', 101.0)")
    conn.execute("INSERT INTO derived_projection_state VALUES ('consumer_a', 'memory', '1', 1, 1, 101.0)")

    # 插入 10 条向量数据
    for i in range(10):
        conn.execute("INSERT INTO memories VALUES (?, 'test', ?)", (i + 1, b"x" * 128))
    conn.commit()

    # 运行三项被优化的探针
    status, evidence = DiagnosticsService._probe_outbox_consumer_lag(conn)
    assert status == "healthy"
    assert evidence["total_lag"] == 0
    assert len(evidence["consumers"]) == 1
    assert evidence["consumers"][0]["applied_write_sequence"] == 1

    status, evidence = DiagnosticsService._probe_derived_projection(conn)
    assert status == "healthy"
    assert evidence["lagged_count"] == 0

    status, evidence = DiagnosticsService._probe_memory_vectors(conn)
    assert status == "healthy"
    assert evidence["canonical_vector_count"] == 10
    assert evidence["blob_sizes"][0]["bytes"] == 128
    conn.close()
