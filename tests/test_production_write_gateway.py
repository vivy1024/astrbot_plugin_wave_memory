from __future__ import annotations

import asyncio
import sys
import threading
import types
from types import SimpleNamespace

import numpy as np
import pytest

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from domain.commands import CommandRejectedError
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.write_coordinator import WriteCoordinator
from services.quality_gate import QualityGate
from services.system_convergence_runtime import ProductionWriteGateway


def _scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef("qq:group:group-1", "qq", "group", "group-1"),
    )


def _private_scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="private",
        session=SessionRef("qq:private:user:user-1", "qq", "private", "user:user-1"),
        subject_principal_id="qq:user:user-1",
    )


@pytest.mark.asyncio
async def test_production_gateway_commits_memory_tags_operation_and_outbox_atomically(tmp_path):
    path = str(tmp_path / "production-write.sqlite3")
    db = WaveMemoryDB(path, dimension=4)
    gateway = ProductionWriteGateway(path)
    try:
        quality_gate = QualityGate(repository=gateway.quality_repository, now=lambda: 99.0)
        proposal = quality_gate.propose(
            operation="message.write",
            content="coordinated memory",
            raw_artifact=quality_gate.make_raw_artifact(
                kind="chat_message",
                artifact_id="event-1",
                content="coordinated memory",
                source_scope=_scope(),
            ),
            target_scope=_scope(),
        )
        assert quality_gate.evaluate(proposal).allowed is True

        kwargs = dict(
            scope=_scope(),
            group_id="group-1",
            content="coordinated memory",
            vector=np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32),
            sender_id="qq:user:user-1",
            sender_name="tester",
            timestamp=100.0,
            importance=0.8,
            source="chat",
            provenance={"event_id": "event-1"},
            origin_metadata={"event_id": "event-1"},
            quarantine=False,
            idempotency_hint="event-1",
        )
        first_id = await gateway.append_memory(**kwargs)
        replayed_id = await gateway.append_memory(**kwargs)
        assert replayed_id == first_id

        tag_count = await gateway.apply_tag_extraction(
            scope=_scope(),
            memory_id=first_id,
            tags=[{
                "name": "topic-a",
                "type": "topic",
                "confidence": np.float32(0.9),
                "embedding": [np.float32(0.1), np.float32(0.2)],
            }],
            status="done",
        )
        assert tag_count == 1
        assert db.conn.execute(
            "SELECT bot_id, session_id, visibility, source FROM memories WHERE id=?",
            (first_id,),
        ).fetchone() == ("bot-alpha", "qq:group:group-1", "group", "chat")
        assert db.conn.execute("SELECT COUNT(*) FROM scoped_tags").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM scoped_memory_tags").fetchone()[0] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM write_operations WHERE status='committed'"
        ).fetchone()[0] == 2
        assert db.conn.execute("SELECT COUNT(*) FROM domain_outbox").fetchone()[0] == 2
        tag_event_payload = db.conn.execute(
            "SELECT payload_json FROM domain_outbox WHERE event_type='memory.tags_applied'"
        ).fetchone()[0]
        assert '"bot_id":"bot-alpha"' in tag_event_payload
        assert '"id":"qq:group:group-1"' in tag_event_payload
        assert '"visibility":"group"' in tag_event_payload
        assert db.conn.execute("SELECT COUNT(*) FROM quality_decisions").fetchone()[0] == 1
    finally:
        await gateway.shutdown()
        db.close()


