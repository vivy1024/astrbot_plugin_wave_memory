"""向量补算链的排队逻辑：幂等键固定，payload 只作审计。

背景（线上事故 2026-10-25）：幂等键 ``maintenance:memory-vector-backfill:v1``
是这条补算链的身份，但 :meth:`JobRepository.create_request` 除了比对
``kind``/``scope``，还会比对 ``payload``。首次创建时 payload 是
``reason=startup_missing_vectors``；此后每次终端超时请求补算都换成
``reason=embedding_terminal_timeout``，于是每次写端请求补算都抛
``JobRequestConflictError``，补算链再没被真正排过队，缺向量只能等下次重启。
因此这里先按幂等键回读已有请求，命中就复用，只有在确实不存在时才创建。

放在 services 层而不是 maintenance mixin 里，是因为维护模块在导入时依赖
AstrBot 运行时，独立可测的排队逻辑不该跟着一起被绑住。
"""

from __future__ import annotations

from typing import Any

try:
    from ..engine.db.job_repo import JobRequest, JobRequestConflictError
except ImportError:  # pragma: no cover - focused tests import top-level packages
    from engine.db.job_repo import JobRequest, JobRequestConflictError

VECTOR_BACKFILL_IDEMPOTENCY_KEY = "maintenance:memory-vector-backfill:v1"
VECTOR_BACKFILL_KIND = "maintenance.vector_backfill.run"
VECTOR_BACKFILL_SCOPE = {"kind": "system_maintenance"}
VECTOR_BACKFILL_SCHEDULE_SLOT = "memory-vector-backfill"


async def _existing_request(jobs: Any, idempotency_key: str) -> JobRequest | None:
    getter = getattr(jobs, "get_request_by_idempotency_key", None)
    if getter is None:
        return None
    return await getter(idempotency_key)


async def queue_vector_backfill_run(jobs: Any, *, reason: str) -> str:
    """Create-or-reuse the vector backfill request and schedule its next run."""

    request = await _existing_request(jobs, VECTOR_BACKFILL_IDEMPOTENCY_KEY)
    if request is None:
        try:
            request = await jobs.create_request(
                idempotency_key=VECTOR_BACKFILL_IDEMPOTENCY_KEY,
                kind=VECTOR_BACKFILL_KIND,
                scope=dict(VECTOR_BACKFILL_SCOPE),
                payload={"kind": "memory_vector_backfill", "reason": str(reason)},
            )
        except JobRequestConflictError:
            # 并发首建：另一个协程已经建好同一幂等键的请求，回读复用。
            request = await _existing_request(jobs, VECTOR_BACKFILL_IDEMPOTENCY_KEY)
            if request is None:
                raise
    run = await jobs.schedule_run(
        request_id=request.request_id,
        schedule_slot=VECTOR_BACKFILL_SCHEDULE_SLOT,
        cursor_generation=0,
        cursor={"phase": "queued", "after_id": 0, "reason": str(reason)},
        # 已经跑完的一轮可以在之后又出现缺向量时重启；进行中的那一轮直接复用。
        reschedule_terminal=True,
    )
    return run.run_id
