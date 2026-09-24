"""Frozen production composition port for the system-convergence SQLite probe."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from ...domain.commands import DomainCommand, EntityChange
    from ...engine.write_coordinator import (
        MutationOutcome,
        OutboxEventDraft,
        WriteCoordinator,
    )
    from ..outbox_dispatcher import OutboxDispatcher
    from ..task_supervisor import TaskSupervisor
except ImportError:  # pragma: no cover - repository tests import top-level packages
    from domain.commands import DomainCommand, EntityChange
    from engine.write_coordinator import (
        MutationOutcome,
        OutboxEventDraft,
        WriteCoordinator,
    )
    from services.outbox_dispatcher import OutboxDispatcher
    from services.task_supervisor import TaskSupervisor

_PROBE_COMMAND = "system_convergence.probe.upsert"


def _probe_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    payload = command.payload
    aggregate_id = str(payload["aggregate_id"])
    aggregate_version = int(payload["aggregate_version"])
    value = str(payload["value"])
    if aggregate_version < 1:
        raise ValueError("aggregate_version must be positive")
    connection.execute(
        """INSERT INTO system_convergence_probe_entities(
               aggregate_id, aggregate_version, value, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(aggregate_id) DO UPDATE SET
               aggregate_version=excluded.aggregate_version,
               value=excluded.value,
               updated_at=excluded.updated_at
           WHERE excluded.aggregate_version > system_convergence_probe_entities.aggregate_version""",
        (aggregate_id, aggregate_version, value, now),
    )
    entity = EntityChange(
        aggregate_kind="system_convergence_probe",
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        change_type="upserted",
    )
    event = OutboxEventDraft(
        aggregate_kind=entity.aggregate_kind,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type="system_convergence.probe.upserted",
        payload={"value": value},
    )
    return MutationOutcome(entities=(entity,), events=(event,))


class RuntimePort:
    def __init__(self, database_path: str, *, consumers: Mapping[str, Any], clock: Any) -> None:
        self.database_uri = database_path
        self._clock = clock
        self._supervisor = TaskSupervisor()
        self.coordinator = WriteCoordinator(
            database_path,
            command_handlers={_PROBE_COMMAND: _probe_handler},
            consumer_names=tuple(consumers),
            clock=clock,
        )
        self._dispatcher = OutboxDispatcher(self.coordinator, consumers, clock)
        self._shutdown = False

    async def __aenter__(self) -> "RuntimePort":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.shutdown()

    async def committed_watermark(self) -> int:
        return await self.coordinator.committed_watermark()

    async def drain_to_watermark(self, watermark: int) -> None:
        await self._dispatcher.drain_to_watermark(watermark)

    async def advance_clock_to_next_attempt(self) -> None:
        await self._dispatcher.advance_clock_to_next_attempt()

    async def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        await self.coordinator.close_accepting()
        watermark = await self.coordinator.committed_watermark()
        await self._dispatcher.drain_to_watermark(watermark)
        await self._dispatcher.close()
        await self._supervisor.shutdown()
        await self.coordinator.shutdown()


def create_runtime(
    database_path: str, *, consumers: Mapping[str, Any], clock: Any
) -> RuntimePort:
    return RuntimePort(database_path, consumers=consumers, clock=clock)


def make_probe_command(
    *,
    operation_id: str,
    idempotency_key: str,
    actor: str,
    scope: Any,
    value: str,
    aggregate_id: str,
    aggregate_version: int,
    request_hash: str,
) -> DomainCommand:
    return DomainCommand(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        actor=actor,
        scope=scope,
        command_type=_PROBE_COMMAND,
        payload={
            "value": value,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
        },
        request_hash=request_hash,
    )