@pytest.mark.asyncio
async def test_private_gateway_write_and_backfill_are_exactly_scoped_without_tags(tmp_path):
    path = str(tmp_path / "private-write.sqlite3")
    db = WaveMemoryDB(path, dimension=4)
    gateway = ProductionWriteGateway(path)
    try:
        scope = _private_scope()
        memory_id = await gateway.append_memory(
            scope=scope,
            group_id="user:user-1",
            content="private canonical memory",
            vector=None,
            sender_id="qq:user:user-1",
            sender_name="tester",
            timestamp=100.0,
            importance=0.8,
            source="chat",
            provenance={"event_id": "private-event"},
            origin_metadata={},
            quarantine=False,
            idempotency_hint="private-event",
        )
        assert db.conn.execute(
            "SELECT bot_id, session_id, visibility, group_id FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone() == ("bot-alpha", "qq:private:user:user-1", "private", "user:user-1")
        assert await gateway.backfill_memory_vector(
            scope=scope,
            memory_id=memory_id,
            vector=np.asarray([1, 2, 3, 4], dtype=np.float32),
            idempotency_hint="private-backfill",
        ) is True
        with pytest.raises(ValueError):
            await gateway.apply_tag_extraction(
                scope=scope, memory_id=memory_id, tags=[{"name": "must-not-write"}], status="done"
            )
        assert db.conn.execute("SELECT COUNT(*) FROM scoped_tags").fetchone()[0] == 0
    finally:
        await gateway.shutdown()
        db.close()


@pytest.mark.asyncio
async def test_vector_backfill_is_scoped_idempotent_and_emits_one_memory_event(tmp_path):
    path = str(tmp_path / "vector-backfill.sqlite3")
    db = WaveMemoryDB(path, dimension=4)
    gateway = ProductionWriteGateway(path)
    try:
        memory_id = await gateway.append_memory(
            scope=_scope(),
            group_id="group-1",
            content="recover this embedding",
            vector=None,
            sender_id="qq:user:user-1",
            sender_name="tester",
            timestamp=100.0,
            importance=0.8,
            source="chat",
            provenance={"event_id": "vectorless-event"},
            origin_metadata={"event_id": "vectorless-event"},
            quarantine=False,
            idempotency_hint="vectorless-event",
        )
        base_version = db.conn.execute(
            "SELECT version FROM memories WHERE id=?", (memory_id,)
        ).fetchone()[0]
        vector = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        assert await gateway.backfill_memory_vector(
            scope=_scope(),
            memory_id=memory_id,
            vector=vector,
            idempotency_hint="first-recovery",
        ) is True
        stored, version = db.conn.execute(
            "SELECT vector, version FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        assert np.frombuffer(stored, dtype=np.float32).tolist() == vector.tolist()
        assert version == base_version + 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM domain_outbox WHERE event_type='memory.vector_backfilled'"
        ).fetchone()[0] == 1

        # A differently-keyed stale retry sees the already-filled vector as a
        # no-op, rather than rewriting it or emitting another projection event.
        assert await gateway.backfill_memory_vector(
            scope=_scope(),
            memory_id=memory_id,
            vector=vector * 2,
            idempotency_hint="stale-retry",
        ) is False
        assert db.conn.execute(
            "SELECT COUNT(*) FROM domain_outbox WHERE event_type='memory.vector_backfilled'"
        ).fetchone()[0] == 1
    finally:
        await gateway.shutdown()
        db.close()


@pytest.mark.asyncio
async def test_production_gateway_enforces_single_process_writer_lease(tmp_path):
    path = str(tmp_path / "writer-lease.sqlite3")
    first = ProductionWriteGateway(path)
    try:
        with pytest.raises(CommandRejectedError) as error:
            ProductionWriteGateway(path)
        assert error.value.reason_code == "writer_lease_unavailable"
    finally:
        await first.shutdown()

    replacement = ProductionWriteGateway(path)
    await replacement.shutdown()


@pytest.mark.asyncio
async def test_transaction_blocking_does_not_deadlock_inside_asyncio_event_loop(tmp_path):
    path = str(tmp_path / "deadlock-guard.sqlite3")
    gateway = ProductionWriteGateway(path)
    try:
        coordinator = gateway.coordinator
        # 在 asyncio loop 中调用 transaction_blocking 会被防死锁守卫捕获，非阻塞返回 None，不会死锁死等 30 秒
        result = coordinator.transaction_blocking(lambda conn: conn.execute("SELECT 1").fetchone())
        assert result is None or result[0] == 1
    finally:
        await gateway.shutdown()


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_stop_writer_thread(tmp_path):
    path = str(tmp_path / "cancelled-caller.sqlite3")
    gateway = ProductionWriteGateway(path)
    started = threading.Event()
    release = threading.Event()

    def slow_transaction(conn):
        started.set()
        assert release.wait(timeout=2.0)
        return conn.execute("SELECT 1").fetchone()

    try:
        task = asyncio.create_task(gateway.coordinator.transaction(slow_transaction))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

        result = await asyncio.wait_for(
            gateway.coordinator.transaction(lambda conn: conn.execute("SELECT 2").fetchone()),
            timeout=2.0,
        )
        assert result[0] == 2
        assert gateway.coordinator._thread.is_alive()
    finally:
        release.set()
        await gateway.shutdown()


@pytest.mark.asyncio
async def test_full_writer_queue_rejects_without_blocking_event_loop(tmp_path):
    coordinator = WriteCoordinator(
        str(tmp_path / "full-queue.sqlite3"),
        command_handlers={},
        consumer_names=(),
        clock=SimpleNamespace(now=lambda: 0.0),
        queue_capacity=1,
    )
    started = threading.Event()
    release = threading.Event()

    def slow_transaction(conn):
        started.set()
        assert release.wait(timeout=2.0)
        return conn.execute("SELECT 1").fetchone()

    try:
        first = asyncio.create_task(coordinator.transaction(slow_transaction))
        assert await asyncio.to_thread(started.wait, 1.0)
        second = asyncio.create_task(
            coordinator.transaction(lambda conn: conn.execute("SELECT 2").fetchone())
        )
        await asyncio.sleep(0)

        with pytest.raises(CommandRejectedError) as error:
            await asyncio.wait_for(
                coordinator.transaction(lambda conn: conn.execute("SELECT 3").fetchone()),
                timeout=0.5,
            )
        assert error.value.reason_code == "writer_queue_full"

        release.set()
        assert (await asyncio.wait_for(first, timeout=2.0))[0] == 1
        assert (await asyncio.wait_for(second, timeout=2.0))[0] == 2
    finally:
        release.set()
        await coordinator.shutdown()
