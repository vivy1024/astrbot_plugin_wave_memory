"""Config Blueprint — 配置管理、热调参、Provider 列表"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from quart import Blueprint, jsonify, request

from ..container import get_container


# HotConfig key → config.json section.key 映射
_HOT_TO_CONFIG_MAP = {
    "query.group_weight_current": ("Social_Settings", "group_weight_current"),
    "query.group_weight_cross": ("Social_Settings", "group_weight_cross"),
    "social.abuse_trigger_count": ("Social_Settings", "abuse_trigger_count"),
    "social.abuse_cooldown_base": ("Social_Settings", "abuse_cooldown_base"),
    "social.abuse_cooldown_max": ("Social_Settings", "abuse_cooldown_max"),
    "social.aba_window_seconds": ("Social_Settings", "aba_window_seconds"),
    "query.min_similarity": ("Query_Settings", "min_similarity"),
}
_CONFIG_TO_HOT_MAP = {value: key for key, value in _HOT_TO_CONFIG_MAP.items()}


def _settings_snapshot(container, cfg: Mapping) -> dict:
    """首次读取时保存运行中的启动配置；后续保存不能覆盖有效值事实。"""
    snapshot = getattr(container, "settings_effective_config_snapshot", None)
    if isinstance(snapshot, Mapping):
        return copy.deepcopy(dict(snapshot))
    snapshot = copy.deepcopy(dict(cfg))
    setattr(container, "settings_effective_config_snapshot", snapshot)
    return copy.deepcopy(snapshot)


def _settings_effective_since(container) -> dict[str, object]:
    values = getattr(container, "settings_effective_since_by_key", None)
    if isinstance(values, dict):
        return values
    values = {}
    setattr(container, "settings_effective_since_by_key", values)
    return values


def _build_settings_payload(container, schema: Mapping | None = None) -> dict:
    from ...services.config.settings_state import build_settings_schema
    from ...services.hot_config import HotConfig

    cfg = getattr(container, "plugin_config", None)
    if cfg is None:
        cfg = {}
    return build_settings_schema(
        schema or _load_schema(),
        cfg,
        _settings_snapshot(container, cfg),
        hot_key_map=_CONFIG_TO_HOT_MAP,
        hot_config=HotConfig(),
        effective_since_by_key=_settings_effective_since(container),
    )


def _restore_config(cfg, snapshot: Mapping) -> None:
    cfg.clear()
    cfg.update(copy.deepcopy(dict(snapshot)))


def _persist_hot_config_to_file(validated: dict):
    """将 HotConfig 变更写回 AstrBot config.json（持久化）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_wave_memory_config.json")
        if not os.path.isfile(config_path):
            return

        with open(config_path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)

        changed = False
        for hot_key, value in validated.items():
            if hot_key in _HOT_TO_CONFIG_MAP:
                section, field = _HOT_TO_CONFIG_MAP[hot_key]
                if section not in cfg:
                    cfg[section] = {}
                cfg[section][field] = value
                changed = True

        if changed:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _hot_params_payload(container, hot) -> list[dict]:
    cfg = getattr(container, "plugin_config", None)
    if cfg is None:
        cfg = {}
    effective_since = _settings_effective_since(container)
    params = []
    for raw_meta in hot.get_tunable_params():
        meta = dict(raw_meta)
        key = meta["key"]
        mapping = _HOT_TO_CONFIG_MAP.get(key)
        saved = None
        source = "runtime_hot_config_only"
        error = None
        if mapping is not None:
            section, field = mapping
            group = cfg.get(section, {}) or {}
            saved = group.get(field, meta["default"]) if isinstance(group, Mapping) else meta["default"]
            source = f"plugin_config.{section}.{field}"
        else:
            error = "该热参数没有持久化配置映射；应用后只对当前进程有效，重启会恢复。"
        effective = hot.get(key, saved if saved is not None else meta["default"])
        params.append({
            **meta,
            "current": effective,
            "saved": saved,
            "effective": effective,
            "source": source,
            "effective_source": "runtime_hot_config",
            "apply_mode": "hot",
            "effective_since": effective_since.get(key),
            "restart_required": False,
            "restart_requirement": "not_required",
            "error": error,
        })
    return params


