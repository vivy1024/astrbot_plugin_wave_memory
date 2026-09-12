from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.outbox_repo import OutboxRepository
from engine.db.memory_repo import MemoryRepo, MemoryRevisionConflict
from engine.db.migrations.scoped_memory_tag_corrections import (
    ensure_scoped_memory_tag_correction_schema_connection,
)
from services.durable_jobs import DurableJobEnvelope, DurableJobService
from services.memory_jobs import MemoryDurableJobHandlers
from services.memory_mutations import (
    MemoryMutationGateway,
    MemoryMutationTarget,
    read_memory_tag_state,
)
from webui.blueprints import memories


def _scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef(
            id="qq:group:group-alpha",
            platform_id="qq",
            kind="group",
            conversation_id="group-alpha",
        ),
        subject_principal_id="qq:user:user-alpha",
    )


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            group_id TEXT NOT NULL,
            content TEXT NOT NULL,
            sender_name TEXT,
            vector BLOB,
            importance REAL NOT NULL DEFAULT 1.0,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            resolution_state TEXT,
            quarantine INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE memory_tags(memory_id INTEGER, tag_id INTEGER);
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            name TEXT NOT NULL,
            tag_type TEXT NOT NULL DEFAULT 'keyword',
            description TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (bot_id, session_id, visibility, name)
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            relevance REAL NOT NULL DEFAULT 1.0,
            created_at REAL NOT NULL,
            PRIMARY KEY (bot_id, session_id, visibility, memory_id, tag_id)
        );
        CREATE TABLE facts(id INTEGER PRIMARY KEY, source_memory_id INTEGER);
        """
    )
    OutboxRepository.migrate(connection)
    ensure_scoped_memory_tag_correction_schema_connection(connection)
    connection.execute(
        """INSERT INTO memories(
               id, group_id, content, vector, importance, bot_id, session_id,
               visibility, resolution_state, quarantine, version
           ) VALUES (1, 'group-alpha', 'before', ?, 1.0, 'bot-alpha',
                     'qq:group:group-alpha', 'group', 'resolved', 0, 7)""",
        (np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),),
    )
    connection.commit()
    return connection


def test_memory_repo_scoped_update_owns_no_commit_and_checks_revision():
    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = MemoryRepo.update_scoped_memory(
            connection,
            scope=_scope(),
            memory_id=1,
            expected_revision=7,
            content="after",
            importance=1.5,
        )
        assert connection.in_transaction is True
        assert result == {"memory_id": 1, "previous_revision": 7, "revision": 8}
        assert connection.execute(
            "SELECT content, importance, vector, version FROM memories WHERE id=1"
        ).fetchone() == ("after", 1.5, None, 8)
        connection.rollback()
        assert connection.execute(
            "SELECT content, importance, version FROM memories WHERE id=1"
        ).fetchone() == ("before", 1.0, 7)
    finally:
        connection.close()


def test_memory_repo_batch_delete_is_all_or_nothing_on_stale_revision():
    connection = _connection()
    try:
        connection.execute(
            """INSERT INTO memories(
                   id, group_id, content, importance, bot_id, session_id,
                   visibility, resolution_state, quarantine, version
               ) VALUES (2, 'group-alpha', 'second', 1.0, 'bot-alpha',
                         'qq:group:group-alpha', 'group', 'resolved', 0, 3)"""
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(MemoryRevisionConflict) as caught:
            MemoryRepo.delete_scoped_memories(
                connection,
                scope=_scope(),
                expected_revisions={1: 7, 2: 99},
            )
        assert caught.value.code == "memory_revision_conflict"
        assert connection.execute("SELECT id FROM memories ORDER BY id").fetchall() == [(1,), (2,)]
        connection.rollback()
    finally:
        connection.close()


class _Coordinator:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._consumer_names = ("memory_index", "runtime_refresh")
        self.actors: list[str | None] = []

    async def transaction(self, callback, *, actor=None):
        self.actors.append(actor)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = callback(self.connection)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    async def read(self, callback):
        return callback(self.connection)


@pytest.mark.asyncio
async def test_memory_mutation_gateway_commits_revision_and_outbox_together():
    connection = _connection()
    coordinator = _Coordinator(connection)
    gateway = MemoryMutationGateway(SimpleNamespace(coordinator=coordinator))
    try:
        result = await gateway.update_memory(
            scope=_scope(),
            target=MemoryMutationTarget(memory_id=1, revision=7),
            content="coordinated",
        )
        assert result.revision == 8
        assert result.operation_id
        operation = connection.execute(
            "SELECT status, command_type FROM write_operations WHERE operation_id=?",
            (result.operation_id,),
        ).fetchone()
        assert operation == ("committed", "memory.webui.update.v1")
        event = connection.execute(
            "SELECT event_type, aggregate_version FROM domain_outbox WHERE operation_id=?",
            (result.operation_id,),
        ).fetchone()
        assert event == ("memory.updated", 8)
        deliveries = connection.execute(
            "SELECT consumer_name, state FROM outbox_deliveries ORDER BY consumer_name"
        ).fetchall()
        assert deliveries == [("memory_index", "pending"), ("runtime_refresh", "pending")]
        assert coordinator.actors == ["webui.memory.update"]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_memory_tag_correction_preserves_automatic_baseline_and_is_undoable():
    connection = _connection()
    coordinator = _Coordinator(connection)
    gateway = MemoryMutationGateway(SimpleNamespace(coordinator=coordinator), clock=_Clock())
    scope = _scope()
    try:
        cursor = connection.execute(
            """INSERT INTO scoped_tags(
                   bot_id, session_id, visibility, name, tag_type, description,
                   confidence, metadata, created_at, updated_at
               ) VALUES (?, ?, ?, '自动标签', 'topic', '', 0.9, '{}', 1, 1)""",
            (scope.bot_id, scope.session.id, scope.visibility),
        )
        connection.execute(
            """INSERT INTO scoped_memory_tags(
                   bot_id, session_id, visibility, memory_id, tag_id,
                   position, relevance, created_at
               ) VALUES (?, ?, ?, 1, ?, 1, 0.9, 1)""",
            (scope.bot_id, scope.session.id, scope.visibility, int(cursor.lastrowid)),
        )
        connection.commit()

        result = await gateway.correct_memory_tags(
            scope=scope,
            target=MemoryMutationTarget(memory_id=1, revision=7),
            operation="add",
            tags=["人工标签"],
            reason="自动提取遗漏关键主题",
        )
        assert result.revision == 8
        assert result.correction is not None
        assert result.correction.status == "active"
        state = read_memory_tag_state(connection, scope=scope, memory_id=1)
        assert [item["name"] for item in state["automatic"]] == ["自动标签"]
        assert [item["name"] for item in state["effective"]] == ["自动标签", "人工标签"]
        assert state["manual"]["reason"] == "自动提取遗漏关键主题"
        correction_row = connection.execute(
            """SELECT operation_id, before_tags_json, after_tags_json, actor, reason,
                      memory_revision_before, memory_revision_after
                 FROM scoped_memory_tag_corrections WHERE correction_id=?""",
            (result.correction.correction_id,),
        ).fetchone()
        assert correction_row == (
            result.operation_id,
            '["自动标签"]',
            '["自动标签","人工标签"]',
            "webui.memory.tags.correct",
            "自动提取遗漏关键主题",
            7,
            8,
        )
        event_payload = connection.execute(
            "SELECT payload_json FROM domain_outbox WHERE operation_id=?",
            (result.operation_id,),
        ).fetchone()[0]
        assert '"reason":"自动提取遗漏关键主题"' in event_payload

        repeated = await gateway.correct_memory_tags(
            scope=scope,
            target=MemoryMutationTarget(memory_id=1, revision=7),
            operation="add",
            tags=["人工标签"],
            reason="自动提取遗漏关键主题",
        )
        assert repeated.operation_id == result.operation_id
        assert connection.execute(
            "SELECT COUNT(*) FROM scoped_memory_tag_corrections"
        ).fetchone() == (1,)

        undone = await gateway.undo_memory_tag_correction(
            scope=scope,
            target=MemoryMutationTarget(memory_id=1, revision=8),
            correction_id=result.correction.correction_id,
            correction_revision=1,
            reason="复核后恢复自动基线",
        )
        assert undone.revision == 9
        assert undone.correction is not None
        assert undone.correction.status == "undone"
        state = read_memory_tag_state(connection, scope=scope, memory_id=1)
        assert state["manual"] is None
        assert [item["name"] for item in state["effective"]] == ["自动标签"]
        assert connection.execute(
            """SELECT status, correction_revision, undone_by_operation_id
                 FROM scoped_memory_tag_corrections WHERE correction_id=?""",
            (result.correction.correction_id,),
        ).fetchone() == ("undone", 2, undone.operation_id)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_memory_tag_correction_rejects_stale_revision_without_partial_writes():
    connection = _connection()
    coordinator = _Coordinator(connection)
    gateway = MemoryMutationGateway(SimpleNamespace(coordinator=coordinator))
    try:
        with pytest.raises(MemoryRevisionConflict):
            await gateway.correct_memory_tags(
                scope=_scope(),
                target=MemoryMutationTarget(memory_id=1, revision=99),
                operation="replace",
                tags=["错误写入"],
                reason="验证 revision 冲突",
            )
        assert connection.execute("SELECT version FROM memories WHERE id=1").fetchone() == (7,)
        assert connection.execute("SELECT COUNT(*) FROM scoped_tags").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM scoped_memory_tag_corrections"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone() == (0,)
    finally:
        connection.close()


class _Clock:
    @staticmethod
    def now():
        return 100.0


@pytest.mark.asyncio
async def test_durable_job_enqueue_returns_stable_envelope_in_one_facade_call():
    connection = _connection()
    coordinator = _Coordinator(connection)
    service = DurableJobService(coordinator, clock=_Clock())
    try:
        envelope = await service.enqueue(
            idempotency_key="memory-reembed:1:7",
            kind="memory.reembed.v1",
            scope=_scope().to_dict(),
            payload={"targets": [{"memory_id": 1, "revision": 7}]},
            schedule_slot="manual:memory-reembed:1:7",
            cursor={"phase": "queued"},
        )
        assert isinstance(envelope, DurableJobEnvelope)
        assert envelope.kind == "memory.reembed.v1"
        assert envelope.status == "queued"
        assert envelope.request_id
        assert envelope.job_id
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_reembed_job_handler_writes_vector_via_revisioned_outbox_gateway():
    connection = _connection()
    coordinator = _Coordinator(connection)
    write_gateway = SimpleNamespace(coordinator=coordinator)

    class Embedding:
        async def get_embedding(self, content):
            assert content == "before"
            return np.asarray([0.25, 0.75], dtype=np.float32)

    class JobService:
        def __init__(self):
            self.progress = []

        async def update_progress(self, run_id, **kwargs):
            self.progress.append((run_id, kwargs))

    service = JobService()
    runner = SimpleNamespace(
        service=service,
        lease_owner="worker-1",
        lease_seconds=60.0,
    )
    run = SimpleNamespace(run_id="run-1", cursor={})
    request = SimpleNamespace(payload={
        "scope": _scope().to_dict(),
        "targets": [{"memory_id": 1, "revision": 7}],
    })
    handlers = MemoryDurableJobHandlers(
        write_gateway=write_gateway,
        db=SimpleNamespace(),
        embedding_service=Embedding(),
        tag_extractor=None,
    )
    try:
        result = await handlers.reembed(run, request, runner)
        row = connection.execute("SELECT vector, version FROM memories WHERE id=1").fetchone()
        assert np.frombuffer(row[0], dtype=np.float32).tolist() == pytest.approx([0.25, 0.75])
        assert row[1] == 8
        assert result == {
            "processed": 1,
            "total": 1,
            "errors": 0,
            "projection": "domain_outbox",
        }
        assert service.progress[0][1]["cursor"] == {"phase": "reembed", "processed": 1}
        assert connection.execute(
            "SELECT event_type FROM domain_outbox ORDER BY created_at DESC LIMIT 1"
        ).fetchone() == ("memory.reembedded",)
    finally:
        connection.close()


@dataclass(frozen=True)
class _Binding:
    scope: RuntimeScope
    revision: int


class _Request:
    def __init__(self, body, *, args=None):
        self._body = body
        self.args = args or {}

    async def get_json(self, *args, **kwargs):
        return self._body


class _Jobs:
    def __init__(self):
        self.calls = []

    async def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return DurableJobEnvelope(
            request_id="request-1",
            job_id="run-1",
            kind=kwargs["kind"],
            status="queued",
        )


@pytest.mark.asyncio
async def test_undo_tag_route_resolves_opaque_correction_ref_without_bare_locator(monkeypatch):
    scope = _scope()
    memory_binding = _Binding(scope=scope, revision=8)
    calls = []

    class Refs:
        def resolve_with_state(self, ref, *, kind, request_scope, locator=None):
            calls.append((ref, kind, request_scope, locator))
            return SimpleNamespace(locator="correction-1", revision=1), "ready"

    class Gateway:
        async def undo_memory_tag_correction(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(operation_id="undo-operation", revision=9)

    row = {
        "id": 1,
        "content": "before",
        "group_id": "group-alpha",
        "bot_id": scope.bot_id,
        "session_id": scope.session.id,
        "visibility": scope.visibility,
        "resolution_state": "resolved",
        "version": 9,
    }
    monkeypatch.setattr(memories, "get_container", lambda: SimpleNamespace(db=SimpleNamespace(conn=object())))
    monkeypatch.setattr(
        memories,
        "request",
        _Request(
            {"correction_ref": "opaque-correction-ref", "reason": "恢复自动基线"},
            args={"ref": "opaque-memory-ref"},
        ),
    )
    monkeypatch.setattr(memories, "jsonify", lambda value: value)
    monkeypatch.setattr(memories, "_resolve_memory_ref", lambda *args, **kwargs: (memory_binding, row))
    monkeypatch.setattr(memories, "_api_composition", lambda: {"object_refs": Refs()})
    monkeypatch.setattr(memories, "_memory_mutation_gateway", lambda container: Gateway())
    monkeypatch.setattr(memories, "_memory_row", lambda conn, memory_id: row)
    monkeypatch.setattr(
        memories,
        "_memory_tag_state_payload",
        lambda conn, **kwargs: {"automatic": [], "effective": [], "manual": None},
    )
    monkeypatch.setattr(memories, "_memory_ref_item", lambda item: item)

    payload = await memories.undo_memory_tag_correction(1)

    assert payload["ok"] is True
    assert payload["operation"]["id"] == "undo-operation"
    assert calls[0] == ("opaque-correction-ref", "memory_tag_correction", scope, None)
    assert calls[1]["correction_id"] == "correction-1"
    assert calls[1]["correction_revision"] == 1
    assert calls[1]["target"] == MemoryMutationTarget(memory_id=1, revision=8)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "body", "kind"),
    [
        ("batch_re_embed", {"refs": [{"id": 1, "ref": "opaque"}]}, "memory.batch.reembed.v1"),
        (
            "batch_extract_tags_for_ids",
            {"refs": [{"id": 1, "ref": "opaque"}], "tag_batch_size": 10},
            "memory.batch.extract_tags.v1",
        ),
    ],
)
async def test_batch_routes_return_durable_json_envelope_not_http_sse(
    monkeypatch, handler_name, body, kind
):
    jobs = _Jobs()
    container = SimpleNamespace(durable_jobs=jobs, db=SimpleNamespace(conn=object()))
    target = MemoryMutationTarget(memory_id=1, revision=7)
    monkeypatch.setattr(memories, "get_container", lambda: container)
    monkeypatch.setattr(memories, "request", _Request(body))
    monkeypatch.setattr(memories, "jsonify", lambda value: value)
    monkeypatch.setattr(
        memories,
        "_resolve_batch_refs",
        lambda conn, refs: (( _scope(), [target]), None),
    )

    payload, status = await getattr(memories, handler_name)()

    assert status == 202
    assert payload["accepted"] is True
    assert payload["operation"] == {"id": "run-1", "kind": kind, "status": "queued"}
    assert "text/event-stream" not in str(payload)
    assert jobs.calls[0]["payload"]["targets"] == [{"memory_id": 1, "revision": 7}]


@pytest.mark.asyncio
async def test_legacy_batch_ids_receive_stable_migration_error(monkeypatch):
    monkeypatch.setattr(
        memories,
        "get_container",
        lambda: SimpleNamespace(db=SimpleNamespace(conn=object()), durable_jobs=_Jobs()),
    )
    monkeypatch.setattr(memories, "request", _Request({"ids": [1]}))
    monkeypatch.setattr(memories, "jsonify", lambda value: value)

    payload, status = await memories.batch_re_embed()

    assert status == 410
    assert payload["error"]["code"] == "memory_object_ref_migration_required"


# ── 回归守卫：WebUI 记忆作业 kind 必须有生产 handler ──
# 这两个 kind 由 webui/blueprints/memories.py 的 re_embed_memory / batch_re_embed 入队；
# 若 main.py 不把 MemoryDurableJobHandlers 合并进 DurableJobRunner，作业会以
# job_handler_missing 直接失败（durable_jobs.py 找不到 handler 即 mark_failed）。


def test_memory_job_kinds_are_registered_in_main_runner():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "main.py"
    text = source.read_text(encoding="utf-8")

    assert "MemoryDurableJobHandlers" in text, "记忆作业 handler 必须接入生产 runner"
    assert "maintenance_handlers.update(" in text


def test_memory_job_kinds_match_webui_enqueue_sites():
    """入队点使用的 kind 必须落在 handler 覆盖范围内，否则作业永远失败。"""
    from pathlib import Path

    memories_src = (
        Path(__file__).resolve().parents[1] / "webui" / "blueprints" / "memories.py"
    ).read_text(encoding="utf-8")
    provided = set(MemoryDurableJobHandlers.REEMBED_KINDS) | {
        MemoryDurableJobHandlers.TAG_KIND
    }

    for kind in MemoryDurableJobHandlers.REEMBED_KINDS:
        assert f'kind="{kind}"' in memories_src, f"{kind} 应仍由记忆端点入队"
    assert provided == {
        "memory.reembed.v1",
        "memory.batch.reembed.v1",
        "memory.batch.extract_tags.v1",
    }
