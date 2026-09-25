"""工具注册表：同一份工具定义同时给 AstrBot 和 Runtime API（Cortico）用。

v5 的 21 个工具在 ``main.py`` 里分组手写实例化，Cortico 只能用 Runtime API 里
另写的 6 个命令。v6 起每个工具登记一个 :class:`ToolSpec`（工厂 + 能力开关 +
分组），由注册表统一实例化：

- **AstrBot 适配**：实例本身就是 ``FunctionTool``，原样交给 ``context.add_llm_tools``；
- **Runtime 适配**：:meth:`ToolRegistry.invoke` 用请求里的 RuntimeScope 构造一个
  只带作用域的调用上下文，调用同一个实例的 ``call``。工具只通过
  ``tools.scope_boundary.extract_event_runtime_scope`` 读作用域，所以两边行为一致。

外部扩展：``<plugin_data>/extensions/*.py`` 里定义 ``register(registry)`` 的模块会被加载，
可以登记自己的工具和注入通道（见 :mod:`services.injection.channel_registry`）。
"""

from __future__ import annotations

import importlib.util
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("astrbot")

ToolFactory = Callable[[Any], Any]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的登记信息。``factory(deps)`` 返回工具实例，返回 None 表示依赖不满足。"""

    key: str
    factory: ToolFactory
    group: str = "memory"
    # runtime_mode 里的能力开关名与缺省值（与 services.runtime_mode.runtime_capability_enabled 一致）
    capability: str = "memory_tools"
    capability_default: bool = True
    writes: bool = False
    runtime_exposed: bool = True
    description: str = ""


@dataclass
class ToolRecord:
    spec: ToolSpec
    instance: Any
    name: str
    enabled: bool = True
    calls: int = 0
    errors: int = 0
    last_latency_ms: float = 0.0


@dataclass
class _RuntimeEvent:
    """Runtime 调用时的最小事件：工具只从这里读已解析的作用域。"""

    _wave_memory_runtime_scope: Any
    _wave_memory_source: str = "runtime"


def runtime_tool_context(scope: Any, *, source: str = "runtime") -> Any:
    event = _RuntimeEvent(_wave_memory_runtime_scope=scope, _wave_memory_source=source)
    return SimpleNamespace(context=SimpleNamespace(event=event))


class ToolRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._records: dict[str, ToolRecord] = {}
        self._build_errors: dict[str, str] = {}
        self._extension_errors: dict[str, str] = {}
        # spec key → 来源（builtin 或扩展文件名）；扩展热重载只卸载扩展登记的工具
        self._origins: dict[str, str] = {}
        self._loading_origin: str | None = None
        self._deps: Any = None
        self._capability_enabled: Callable[[str, bool], bool] | None = None

    # ------------------------------------------------------------------ 登记

    def register(self, spec: ToolSpec) -> None:
        if spec.key in self._specs:
            raise ToolRegistryError("duplicate_tool", f"tool spec {spec.key!r} already registered")
        self._specs[spec.key] = spec
        self._origins[spec.key] = self._loading_origin or "builtin"

    def register_many(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    # ------------------------------------------------------------------ 实例化

    def build(self, deps: Any, *, capability_enabled: Callable[[str, bool], bool]) -> list[Any]:
        """按能力开关实例化全部工具，返回可交给 AstrBot 的实例列表。"""
        self._records.clear()
        self._build_errors.clear()
        self._deps = deps
        self._capability_enabled = capability_enabled
        instances: list[Any] = []
        for spec in self._specs.values():
            instances.extend(self._build_spec(spec))
        return instances

    def _build_spec(self, spec: ToolSpec) -> list[Any]:
        capability_enabled = self._capability_enabled or (lambda _name, default: default)
        if not capability_enabled(spec.capability, spec.capability_default):
            return []
        try:
            produced = spec.factory(self._deps)
        except Exception as exc:  # 单个工具构造失败不影响其他工具
            self._build_errors[spec.key] = f"{type(exc).__name__}: {exc}"
            logger.warning("[WaveMemory] 工具 %s 构造失败: %s", spec.key, exc)
            return []
        instances: list[Any] = []
        for instance in produced if isinstance(produced, (list, tuple)) else [produced]:
            if instance is None:
                continue
            name = str(getattr(instance, "name", "") or spec.key)
            if name in self._records:
                self._build_errors[spec.key] = f"duplicate tool name {name!r}"
                continue
            self._records[name] = ToolRecord(spec=spec, instance=instance, name=name)
            instances.append(instance)
        return instances

    def records(self) -> list[ToolRecord]:
        return list(self._records.values())

    def get(self, name: str) -> ToolRecord | None:
        return self._records.get(str(name or ""))

    def set_enabled(self, name: str, enabled: bool) -> ToolRecord:
        record = self.get(name)
        if record is None:
            raise ToolRegistryError("tool_not_found", f"没有工具 {name!r}")
        record.enabled = bool(enabled)
        return record

    # ------------------------------------------------------------------ 调用

    def describe(self, *, profile: Any = None, runtime_only: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for record in self._records.values():
            if runtime_only and not record.spec.runtime_exposed:
                continue
            allowed = record.enabled and (profile is None or profile.tool_enabled(record.name))
            out.append({
                "name": record.name,
                "description": str(getattr(record.instance, "description", "") or record.spec.description),
                "parameters": dict(getattr(record.instance, "parameters", None) or {"type": "object", "properties": {}}),
                "group": record.spec.group,
                "writes": record.spec.writes,
                "enabled": allowed,
                "runtime_exposed": record.spec.runtime_exposed,
                "stats": {"calls": record.calls, "errors": record.errors, "last_latency_ms": record.last_latency_ms},
            })
        return out

    async def invoke(
        self,
        name: str,
        *,
        scope: Any,
        arguments: Mapping[str, Any] | None = None,
        profile: Any = None,
        source: str = "runtime",
    ) -> str:
        record = self.get(name)
        if record is None or not record.spec.runtime_exposed:
            raise ToolRegistryError("tool_not_found", f"没有可调用的工具 {name!r}")
        if not record.enabled:
            raise ToolRegistryError("tool_disabled", f"工具 {name!r} 已在 9876 停用")
        if profile is not None and not profile.tool_enabled(record.name):
            raise ToolRegistryError("tool_denied", f"Bot {profile.db_id} 不允许使用工具 {name!r}")
        args = dict(arguments or {})
        started = time.perf_counter()
        record.calls += 1
        try:
            result = await record.instance.call(runtime_tool_context(scope, source=source), **args)
        except TypeError as exc:
            record.errors += 1
            raise ToolRegistryError("invalid_arguments", f"{name} 参数不合法: {exc}") from exc
        except Exception:
            record.errors += 1
            raise
        finally:
            record.last_latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return result if isinstance(result, str) else str(result)

    def filter_request_tools(self, func_tool: Any, profile: Any) -> list[str]:
        """AstrBot 适配：按 Bot Profile 与 9876 开关，从本次请求的 ToolSet 里去掉不允许的工具。"""
        removed: list[str] = []
        remove = getattr(func_tool, "remove_tool", None)
        if not callable(remove):
            return removed
        for record in self._records.values():
            allowed = record.enabled and (profile is None or profile.tool_enabled(record.name))
            if not allowed:
                try:
                    remove(record.name)
                    removed.append(record.name)
                except Exception:
                    continue
        return removed

    # ------------------------------------------------------------------ 诊断

    def status(self) -> dict[str, Any]:
        return {
            "registered": len(self._specs),
            "built": len(self._records),
            "build_errors": dict(self._build_errors),
            "extension_errors": dict(self._extension_errors),
            "extensions": self.extensions(),
        }

    def extensions(self) -> dict[str, list[str]]:
        """扩展文件 → 它登记的工具 key。"""
        out: dict[str, list[str]] = {}
        for key, origin in self._origins.items():
            if origin != "builtin":
                out.setdefault(origin, []).append(key)
        return out

    # ------------------------------------------------------------------ 扩展

    def unload_extensions(self) -> list[str]:
        """卸载扩展登记的工具，返回已实例化、需要从宿主撤下的工具名。内置工具不动。"""
        keys = {key for key, origin in self._origins.items() if origin != "builtin"}
        names = [name for name, record in self._records.items() if record.spec.key in keys]
        for name in names:
            self._records.pop(name, None)
        for key in keys:
            self._specs.pop(key, None)
            self._origins.pop(key, None)
            self._build_errors.pop(key, None)
        self._extension_errors.clear()
        return names

    def build_extensions(self) -> list[Any]:
        """只实例化扩展登记的工具（热重载后调用；沿用首次 build 的依赖与能力开关）。"""
        instances: list[Any] = []
        for key, spec in list(self._specs.items()):
            if self._origins.get(key, "builtin") != "builtin":
                instances.extend(self._build_spec(spec))
        return instances

    def load_extensions(self, directory: str | Path, *, extra_registries: Mapping[str, Any] | None = None) -> list[str]:
        """加载 ``directory/*.py`` 中的 ``register(tool_registry, **registries)``。

        每次都重新执行模块文件（不进 ``sys.modules``），所以热重载能拿到改过的代码。
        """
        loaded: list[str] = []
        root = Path(directory)
        if not root.is_dir():
            return loaded
        registries = dict(extra_registries or {})
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._set_loading_origin(path.name, registries)
            try:
                spec = importlib.util.spec_from_file_location(f"wave_memory_ext_{path.stem}", path)
                if spec is None or spec.loader is None:
                    raise ImportError("cannot load module spec")
                module = importlib.util.module_from_spec(spec)
                # 直接从源码编译，不走 __pycache__：pyc 按秒记源文件修改时间，
                # 同一秒内改过的扩展会读到旧字节码，热重载拿不到新代码。
                exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise AttributeError("extension has no register(tool_registry, ...) function")
                register(self, **registries)
                loaded.append(path.name)
            except Exception as exc:
                self._extension_errors[path.name] = f"{type(exc).__name__}: {exc}"
                logger.warning("[WaveMemory] 扩展 %s 加载失败: %s", path.name, exc)
            finally:
                self._set_loading_origin(None, registries)
        if loaded:
            logger.info("[WaveMemory] 已加载扩展: %s", ", ".join(loaded))
        return loaded

    def _set_loading_origin(self, origin: str | None, registries: Mapping[str, Any]) -> None:
        self._loading_origin = origin
        for other in registries.values():
            if hasattr(other, "_loading_origin"):
                other._loading_origin = origin


__all__ = ["ToolRecord", "ToolRegistry", "ToolRegistryError", "ToolSpec", "runtime_tool_context"]
