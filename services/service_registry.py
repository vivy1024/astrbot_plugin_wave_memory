"""后台服务注册表：9876 上单独启动、停止、重启某个后台服务，不重启 AstrBot。

v5 的做梦、记忆淘汰、标签 worker、好感生命周期、维护任务都只在启动时按静态开关
``start()`` 一次，想停只能重启整个容器。这里给每个服务登记一个 :class:`ServiceSpec`：

- ``get()`` 取当前实例（静态配置没开的服务是 None，页面显示"未创建"）；
- 停止调用服务自己的 ``stop()``（服务负责收尾，如好感引擎先 flush 缓冲）；
- 启动调用 ``start(supervisor)``。TaskSupervisor 的任务名在整个进程里必须唯一，
  所以重启时交给服务一个会给任务名加代次后缀的代理，而不是原始 supervisor。
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("astrbot")


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    title: str
    get: Callable[[], Any]
    # TaskSupervisor 里的 owner，用来读任务健康状态
    owner: str
    description: str = ""
    # 核心服务（写入器等）不允许从页面停掉
    stoppable: bool = True


class _GenerationSupervisor:
    """把任务名加上代次后缀后转交真正的 TaskSupervisor。"""

    def __init__(self, supervisor: Any, generation: int) -> None:
        self._supervisor = supervisor
        self._generation = generation

    def start(self, name: str, coro: Any, *, owner: str = "plugin") -> Any:
        return self._supervisor.start(f"{name}#g{self._generation}", coro, owner=owner)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._supervisor, item)


class ServiceRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceRegistry:
    def __init__(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        self._specs: dict[str, ServiceSpec] = {}
        self._generation: dict[str, int] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    def register(self, spec: ServiceSpec) -> None:
        if spec.name in self._specs:
            raise ServiceRegistryError("duplicate_service", f"service {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def _spec(self, name: str) -> ServiceSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise ServiceRegistryError("service_not_found", f"没有后台服务 {name!r}")
        return spec

    def _instance(self, spec: ServiceSpec) -> Any:
        instance = spec.get()
        if instance is None:
            raise ServiceRegistryError(
                "service_not_created",
                f"{spec.title} 没有创建（静态配置里关着，或依赖不满足）；需要在 AstrBot 配置里打开后重启一次",
            )
        return instance

    @staticmethod
    def _running(instance: Any) -> bool | None:
        running = getattr(instance, "_running", None)
        if isinstance(running, bool):
            task = getattr(instance, "_task", None)
            if running and task is not None and task.done():
                return False
            return running
        return None

    def _record(self, name: str, action: str, ok: bool, detail: str = "") -> None:
        entries = self._history.setdefault(name, [])
        entries.append({"action": action, "ok": ok, "detail": detail, "at": time.time()})
        del entries[:-10]

    async def stop(self, name: str) -> dict[str, Any]:
        spec = self._spec(name)
        if not spec.stoppable:
            raise ServiceRegistryError("service_not_stoppable", f"{spec.title} 是核心服务，不能从页面停止")
        instance = self._instance(spec)
        if self._running(instance) is False:
            return self.describe(name)
        try:
            result = instance.stop()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self._record(name, "stop", False, str(exc))
            raise
        self._record(name, "stop", True)
        logger.info("[WaveMemory] 后台服务已停止: %s", name)
        return self.describe(name)

    async def start(self, name: str) -> dict[str, Any]:
        spec = self._spec(name)
        instance = self._instance(spec)
        if self._running(instance):
            return self.describe(name)
        generation = self._generation.get(name, 0) + 1
        self._generation[name] = generation
        try:
            instance.start(_GenerationSupervisor(self._supervisor, generation))
        except Exception as exc:
            self._record(name, "start", False, str(exc))
            raise
        self._record(name, "start", True, f"g{generation}")
        logger.info("[WaveMemory] 后台服务已启动: %s (g%s)", name, generation)
        return self.describe(name)

    async def restart(self, name: str) -> dict[str, Any]:
        await self.stop(name)
        return await self.start(name)

    def describe(self, name: str) -> dict[str, Any]:
        spec = self._spec(name)
        instance = spec.get()
        tasks: list[dict[str, Any]] = []
        snapshot = getattr(self._supervisor, "health_snapshot", None)
        if callable(snapshot):
            try:
                records = (snapshot() or {}).get("tasks", {})
                values = records.values() if isinstance(records, dict) else records
                tasks = [r for r in values if isinstance(r, dict) and r.get("owner") == spec.owner]
            except Exception:
                tasks = []
        running = self._running(instance) if instance is not None else None
        return {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "created": instance is not None,
            "running": running,
            "stoppable": spec.stoppable,
            "generation": self._generation.get(name, 0),
            "tasks": tasks[-3:],
            "history": list(self._history.get(name, [])),
        }

    def status(self) -> list[dict[str, Any]]:
        return [self.describe(name) for name in self._specs]


__all__ = ["ServiceRegistry", "ServiceRegistryError", "ServiceSpec"]