from ..middleware.auth import require_auth

config_bp = Blueprint("config", __name__, url_prefix="/api")

# 插件根目录下的 _conf_schema.json（webui/blueprints/config.py 往上 2 级）
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "_conf_schema.json"


def _load_schema() -> dict:
    """读取插件配置 schema。"""
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _coerce(value, type_name: str, default=None):
    """按 schema 声明的类型转换前端传入的值。"""
    try:
        if type_name == "bool":
            if value is None:
                return bool(default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if type_name == "int":
            if isinstance(value, bool):
                return int(value)
            return int(float(value)) if value != "" and value is not None else 0
        # string 类型：强制转为字符串（避免 float/int 值绕过 AstrBot 类型校验）
        if type_name == "string":
            if value is None:
                return ""
            return str(value)
        return value
    except (ValueError, TypeError):
        return value


@config_bp.route("/config", methods=["GET"])
@require_auth
async def get_config():
    """返回当前插件运行配置。"""
    from ...services.runtime_mode import resolve_runtime_mode

    c = get_container()
    cfg = c.plugin_config
    runtime_mode = resolve_runtime_mode(cfg)
    return jsonify({
        "runtime": runtime_mode.to_web_payload(),
        "embedding_provider_id": cfg.get("embedding_provider_id", ""),
        "embedding_dimension": cfg.get("embedding_dimension", 1024),
        "tag_llm_provider_id": cfg.get("tag_llm_provider_id", ""),
        "query": cfg.get("Query_Settings", {}),
        "inject": cfg.get("Inject_Settings", {}),
        "tags": cfg.get("Tag_Settings", {}),
        "storage": cfg.get("Storage_Settings", {}),
        "memory_index": cfg.get("Memory_Index_Settings", {}),
        "filter": cfg.get("Message_Filter", {}),
        "performance": cfg.get("Performance_Settings", {}),
        "lifecycle": cfg.get("Lifecycle_Settings", {}),
        "webui": {
            "enabled": cfg.get("WebUI_Settings", {}).get("webui_enabled", True),
            "host": cfg.get("WebUI_Settings", {}).get("webui_host", "0.0.0.0"),
            "port": cfg.get("WebUI_Settings", {}).get("webui_port", 7890),
        },
    })


@config_bp.route("/config", methods=["POST"])
@require_auth
async def update_config():
    """更新插件配置。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    cfg = c.plugin_config

    field_map = {
        "runtime": "Runtime_Settings",
        "embedding_provider_id": "embedding_provider_id",
        "embedding_dimension": "embedding_dimension",
        "tag_llm_provider_id": "tag_llm_provider_id",
        "query": "Query_Settings",
        "inject": "Inject_Settings",
        "tags": "Tag_Settings",
        "storage": "Storage_Settings",
        "memory_index": "Memory_Index_Settings",
        "filter": "Message_Filter",
        "performance": "Performance_Settings",
        "lifecycle": "Lifecycle_Settings",
    }

    changed = []
    for front_key, cfg_key in field_map.items():
        if front_key in body:
            val = body[front_key]
            if isinstance(val, dict):
                existing = cfg.get(cfg_key, {})
                existing.update(val)
                cfg[cfg_key] = existing
            else:
                cfg[cfg_key] = val
            changed.append(front_key)

    if changed and hasattr(cfg, "save_config"):
        cfg.save_config()

    return jsonify({"ok": True, "changed": changed, "message": "配置已保存，部分参数需重启生效"})


@config_bp.route("/config/hot", methods=["GET"])
@require_auth
async def get_hot_config():
    """返回可热调参数及其 saved/effective 事实状态。"""
    from ...services.hot_config import HotConfig

    c = get_container()
    hot = HotConfig()
    return jsonify({"params": _hot_params_payload(c, hot), "config": hot.get_all()})


@config_bp.route("/config/hot", methods=["POST"])
@require_auth
async def update_hot_config():
    """校验、保存可映射值，再热应用；响应不把仅运行时值冒充已持久化。"""
    from ...services.hot_config import HotConfig

    c = get_container()
    body = await request.get_json(silent=True) or {}
    hot = HotConfig()
    params_meta = {p["key"]: p for p in hot.get_tunable_params()}
    validated = {}
    errors = []

    for key, value in body.items():
        if key not in params_meta:
            errors.append(f"Unknown param: {key}")
            continue
        meta = params_meta[key]
        try:
            if meta["type"] == "float":
                value = float(value)
            elif meta["type"] == "int":
                value = int(value)
            if value < meta["min"] or value > meta["max"]:
                errors.append(f"{key}: out of range [{meta['min']}, {meta['max']}]")
                continue
            validated[key] = value
        except (ValueError, TypeError) as exc:
            errors.append(f"{key}: invalid value - {exc}")

    if errors:
        return jsonify({
            "ok": False,
            "updated": [],
            "saved": [],
            "errors": errors,
            "message": "热参数校验失败，未保存也未应用。",
            "params": _hot_params_payload(c, hot),
        })

    cfg = getattr(c, "plugin_config", {}) or {}
    snapshot = copy.deepcopy(dict(cfg))
    saved_keys = [key for key in validated if key in _HOT_TO_CONFIG_MAP]
    runtime_only_keys = [key for key in validated if key not in _HOT_TO_CONFIG_MAP]
    try:
        for key in saved_keys:
            section, field = _HOT_TO_CONFIG_MAP[key]
            group = dict(cfg.get(section, {}) or {})
            group[field] = validated[key]
            cfg[section] = group
        save = getattr(cfg, "save_config", None)
        if saved_keys and callable(save):
            save()
        elif saved_keys:
            _persist_hot_config_to_file({key: validated[key] for key in saved_keys})
    except Exception as exc:
        _restore_config(cfg, snapshot)
        return jsonify({
            "ok": False,
            "updated": [],
            "saved": [],
            "errors": [f"配置持久化失败: {exc}"],
            "message": "保存失败，热参数未应用。",
            "params": _hot_params_payload(c, hot),
        })

    hot.update(validated)
    now = time.time()
    effective_since = _settings_effective_since(c)
    for key in validated:
        effective_since[key] = now
    warnings = [f"{key}: 仅当前进程生效，未持久化" for key in runtime_only_keys]
    return jsonify({
        "ok": True,
        "updated": list(validated.keys()),
        "saved": saved_keys,
        "runtime_only": runtime_only_keys,
        "errors": [],
        "warnings": warnings,
        "message": "热参数已应用。" + (" 部分参数仅当前进程生效。" if runtime_only_keys else " 已持久化保存。"),
        "params": _hot_params_payload(c, hot),
    })


@config_bp.route("/config/schema", methods=["GET"])
@require_auth
async def get_config_schema():
    """返回可编辑 schema，以及独立的 saved/default/effective/apply 状态。"""
    c = get_container()
    return jsonify(_build_settings_payload(c))


@config_bp.route("/config/full", methods=["POST"])
@require_auth
async def update_config_full():
    """事务式保存 schema 配置，并分别报告保存值与运行时有效值。"""
    from ...services.hot_config import HotConfig

    c = get_container()
    cfg = c.plugin_config
    schema = _load_schema()
    body = await request.get_json(silent=True) or {}
    _settings_snapshot(c, cfg)

    before = copy.deepcopy(dict(cfg))
    candidate = copy.deepcopy(before)
    changed_fields: list[str] = []
    changed_groups: list[str] = []
    errors: list[str] = []
    restart_fields: list[str] = []
    hot_updates: dict[str, object] = {}

    for key, incoming in body.items():
        meta = schema.get(key)
        if not isinstance(meta, dict):
            errors.append(f"未知配置项: {key}")
            continue
        mtype = meta.get("type", "string")
        if mtype == "object":
            if not isinstance(incoming, dict):
                errors.append(f"{key} 应为对象")
                continue
            sub_schema = meta.get("items", {}) or {}
            existing = dict(candidate.get(key, {}) or {})
            for sub_key, sub_val in incoming.items():
                sub_meta = sub_schema.get(sub_key)
                if not isinstance(sub_meta, dict):
                    errors.append(f"未知配置项: {key}.{sub_key}")
                    continue
                value = _coerce(sub_val, sub_meta.get("type", "string"), sub_meta.get("default"))
                if existing.get(sub_key, object()) == value:
                    continue
                existing[sub_key] = value
                path = f"{key}.{sub_key}"
                changed_fields.append(path)
                if sub_meta.get("restart_required"):
                    restart_fields.append(path)
                hot_key = _CONFIG_TO_HOT_MAP.get((key, sub_key))
                if hot_key:
                    hot_updates[hot_key] = value
            candidate[key] = existing
        else:
            value = _coerce(incoming, mtype, meta.get("default"))
            if candidate.get(key, object()) == value:
                continue
            candidate[key] = value
            changed_fields.append(key)
            if meta.get("restart_required"):
                restart_fields.append(key)

    if errors:
        return jsonify({
            "ok": False,
            "saved": False,
            "changed": [],
            "changed_fields": [],
            "errors": errors,
            "restart_required": False,
            "message": "配置校验失败，未保存也未应用。",
        })

    changed_groups = list(dict.fromkeys(path.split(".", 1)[0] for path in changed_fields))
    try:
        if changed_fields:
            _restore_config(cfg, candidate)
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
    except Exception as exc:
        _restore_config(cfg, before)
        return jsonify({
            "ok": False,
            "saved": False,
            "changed": [],
            "changed_fields": [],
            "errors": [f"配置保存失败: {exc}"],
            "restart_required": False,
            "message": "配置保存失败，运行时有效值未改变。",
        })

    effective_since = _settings_effective_since(c)
    if hot_updates:
        HotConfig().update(hot_updates)
        now = time.time()
        for key in hot_updates:
            effective_since[key] = now

    restart_required = bool(restart_fields)
    pending_fields = [path for path in changed_fields if path not in restart_fields and path not in {
        f"{section}.{field}" for hot_key, (section, field) in _HOT_TO_CONFIG_MAP.items() if hot_key in hot_updates
    }]
    message = "配置已保存。"
    if restart_required:
        message += " 需重启的字段仍保持原运行时值。"
    if pending_fields:
        message += " 其余非热字段将在下次运行路径读取时生效，当前未冒充已生效。"
    if hot_updates:
        message += " 已完成热配置回读。"

    return jsonify({
        "ok": True,
        "saved": True,
        "changed": changed_groups,
        "changed_fields": changed_fields,
        "errors": [],
        "restart_required": restart_required,
        "restart_fields": restart_fields,
        "apply_modes": {
            "hot": list(hot_updates),
            "restart": restart_fields,
            "next_run": pending_fields,
        },
        "effective_since": dict(effective_since),
        "message": message,
        "schema": _build_settings_payload(c, schema),
    })


@config_bp.route("/providers", methods=["GET"])
@require_auth
async def list_providers():
    """列出可用的 LLM/Embedding Provider。"""
    c = get_container()
    try:
        providers = []
        seen_ids = set()
        embed_id = c.plugin_config.get("embedding_provider_id", "")
        all_provs = c.embedding_service.context.get_all_providers()
        for prov in all_provs:
            try:
                meta = prov.meta()
                if meta.id not in seen_ids:
                    ptype = "embedding" if meta.id == embed_id else (meta.type or "unknown")
                    providers.append({"id": meta.id, "model": meta.model or "", "type": ptype})
                    seen_ids.add(meta.id)
            except Exception:
                pass
        if embed_id and embed_id not in seen_ids:
            providers.insert(0, {"id": embed_id, "model": embed_id.split("/")[-1] if "/" in embed_id else embed_id, "type": "embedding"})
            seen_ids.add(embed_id)
        tag_id = c.plugin_config.get("tag_llm_provider_id", "")
        if tag_id and tag_id not in seen_ids:
            providers.append({"id": tag_id, "model": tag_id.split("/")[-1] if "/" in tag_id else tag_id, "type": "llm"})
        return jsonify({"providers": providers})
    except Exception as e:
        return jsonify({"providers": [], "error": str(e)})
