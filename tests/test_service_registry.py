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


def test_reconfigure_rebuilds_creates_and_disables_by_config():
    async def scenario():
        supervisor = TaskSupervisor()
        holder = {"instance": None}
        config = {"enabled": True, "interval": 6}
        built = []

        def factory():
            holder["instance"] = None
            if config["enabled"]:
                holder["instance"] = _LoopService()
                built.append(config["interval"])
            return holder["instance"]

        registry = ServiceRegistry(supervisor)
        registry.register(ServiceSpec(
            "dream", "做梦", lambda: holder["instance"], owner="dream",
            factory=factory, config_keys=("Lifecycle_Settings.enable_dream", "Eviction_Settings."),
        ))
        registry.register(ServiceSpec("writer", "写入器", lambda: None, owner="w"))
        assert registry.affected_by(["Lifecycle_Settings.enable_dream", "Other.x"]) == {"dream": ["Lifecycle_Settings.enable_dream"]}
        assert registry.affected_by(["Eviction_Settings.interval_hours"]) == {"dream": ["Eviction_Settings.interval_hours"]}
        assert registry.affected_by(["Eviction_SettingsX.a"]) == {}
        assert registry.rebuildable() == {"dream": ("Lifecycle_Settings.enable_dream", "Eviction_Settings.")}

        created = await registry.reconfigure("dream")
        assert created["action"] == "created" and created["running"] is True
        first = holder["instance"]
        config["interval"] = 3
        rebuilt = await registry.reconfigure("dream")
        assert rebuilt["action"] == "rebuilt" and first.stops == 1 and holder["instance"] is not first
        assert built == [6, 3]
        config["enabled"] = False
        second = holder["instance"]
        disabled = await registry.reconfigure("dream")
        assert disabled["action"] == "disabled" and disabled["created"] is False and second.stops == 1
        with pytest.raises(ServiceRegistryError) as err:
            await registry.reconfigure("writer")
        assert err.value.code == "service_not_rebuildable"
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_settings_state_marks_service_fields():
    from services.config.settings_state import build_settings_schema

    schema = {"Lifecycle_Settings": {"type": "object", "items": {
        "enable_dream": {"type": "bool", "default": True},
        "mood_duration_hours": {"type": "string", "default": "2.0"},
        "x": {"type": "bool", "default": True, "restart_required": True},
    }}}
    payload = build_settings_schema(
        schema, {"Lifecycle_Settings": {"enable_dream": True, "mood_duration_hours": "2.0", "x": True}}, {},
        service_for=lambda path: "dream" if path in {"Lifecycle_Settings.enable_dream", "Lifecycle_Settings.x"} else None,
    )
    items = {item["key"]: item for item in payload["groups"][0]["items"]}
    assert (items["enable_dream"]["apply_mode"], items["enable_dream"]["service"]) == ("service", "dream")
    assert (items["mood_duration_hours"]["apply_mode"], items["mood_duration_hours"]["service"]) == ("next_run", None)
    assert items["x"]["apply_mode"] == "restart"  # 标了需重启的仍需重启
