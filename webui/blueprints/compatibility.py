"""Compatibility API — 真实能力、插件探测证据与静态接口文档。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

try:
    from quart import Blueprint, jsonify
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时的轻量兜底
    class Blueprint:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    def jsonify(value=None, **kwargs):  # type: ignore[no-redef]
        return value if value is not None else kwargs

try:
    from ..container import get_container
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover
    def get_container():  # type: ignore[no-redef]
        return None

    def require_auth(func):  # type: ignore[no-redef]
        return func

try:
    from services.compat.plugin_detection import (
        build_duplicate_memory_warnings,
        plugin_probe_from_result,
        probe_memory_plugins,
    )
    from services.runtime_mode import resolve_runtime_mode
except Exception:  # pragma: no cover - AstrBot 包导入路径
    from ...services.compat.plugin_detection import (
        build_duplicate_memory_warnings,
        plugin_probe_from_result,
        probe_memory_plugins,
    )
    from ...services.runtime_mode import resolve_runtime_mode


compatibility_bp = Blueprint("compatibility", __name__, url_prefix="/api/compat")

_FACADE_DOCUMENTATION = [
    "search_memories(query, k=5, session_id=None, persona_id=None)",
    "add_memory(content, session_id=None, persona_id=None, importance=0.7, metadata=None)",
]
_ALIAS_DOCUMENTATION = {
    "recall_long_term_memory": "LivingMemory 风格检索别名，目标为 facade search。",
    "memorize_long_term_memory": "LivingMemory 风格写入别名，目标为 facade add。",
}


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _capability(
    capability_id: str,
    *,
    actual: bool | None,
    source: str,
    checked_at: str,
    configured: Any = None,
) -> dict[str, Any]:
    if actual is None:
        status = "not_configured"
        error = "运行时没有提供该能力的注册状态。"
        evidence = [{
            "kind": "runtime_capability",
            "summary": "缺少运行时注册状态，不能仅依据静态接口说明判断能力可用。",
            "source": source,
        }]
    else:
        status = "detected" if actual else "not_detected"
        error = None
        evidence = [{
            "kind": "runtime_capability",
            "summary": "运行时已注册该能力。" if actual else "运行时未注册该能力。",
            "source": source,
            "actual": actual,
        }]
    return {
        "id": capability_id,
        "status": status,
        "enabled": bool(actual),
        "configured": configured,
        "source": source,
        "checked_at": checked_at,
        "error": error,
        "evidence": evidence,
    }


def _recommendations(
    probe: Mapping[str, Any],
    warnings: list[dict[str, Any]],
    facade: Mapping[str, Any],
    aliases: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    probe_status = str(probe.get("status") or "not_configured")
    if probe_status == "probe_failed":
        return ["插件探测失败，当前无法判断重复注入风险；请先修复探测错误并重新检查。"]
    if probe_status == "not_configured":
        return ["当前进程未提供插件 registry 探测源；配置探测源后再评估生态冲突。"]

    recommendations: list[str] = []
    if warnings:
        names = "、".join(str(item.get("name") or item.get("plugin_id")) for item in warnings)
        recommendations.append(f"已检测到 {names} 处于活动状态；请只保留一条自动注入路径，并通过 Observatory 验证。")
    else:
        recommendations.append("本次 registry 探测未发现已知重复记忆插件；该结论仅对本次探测来源和时间有效。")

    if facade.get("status") != "detected":
        recommendations.append("LivingMemory facade 当前未注册；只有确需第三方兼容调用时才启用并重启验证。")
    if any(item.get("status") == "detected" for item in aliases.values()):
        recommendations.append("工具别名已注册；请确认没有其他插件注册同名工具。")
    return recommendations


def build_compatibility_payload(
    plugin_config: Mapping[str, Any] | None,
    detection_result: list[dict[str, Any]] | None = None,
    *,
    probe_result: Mapping[str, Any] | None = None,
    facade_enabled: bool | None = None,
    aliases_registered: bool | None = None,
) -> dict[str, Any]:
    """构造兼容页 payload；实时事实与静态文档严格分区。"""
    plugin_config = plugin_config or {}
    runtime = resolve_runtime_mode(plugin_config)
    compat_cfg: Mapping[str, Any] = {}

    if probe_result is None:
        if detection_result is None:
            probe: dict[str, Any] = {
                "status": "not_configured",
                "source": "not_provided",
                "checked_at": _checked_at(),
                "error": "未提供插件探测结果或可执行探测源。",
                "evidence": [{
                    "kind": "probe_configuration",
                    "summary": "没有可用于判断插件冲突的探测事实。",
                    "source": "not_provided",
                }],
                "plugins": [],
            }
        else:
            probe = plugin_probe_from_result(detection_result, source="provided_detection_result")
    else:
        probe = dict(probe_result)
        probe.setdefault("plugins", list(detection_result or []))
        probe.setdefault("status", "probe_failed")
        probe.setdefault("source", "unknown")
        probe.setdefault("checked_at", _checked_at())
        probe.setdefault("error", None)
        probe.setdefault("evidence", [])

    detected = list(probe.get("plugins") or [])
    warnings = build_duplicate_memory_warnings(detected)
    checked_at = str(probe.get("checked_at") or _checked_at())

    configured_alias = compat_cfg.get("livingmemory_alias_tools_enabled") if "livingmemory_alias_tools_enabled" in compat_cfg else None
    configured_facade = compat_cfg.get("facade_enabled") if "facade_enabled" in compat_cfg else None
    if facade_enabled is None and configured_facade is not None:
        facade_enabled = _as_bool(configured_facade)
    if aliases_registered is None and configured_alias is not None:
        aliases_registered = _as_bool(configured_alias)

    facade = _capability(
        "livingmemory_facade",
        actual=facade_enabled,
        configured=configured_facade,
        source="runtime.livingmemory_facade_enabled",
        checked_at=checked_at,
    )
    aliases = {
        name: _capability(
            name,
            actual=aliases_registered,
            configured=configured_alias,
            source="runtime.livingmemory_alias_tools_registered",
            checked_at=checked_at,
        )
        for name in _ALIAS_DOCUMENTATION
    }

    return {
        "runtime": {
            **runtime.to_web_payload(),
            "status": "detected",
            "source": "plugin_config.Runtime_Settings.runtime_mode",
            "checked_at": checked_at,
            "error": None,
            "evidence": [{
                "kind": "runtime_mode",
                "configured": runtime.source_value,
                "effective": runtime.mode,
            }],
        },
        "probe": probe,
        "status": probe.get("status"),
        "source": probe.get("source"),
        "checked_at": probe.get("checked_at"),
        "error": probe.get("error"),
        "evidence": probe.get("evidence"),
        "facade": facade,
        "tool_aliases": aliases,
        "capabilities": [facade, *aliases.values()],
        "detected_plugins": detected,
        "duplicate_warnings": warnings,
        "recommended_settings": _recommendations(probe, warnings, facade, aliases),
        "documentation": {
            "kind": "static",
            "facade_interfaces": list(_FACADE_DOCUMENTATION),
            "tool_aliases": dict(_ALIAS_DOCUMENTATION),
            "notice": "本区仅说明接口契约，不代表运行时已经注册或可调用。",
        },
    }


def _runtime_probe(container: Any) -> dict[str, Any]:
    explicit_probe = getattr(container, "compatibility_probe", None)
    if isinstance(explicit_probe, Mapping):
        return dict(explicit_probe)
    if container is not None and hasattr(container, "detected_memory_plugins"):
        return plugin_probe_from_result(
            getattr(container, "detected_memory_plugins", None),
            source="startup.detected_memory_plugins",
            checked_at=getattr(container, "compatibility_checked_at", None),
        )
    return probe_memory_plugins()


@compatibility_bp.route("/status", methods=["GET"])
@require_auth
async def get_compatibility_status():
    c = get_container()
    plugin_config = getattr(c, "plugin_config", {}) or {}
    probe = _runtime_probe(c)
    return jsonify(build_compatibility_payload(
        plugin_config,
        probe_result=probe,
        facade_enabled=getattr(c, "livingmemory_facade_enabled", None),
        aliases_registered=getattr(c, "livingmemory_alias_tools_registered", None),
    ))


__all__ = ["compatibility_bp", "build_compatibility_payload"]
