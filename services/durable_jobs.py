"""Coordinator-facing service facade for the durable job repository."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

try:
    from ..engine.db.job_repo import JobLeaseLostError, JobRepository, JobRequest, JobRun
except ImportError:  # pragma: no cover - focused tests import top-level packages
    from engine.db.job_repo import JobLeaseLostError, JobRepository, JobRequest, JobRun

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DurableJobEnvelope:
    """Stable HTTP-facing identity for one accepted durable job run."""

    request_id: str
    job_id: str
    kind: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": True,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "status": self.status,
            "operation": {
                "id": self.job_id,
                "kind": self.kind,
                "status": self.status,
            },
            "revision": None,
        }


class _SystemClock:
    @staticmethod
    def now() -> float:
        return time.time()


class DurableJobService:
    """Run durable job mutations through an injected transaction coordinator.

    The coordinator must expose async ``transaction(callback)`` and ``read(callback)``
    methods, matching ``WriteCoordinator`` without taking ownership of its lifecycle.
    """

    def __init__(self, coordinator: Any, *, clock: Any | None = None) -> None:
        self._coordinator = coordinator
        self._clock = clock or _SystemClock()

    def _now(self) -> float:
        return float(self._clock.now())

    async def migrate(self) -> None:
        await self._coordinator.transaction(JobRepository.migrate)

    async def create_request(
        self,
        *,
        idempotency_key: str,
        kind: str,
        scope: Any,
        payload: Any,
        request_id: str | None = None,
    ) -> JobRequest:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.create_request(
                connection,
                request_id=request_id,
                idempotency_key=idempotency_key,
                kind=kind,
                scope=scope,
                payload=payload,
                created_at=now,
            )
        )

    submit_request = create_request

    async def enqueue(
        self,
        *,
        idempotency_key: str,
        kind: str,
        scope: Any,
        payload: Any,
        schedule_slot: str,
        cursor_generation: int = 0,
        cursor: Any | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
        reschedule_terminal: bool = False,
    ) -> DurableJobEnvelope:
        """Create the immutable request and first run in one writer transaction."""
        now = self._now()

        def transaction(connection):
            request_row = JobRepository.create_request(
                connection,
                request_id=request_id,
                idempotency_key=idempotency_key,
                kind=kind,
                scope=scope,
                payload=payload,
                created_at=now,
            )
            run = JobRepository.schedule_run(
                connection,
                request_id=request_row.request_id,
                schedule_slot=schedule_slot,
                cursor_generation=cursor_generation,
                cursor=cursor,
                run_id=run_id,
                created_at=now,
                reschedule_terminal=reschedule_terminal,
            )
            status = "queued" if run.status == JobRepository.PENDING else run.status
            return DurableJobEnvelope(
                request_id=request_row.request_id,
                job_id=run.run_id,
                kind=request_row.kind,
                status=status,
            )

        return await self._coordinator.transaction(transaction)

    async def schedule_run(
        self,
        *,
        request_id: str,
        schedule_slot: str,
        cursor_generation: int = 0,
        cursor: Any | None = None,
        run_id: str | None = None,
        reschedule_terminal: bool = False,
    ) -> JobRun:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.schedule_run(
                connection,
                request_id=request_id,
                schedule_slot=schedule_slot,
                cursor_generation=cursor_generation,
                cursor=cursor,
                run_id=run_id,
                created_at=now,
                reschedule_terminal=reschedule_terminal,
            )
        )

    async def claim_next(
        self,
        *,
        lease_owner: str,
        lease_seconds: float = 30.0,
        kinds: Iterable[str] | None = None,
        excluded_run_ids: Iterable[str] = (),
    ) -> JobRun | None:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.claim_next(
                connection,
                now=now,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                kinds=kinds,
                excluded_run_ids=excluded_run_ids,
            )
        )

    async def update_progress(
        self,
        run_id: str,
        *,
        lease_owner: str,
        progress: Any | None = None,
        cursor: Any | None = None,
        lease_seconds: float = 30.0,
    ) -> JobRun:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.update_progress(
                connection,
                run_id,
                lease_owner=lease_owner,
                now=now,
                progress=progress,
                cursor=cursor,
                lease_seconds=lease_seconds,
            )
        )

    async def request_cancel(self, run_id: str) -> JobRun | None:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.request_cancel(connection, run_id, now=now)
        )

    cancel = request_cancel

    async def cancellation_requested(self, run_id: str) -> bool:
        return await self._coordinator.read(
            lambda connection: JobRepository.cancellation_requested(connection, run_id)
        )

    async def mark_cancelled(self, run_id: str, *, lease_owner: str | None = None) -> JobRun:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.mark_cancelled(
                connection, run_id, lease_owner=lease_owner, now=now
            )
        )

    async def mark_succeeded(
        self,
        run_id: str,
        *,
        lease_owner: str,
        result: Any | None = None,
        progress: Any | None = None,
        cursor: Any | None = None,
    ) -> JobRun:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.mark_succeeded(
                connection,
                run_id,
                lease_owner=lease_owner,
                now=now,
                result=result,
                progress=progress,
                cursor=cursor,
            )
        )

    async def mark_failed(
        self,
        run_id: str,
        *,
        lease_owner: str,
        error_code: str,
        error_message: str,
        progress: Any | None = None,
        cursor: Any | None = None,
    ) -> JobRun:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.mark_failed(
                connection,
                run_id,
                lease_owner=lease_owner,
                now=now,
                error_code=error_code,
                error_message=error_message,
                progress=progress,
                cursor=cursor,
            )
        )

    async def release_for_retry(
        self,
        run_id: str,
        *,
        lease_owner: str,
        reason: str = "worker_stopped",
    ) -> JobRun:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.release_for_retry(
                connection,
                run_id,
                lease_owner=lease_owner,
                now=now,
                reason=reason,
            )
        )

    async def recover_expired_leases(self) -> int:
        now = self._now()
        return await self._coordinator.transaction(
            lambda connection: JobRepository.recover_expired_leases(connection, now=now)
        )

    async def get_request(self, request_id: str) -> JobRequest | None:
        return await self._coordinator.read(
            lambda connection: JobRepository.get_request(connection, request_id)
        )

    async def get_request_by_idempotency_key(
        self, idempotency_key: str
    ) -> JobRequest | None:
        return await self._coordinator.read(
            lambda connection: JobRepository.get_request_by_idempotency_key(
                connection, idempotency_key
            )
        )

    async def get_run(self, run_id: str) -> JobRun | None:
        return await self._coordinator.read(
            lambda connection: JobRepository.get_run(connection, run_id)
        )

    async def list_runs(
        self,
        *,
        request_id: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> tuple[JobRun, ...]:
        return await self._coordinator.read(
            lambda connection: JobRepository.list_runs(
                connection,
                request_id=request_id,
                statuses=statuses,
                limit=limit,
            )
        )


class DurableJobRunner:
    """Lease and execute registered durable jobs under TaskSupervisor ownership."""

    def __init__(
        self,
        service: DurableJobService,
        handlers: Mapping[str, Any],
        *,
        poll_interval: float = 1.0,
        lease_seconds: float = 60.0,
        task_name: str = "wave-memory:durable-job-runner",
        owner: str = "durable-jobs",
    ) -> None:
        self.service = service
        self.handlers = dict(handlers)
        self.poll_interval = max(0.05, float(poll_interval))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.lease_owner = f"job-runner-{uuid.uuid4().hex}"
        # 多个执行器共用同一张任务表：各自只认领自己登记的 kind，任务名 / owner 在 TaskSupervisor 里必须唯一。
        self.task_name = str(task_name)
        self.owner = str(owner)
        self._running = False
        self._task = None

    def start(self, supervisor=None):
        if self._running:
            return self._task
        self._running = True
        if supervisor is None:
            self._task = asyncio.create_task(self._loop())
        else:
            self._task = supervisor.start(
                self.task_name,
                self._loop(),
                owner=self.owner,
            )
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            await self.service.recover_expired_leases()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("durable job lease recovery failed; polling will continue")

        while self._running:
            try:
                run = await self.service.claim_next(
                    lease_owner=self.lease_owner,
                    lease_seconds=self.lease_seconds,
                    kinds=tuple(self.handlers),
                )
                if run is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                await self._execute(run)
            except asyncio.CancelledError:
                raise
            except JobLeaseLostError as exc:
                # Expected when AstrBot restarts mid-rebuild: the old runner dies,
                # lease recovery requeues, and a new owner continues. Do not dump
                # a full traceback as if the control plane crashed.
                logger.warning(
                    "durable job lease lost (will recover/retry): %s",
                    exc,
                )
                if self._running:
                    await asyncio.sleep(self.poll_interval)
            except Exception:
                # A stale lease or one failed terminal mark belongs to that run; it
                # must not take down the process-wide durable polling loop.
                logger.exception("durable job iteration failed; polling will continue")
                if self._running:
                    await asyncio.sleep(self.poll_interval)

    async def _execute(self, run: JobRun) -> None:
        request = await self.service.get_request(run.request_id)
        if request is None:
            await self.service.mark_failed(
                run.run_id,
                lease_owner=self.lease_owner,
                error_code="job_request_missing",
                error_message="durable job request no longer exists",
            )
            return
        handler = self.handlers.get(request.kind)
        if handler is None:
            await self.service.mark_failed(
                run.run_id,
                lease_owner=self.lease_owner,
                error_code="job_handler_missing",
                error_message=f"no handler registered for {request.kind}",
            )
            return
        try:
            result = handler(run, request, self)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            try:
                await asyncio.shield(
                    self.service.release_for_retry(
                        run.run_id,
                        lease_owner=self.lease_owner,
                        reason="runner_shutdown",
                    )
                )
            except Exception:
                # If the coordinator is already unavailable, lease recovery on the
                # next startup remains the authoritative fallback.
                pass
            raise
        except Exception as exc:
            await self.service.mark_failed(
                run.run_id,
                lease_owner=self.lease_owner,
                error_code="job_execution_failed",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        else:
            if await self.service.cancellation_requested(run.run_id):
                await self.service.mark_cancelled(
                    run.run_id,
                    lease_owner=self.lease_owner,
                )
                return
            await self.service.mark_succeeded(
                run.run_id,
                lease_owner=self.lease_owner,
                result={} if result is None else result,
                progress={"phase": "verified", "completed": True},
            )


DurableJobs = DurableJobService

__all__ = ["DurableJobEnvelope", "DurableJobService", "DurableJobRunner", "DurableJobs"]
