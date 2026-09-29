"""角色经历任务接上真实的 durable job 表：排队、认领、执行、取消、续跑（新一代运行只处理剩下的）、任务列表。虚构数据。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from character_experience_support import FakeEmbedding, FakeLLM, bot, make_registry, make_service
from engine.db.connection import ConnectionManager
from engine.db.job_repo import JobRepository
from services.character_experience import EMBED_KIND, EXTRACT_KIND, JOB_KINDS
from services.durable_jobs import DurableJobRunner, DurableJobService

NOVEL = "\n".join([
    "第1章 渡口", "阿棠在渡口等船，等到天黑也没等来，只好在茶棚里借宿。" * 5,
    "第2章 集市", "阿棠第一次逛集市，用攒了半年的铜钱买了一支木簪。" * 5,
    "第3章 回家", "阿棠回家的路上下起了雪，她把木簪揣在怀里捂着。" * 5,
])


class _Coordinator:
    """WriteCoordinator 的最小替身：同一个库文件上的 transaction / read。"""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.conn.execute("PRAGMA foreign_keys=ON")

    async def transaction(self, callback):
        try:
            result = callback(self.conn)
            self.conn.commit()
            return result
        except BaseException:
            self.conn.rollback()
            raise

    async def read(self, callback):
        return callback(self.conn)


def _memory(prompt: str):
    for chapter, text in (("第1章", "那晚我在渡口的茶棚里借宿，听了一夜的江水声。"), ("第2章", "我用攒了半年的铜钱买了一支木簪，高兴得一路小跑。"),
                          ("第3章", "回家路上下雪了，我把木簪揣在怀里，手冻得通红。")):
        if f"来源：{chapter}" in prompt:
            return [{"memory": text, "emotion": "日常", "characters": []}]
    return []


@pytest.fixture
def env(tmp_path):
    path = str(tmp_path / "ce.sqlite3")
    cm = ConnectionManager(path)
    registry = make_registry(cm, bot("oc_t", "阿棠", "10000010"))
    coordinator = _Coordinator(path)
    jobs = DurableJobService(coordinator)
    asyncio.run(jobs.migrate())
    root = tmp_path / "novel_docs"
    root.mkdir()
    (root / "虚构渡口.txt").write_text(NOVEL, encoding="utf-8")
    llm = FakeLLM(default=_memory)
    service = make_service(cm, registry, llm=llm, embedding=FakeEmbedding(), jobs=jobs, source_roots=[str(root)])
    runner = DurableJobRunner(jobs, service.job_handlers(), lease_seconds=60.0,
                              task_name="wave-memory:character-experience-jobs", owner="character-experience-jobs")
    yield {"service": service, "jobs": jobs, "runner": runner, "llm": llm, "path": str(root / "虚构渡口.txt")}
    coordinator.conn.close()
    cm.close()


async def _claim_and_execute(runner):
    run = await runner.service.claim_next(lease_owner=runner.lease_owner, lease_seconds=60.0, kinds=tuple(runner.handlers))
    assert run is not None
    await runner._execute(run)
    return await runner.service.get_run(run.run_id)


def test_runner_only_claims_character_experience_kinds(env):
    assert set(env["runner"].handlers) == set(JOB_KINDS)
    assert env["runner"].task_name == "wave-memory:character-experience-jobs"


def test_extract_job_runs_cancels_and_resumes_on_real_job_tables(env):
    service, jobs, runner, llm = env["service"], env["jobs"], env["runner"], env["llm"]
    body = {"path": env["path"], "name": "阿棠", "concurrency": 1}
    started = asyncio.run(service.start_extraction("oc_t", {**body, "confirm": True, "expected_calls": 3}))
    assert started["job"]["status"] == "queued"

    async def cancel_then_run():
        run = await jobs.claim_next(lease_owner=runner.lease_owner, lease_seconds=60.0, kinds=tuple(runner.handlers))
        await jobs.request_cancel(run.run_id)  # 运行中请求取消：处理完手上这一批就停
        await runner._execute(run)
        return await jobs.get_run(run.run_id)

    cancelled = asyncio.run(cancel_then_run())
    assert cancelled.status == "cancelled"
    assert len(llm.prompts) == 1 and service.repo.counts("oc_t")["total"] == 1
    [listed] = service.list_jobs("oc_t")
    assert listed["status"] == "cancelled" and listed["resumable"] and "scene_keys" not in listed["payload"]

    resumed = asyncio.run(service.resume_job(cancelled.run_id))
    assert resumed["generation"] == 1 and resumed["status"] == "pending"
    finished = asyncio.run(_claim_and_execute(runner))
    assert finished.status == "succeeded" and finished.result["done"] == 3
    assert len(llm.prompts) == 3, "续跑只处理剩下的两个场景"
    assert service.repo.counts("oc_t")["total"] == 3
    assert [job["status"] for job in service.list_jobs("oc_t")] == ["succeeded", "cancelled"]


def test_embed_job_fills_vectors_for_active_rows(env):
    service = env["service"]
    service.import_jsonl("oc_t", '{"memory": "我在渡口等了一整天的船。"}\n{"memory": "集市上的糖人真好看。"}', status="active")
    assert service.repo.counts("oc_t")["active_missing_vectors"] == 2
    envelope = asyncio.run(service.start_embedding("oc_t"))
    assert envelope["job_id"]
    run = asyncio.run(_claim_and_execute(env["runner"]))
    assert run.status == "succeeded" and run.result["kind"] == "embed" and run.result["embedded"] == 2
    assert service.repo.counts("oc_t")["active_missing_vectors"] == 0
    assert asyncio.run(service.start_embedding("oc_t")) is None
    assert EMBED_KIND in JOB_KINDS and EXTRACT_KIND in JOB_KINDS


def test_failed_job_marked_failed_and_resumable(env):
    service, jobs, runner, llm = env["service"], env["jobs"], env["runner"], env["llm"]
    asyncio.run(service.start_extraction("oc_t", {"path": env["path"], "name": "阿棠", "confirm": True, "expected_calls": 3}))
    service._llm_for = lambda bot_id: None  # 运行时模型不见了（被删掉 / 余额用光）
    failed = asyncio.run(_claim_and_execute(runner))
    assert failed.status == "failed" and "model_unavailable" in failed.error_message
    [listed] = service.list_jobs("oc_t")
    assert listed["resumable"]
    service._llm_for = lambda bot_id: llm
    asyncio.run(service.resume_job(failed.run_id))
    assert asyncio.run(_claim_and_execute(runner)).status == "succeeded"
    assert JobRepository.TERMINAL_STATUSES >= {"failed", "succeeded"}
