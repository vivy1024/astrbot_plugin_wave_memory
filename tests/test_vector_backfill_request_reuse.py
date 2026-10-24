"""向量补算链的排队逻辑：幂等键固定，payload 只作审计。

背景（线上事故 2026-10-25）：幂等键 ``maintenance:memory-vector-backfill:v1``
是这条补算链的身份，但 :meth:`JobRepository.create_request` 除了比对
``kind``/``scope``，还会比对 ``payload``。首次创建时 payload 是
``reason=startup_missing_vectors``；此后每次终端超时请求补算都换成
``reason=embedding_terminal_timeout``，于是每次写端请求补算都抛
``JobRequestConflictError`` 被吞成一行 "vector backfill queue request failed"，
补算链再没被真正排过队，缺向量只能等下次重启。

因此这里先按幂等键回读已有请求，命中就复用，只有确实不存在时才创建。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from engine.db.job_repo import JobRequest, JobRequestConflictError
except ImportError:  # pragma: no cover
    from astrbot_plugin_wave_memory.engine.db.job_repo import (
        JobRequest,
        JobRequestConflictError,
    )
from services.vector_backfill_queue import (
    VECTOR_BACKFILL_IDEMPOTENCY_KEY,
    VECTOR_BACKFILL_SCHEDULE_SLOT,
    queue_vector_backfill_run,
)


def _request(reason: str) -> JobRequest:
    return JobRequest(
        request_id="request-vector-backfill",
        idempotency_key=VECTOR_BACKFILL_IDEMPOTENCY_KEY,
        kind="maintenance.vector_backfill.run",
        scope={"kind": "system_maintenance"},
        payload={"kind": "memory_vector_backfill", "reason": reason},
        created_at=1.0,
    )


class _Jobs:
    """复用真实 create_request 的 payload 冲突语义。"""

    def __init__(self, existing: JobRequest | None = None):
        self.existing = existing
        self.create_calls: list[dict] = []
        self.scheduled: list[dict] = []

    async def get_request_by_idempotency_key(self, idempotency_key):
        if self.existing is not None and self.existing.idempotency_key == idempotency_key:
            return self.existing
        return None

    async def create_request(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.existing is not None:
            if (
                self.existing.kind != kwargs["kind"]
                or self.existing.scope != kwargs["scope"]
                or self.existing.payload != kwargs["payload"]
            ):
                raise JobRequestConflictError(
                    f"idempotency key {kwargs['idempotency_key']!r} already belongs "
                    "to a different job request"
                )
            return self.existing
        self.existing = JobRequest(
            request_id="request-vector-backfill",
            idempotency_key=kwargs["idempotency_key"],
            kind=kwargs["kind"],
            scope=kwargs["scope"],
            payload=kwargs["payload"],
            created_at=1.0,
        )
        return self.existing

    async def schedule_run(self, **kwargs):
        self.scheduled.append(kwargs)
        return SimpleNamespace(run_id=f"run-{len(self.scheduled)}")


@pytest.mark.asyncio
async def test_reuses_existing_request_even_with_different_reason():
    """线上原始故障：已存在 startup_missing_vectors，再来 embedding_terminal_timeout。"""

    jobs = _Jobs(existing=_request("startup_missing_vectors"))

    run_id = await queue_vector_backfill_run(
        jobs, reason="embedding_terminal_timeout"
    )

    assert run_id == "run-1"
    assert jobs.create_calls == []  # 不重复创建，也就不会撞 payload 冲突
    assert jobs.scheduled[0]["request_id"] == "request-vector-backfill"
    assert jobs.scheduled[0]["schedule_slot"] == VECTOR_BACKFILL_SCHEDULE_SLOT
    assert jobs.scheduled[0]["reschedule_terminal"] is True
    assert jobs.scheduled[0]["cursor"]["reason"] == "embedding_terminal_timeout"


@pytest.mark.asyncio
async def test_creates_request_once_when_absent_then_reuses():
    jobs = _Jobs()

    await queue_vector_backfill_run(jobs, reason="startup_missing_vectors")
    await queue_vector_backfill_run(jobs, reason="embedding_terminal_timeout")

    assert [call["payload"]["reason"] for call in jobs.create_calls] == [
        "startup_missing_vectors"
    ]
    assert len(jobs.scheduled) == 2
    assert {call["request_id"] for call in jobs.scheduled} == {"request-vector-backfill"}


@pytest.mark.asyncio
async def test_survives_concurrent_first_creation():
    """并发首建：create_request 抛冲突后回读并复用，而不是把异常抛给调用方。"""

    class _RacingJobs(_Jobs):
        def __init__(self):
            super().__init__()
            self.raced = False

        async def create_request(self, **kwargs):
            if not self.raced:
                self.raced = True
                self.existing = _request("startup_missing_vectors")
                raise JobRequestConflictError("idempotency key conflict")
            return await super().create_request(**kwargs)

    jobs = _RacingJobs()

    run_id = await queue_vector_backfill_run(jobs, reason="embedding_terminal_timeout")

    assert run_id == "run-1"
    assert jobs.existing is not None and jobs.existing.payload["reason"] == (
        "startup_missing_vectors"
    )
    assert jobs.scheduled[0]["request_id"] == "request-vector-backfill"


@pytest.mark.asyncio
async def test_missing_reader_falls_back_to_create():
    """没有按幂等键回读能力的老 gateway 仍然能工作（直接创建）。"""

    class _NoReader(_Jobs):
        get_request_by_idempotency_key = None

    jobs = _NoReader()

    await queue_vector_backfill_run(jobs, reason="startup_missing_vectors")

    assert jobs.create_calls[0]["idempotency_key"] == VECTOR_BACKFILL_IDEMPOTENCY_KEY
