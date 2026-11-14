"""合并工具：同一族的几个工具合成「一个工具 + mode 参数」，减少每次请求的 schema 体积。

合并工具是**薄路由**：逻辑一行不重复，每个 mode 把调用原样转发给原来的工具实例。
- schema 是各子工具参数的**并集**，保留每个参数的类型与枚举，模型填参可靠；
- 转发时只传该子工具自己声明过的参数，别的 mode 的参数不会漏进来；
- 必填项按子工具自己的 schema 在转发前校验，缺哪个就说哪个；
- ``mode`` 的枚举由实际存在、且这个 Bot 被允许的子工具动态生成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from pydantic.dataclasses import dataclass as pydantic_dataclass

try:
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover - 没有 AstrBot 的隔离测试
    from typing import Generic, TypeVar

    _T = TypeVar("_T")

    class FunctionTool(Generic[_T]):  # type: ignore[no-redef]
        pass

    class ContextWrapper(Generic[_T]):  # type: ignore[no-redef]
        pass

    class AstrAgentContext:  # type: ignore[no-redef]
        pass


@dataclass(frozen=True)
class ModeSpec:
    """一个 mode 对应的子工具。"""

    mode: str
    tool_name: str
    # 给模型看的一句话，写进合并工具的 description
    summary: str
    # 额外固定参数（例如 relation 视图映射到 affinity 的 mode）
    fixed: Mapping[str, Any] = field(default_factory=dict)
    # 把合并工具的参数名改成子工具的参数名：{合并参数: 子工具参数}
    rename: Mapping[str, str] = field(default_factory=dict)
    # 只为兼容旧调用保留的别名参数：不写进模型可见的 schema（省提示词），但调用时传了仍会转发
    hide: frozenset[str] = frozenset()


class MergedToolError(ValueError):
    pass


def _prop_signature(prop: Mapping[str, Any]) -> tuple:
    return (prop.get("type"), tuple(prop.get("enum") or ()))


def union_schema(
    children: Sequence[tuple[ModeSpec, Mapping[str, Any]]],
    *,
    mode_description: str,
) -> dict[str, Any]:
    """各子工具参数的并集。同名参数类型/枚举不一致时，按类型取并（枚举合并去重）。"""
    properties: dict[str, dict[str, Any]] = {}
    for spec, schema in children:
        reverse = {original: merged_name for merged_name, original in spec.rename.items()}
        for name, prop in (schema.get("properties") or {}).items():
            if name in spec.fixed or name in spec.hide:
                continue  # 固定参数与兼容别名不暴露给模型
            target = reverse.get(name, name)
            clean = {k: v for k, v in prop.items() if k not in {"default", "minimum", "maximum"}}
            existing = properties.get(target)
            if existing is None:
                properties[target] = clean
                continue
            if "enum" in existing or "enum" in clean:
                merged_enum = list(dict.fromkeys([*(existing.get("enum") or []), *(clean.get("enum") or [])]))
                if merged_enum:
                    existing["enum"] = merged_enum
            if not existing.get("description") and clean.get("description"):
                existing["description"] = clean["description"]
    properties = {
        "mode": {"type": "string", "enum": [spec.mode for spec, _ in children], "description": mode_description},
        **properties,
    }
    return {"type": "object", "properties": properties, "required": ["mode"]}


def forward_arguments(
    spec: ModeSpec, child_schema: Mapping[str, Any], arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """只保留子工具声明过的参数；套用改名与固定参数。"""
    declared = set((child_schema.get("properties") or {}).keys())
    out: dict[str, Any] = {}
    reverse = {original: merged for merged, original in spec.rename.items()}
    for name in declared:
        source = reverse.get(name, name)
        if source in arguments and arguments[source] not in (None, ""):
            out[name] = arguments[source]
    out.update(spec.fixed)
    return out


def missing_required(child_schema: Mapping[str, Any], forwarded: Mapping[str, Any]) -> list[str]:
    return [name for name in (child_schema.get("required") or []) if forwarded.get(name) in (None, "")]


def build_description(intro: str, modes: Sequence[ModeSpec]) -> str:
    lines = [intro.strip()]
    lines.extend(f"mode={spec.mode}：{spec.summary}" for spec in modes)
    return "\n".join(lines)


class MergedTool:
    """路由器的实现部分：持有 {mode: 子工具实例}，与 AstrBot 的 FunctionTool 通过 ``as_function_tool`` 衔接。"""

    def __init__(
        self,
        *,
        name: str,
        intro: str,
        modes: Sequence[ModeSpec],
        resolve_child: Callable[[str], Any | None],
        is_allowed: Callable[[Any, str, str], bool] | None = None,
        mode_description: str = "要做哪一类操作，见上面的 mode 说明",
    ) -> None:
        self.name = name
        self._intro = intro
        self._all_modes = list(modes)
        self._resolve_child = resolve_child
        # (ctx, 合并工具名, mode) -> 这个 Bot 在这一刻能不能用；同一份工具清单服务多个 Bot，
        # 所以权限只能在**调用时**按当前 Bot 判断，schema 里保留全部 mode。
        self._is_allowed = is_allowed or (lambda _ctx, _tool, _mode: True)
        self._mode_description = mode_description

    # ── 可用 mode：子工具存在，且这个调用场景允许 ──

    def available_modes(self) -> list[ModeSpec]:
        return [spec for spec in self._all_modes if self._resolve_child(spec.tool_name) is not None]

    def _child_schema(self, spec: ModeSpec) -> Mapping[str, Any]:
        child = self._resolve_child(spec.tool_name)
        return dict(getattr(child, "parameters", None) or {"type": "object", "properties": {}})

    @property
    def description(self) -> str:
        return build_description(self._intro, self.available_modes())

    @property
    def parameters(self) -> dict[str, Any]:
        modes = self.available_modes()
        return union_schema(
            [(spec, self._child_schema(spec)) for spec in modes],
            mode_description=self._mode_description,
        )

    # ── 调用 ──

    async def dispatch(self, ctx: Any, **kwargs: Any) -> str:
        mode = str(kwargs.pop("mode", "") or "").strip().lower()
        modes = {spec.mode: spec for spec in self.available_modes()}
        if mode not in modes:
            return f"mode 必须是 {'、'.join(modes) or '（当前没有可用的 mode）'} 之一"
        spec = modes[mode]
        if not self._is_allowed(ctx, self.name, mode):
            return f"mode={mode} 当前不可用"
        child = self._resolve_child(spec.tool_name)
        schema = self._child_schema(spec)
        forwarded = forward_arguments(spec, schema, kwargs)
        missing = missing_required(schema, forwarded)
        if missing:
            return f"mode={mode} 缺少必填参数：{'、'.join(missing)}"
        return await child.call(ctx, **forwarded)


def as_function_tool(merged: MergedTool) -> Any:
    """把路由器包成 AstrBot 的 FunctionTool 实例（schema 在构造时取一次快照）。"""

    @pydantic_dataclass
    class _Tool(FunctionTool[AstrAgentContext]):  # type: ignore[misc,valid-type]
        name: str = merged.name
        description: str = merged.description
        parameters: dict = field(default_factory=lambda: merged.parameters)
        router: Any = field(default=None, repr=False)

        async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:  # type: ignore[valid-type]
            return await self.router.dispatch(ctx, **kwargs)

    return _Tool(router=merged)


__all__ = ["MergedTool", "MergedToolError", "ModeSpec", "as_function_tool", "union_schema"]
