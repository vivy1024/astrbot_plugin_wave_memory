"""Single-owner SQLite writer thread and durable command coordinator."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import queue
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

try:
    from ..domain.commands import (
        CommandRejectedError,
        DomainCommand,
        DomainWriteResult,
        EntityChange,
        IdempotencyConflictError,
        OutboxEventRef,
    )
    from ..domain.scope import scope_to_dict
    from .db.outbox_repo import OutboxRepository
    from .writer_lease import WriterLease, WriterLeaseUnavailableError
except ImportError:  # pragma: no cover - repository tests import top-level packages
    from domain.commands import (
        CommandRejectedError,
        DomainCommand,
        DomainWriteResult,
        EntityChange,
        IdempotencyConflictError,
        OutboxEventRef,
    )
    from domain.scope import scope_to_dict
    from engine.db.outbox_repo import OutboxRepository
    from engine.writer_lease import WriterLease, WriterLeaseUnavailableError


@dataclass(frozen=True)
class OutboxEventDraft:
    aggregate_kind: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    payload: Mapping[str, Any]
    payload_version: int = 1


@dataclass(frozen=True)
class MutationOutcome:
    entities: tuple[EntityChange, ...]
    events: tuple[OutboxEventDraft, ...]
    warnings: tuple[str, ...] = ()


CommandHandler = Callable[[sqlite3.Connection, DomainCommand, float], MutationOutcome]


class WriteCoordinator:
    """Owns the only writable connection and serializes all transactional work."""

    def __init__(
        self,
        database_path: str,
        *,
        command_handlers: Mapping[str, CommandHandler],
        consumer_names: tuple[str, ...],
        clock: Any,
        queue_capacity: int = 256,
    ) -> None:
        self.database_path = os.path.abspath(database_path)
        self._state_lock = threading.Lock()
        try:
            self._writer_lease = WriterLease.acquire(self.database_path)
        except WriterLeaseUnavailableError as exc:
            raise CommandRejectedError("writer_lease_unavailable") from exc
        self._handlers = dict(command_handlers)
        self._consumer_names = tuple(sorted(set(consumer_names)))
        self._clock = clock
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_capacity)
        self._accept_lock = threading.Lock()
        self._accepting = True
        self._stopped = False
        self._failure: BaseException | None = None
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(
            target=self._writer_main, name="wave-memory-writer", daemon=True
        )
        self._thread.start()
        try:
            self._ready.result(timeout=30)
        except BaseException:
            # The lease must outlive the bootstrap connection even when migration fails.
            self._thread.join(timeout=30)
            self._release_writer_lease()
            raise

    def _release_writer_lease(self) -> None:
        lease = getattr(self, "_writer_lease", None)
        if lease is not None:
            lease.release()

    @staticmethod
    def _set_result_if_pending(
        future: concurrent.futures.Future[Any], result: Any
    ) -> None:
        """Complete a caller future without letting cancellation kill the writer."""
        if future.done():
            return
        try:
            future.set_result(result)
        except concurrent.futures.InvalidStateError:
            # Cancellation can race with completion after the database work commits.
            pass

    @staticmethod
    def _set_exception_if_pending(
        future: concurrent.futures.Future[Any], exc: BaseException
    ) -> None:
        """Fail a caller future unless it was already cancelled or completed."""
        if future.done():
            return
        try:
            future.set_exception(exc)
        except concurrent.futures.InvalidStateError:
            pass

    def _writer_main(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            parent = os.path.dirname(self.database_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            connection = sqlite3.connect(
                self.database_path, isolation_level=None, timeout=30.0
            )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA synchronous=NORMAL")
            OutboxRepository.migrate(connection)
            if connection.in_transaction:
                raise RuntimeError("bootstrap left an active transaction")
            self._ready.set_result(None)
            while True:
                item = self._queue.get()
                try:
                    if item is None:
                        return
                    function, transactional, future = item
                    if future.cancelled():
                        continue
                    try:
                        if connection.in_transaction:
                            raise RuntimeError("writer connection already in transaction")
                        if transactional:
                            connection.execute("BEGIN IMMEDIATE")
                        result = function(connection)
                        if transactional:
                            connection.commit()
                        if connection.in_transaction:
                            raise RuntimeError("writer transaction did not settle")
                    except BaseException as exc:
                        if connection.in_transaction:
                            try:
                                connection.rollback()
                            except BaseException:
                                pass
                        self._set_exception_if_pending(future, exc)
                    else:
                        self._set_result_if_pending(future, result)
                finally:
                    self._queue.task_done()
        except BaseException as exc:
            with self._accept_lock:
                self._accepting = False
                with self._state_lock:
                    self._failure = exc
                    self._stopped = True
            self._set_exception_if_pending(self._ready, exc)
            logger.exception("[WriteCoordinator] writer thread stopped unexpectedly")
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    self._set_exception_if_pending(item[2], exc)
                self._queue.task_done()
        finally:
            if connection is not None:
                connection.close()

    def _enqueue(
        self,
        function: Callable[[sqlite3.Connection], Any],
        transactional: bool,
        future: concurrent.futures.Future[Any],
    ) -> None:
        # Keep the health check and enqueue on the same side of the shutdown sentinel.
        # Never block a caller (especially AstrBot's asyncio loop) on a full queue.
        with self._state_lock:
            if self._stopped:
                raise RuntimeError("write coordinator is stopped")
            if not self._thread.is_alive():
                self._stopped = True
                raise RuntimeError("write coordinator writer thread is unavailable")
            try:
                self._queue.put_nowait((function, transactional, future))
            except queue.Full as exc:
                raise CommandRejectedError("writer_queue_full") from exc

    async def _dispatch(self, function: Callable[[sqlite3.Connection], Any], transactional: bool) -> Any:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._enqueue(function, transactional, future)
        return await asyncio.wrap_future(future)

    async def transaction(
        self,
        function: Callable[[sqlite3.Connection], Any],
        *,
        actor: str | None = None,
    ) -> Any:
        """Run a serialized transaction; ``actor`` is accepted for caller audit context."""
        del actor
        return await self._dispatch(function, True)

    def transaction_blocking(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        """Submit a short synchronous caller operation to the writer-owned transaction."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("writer thread cannot synchronously dispatch to itself")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        in_event_loop = loop is not None and loop.is_running()

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            self._enqueue(function, True, future)
        except (CommandRejectedError, RuntimeError):
            if not in_event_loop:
                raise
            logger.warning(
                "[WriteCoordinator] synchronous write rejected inside active event loop; "
                "best-effort mutation was skipped.",
                exc_info=True,
            )
            return None

        # A synchronous caller on the asyncio thread may only consume an already-fast
        # result. Slow work remains queued and must never freeze the event loop.
        if in_event_loop:
            try:
                return future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "[WriteCoordinator] transaction_blocking called inside active event loop; "
                    "returning without waiting for the queued mutation."
                )
                return None

        return future.result(timeout=30)

    async def read(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        return await self._dispatch(function, False)

    async def submit(self, command: DomainCommand) -> DomainWriteResult:
        if not isinstance(command, DomainCommand):
            raise TypeError("submit requires DomainCommand")
        future: concurrent.futures.Future[DomainWriteResult] = concurrent.futures.Future()
        with self._accept_lock:
            if not self._accepting or self._stopped:
                raise CommandRejectedError("ingress_closed")
            # Enqueue while holding the ingress lock: close_accepting is therefore a real
            # acceptance fence rather than a check-then-enqueue race.
            self._enqueue(lambda conn: self._execute_command(conn, command), True, future)
        return await asyncio.wrap_future(future)

    def _execute_command(
        self, connection: sqlite3.Connection, command: DomainCommand
    ) -> DomainWriteResult:
        existing = connection.execute(
            """SELECT operation_id, request_hash, result_json, status
               FROM write_operations WHERE idempotency_key=?""",
            (command.idempotency_key,),
        ).fetchone()
        if existing is not None:
            if str(existing[1]) != command.request_hash:
                raise IdempotencyConflictError()
            if existing[3] != "committed" or not existing[2]:
                raise CommandRejectedError("operation_incomplete")
            return self._decode_result(str(existing[2]))
        collision = connection.execute(
            "SELECT 1 FROM write_operations WHERE operation_id=?", (command.operation_id,)
        ).fetchone()
        if collision is not None:
            raise IdempotencyConflictError("operation_id was reused")
        handler = self._handlers.get(command.command_type)
        if handler is None:
            raise CommandRejectedError("unknown_command_type")
        now = float(self._clock.now())
        sequence = OutboxRepository.next_write_sequence(connection)
        connection.execute(
            """INSERT INTO write_operations(
                   operation_id, idempotency_key, request_hash, command_type, scope_json,
                   status, write_sequence, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                command.operation_id, command.idempotency_key, command.request_hash,
                command.command_type,
                json.dumps(scope_to_dict(command.scope), sort_keys=True, separators=(",", ":")),
                sequence, now,
            ),
        )
        outcome = handler(connection, command, now)
        effects: list[OutboxEventRef] = []
        for index, draft in enumerate(outcome.events):
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"wave-memory:{command.operation_id}:{index}"
            ).hex
            connection.execute(
                """INSERT INTO domain_outbox(
                       event_id, operation_id, aggregate_kind, aggregate_id,
                       aggregate_version, event_type, payload_version, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, command.operation_id, draft.aggregate_kind, draft.aggregate_id,
                    draft.aggregate_version, draft.event_type, draft.payload_version,
                    json.dumps(dict(draft.payload), sort_keys=True, separators=(",", ":")), now,
                ),
            )
            OutboxRepository.add_deliveries(
                connection, event_id, self._consumer_names, now
            )
            effects.append(
                OutboxEventRef(
                    event_id=event_id, event_type=draft.event_type,
                    aggregate_kind=draft.aggregate_kind, aggregate_id=draft.aggregate_id,
                    aggregate_version=draft.aggregate_version,
                )
            )
        result = DomainWriteResult(
            operation_id=command.operation_id,
            committed_at=now,
            write_sequence=sequence,
            entities=outcome.entities,
            effects=tuple(effects),
            warnings=outcome.warnings,
        )
        encoded = self._encode_result(result)
        connection.execute(
            """UPDATE write_operations SET status='committed', result_json=?, committed_at=?
               WHERE operation_id=?""",
            (encoded, now, command.operation_id),
        )
        return result

    @staticmethod
    def _encode_result(result: DomainWriteResult) -> str:
        return json.dumps(asdict(result), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode_result(raw: str) -> DomainWriteResult:
        value = json.loads(raw)
        return DomainWriteResult(
            operation_id=value["operation_id"],
            committed_at=float(value["committed_at"]),
            write_sequence=int(value["write_sequence"]),
            entities=tuple(EntityChange(**item) for item in value.get("entities", ())),
            effects=tuple(OutboxEventRef(**item) for item in value.get("effects", ())),
            warnings=tuple(value.get("warnings", ())),
        )

    async def committed_watermark(self) -> int:
        return int(await self.read(OutboxRepository.committed_watermark))

    async def close_accepting(self) -> None:
        with self._accept_lock:
            self._accepting = False

    async def shutdown(self) -> None:
        await self.close_accepting()
        with self._state_lock:
            if not self._stopped:
                self._stopped = True
                self._queue.put(None)
        # Concurrent/repeated shutdown callers all wait for the same writer exit.
        await asyncio.to_thread(self._thread.join, 30)
        if self._thread.is_alive():
            # Never expose the database to a replacement writer while this one lives.
            raise RuntimeError("writer thread did not stop")
        self._release_writer_lease()
