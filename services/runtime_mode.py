"""WaveMemory 运行模式解析与能力门控。

该模块只负责把静态配置解析为明确的运行模式，避免 main.py 直接散落
字符串判断。默认兼容旧配置：没有 Runtime_Settings 时视为 full。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


VALID_RUNTIME_MODES = {"full", "memory_only", "compat_only"}
ADVANCED_QUERY_FLAGS = {
    "enable_spike_routing",
    "enable_residual_pyramid",
    "enable_epa",
    "enable_geodesic_rerank",
}

PURE_MEMORY_CAPABILITIES = {
    "message_capture",
    "writer_queue",
    "vector_query",
    "basic_injection",
    "injection_trace",
    "memory_tools",
    "compat_facade",
    "eviction",
}

MEMORY_ONLY_DISABLED_CAPABILITIES = {
    "advanced_query",
    "affinity",
    "affinity_tools",
    "persona",
    "persona_tools",
    "mood",
    "consolidation",
    "metathinking",
    "belief",
    "belief_emergence",
    "mood_trajectory",
    "jargon",
    "fewshot",
    "book_lore",
    "book_lore_tools",
    "game_knowledge",
    "study",
    "self_reflect",
}

COMPAT_ONLY_DISABLED_CAPABILITIES = MEMORY_ONLY_DISABLED_CAPABILITIES | {
    "basic_injection",
    "injection_trace",
    "memory_tools",
    "agent_feedback_tools",
}


@dataclass(frozen=True)
class RuntimeMode:
    """解析后的运行模式。"""

    mode: str
    label: str
    description: str
    advanced_query_default: bool
    native_injection_default: bool
    disabled_capabilities: list[str]
    source_value: str

    def to_web_payload(self) -> dict[str, Any]:
        """返回 WebUI 可直接展示的运行模式摘要。"""
        return {
            "mode": self.mode,
            "label": self.label,
            "description": self.description,
            "advanced_query_default": self.advanced_query_default,
            "native_injection_default": self.native_injection_default,
            "disabled_capabilities": list(self.disabled_capabilities),
            "source_value": self.source_value,
        }


_MODE_TABLE: dict[str, RuntimeMode] = {
    "full": RuntimeMode(
        mode="full",
        label="完整模式",
        description="启用记忆采集、检索、注入以及高级检索/认知能力，兼容旧配置默认行为。",
        advanced_query_default=True,
        native_injection_default=True,
        disabled_capabilities=[],
        source_value="full",
    ),
    "memory_only": RuntimeMode(
        mode="memory_only",
        label="纯记忆模式",
        description="保留记忆采集、检索、注入与解释，默认关闭高级检索和类学习/认知增强能力。",
        advanced_query_default=False,
        native_injection_default=True,
        disabled_capabilities=sorted(MEMORY_ONLY_DISABLED_CAPABILITIES),
        source_value="memory_only",
    ),
    "compat_only": RuntimeMode(
        mode="compat_only",
        label="兼容接口模式",
        description="仅保留记忆存取和 LivingMemory 兼容接口，默认不主动注入，避免与生态插件重复注入。",
        advanced_query_default=False,
        native_injection_default=False,
        disabled_capabilities=sorted(COMPAT_ONLY_DISABLED_CAPABILITIES | {"native_injection"}),
        source_value="compat_only",
    ),
}


def _get_section(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if not config:
        return {}
    value = config.get(name, {})
    return value if isinstance(value, Mapping) else {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def resolve_runtime_mode(config: Mapping[str, Any] | None) -> RuntimeMode:
    """从插件配置解析运行模式；缺失或非法值都回落到 full。"""
    runtime_cfg = _get_section(config, "Runtime_Settings")
    raw_value = str(runtime_cfg.get("runtime_mode", "full") or "full").strip().lower()
    mode = raw_value if raw_value in VALID_RUNTIME_MODES else "full"
    base = _MODE_TABLE[mode]
    if raw_value == base.source_value:
        return base
    return RuntimeMode(
        mode=base.mode,
        label=base.label,
        description=base.description,
        advanced_query_default=base.advanced_query_default,
        native_injection_default=base.native_injection_default,
        disabled_capabilities=list(base.disabled_capabilities),
        source_value=raw_value,
    )


def effective_query_feature(query_cfg: Mapping[str, Any] | None, key: str, mode: RuntimeMode) -> bool:
    """返回查询特性的有效开关值。

    - 高级查询开关缺省时由运行模式决定。
    - 已存在的旧配置显式值会保留，避免破坏旧配置。
    - 非高级开关默认 False，除非配置显式开启。
    """
    query_cfg = query_cfg or {}
    if key in query_cfg:
        return _as_bool(query_cfg.get(key))
    if key in ADVANCED_QUERY_FLAGS:
        return mode.advanced_query_default
    return False


def effective_native_injection_enabled(
    query_cfg: Mapping[str, Any] | None,
    mode: RuntimeMode,
    *,
    compat_cfg: Mapping[str, Any] | None = None,
) -> bool:
    """返回原生 on_llm_request 自动注入是否启用。

    compat_only 是 LivingMemory 风格兼容后端模式，旧配置中的
    Query_Settings.enable_auto_inject=true 很可能只是历史默认值，不能让
    compat_only 默认重新主动注入。该模式仅在专用兼容开关
    Compatibility_Settings.compat_only_auto_inject_enabled=true 时启用原生注入。
    """
    query_cfg = query_cfg or {}
    compat_cfg = compat_cfg or {}
    if mode.mode == "compat_only":
        return _as_bool(compat_cfg.get("compat_only_auto_inject_enabled", False))
    if "enable_auto_inject" in query_cfg:
        return _as_bool(query_cfg.get("enable_auto_inject"))
    return mode.native_injection_default


def runtime_capability_enabled(mode: RuntimeMode | str, capability: str, configured: Any = True) -> bool:
    """返回某项运行时能力在当前模式下是否允许启用。

    `configured` 表示静态配置中的开关值。`memory_only` 会强制关闭高级
    社交/人格/世界观/风格/BookLore 能力，避免旧配置里的 default=true 在升级
    后把纯记忆模式重新变成 full 行为。
    """
    mode_name = mode.mode if isinstance(mode, RuntimeMode) else str(mode or "full")
    normalized = str(capability or "").strip().lower()
    if mode_name == "memory_only" and normalized in MEMORY_ONLY_DISABLED_CAPABILITIES:
        return False
    if mode_name == "compat_only" and normalized in COMPAT_ONLY_DISABLED_CAPABILITIES:
        return False
    return _as_bool(configured)


def should_self_heal_advanced_query(mode: RuntimeMode) -> bool:
    """是否应该把全关的高级检索开关自愈回开启。

    只有完整模式需要沿用历史自愈；纯记忆/兼容模式下全关是合法状态。
    """
    return mode.mode == "full"
