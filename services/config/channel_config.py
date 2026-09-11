"""注入通道热配置模型。

把旧 `Query_Settings` / `Inject_Settings` 映射为通道级默认配置，并对热更新做防御性校验。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
from typing import Any, Mapping

from ..impression_timeline import normalize_timeline_half_life

try:
    from ...domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - top-level services imports
    from domain.scope import RuntimeScope

from .effective_config import (
    EffectiveConfigResult,
    patches_for_scope,
    resolve_effective_config,
    validate_layer_store,
)


KNOWN_MODES = frozenset({"full", "memory_only", "compat_only"})
MAX_TIMEOUT_MS = 5000
KNOWN_CHANNELS = (
    "safety",
    "memory",
    "facts",
    "persona",
    "belief",
    "jargon",
    "fewshot",
    "book_lore",
    "fts5",
    "affinity",
    "soul_state",
)
# 已退役通道：旧 Channel_Settings 里可能仍保存其覆盖项；校验时静默忽略而不是拒绝。
RETIRED_CHANNELS = frozenset({"timeline"})

_ADVANCED_FULL_ONLY = {"persona", "belief", "jargon", "fewshot", "book_lore", "affinity", "soul_state"}
_OPTIONAL_MEMORY_ONLY = {"facts", "fts5"}
_QUERY_STAGE_NAMES = frozenset({"epa", "pyramid", "spike", "geodesic"})
_QUERY_PARAM_LIMITS = {
    "pyramid_max_levels": (int, 1, 10),
    "pyramid_top_k": (int, 1, 50),
    "spike_max_hops": (int, 0, 16),
    "spike_firing_threshold": (float, 0.0, 1.0),
    "geodesic_alpha": (float, 0.0, 1.0),
}
_ROOT_OVERRIDE_FIELDS = frozenset({"channels", "recent_dedup_minutes", "timeline_days", "timeline_decay_half_life_days", "trace_enabled", "query_options", "memory_recall"})
_MEMORY_RECALL_FIELDS = frozenset({"enable_shotgun", "skip_recent_minutes", "source_filter", "exclude_sources"})
_EFFECTIVE_FIELD_METADATA = {
    "*": {"apply_mode": "hot", "restart_required": False},
}


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    enabled: bool
    priority: int
    top_k: int | None = None
    max_items: int | None = None
    token_budget: int = 300
    timeout_ms: int = 300
    min_score: float | None = None
    modes: tuple[str, ...] = ("full",)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["modes"] = list(self.modes)
        return payload


@dataclass(frozen=True)
class ChannelConfigSet:
    mode: str
    channels: dict[str, ChannelConfig]
    recent_dedup_minutes: int = 30
    timeline_days: int = 0
    trace_enabled: bool = True
    query_stages: dict[str, bool] = field(default_factory=dict)
    query_params: dict[str, int | float] = field(default_factory=dict)
    memory_recall: dict[str, Any] = field(default_factory=dict)
    timeline_decay_half_life_days: float = 21.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "recent_dedup_minutes": self.recent_dedup_minutes,
            "timeline_days": self.timeline_days,
            "timeline_decay_half_life_days": self.timeline_decay_half_life_days,
            "trace_enabled": self.trace_enabled,
            "query_options": {
                "stages": dict(self.query_stages),
                "params": dict(self.query_params),
            },
            "memory_recall": dict(self.memory_recall),
            "channels": {name: cfg.to_dict() for name, cfg in self.channels.items()},
        }


def _int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _strict_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


def _strict_number(value: Any, *, field_name: str, caster: type[int] | type[float]) -> int | float:
    if isinstance(value, bool) or value == "" or value is None:
        raise ValueError(f"{field_name} must be a {caster.__name__}")
    try:
        converted = caster(float(value)) if caster is int else caster(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a {caster.__name__}") from exc
    if caster is int and float(value) != converted:
        raise ValueError(f"{field_name} must be an integer")
    return converted


def _strict_timeline_half_life(value: Any) -> float:
    """热覆盖只接受正有限天数；旧 Inject_Settings 的回退不适用于这里。"""
    field_name = "timeline_decay_half_life_days"
    try:
        converted = float(_strict_number(value, field_name=field_name, caster=float))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a positive finite number") from exc
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return normalize_timeline_half_life(converted)


def _strict_string_list(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a string array")
    return list(value)


def _modes_for(name: str, mode: str) -> tuple[str, ...]:
    if name == "safety":
        return ("full", "memory_only", "compat_only")
    if name == "memory":
        return ("full", "memory_only") if mode != "compat_only" else ("full", "memory_only", "compat_only")
    if name in _OPTIONAL_MEMORY_ONLY:
        return ("full", "memory_only")
    return ("full",)


def _enabled_for(name: str, mode: str, inject_cfg: Mapping[str, Any]) -> bool:
    if name == "safety":
        return True
    if mode == "compat_only":
        return False
    if mode == "memory_only" and name in _ADVANCED_FULL_ONLY:
        return False
    if name == "facts":
        return _int(inject_cfg.get("facts_max"), 5) > 0
    return True


def build_default_channel_config(
    *,
    runtime_mode: str = "full",
    query_cfg: Mapping[str, Any] | None = None,
    inject_cfg: Mapping[str, Any] | None = None,
) -> ChannelConfigSet:
    """从旧配置映射为模式感知的通道默认配置。"""
    mode = runtime_mode if runtime_mode in KNOWN_MODES else "full"
    query_cfg = query_cfg or {}
    inject_cfg = inject_cfg or {}
    inject_top_k = _int(query_cfg.get("inject_top_k"), 5)
    min_similarity = _float(query_cfg.get("min_similarity"), 0.35)
    facts_max = _int(inject_cfg.get("facts_max"), 5)
    timeline_days = _int(inject_cfg.get("timeline_days"), 0)
    recent_dedup = _int(inject_cfg.get("skip_recent_minutes"), 30)

    defaults = {
        "safety": ChannelConfig("safety", True, priority=1000, token_budget=0, timeout_ms=50, modes=_modes_for("safety", mode)),
        # Online embedding providers routinely need >1s; keep memory channel
        # soft-timeout at 2s so remote vector recall is not cancelled early.
        "memory": ChannelConfig("memory", _enabled_for("memory", mode, inject_cfg), priority=100, top_k=inject_top_k, token_budget=600, timeout_ms=2000, min_score=min_similarity, modes=_modes_for("memory", mode)),
        "facts": ChannelConfig("facts", _enabled_for("facts", mode, inject_cfg), priority=75, max_items=facts_max, token_budget=260, timeout_ms=120, modes=_modes_for("facts", mode)),
        # max_items=2 lets the scope-keyed speaker statistics block ride along with
        # the bot's own persona block; at 1 only self_persona could ever be injected.
        "persona": ChannelConfig("persona", _enabled_for("persona", mode, inject_cfg), priority=70, max_items=2, token_budget=460, timeout_ms=500, modes=_modes_for("persona", mode)),
        "belief": ChannelConfig("belief", _enabled_for("belief", mode, inject_cfg), priority=65, max_items=5, token_budget=220, timeout_ms=300, modes=_modes_for("belief", mode)),
        "jargon": ChannelConfig("jargon", _enabled_for("jargon", mode, inject_cfg), priority=60, max_items=3, token_budget=180, timeout_ms=160, modes=_modes_for("jargon", mode)),
        "fewshot": ChannelConfig("fewshot", _enabled_for("fewshot", mode, inject_cfg), priority=50, max_items=3, token_budget=260, timeout_ms=300, modes=_modes_for("fewshot", mode)),
        "book_lore": ChannelConfig("book_lore", _enabled_for("book_lore", mode, inject_cfg), priority=45, max_items=1, token_budget=260, timeout_ms=800, min_score=0.35, modes=_modes_for("book_lore", mode)),
        "fts5": ChannelConfig("fts5", _enabled_for("fts5", mode, inject_cfg), priority=85, top_k=10, token_budget=350, timeout_ms=600, min_score=0.0, modes=_modes_for("fts5", mode)),
        # affinity 承载全量印象摘要、指数时间权重与关系分数；半衰期只影响权重。
        # 全量保留由通道与编排器协议保证，不依赖下面的大固定预算，不按条数裁剪。
        "affinity": ChannelConfig("affinity", _enabled_for("affinity", mode, inject_cfg), priority=68, max_items=3, token_budget=1600, timeout_ms=800, modes=_modes_for("affinity", mode)),
        "soul_state": ChannelConfig("soul_state", _enabled_for("soul_state", mode, inject_cfg), priority=67, max_items=1, token_budget=260, timeout_ms=180, modes=_modes_for("soul_state", mode)),
    }
    query_stages = {
        "epa": _bool(query_cfg.get("enable_epa"), True),
        "pyramid": _bool(query_cfg.get("enable_residual_pyramid"), True),
        "spike": _bool(query_cfg.get("enable_spike_routing"), True),
        "geodesic": _bool(query_cfg.get("enable_geodesic_rerank"), True),
    }
    memory_recall = {
        "enable_shotgun": _bool(query_cfg.get("enable_shotgun"), False),
        "skip_recent_minutes": recent_dedup,
    }
    return ChannelConfigSet(
        mode=mode,
        channels=defaults,
        recent_dedup_minutes=recent_dedup,
        timeline_days=timeline_days,
        trace_enabled=True,
        query_stages=query_stages,
        query_params={},
        memory_recall=memory_recall,
        timeline_decay_half_life_days=normalize_timeline_half_life(
            inject_cfg.get("timeline_decay_half_life_days")
        ),
    )


def _validate_channel_config(name: str, cfg: ChannelConfig) -> None:
    if name not in KNOWN_CHANNELS:
        raise ValueError(f"unknown channel: {name}")
    if name == "safety" and not cfg.enabled:
        raise ValueError("safety channel cannot be disabled while injection is enabled")
    if cfg.priority < 0:
        raise ValueError(f"{name}.priority must be non-negative")
    if cfg.top_k is not None and cfg.top_k < 0:
        raise ValueError(f"{name}.top_k must be non-negative")
    if cfg.max_items is not None and cfg.max_items < 0:
        raise ValueError(f"{name}.max_items must be non-negative")
    if cfg.token_budget < 0:
        raise ValueError(f"{name}.token_budget must be non-negative")
    if cfg.timeout_ms < 0 or cfg.timeout_ms > MAX_TIMEOUT_MS:
        raise ValueError(f"{name}.timeout_ms must be between 0 and {MAX_TIMEOUT_MS}")
    if cfg.min_score is not None and (cfg.min_score < 0 or cfg.min_score > 1):
        raise ValueError(f"{name}.min_score must be between 0 and 1")
    invalid_modes = [m for m in cfg.modes if m not in KNOWN_MODES]
    if invalid_modes:
        raise ValueError(f"{name}.modes contains unknown mode: {invalid_modes[0]}")


def apply_channel_overrides(base: ChannelConfigSet, overrides: Mapping[str, Any] | None) -> ChannelConfigSet:
    """完整校验并应用请求级覆盖；任意非法字段都会整体拒绝。"""
    overrides = overrides or {}
    if not isinstance(overrides, Mapping):
        raise ValueError("channel override must be an object")
    unknown_root = [field for field in overrides if field not in _ROOT_OVERRIDE_FIELDS]
    if unknown_root:
        raise ValueError(f"unknown channel override field: {unknown_root[0]}")

    raw_channels = overrides.get("channels", {})
    channels_override = {} if raw_channels is None else raw_channels
    if not isinstance(channels_override, Mapping):
        raise ValueError("channels override must be an object")

    updated = dict(base.channels)
    for name, patch in channels_override.items():
        if name in RETIRED_CHANNELS:
            # 已退役通道（如 timeline）的旧覆盖项：静默忽略，保持旧配置可读。
            continue
        if name not in updated:
            raise ValueError(f"unknown channel: {name}")
        if patch is None:
            continue
        if not isinstance(patch, Mapping):
            raise ValueError(f"{name} override must be an object")
        current = updated[name]
        allowed = {"enabled", "priority", "top_k", "max_items", "token_budget", "timeout_ms", "min_score"}
        unknown_fields = [field for field in patch if field not in allowed]
        if unknown_fields:
            raise ValueError(f"unknown field for {name}: {unknown_fields[0]}")
        values: dict[str, Any] = {}
        for field, value in patch.items():
            if value is None:
                continue
            field_name = f"channels.{name}.{field}"
            if field == "enabled":
                values[field] = _strict_bool(value, field_name=field_name)
            elif field in {"top_k", "max_items", "priority", "token_budget", "timeout_ms"}:
                values[field] = _strict_number(value, field_name=field_name, caster=int)
            elif field == "min_score":
                values[field] = _strict_number(value, field_name=field_name, caster=float)
        candidate = replace(current, **values)
        _validate_channel_config(name, candidate)
        updated[name] = candidate

    recent_dedup = base.recent_dedup_minutes
    if "recent_dedup_minutes" in overrides and overrides.get("recent_dedup_minutes") is not None:
        recent_dedup = int(_strict_number(
            overrides.get("recent_dedup_minutes"),
            field_name="recent_dedup_minutes",
            caster=int,
        ))
    timeline_days = base.timeline_days
    if "timeline_days" in overrides and overrides.get("timeline_days") is not None:
        timeline_days = int(_strict_number(
            overrides.get("timeline_days"),
            field_name="timeline_days",
            caster=int,
        ))
        if timeline_days < 0:
            raise ValueError("timeline_days must be non-negative")
    timeline_half_life = base.timeline_decay_half_life_days
    if overrides.get("timeline_decay_half_life_days") is not None:
        timeline_half_life = _strict_timeline_half_life(overrides["timeline_decay_half_life_days"])
    trace_enabled = base.trace_enabled
    if "trace_enabled" in overrides and overrides.get("trace_enabled") is not None:
        trace_enabled = _strict_bool(overrides.get("trace_enabled"), field_name="trace_enabled")

    query_stages = dict(base.query_stages)
    query_params = dict(base.query_params)
    raw_query_options = overrides.get("query_options")
    if raw_query_options is not None:
        if not isinstance(raw_query_options, Mapping) or any(key not in {"stages", "params"} for key in raw_query_options):
            raise ValueError("query_options must contain only stages and params")
        raw_stages = raw_query_options.get("stages")
        if raw_stages is not None:
            if not isinstance(raw_stages, Mapping):
                raise ValueError("query_options.stages must be an object")
            for name, value in raw_stages.items():
                if name not in _QUERY_STAGE_NAMES:
                    raise ValueError(f"unknown query stage: {name}")
                if value is not None:
                    query_stages[name] = _strict_bool(value, field_name=f"query_options.stages.{name}")
        raw_params = raw_query_options.get("params")
        if raw_params is not None:
            if not isinstance(raw_params, Mapping):
                raise ValueError("query_options.params must be an object")
            for name, value in raw_params.items():
                limits = _QUERY_PARAM_LIMITS.get(name)
                if limits is None:
                    raise ValueError(f"unknown query param: {name}")
                if value is None:
                    continue
                caster, minimum, maximum = limits
                converted = _strict_number(value, field_name=f"query_options.params.{name}", caster=caster)
                if converted < minimum or converted > maximum:
                    raise ValueError(f"query_options.params.{name} must be between {minimum} and {maximum}")
                query_params[name] = converted

    memory_recall = dict(base.memory_recall)
    raw_memory_recall = overrides.get("memory_recall")
    if raw_memory_recall is not None:
        if not isinstance(raw_memory_recall, Mapping):
            raise ValueError("memory_recall must be an object")
        unknown_fields = [field for field in raw_memory_recall if field not in _MEMORY_RECALL_FIELDS]
        if unknown_fields:
            raise ValueError(f"unknown memory_recall field: {unknown_fields[0]}")
        for field, value in raw_memory_recall.items():
            if value is None:
                continue
            if field == "enable_shotgun":
                memory_recall[field] = _strict_bool(value, field_name=f"memory_recall.{field}")
            elif field == "skip_recent_minutes":
                converted = int(_strict_number(value, field_name=f"memory_recall.{field}", caster=int))
                if converted < 0:
                    raise ValueError("memory_recall.skip_recent_minutes must be non-negative")
                memory_recall[field] = converted
            else:
                memory_recall[field] = _strict_string_list(value, field_name=f"memory_recall.{field}")

    candidate_set = ChannelConfigSet(
        mode=base.mode,
        channels=updated,
        recent_dedup_minutes=recent_dedup,
        timeline_days=timeline_days,
        trace_enabled=trace_enabled,
        query_stages=query_stages,
        query_params=query_params,
        memory_recall=memory_recall,
        timeline_decay_half_life_days=timeline_half_life,
    )
    for name, cfg in candidate_set.channels.items():
        _validate_channel_config(name, cfg)
    if candidate_set.recent_dedup_minutes < 0:
        raise ValueError("recent_dedup_minutes must be non-negative")
    return candidate_set


def _channel_settings(plugin_config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = plugin_config.get("Channel_Settings", {})
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        raise ValueError("Channel_Settings must be an object")
    return settings


def _legacy_system_overrides(settings: Mapping[str, Any]) -> dict[str, Any]:
    allowed = set(_ROOT_OVERRIDE_FIELDS) | {"layers"}
    unknown = [field for field in settings if field not in allowed]
    if unknown:
        raise ValueError(f"unknown Channel_Settings field: {unknown[0]}")
    return {field: settings[field] for field in _ROOT_OVERRIDE_FIELDS if field in settings}


def _config_set_from_payload(payload: Mapping[str, Any]) -> ChannelConfigSet:
    if not isinstance(payload, Mapping):
        raise ValueError("effective channel config must be an object")
    raw_channels_payload = payload.get("channels")
    if not isinstance(raw_channels_payload, Mapping) or not set(KNOWN_CHANNELS).issubset(set(raw_channels_payload)):
        raise ValueError("effective channel config must contain every known channel")
    # 只读取当前已知通道；历史有效层里可能还带着已退役通道（如 timeline）。
    channels_payload = {name: raw_channels_payload[name] for name in KNOWN_CHANNELS}
    channels: dict[str, ChannelConfig] = {}
    for name in KNOWN_CHANNELS:
        raw = channels_payload[name]
        if not isinstance(raw, Mapping):
            raise ValueError(f"effective channel {name} must be an object")
        expected = {"name", "enabled", "priority", "top_k", "max_items", "token_budget", "timeout_ms", "min_score", "modes"}
        if set(raw) != expected or raw.get("name") != name:
            raise ValueError(f"effective channel {name} has invalid fields")
        cfg = ChannelConfig(
            name=name,
            enabled=raw["enabled"],
            priority=raw["priority"],
            top_k=raw["top_k"],
            max_items=raw["max_items"],
            token_budget=raw["token_budget"],
            timeout_ms=raw["timeout_ms"],
            min_score=raw["min_score"],
            modes=tuple(raw["modes"]),
        )
        _validate_channel_config(name, cfg)
        channels[name] = cfg
    query_options = payload.get("query_options", {})
    memory_recall = payload.get("memory_recall", {})
    if not isinstance(query_options, Mapping) or not isinstance(memory_recall, Mapping):
        raise ValueError("effective query_options and memory_recall must be objects")
    candidate = ChannelConfigSet(
        mode=str(payload.get("mode") or ""),
        channels=channels,
        recent_dedup_minutes=int(payload.get("recent_dedup_minutes")),
        timeline_days=_int(payload.get("timeline_days"), 0),
        trace_enabled=payload.get("trace_enabled"),
        query_stages=dict(query_options.get("stages") or {}),
        query_params=dict(query_options.get("params") or {}),
        memory_recall=dict(memory_recall),
        timeline_decay_half_life_days=_strict_timeline_half_life(
            payload.get("timeline_decay_half_life_days", 21.0)
        ),
    )
    # Reuse the strict validator for non-channel request-level options.
    validated = apply_channel_overrides(
        replace(candidate, query_stages={}, query_params={}, memory_recall={}),
        {
            "query_options": {"stages": candidate.query_stages, "params": candidate.query_params},
            "memory_recall": candidate.memory_recall,
            "recent_dedup_minutes": candidate.recent_dedup_minutes,
            "timeline_days": candidate.timeline_days,
            "timeline_decay_half_life_days": candidate.timeline_decay_half_life_days,
            "trace_enabled": candidate.trace_enabled,
        },
    )
    return replace(validated, channels=channels, mode=candidate.mode)


def _is_private_scope(scope: Any) -> bool:
    return bool(
        isinstance(scope, RuntimeScope)
        and scope.visibility == "private"
        and scope.session is not None
        and scope.session.kind == "private"
    )


def _restrict_private_channels(config: ChannelConfigSet) -> ChannelConfigSet:
    """Apply the non-overridable private-session channel safety envelope."""
    allowed = {"safety", "memory", "fts5"}
    channels = {
        name: cfg if name in allowed else replace(cfg, enabled=False, modes=("full",))
        for name, cfg in config.channels.items()
    }
    return replace(
        config,
        channels=channels,
        trace_enabled=False,
        query_stages={name: False for name in _QUERY_STAGE_NAMES},
        memory_recall={**config.memory_recall, "enable_shotgun": False},
        timeline_days=0,
    )


def resolve_effective_channel_config(
    plugin_config: Mapping[str, Any] | None,
    *,
    scope: Any,
) -> tuple[ChannelConfigSet, EffectiveConfigResult]:
    """解析 exact RuntimeScope 的通道配置，并返回 provenance/revision。"""
    plugin_config = plugin_config or {}
    try:
        from ..runtime_mode import resolve_runtime_mode
        mode = resolve_runtime_mode(plugin_config).mode
    except Exception:
        mode = "full"
    base = build_default_channel_config(
        runtime_mode=mode,
        query_cfg=plugin_config.get("Query_Settings", {}) or {},
        inject_cfg=plugin_config.get("Inject_Settings", {}) or {},
    )
    settings = _channel_settings(plugin_config)
    system_config = apply_channel_overrides(base, _legacy_system_overrides(settings))
    layers = settings.get("layers", {})
    # validate_layer_store checks every layer, including entries unrelated to this Scope.
    validate_layer_store(layers)
    selected = patches_for_scope(layers, scope)

    candidate = system_config
    for layer in ("bot", "session", "user", "relationship"):
        patch = selected.get(layer)
        if patch is not None:
            candidate = apply_channel_overrides(candidate, patch)
    effective = resolve_effective_config(
        system_config.to_dict(),
        scope=scope,
        bot_config=selected.get("bot"),
        session_config=selected.get("session"),
        user_config=selected.get("user"),
        relationship_config=selected.get("relationship"),
        field_metadata=_EFFECTIVE_FIELD_METADATA,
    )
    reconstructed = _config_set_from_payload(effective.values)
    if reconstructed.to_dict() != candidate.to_dict():
        raise ValueError("effective resolver output does not match validated channel candidate")
    if _is_private_scope(scope):
        candidate = _restrict_private_channels(candidate)
        # Keep the effective-config API truthful after applying the
        # non-overridable private envelope; provenance remains useful for the
        # underlying layer values while the returned values are safe-to-use.
        canonical = json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        effective = replace(
            effective,
            values=candidate.to_dict(),
            revision=f"effective-private-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]}",
        )
    return candidate, effective


def build_channel_config_from_plugin_config(
    plugin_config: Mapping[str, Any] | None,
    *,
    scope: Any = None,
) -> ChannelConfigSet:
    """兼容旧 Channel_Settings；有 Scope 时叠加严格分层配置。"""
    plugin_config = plugin_config or {}
    if scope is not None:
        return resolve_effective_channel_config(plugin_config, scope=scope)[0]
    try:
        from ..runtime_mode import resolve_runtime_mode
        mode = resolve_runtime_mode(plugin_config).mode
    except Exception:
        mode = "full"
    base = build_default_channel_config(
        runtime_mode=mode,
        query_cfg=plugin_config.get("Query_Settings", {}) or {},
        inject_cfg=plugin_config.get("Inject_Settings", {}) or {},
    )
    settings = _channel_settings(plugin_config)
    validate_layer_store(settings.get("layers", {}))
    return apply_channel_overrides(base, _legacy_system_overrides(settings))


def channel_config_revision(config: ChannelConfigSet) -> str:
    """返回可复现的有效配置 revision，供 API 与后续 Trace 对账。"""
    canonical = json.dumps(config.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"cfg-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def channel_config_diff(base: ChannelConfigSet, target: ChannelConfigSet) -> list[dict[str, Any]]:
    """返回 WebUI 可展示的通道配置差异。"""
    diffs: list[dict[str, Any]] = []

    def add(path: str, before: Any, after: Any) -> None:
        if before != after:
            diffs.append({"path": path, "before": before, "after": after})

    add("recent_dedup_minutes", base.recent_dedup_minutes, target.recent_dedup_minutes)
    add("trace_enabled", base.trace_enabled, target.trace_enabled)
    for stage in sorted(set(base.query_stages) | set(target.query_stages)):
        add(f"query_options.stages.{stage}", base.query_stages.get(stage), target.query_stages.get(stage))
    for param in sorted(set(base.query_params) | set(target.query_params)):
        add(f"query_options.params.{param}", base.query_params.get(param), target.query_params.get(param))
    for field in sorted(set(base.memory_recall) | set(target.memory_recall)):
        add(f"memory_recall.{field}", base.memory_recall.get(field), target.memory_recall.get(field))
    for name in KNOWN_CHANNELS:
        before = base.channels.get(name)
        after = target.channels.get(name)
        if not before or not after:
            continue
        for field in ("enabled", "priority", "top_k", "max_items", "token_budget", "timeout_ms", "min_score", "modes"):
            before_value = getattr(before, field)
            after_value = getattr(after, field)
            if isinstance(before_value, tuple):
                before_value = list(before_value)
            if isinstance(after_value, tuple):
                after_value = list(after_value)
            add(f"channels.{name}.{field}", before_value, after_value)
    return diffs
