"""后台服务热插拔：停止、再启动（任务名加代次）、未创建与核心服务保护。"""

from __future__ import annotations

import asyncio

import pytest

from services.service_registry import ServiceRegistry, ServiceRegistryError, ServiceSpec
from services.task_supervisor import TaskSupervisor


class _LoopService:
    def __init__(self):
        self._running = False
        self._task = None
        self.stops = 0

    def start(self, supervisor=None):
        self._running = True
        self._task = supervisor.start("wave-memory:dream", self._loop(), owner="dream")

    def stop(self):
        self._running = False
        self.stops += 1
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while self._running:
            await asyncio.sleep(0.01)


def test_stop_then_start_again_under_same_supervisor():
    async def scenario():
        supervisor = TaskSupervisor()
        service = _LoopService()
        service.start(supervisor)  # 插件启动时的第一次
        registry = ServiceRegistry(supervisor)
        registry.register(ServiceSpec("dream", "做梦", lambda: service, owner="dream"))
        assert registry.describe("dream")["running"] is True
        stopped = await registry.stop("dream")
        assert stopped["running"] is False and service.stops == 1
        await asyncio.sleep(0.02)
        started = await registry.start("dream")
        assert started["running"] is True and started["generation"] == 1
        restarted = await registry.restart("dream")
        assert restarted["generation"] == 2
        names = [t["name"] for t in restarted["tasks"]]
        assert "wave-memory:dream#g2" in names
        assert [h["action"] for h in restarted["history"]] == ["stop", "start", "stop", "start"]
        service.stop()
        await asyncio.sleep(0.02)

    asyncio.run(scenario())


def test_missing_and_core_services_are_refused():
    async def scenario():
        registry = ServiceRegistry(TaskSupervisor())
        registry.register(ServiceSpec("eviction", "记忆淘汰", lambda: None, owner="eviction"))
        registry.register(ServiceSpec("writer", "写入器", lambda: _LoopService(), owner="writer", stoppable=False))
        with pytest.raises(ServiceRegistryError) as missing:
            await registry.start("eviction")
        assert missing.value.code == "service_not_created"
        with pytest.raises(ServiceRegistryError) as core:
            await registry.stop("writer")
        assert core.value.code == "service_not_stoppable"
        assert registry.describe("eviction")["created"] is False

    asyncio.run(scenario())
