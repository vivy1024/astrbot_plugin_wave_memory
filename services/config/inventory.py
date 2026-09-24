"""配置总览：把分散在四处的配置摊平成一张表，每一项都能查到当前值从哪来、改了怎么生效。

四层按优先级从低到高：

1. ``builtin``   代码内置默认值（schema default、通道默认、热参数默认）
2. ``static``    AstrBot 6185 静态配置（``astrbot_plugin_wave_memory_config.json``）
3. ``bot``       Bot Profile 自带的覆盖（目前是注入通道覆盖）
4. ``override``  9876 保存的覆盖（热参数数据库覆盖、通道分层配置）

本模块只做只读汇总，不改任何配置。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

PRECEDENCE = ("builtin", "static", "bot", "override")


def _row(
    *,
    key: str,
    layer: str,
    title: str,
    default: Any,
    effective: Any,
    source: str,
    apply_mode: str,
    scope: str = "global",
    saved: Any = None,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "layer": layer,
        "title": title,
        "scope": scope,
        "default": default,
        "saved": saved,
        "effective": effective,
        "source": source,
        "apply_mode": apply_mode,
        "changed": effective != default,
        "warning": warning,
    }


def static_rows(settings_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in settings_payload.get("groups", []) or []:
        items = group.get("items") if group.get("kind") == "object" else [group]
        for item in items or []:
            key = f"{group['key']}.{item['key']}" if group.get("kind") == "object" else str(group["key"])
            source = item.get("source", "")
            layer = "static" if source == "plugin_config" else "builtin"
            if item.get("effective_source") == "runtime_hot_config":
                layer = "override"
            rows.append(_row(
                key=key,
                layer=layer,
                title=str(item.get("description") or key),
                default=item.get("default"),
                saved=item.get("saved"),
                effective=item.get("effective"),
                source=source,
                apply_mode=str(item.get("apply_mode") or "unknown"),
                warning=item.get("error"),
            ))
    return rows


def hot_rows(params: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for param in params:
        source = str(param.get("source") or "")
        if source == "wavememory_db.config_overrides":
            layer = "override"
        elif source.startswith("plugin_config."):
            layer = "static"
        else:
            layer = "builtin"
        effective = param.get("effective")
        if param.get("saved") is not None and effective != param.get("saved"):
            # 本进程里热改过但还没落盘（或落盘失败）的值。
            layer = "override"
            source = f"{source}+runtime"
        rows.append(_row(
            key=f"hot:{param['key']}",
            layer=layer,
            title=str(param.get("description") or param["key"]),
            default=param.get("default"),
            saved=param.get("saved"),
            effective=effective,
            source=source,
            apply_mode="hot",
            warning=param.get("error"),
        ))
    return rows


_CHANNEL_FIELDS = ("enabled", "priority", "top_k", "max_items", "token_budget", "timeout_ms", "min_score")


def channel_rows(default_config: Any, effective_config: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, effective in sorted(effective_config.channels.items()):
        base = default_config.channels.get(name)
        for field in _CHANNEL_FIELDS:
            default_value = getattr(base, field, None) if base is not None else None
            value = getattr(effective, field, None)
            rows.append(_row(
                key=f"channel:{name}.{field}",
                layer="override" if value != default_value else "builtin",
                title=f"注入通道 {name} · {field}",
                default=default_value,
                effective=value,
                source="Channel_Settings" if value != default_value else "channel_default",
                apply_mode="hot",
            ))
    return rows


def bot_rows(profiles: Iterable[Any], default_config: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        for name, patch in sorted((getattr(profile, "channels", None) or {}).items()):
            base = default_config.channels.get(name)
            for field, value in sorted((patch or {}).items()):
                rows.append(_row(
                    key=f"bot:{profile.db_id}.channels.{name}.{field}",
                    layer="bot",
                    title=f"{profile.name} 的通道覆盖 {name} · {field}",
                    scope=f"bot:{profile.db_id}",
                    default=getattr(base, field, None) if base is not None else None,
                    effective=value,
                    source=f"bot_profiles.{profile.db_id}",
                    apply_mode="hot",
                ))
    return rows


def bool_flip_suspects(schema: Mapping[str, Any], saved_config: Mapping[str, Any]) -> list[dict[str, str]]:
    """默认开启、但静态配置里保存成 false 的开关。

    AstrBot 保存表单时会把新增的布尔项写成 false（见 CLAUDE.md 头号教训），
    这里列出来让用户确认是真的想关，还是被覆盖了。
    """
    suspects: list[dict[str, str]] = []
    for group_key, meta in (schema or {}).items():
        if not isinstance(meta, Mapping):
            continue
        items = meta.get("items") if meta.get("type") == "object" else {None: meta}
        group_saved = saved_config.get(group_key) if isinstance(saved_config.get(group_key), Mapping) else {}
        for item_key, item_meta in (items or {}).items():
            if not isinstance(item_meta, Mapping) or item_meta.get("type") != "bool" or item_meta.get("default") is not True:
                continue
            value = saved_config.get(group_key) if item_key is None else group_saved.get(item_key)
            if value is False:
                key = group_key if item_key is None else f"{group_key}.{item_key}"
                suspects.append({
                    "key": key,
                    "message": f"{item_meta.get('description') or key}：默认开启，当前保存为关闭。如果不是你手动关的，可能是 AstrBot 保存表单时覆盖的。",
                })
    return suspects


def build_inventory(
    *,
    settings_payload: Mapping[str, Any],
    hot_params: Iterable[Mapping[str, Any]],
    default_channel_config: Any,
    effective_channel_config: Any,
    bot_profiles: Iterable[Any],
    schema: Mapping[str, Any],
    saved_config: Mapping[str, Any],
) -> dict[str, Any]:
    items = [
        *static_rows(settings_payload),
        *hot_rows(hot_params),
        *channel_rows(default_channel_config, effective_channel_config),
        *bot_rows(bot_profiles, default_channel_config),
    ]
    canonical = json.dumps(
        [(row["key"], row["effective"]) for row in items],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    counts: dict[str, int] = {}
    for row in items:
        counts[row["layer"]] = counts.get(row["layer"], 0) + 1
    return {
        "revision": f"inv-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}",
        "precedence": list(PRECEDENCE),
        "counts": counts,
        "items": items,
        "suspects": bool_flip_suspects(schema, saved_config),
    }


__all__ = ["PRECEDENCE", "bool_flip_suspects", "build_inventory"]
