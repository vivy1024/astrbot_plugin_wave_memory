"""Settings 页的 saved/default/effective 事实模型。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MISSING = object()


def _section(config: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    value = (config or {}).get(key, {})
    return value if isinstance(value, Mapping) else {}


def _raw(config: Mapping[str, Any] | None, group_key: str, item_key: str | None) -> tuple[bool, Any]:
    if item_key is None:
        if config is not None and group_key in config:
            return True, config.get(group_key)
        return False, _MISSING
    group = _section(config, group_key)
    if item_key in group:
        return True, group.get(item_key)
    return False, _MISSING


def _resolved(present: bool, raw: Any, default: Any) -> Any:
    # 新 bool 与旧 config 的兼容规则同样适用于其他 schema 值：缺失/None 回退，
    # 但显式 False、0 和空字符串必须原样保留。
    return raw if present and raw is not None else default


def _source(present: bool, raw: Any) -> str:
    if not present:
        return "schema_default_missing"
    if raw is None:
        return "schema_default_none"
    return "plugin_config"


# 敏感配置：接口只报告「是否已设置」，不回传明文；保存时空值表示不修改。
SECRET_FIELDS = frozenset({("WebUI_Settings", "webui_password")})


def is_secret_field(group_key: str, item_key: str | None) -> bool:
    return (group_key, item_key) in SECRET_FIELDS


def build_field_state(
    *,
    group_key: str,
    item_key: str | None,
    meta: Mapping[str, Any],
    saved_config: Mapping[str, Any] | None,
    effective_config: Mapping[str, Any] | None,
    hot_key: str | None = None,
    hot_config: Any = None,
    effective_since: Any = None,
    service: str | None = None,
) -> dict[str, Any]:
    """构造单字段状态，并保留缺键/None/False 的区别。

    ``service``：保存后只重建这个后台服务即生效（ServiceSpec.factory），生效方式记为 ``service``。
    """
    default = meta.get("default")
    saved_present, saved_raw = _raw(saved_config, group_key, item_key)
    effective_present, effective_raw = _raw(effective_config, group_key, item_key)
    saved_value = _resolved(saved_present, saved_raw, default)
    effective_value = _resolved(effective_present, effective_raw, default)
    saved_wire = saved_raw if saved_present and saved_raw is not _MISSING else None

    restart_required = bool(meta.get("restart_required", False))
    apply_mode = "restart" if restart_required else "next_run"
    effective_source = "runtime_startup_snapshot"
    if hot_key is not None:
        apply_mode = "hot"
        getter = getattr(hot_config, "get", None)
        if callable(getter):
            effective_value = getter(hot_key, effective_value)
            effective_source = "runtime_hot_config"
    elif service and not restart_required:
        apply_mode = "service"

    error = None
    if meta.get("type") == "bool" and (not saved_present or saved_raw is None):
        reason = "缺失" if not saved_present else "为 None"
        error = f"旧配置中的布尔项{reason}，当前按 schema 默认值 {default!r} 解析；显式 False 不会被覆盖。"

    numeric_limits = {
        key: meta[key]
        for key in ("min", "max", "minimum", "maximum")
        if key in meta
    }
    secret = {}
    if is_secret_field(group_key, item_key):
        secret = {"secret": True, "has_value": bool(saved_value or effective_value)}
        saved_wire = "" if saved_wire else saved_wire
        saved_value = ""
        effective_value = ""
    return {
        **secret,
        **numeric_limits,
        "default": default,
        "saved": saved_wire,
        "saved_present": saved_present,
        "effective": effective_value,
        "value": saved_value,
        "source": _source(saved_present, saved_raw),
        "effective_source": effective_source,
        "apply_mode": apply_mode,
        "effective_since": effective_since,
        "restart_required": restart_required,
        "restart_requirement": "required" if restart_required else "not_required",
        "service": service if apply_mode == "service" else None,
        "error": error,
    }


def build_settings_schema(
    schema: Mapping[str, Any] | None,
    saved_config: Mapping[str, Any] | None,
    effective_config: Mapping[str, Any] | None,
    *,
    hot_key_map: Mapping[tuple[str, str], str] | None = None,
    hot_config: Any = None,
    effective_since_by_key: Mapping[str, Any] | None = None,
    service_for: Any = None,
) -> dict[str, Any]:
    """将 AstrBot schema 转换成 Settings 页可编辑且可解释的 DTO。

    ``service_for(path)`` 返回保存后会被重建的后台服务名（没有则 None）。
    """
    hot_key_map = hot_key_map or {}
    effective_since_by_key = effective_since_by_key or {}
    groups: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []

    for key, raw_meta in (schema or {}).items():
        if not isinstance(raw_meta, Mapping):
            continue
        meta = dict(raw_meta)
        mtype = str(meta.get("type", "string"))
        base = {
            "key": key,
            "description": str(meta.get("description", key)),
            "hint": str(meta.get("hint", "")),
        }
        if mtype == "object":
            items = []
            for sub_key, raw_sub_meta in (meta.get("items", {}) or {}).items():
                if not isinstance(raw_sub_meta, Mapping):
                    continue
                sub_meta = dict(raw_sub_meta)
                hot_key = hot_key_map.get((key, sub_key))
                state = build_field_state(
                    group_key=key,
                    item_key=sub_key,
                    meta=sub_meta,
                    saved_config=saved_config,
                    effective_config=effective_config,
                    hot_key=hot_key,
                    hot_config=hot_config,
                    effective_since=effective_since_by_key.get(hot_key or f"{key}.{sub_key}"),
                    service=service_for(f"{key}.{sub_key}") if callable(service_for) else None,
                )
                item = {
                    "key": sub_key,
                    "type": str(sub_meta.get("type", "string")),
                    "description": str(sub_meta.get("description", sub_key)),
                    "hint": str(sub_meta.get("hint", "")),
                    "special": str(sub_meta.get("_special", "")),
                    **state,
                }
                items.append(item)
                if state["error"]:
                    warnings.append({"key": f"{key}.{sub_key}", "message": state["error"]})
            groups.append({**base, "kind": "object", "items": items})
            continue

        state = build_field_state(
            group_key=key,
            item_key=None,
            meta=meta,
            saved_config=saved_config,
            effective_config=effective_config,
            effective_since=effective_since_by_key.get(key),
        )
        groups.append({
            **base,
            "kind": "scalar",
            "type": mtype,
            "special": str(meta.get("_special", "")),
            **state,
        })
        if state["error"]:
            warnings.append({"key": key, "message": state["error"]})

    return {"groups": groups, "warnings": warnings}


__all__ = ["build_field_state", "build_settings_schema"]
