"""角色经历测试的共用替身：虚构角色、虚构 QQ 号与虚构内容，不含任何真实小说原文或真实群友信息。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import numpy as np

from domain.bot_profile import BotProfile
from engine.db.bot_profile_repo import BotProfileRepo
from engine.db.character_experience_repo import CharacterExperienceRepo
from engine.db.connection import ConnectionManager
from services.bot_registry import BotRegistry
from services.character_experience import CharacterExperienceService

DIM = 8
# 虚构词表：向量前 7 维 = 文本里是否出现第 i 个词，第 8 维 = 一个词都没出现（余弦相似度 ≈ 关键词重合度）
VOCAB = ("小鹿", "柴房", "马车", "书院", "师父", "海边", "灯会")


def vector_for(text: str) -> np.ndarray:
    hits = [1.0 if word in (text or "") else 0.0 for word in VOCAB]
    vec = np.array([*hits, 0.0 if any(hits) else 1.0], dtype=np.float32)
    vec += 0.001  # 不出现零向量
    return vec / np.linalg.norm(vec)


class FakeEmbedding:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def get_embedding(self, text: str):
        self.calls += 1
        if self.fail:
            return None
        return vector_for(text)

    async def get_embeddings(self, texts):
        self.calls += 1
        if self.fail:
            return [None for _ in texts]
        return [vector_for(text) for text in texts]


class FakeLLM:
    """按提示词里的内容返回预设输出；``script`` 是 (关键字, 输出或异常) 列表，按顺序匹配第一条。"""

    def __init__(self, script: list[tuple[str, Any]] | None = None, default: Any = "[]") -> None:
        self.script = list(script or [])
        self.default = default
        self.prompts: list[tuple[str, str]] = []

    async def text_chat(self, *, prompt: str, system_prompt: str | None = None, **_kwargs):
        self.prompts.append((system_prompt or "", prompt))
        output = self.default
        for key, value in self.script:
            if key in prompt:
                output = value
                break
        if isinstance(output, BaseException):
            raise output
        if callable(output):
            output = output(prompt)
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return SimpleNamespace(completion_text=output)


class FakeJobs:
    """DurableJobService 的最小替身：记录 enqueue，可取回 request/run，支持取消与续跑排期。"""

    def __init__(self) -> None:
        self.requests: dict[str, Any] = {}
        self.runs: dict[str, Any] = {}
        self.cancelled: set[str] = set()
        self.progress: list[dict[str, Any]] = []

    async def enqueue(self, *, idempotency_key, kind, scope, payload, schedule_slot, cursor=None, **_kwargs):
        request_id = f"req-{len(self.requests) + 1}"
        run_id = f"run-{len(self.runs) + 1}"
        self.requests[request_id] = SimpleNamespace(request_id=request_id, kind=kind, scope=scope, payload=payload, idempotency_key=idempotency_key)
        self.runs[run_id] = SimpleNamespace(run_id=run_id, request_id=request_id, status="pending", schedule_slot=schedule_slot,
                                            cursor_generation=0, cursor=cursor or {})
        return SimpleNamespace(to_dict=lambda: {"accepted": True, "request_id": request_id, "job_id": run_id, "status": "queued"})

    async def get_run(self, run_id):
        return self.runs.get(run_id)

    async def get_request(self, request_id):
        return self.requests.get(request_id)

    async def schedule_run(self, *, request_id, schedule_slot, cursor_generation=0, cursor=None, reschedule_terminal=False, **_kwargs):
        run_id = f"run-{len(self.runs) + 1}"
        run = SimpleNamespace(run_id=run_id, request_id=request_id, status="pending", schedule_slot=schedule_slot,
                              cursor_generation=cursor_generation + 1, cursor=cursor or {})
        self.runs[run_id] = run
        return run

    async def cancellation_requested(self, run_id):
        return run_id in self.cancelled

    async def update_progress(self, run_id, *, lease_owner, lease_seconds=30.0, progress=None, cursor=None):
        self.progress.append({"run_id": run_id, "progress": progress, "cursor": cursor})
        return self.runs.get(run_id)


class FakeRunner:
    def __init__(self, jobs: FakeJobs) -> None:
        self.service = jobs
        self.lease_owner = "test-runner"
        self.lease_seconds = 60.0


def make_registry(cm: ConnectionManager, *bots: dict[str, Any]) -> BotRegistry:
    registry = BotRegistry(None)
    registry.attach(BotProfileRepo(cm), connection=cm)
    for data in bots:
        registry.save(BotProfile.from_dict(data), expected_version=0, changed_by="test")
    return registry


def bot(db_id: str, name: str, qq_id: str = "", **extra: Any) -> dict[str, Any]:
    return {"db_id": db_id, "name": name, "qq_id": qq_id, **extra}


def make_service(cm: ConnectionManager, registry: BotRegistry, *, llm: Any = None, embedding: Any = None,
                 jobs: Any = None, source_roots=(), lore_db_path: str = "") -> CharacterExperienceService:
    repo = CharacterExperienceRepo(cm)
    return CharacterExperienceService(
        repo=repo,
        registry=registry,
        embedding_service=embedding,
        llm_for=(lambda bot_id: llm) if llm is not None else None,
        jobs=jobs,
        source_roots=source_roots,
        lore_db_path=lore_db_path,
        dimension=DIM,
    )


def job(kind: str, payload: dict[str, Any], request_id: str = "req-test") -> tuple[Any, Any]:
    run = SimpleNamespace(run_id=f"run-{request_id}", request_id=request_id, cursor={})
    request = SimpleNamespace(request_id=request_id, kind=kind, payload=payload)
    return run, request
