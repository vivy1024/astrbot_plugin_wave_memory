"""服务与扩展 API：启停后台服务、工具开关。"""

from __future__ import annotations

import asyncio

import pytest
from quart import Quart

from services.service_registry import ServiceRegistry, ServiceSpec
from services.task_supervisor import TaskSupervisor
from services.tool_registry import ToolRegistry, ToolSpec
from webui.blueprints.services import services_bp
from webui.container import get_container


class _Svc:
    def __init__(self):
        self._running = False
        self._task = None

    def start(self, supervisor=None):
        self._running = True
        self._task = supervisor.start("wave-memory:dream", self._loop(), owner="dream")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while self._running:
            await asyncio.sleep(0.01)


class _Tool:
    name = "wave_memory_echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def call(self, context, **kwargs):
        return "x"


@pytest.mark.asyncio
async def test_services_listing_and_control():
    svc = _Svc()
    services = ServiceRegistry(TaskSupervisor())
    services.register(ServiceSpec("dream", "做梦", lambda: svc, owner="dream"))
    tools = ToolRegistry()
    tools.register(ToolSpec("echo", lambda d: _Tool()))
    tools.build(None, capability_enabled=lambda c, d: True)
    c = get_container()
    c.service_registry, c.tool_registry, c.password = services, tools, ""
    app = Quart(__name__)
    app.register_blueprint(services_bp)
    client = app.test_client()
    try:
        data = await (await client.get("/api/services")).get_json()
        assert data["services"][0]["name"] == "dream" and data["services"][0]["running"] is False
        assert data["tools"][0]["name"] == "wave_memory_echo"

        started = await (await client.post("/api/services/dream/start")).get_json()
        assert started["item"]["running"] is True
        stopped = await (await client.post("/api/services/dream/stop")).get_json()
        assert stopped["item"]["running"] is False
        assert (await client.post("/api/services/dream/explode")).status_code == 400
        assert (await client.post("/api/services/nope/start")).status_code == 404

        res = await client.post("/api/tools/wave_memory_echo/enabled", json={"enabled": False})
        assert (await res.get_json())["enabled"] is False
        assert tools.get("wave_memory_echo").enabled is False
        assert (await client.post("/api/tools/nope/enabled", json={"enabled": True})).status_code == 404
    finally:
        c.service_registry = c.tool_registry = None
        await asyncio.sleep(0.02)
