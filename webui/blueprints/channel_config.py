"""Channel Config API — 注入通道热配置观察、校验与应用。"""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from typing import Any, Mapping

try:
    from quart import Blueprint, jsonify, request
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时的轻量兜底
    class Blueprint:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    def jsonify(value=None, **kwargs):  # type: ignore[no-redef]
        return value if value is not None else kwargs

    class _Request:
        async def get_json(self, *args, **kwargs):
            return {}
    request = _Request()  # type: ignore[assignment]

try:
    from ..container import get_container
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover
    def get_container():  # type: ignore[no-redef]
        return None

    def require_auth(func):  # type: ignore[no-redef]
        return func

try:
    from services.config.channel_config import (
        MAX_TIMEOUT_MS,
        apply_channel_overrides,
        build_channel_config_from_plugin_config,
        build_default_channel_config,
        channel_config_diff,
        channel_config_revision,
        resolve_effective_channel_config,
    )
    from services.config.effective_config import EffectiveConfigError, upsert_layer_patch, validate_layer_store
    from domain.scope import RuntimeScope
    from services.runtime_mode import resolve_runtime_mode
except Exception:  # pragma: no cover - AstrBot 包导入路径
    from ...services.config.channel_config import (
        MAX_TIMEOUT_MS,
        apply_channel_overrides,
        build_channel_config_from_plugin_config,
        build_default_channel_config,
        channel_config_diff,
        channel_config_revision,
        resolve_effective_channel_config,
    )
    from ...services.config.effective_config import EffectiveConfigError, upsert_layer_patch, validate_layer_store
    from ...domain.scope import RuntimeScope
    from ...services.runtime_mode import resolve_runtime_mode


from ..api_contract import field_value_state, mutation_response


channel_config_bp = Blueprint("channel_config", __name__, url_prefix="/api")

_EDITABLE_FIELDS = ["enabled", "priority", "top_k", "max_items", "token_budget", "timeout_ms", "min_score"]
_CHANNEL_DESCRIPTORS = {
    "safety": ("身份污染、近期去重与安全边界", [], "critical", None),
    "memory": ("召回当前作用域的长期记忆", ["memory_index"], "high", "/memories"),
    "facts": ("注入同作用域且证据健康的事实", ["facts_repository"], "high", "/knowledge/facts"),
    "persona": ("读取当前 Bot/会话的人格投影", ["soul_runtime"], "high", "/soul"),
    "belief": ("注入 active 且证据健康的信念", ["belief_service", "evidence_resolver"], "high", "/beliefs"),
    "jargon": ("解释显式命中的群聊黑话", ["jargon_service"], "medium", "/jargon"),
    "fewshot": ("提供已审核且健康的 Bot 风格样例", ["fewshot_service"], "high", "/knowledge/style-examples"),
    "book_lore": ("检索已审核的 BookLore projection", ["book_lore_adapter"], "medium", "/knowledge/book-lore"),
    "fts5": ("全文原词命中（FTS5），与向量记忆分开", ["fts_memories"], "medium", "/diagnostics/indexes"),
    "affinity": ("印象摘要、印象时间线与关系分数一起注入", ["relationship_projection"], "medium", "/people"),
    "soul_state": ("注入当前 Scope 的 Mood、Concern、Timeline 与 Soul 状态", ["soul_repository"], "high", "/soul"),
}


def _plugin_config(container_or_config: Any) -> Mapping[str, Any]:
    if isinstance(container_or_config, Mapping):
        return container_or_config
    return getattr(container_or_config, "plugin_config", {}) or {}


def _scope_from_payload(value: Any) -> RuntimeScope | None:
    if isinstance(value, RuntimeScope):
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        return RuntimeScope.from_dict(value)
    except Exception:
        return None


def _request_scope_from_container(container: Any) -> RuntimeScope | None:
    provider = getattr(container, "request_scope_provider", None)
    getter = getattr(provider, "get_request_scope", None)
    if not callable(getter):
        return None
    try:
        scope = getter()
    except Exception:
        return None
    return scope if isinstance(scope, RuntimeScope) else None


def _core_patch(body: Mapping[str, Any] | None) -> dict[str, Any]:
    transport = {
        "layer", "scope", "preflight_token", "confirmation", "message", "sender_id",
        "sender_name", "recent_context", "max_items",
    }
    return {key: copy.deepcopy(value) for key, value in dict(body or {}).items() if key not in transport}


def _merge_patch(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if value is None:
            continue
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge_patch(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _candidate_channel_settings(
    plugin_config: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    scope: RuntimeScope | None,
) -> tuple[str, dict[str, Any]]:
    layer = str(body.get("layer") or "system").strip()
    if layer == "group":
        layer = "session"
    if layer not in {"system", "bot", "session", "user", "relationship"}:
        raise EffectiveConfigError("invalid_config_layer", f"unsupported config layer: {layer!r}")
    patch = _core_patch(body)
    settings = plugin_config.get("Channel_Settings", {}) or {}
    if not isinstance(settings, Mapping):
        raise ValueError("Channel_Settings must be an object")
    candidate = copy.deepcopy(dict(settings))
    validate_layer_store(candidate.get("layers", {}))
    if layer == "system":
        system = {key: value for key, value in candidate.items() if key != "layers"}
        system = _merge_patch(system, patch)
        candidate = {**system, "layers": copy.deepcopy(candidate.get("layers", {}))}
    else:
        if scope is None:
            raise EffectiveConfigError("runtime_scope_required", f"{layer} layer patch requires exact RuntimeScope")
        candidate["layers"] = upsert_layer_patch(candidate.get("layers", {}), layer=layer, scope=scope, patch=patch)
    return layer, candidate


def _plugin_with_channel_settings(plugin_config: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(plugin_config))
    candidate["Channel_Settings"] = copy.deepcopy(dict(settings))
    return candidate


def _trace_store_from_container(container: Any) -> Any:
    direct = getattr(container, "injection_trace_store", None)
    if direct:
        return direct
    db = getattr(container, "db", None)
    if not db:
        return None
    if getattr(db, "closed", False):
        try:
            db.reopen()
        except Exception:
            return None
    conn = getattr(db, "conn", None)
    if not conn:
        return None
    try:
        try:
            from services.injection.trace_store import InjectionTraceStore
        except Exception:  # pragma: no cover - AstrBot 包导入路径
            from ...services.injection.trace_store import InjectionTraceStore
        store = InjectionTraceStore(conn)
        store.ensure_schema()
        return store
    except Exception:
        return None


def _default_config(plugin_config: Mapping[str, Any]):
    mode = resolve_runtime_mode(plugin_config).mode
    return build_default_channel_config(
        runtime_mode=mode,
        query_cfg=plugin_config.get("Query_Settings", {}) or {},
        inject_cfg=plugin_config.get("Inject_Settings", {}) or {},
    )


def _latest_channel_runtime_stats(trace_store: Any = None) -> dict[str, dict[str, Any]]:
    conn = getattr(trace_store, "conn", None)
    if not conn:
        return {}
    try:
        rows = conn.execute(
            """SELECT c.channel, c.status, c.latency_ms, c.item_count
               FROM injection_trace_channels c
               JOIN injection_traces t ON t.trace_id = c.trace_id
               ORDER BY t.timestamp DESC, c.id DESC"""
        ).fetchall()
    except Exception:
        return {}
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row[0] or "")
        if not name or name in stats:
            continue
        try:
            latency_ms = int(round(float(row[2] or 0)))
        except (TypeError, ValueError):
            latency_ms = 0
        try:
            hit_count = int(row[3] or 0)
        except (TypeError, ValueError):
            hit_count = 0
        stats[name] = {
            "status": str(row[1] or "unknown"),
            "last_latency_ms": max(0, latency_ms),
            "last_hit_count": max(0, hit_count),
        }
    return stats


def _attach_runtime_status_defaults(config_payload: dict[str, Any], trace_store: Any = None) -> dict[str, Any]:
    stats = _latest_channel_runtime_stats(trace_store)
    for name, channel in (config_payload.get("channels") or {}).items():
        if not isinstance(channel, dict):
            continue
        channel.setdefault("status", "unknown")
        channel.setdefault("last_latency_ms", 0)
        channel.setdefault("last_hit_count", 0)
        channel.update(stats.get(name, {}))
    return config_payload


def build_channel_config_payload(
    plugin_config: Mapping[str, Any] | None,
    current_config: Any = None,
    *,
    trace_store: Any = None,
    effective_since: float | None = None,
    scope: RuntimeScope | None = None,
) -> dict[str, Any]:
    """构造真实 registry descriptor 与 saved/effective/revision 配置契约。"""
    plugin_config = plugin_config or {}
    defaults = _default_config(plugin_config)
    effective = None
    if scope is not None:
        current, effective = resolve_effective_channel_config(plugin_config, scope=scope)
    else:
        current = current_config or build_channel_config_from_plugin_config(plugin_config)
    runtime = resolve_runtime_mode(plugin_config).to_web_payload()
    default_payload = defaults.to_dict()
    current_payload = _attach_runtime_status_defaults(current.to_dict(), trace_store)
    overrides = plugin_config.get("Channel_Settings", {}) or {}
    saved_channels = overrides.get("channels", {}) if isinstance(overrides, Mapping) else {}
    descriptors = []
    for name, effective_channel in (current_payload.get("channels") or {}).items():
        purpose, dependencies, risk, management_route = _CHANNEL_DESCRIPTORS[name]
        default_channel = (default_payload.get("channels") or {}).get(name, {})
        saved_channel = saved_channels.get(name, {}) if isinstance(saved_channels, Mapping) else {}
        effective_channel["field_states"] = {
            field: field_value_state(
                default=default_channel.get(field),
                saved=saved_channel.get(field, default_channel.get(field)),
                effective=effective_channel.get(field),
                apply_mode="hot",
                effective_since=effective_since,
            )
            for field in _EDITABLE_FIELDS
        }
        descriptors.append({
            "id": name,
            "purpose": purpose,
            "dependencies": dependencies,
            "risk": risk,
            "management_route": management_route,
            "verification_filters": {"channel": name},
            "available": True,
        })
    revision = channel_config_revision(current)
    return {
        "runtime": runtime,
        "current": current_payload,
        "defaults": default_payload,
        "overrides": overrides,
        "diff": channel_config_diff(defaults, current),
        "descriptors": descriptors,
        "revision": effective.revision if effective is not None else revision,
        "channel_revision": revision,
        "provenance": effective.provenance if effective is not None else {},
        "applied_layers": list(effective.applied_layers) if effective is not None else [{"layer": "system", "selector": {}}],
        "apply_mode": effective.apply_mode if effective is not None else "hot",
        "restart_required": effective.restart_required if effective is not None else False,
        "effective_since": effective_since,
        "verification_url": f"/observatory?config_revision={effective.revision if effective is not None else revision}",
        "editable_fields": list(_EDITABLE_FIELDS),
        "limits": {"timeout_ms_max": MAX_TIMEOUT_MS, "min_score_min": 0, "min_score_max": 1},
    }


def validate_channel_config_patch(
    plugin_config: Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
    current_config: Any = None,
    *,
    expected_scope: RuntimeScope | None = None,
) -> dict[str, Any]:
    """完整校验 system/scoped 补丁并返回候选；不修改运行时或持久化配置。"""
    plugin_config = plugin_config or {}
    if patch is None:
        patch = {}
    if not isinstance(patch, Mapping):
        return {"ok": False, "errors": ["patch must be an object"], "current": None, "candidate": None, "diff": []}
    body = dict(patch)
    scope = _scope_from_payload(body.get("scope"))
    if body.get("scope") is not None and scope is None:
        return {"ok": False, "error_code": "invalid_runtime_scope", "errors": ["scope must be an exact RuntimeScope payload"], "current": None, "candidate": None, "diff": []}
    if expected_scope is not None and scope != expected_scope:
        return {"ok": False, "error_code": "cross_scope_preview_rejected", "errors": ["request Scope and patch Scope must match exactly"], "current": None, "candidate": None, "diff": []}
    try:
        layer, candidate_settings = _candidate_channel_settings(plugin_config, body, scope=scope)
        candidate_plugin = _plugin_with_channel_settings(plugin_config, candidate_settings)
        if scope is not None:
            current, current_effective = resolve_effective_channel_config(plugin_config, scope=scope)
            candidate, candidate_effective = resolve_effective_channel_config(candidate_plugin, scope=scope)
            token = candidate_effective.revision
            provenance = candidate_effective.provenance
            apply_mode = candidate_effective.apply_mode
            restart_required = candidate_effective.restart_required
        else:
            current = current_config or build_channel_config_from_plugin_config(plugin_config)
            candidate = build_channel_config_from_plugin_config(candidate_plugin)
            current_effective = candidate_effective = None
            token = channel_config_revision(candidate)
            provenance = {}
            apply_mode = "hot"
            restart_required = False
    except (ValueError, EffectiveConfigError) as exc:
        current_payload = None
        try:
            current_payload = (current_config or build_channel_config_from_plugin_config(plugin_config)).to_dict()
        except Exception:
            pass
        return {
            "ok": False,
            "error_code": getattr(exc, "reason_code", "invalid_channel_config"),
            "errors": [str(exc)],
            "current": current_payload,
            "candidate": None,
            "diff": [],
        }
    return {
        "ok": True,
        "errors": [],
        "layer": layer,
        "scope": scope.to_dict() if scope is not None else None,
        "current": current.to_dict(),
        "candidate": candidate.to_dict(),
        "diff": channel_config_diff(current, candidate),
        "preflight_token": token,
        "current_revision": current_effective.revision if current_effective is not None else channel_config_revision(current),
        "candidate_revision": token,
        "provenance": provenance,
        "apply_mode": apply_mode,
        "restart_required": restart_required,
    }


def apply_channel_config_patch(container: Any, patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """应用完整校验后的 system/scoped 配置；scoped 层只在后续 exact Scope 请求生效。"""
    plugin_config = _plugin_config(container)
    current_global = getattr(container, "injection_channel_config", None) or build_channel_config_from_plugin_config(plugin_config)
    preview = validate_channel_config_patch(plugin_config, patch, current_global)
    if not preview.get("ok"):
        return preview
    if (
        str((patch or {}).get("preflight_token") or "") != str(preview.get("preflight_token") or "")
        or str((patch or {}).get("confirmation") or "") != "apply"
    ):
        return {
            **preview,
            "ok": False,
            "error_code": "channel_config_confirmation_required",
            "errors": ["应用配置需要使用最新校验返回的 preflight token 并显式确认"],
        }

    scope = _scope_from_payload((patch or {}).get("scope"))
    try:
        layer, candidate_settings = _candidate_channel_settings(plugin_config, dict(patch or {}), scope=scope)
        candidate_plugin = _plugin_with_channel_settings(plugin_config, candidate_settings)
        candidate = (
            resolve_effective_channel_config(candidate_plugin, scope=scope)[0]
            if scope is not None
            else build_channel_config_from_plugin_config(candidate_plugin)
        )
        cfg = getattr(container, "plugin_config", None)
        if isinstance(cfg, dict):
            cfg["Channel_Settings"] = copy.deepcopy(candidate_settings)
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
        if layer == "system":
            setattr(container, "injection_channel_config", candidate)
            setter = getattr(container, "injection_channel_config_setter", None)
            if callable(setter):
                setter(candidate)
            effective = getattr(container, "injection_channel_config", None)
            if effective is not candidate and getattr(effective, "to_dict", lambda: None)() != candidate.to_dict():
                raise RuntimeError("运行时回读值与候选配置不一致")
        now = time.time()
        setattr(container, "injection_channel_config_effective_since", now)
        setattr(container, "injection_channel_config_revision", preview["candidate_revision"])
    except Exception as exc:
        return {
            "ok": False,
            "error_code": getattr(exc, "reason_code", "channel_config_apply_failed"),
            "errors": [str(exc)],
            "current": preview.get("current"),
            "candidate": preview.get("candidate"),
            "diff": preview.get("diff", []),
        }

    revision = preview["candidate_revision"]
    return {
        **mutation_response(operation_kind="channel_config.apply", status="succeeded", revision=revision),
        "errors": [],
        "layer": layer,
        "scope": scope.to_dict() if scope is not None else None,
        "current": preview["current"],
        "candidate": preview["candidate"],
        "effective": candidate.to_dict(),
        "diff": preview["diff"],
        "provenance": preview.get("provenance", {}),
        "apply_mode": "hot" if layer == "system" else "next_request",
        "restart_required": False,
        "message": "通道配置已保存；system 层已热应用，scoped 层将在匹配 Scope 的下一次请求生效。",
        "verification_url": f"/observatory?config_revision={revision}",
        "effective_since": getattr(container, "injection_channel_config_effective_since", None),
    }


async def preview_channel_config_impact(
    container: Any,
    body: Mapping[str, Any] | None,
    *,
    request_scope: RuntimeScope | None = None,
) -> dict[str, Any]:
    """同一正式 Scope 下运行 current/candidate 注入与查询 dry-run。"""
    if not isinstance(body, Mapping):
        return {"ok": False, "error_code": "invalid_preview_request", "errors": ["preview body must be an object"]}
    scope = _scope_from_payload(body.get("scope"))
    if scope is None or scope.visibility != "group" or scope.session is None:
        return {"ok": False, "error_code": "canonical_group_scope_required", "errors": ["preview requires exact group RuntimeScope"]}
    if request_scope is None or request_scope != scope:
        return {"ok": False, "error_code": "cross_scope_preview_rejected", "errors": ["request Scope and preview Scope must match exactly"]}
    channels = list(getattr(container, "injection_channels", None) or [])
    if not channels:
        return {"ok": False, "error_code": "injection_channels_unavailable", "errors": ["production injection channel registry is unavailable"]}
    query_engine = getattr(container, "query_engine", None)
    if query_engine is None:
        return {"ok": False, "error_code": "query_engine_unavailable", "errors": ["production QueryEngine is unavailable"]}

    validation = validate_channel_config_patch(_plugin_config(container), body, expected_scope=scope)
    if not validation.get("ok"):
        return validation
    plugin_config = _plugin_config(container)
    try:
        current_config, current_effective = resolve_effective_channel_config(plugin_config, scope=scope)
        _, candidate_settings = _candidate_channel_settings(plugin_config, dict(body), scope=scope)
        candidate_plugin = _plugin_with_channel_settings(plugin_config, candidate_settings)
        candidate_config, candidate_effective = resolve_effective_channel_config(candidate_plugin, scope=scope)
    except (ValueError, EffectiveConfigError) as exc:
        return {"ok": False, "error_code": getattr(exc, "reason_code", "invalid_channel_config"), "errors": [str(exc)]}

    subject = scope.subject_principal_id or ""
    sender_id = str(body.get("sender_id") or (subject.rsplit(":", 1)[-1] if subject else "preview-user"))
    if subject and not subject.endswith(f":{sender_id}"):
        return {"ok": False, "error_code": "cross_scope_preview_rejected", "errors": ["sender_id does not match Scope subject_principal_id"]}
    recent_context = body.get("recent_context") or []
    if not isinstance(recent_context, list) or any(not isinstance(item, str) for item in recent_context):
        return {"ok": False, "error_code": "invalid_preview_request", "errors": ["recent_context must be a string array"]}
    message = body.get("message", "")
    if not isinstance(message, str):
        return {"ok": False, "error_code": "invalid_preview_request", "errors": ["message must be a string"]}

    try:
        from services.injection.context import InjectionContext
        from services.injection.shadow import run_config_impact_preview
    except ImportError:  # pragma: no cover - AstrBot 包导入路径
        from ...services.injection.context import InjectionContext
        from ...services.injection.shadow import run_config_impact_preview
    ctx = InjectionContext(
        event=None,
        req=SimpleNamespace(system_prompt="", extra_user_content_parts=[]),
        message=message,
        group_id=scope.session.conversation_id,
        sender_id=sender_id,
        sender_name=str(body.get("sender_name") or sender_id),
        bot_id=scope.bot_id,
        bot_profile_id=scope.bot_id,
        scope=scope,
        recent_context=list(recent_context),
        mode=current_config.mode,
        config={"memory_recall": {"context_messages": list(recent_context)}},
        now=time.time(),
    )
    return await run_config_impact_preview(
        ctx=ctx,
        channels=channels,
        current_config=current_config,
        candidate_config=candidate_config,
        expected_scope=scope,
        current_revision=current_effective.revision,
        candidate_revision=candidate_effective.revision,
        current_provenance=current_effective.provenance,
        candidate_provenance=candidate_effective.provenance,
        max_items=body.get("max_items", 20),
    )


def reset_channel_config_to_defaults(container: Any) -> dict[str, Any]:
    """恢复当前运行模式的默认通道配置。"""
    plugin_config = dict(_plugin_config(container))
    current = getattr(container, "injection_channel_config", None) or build_channel_config_from_plugin_config(plugin_config)
    defaults = _default_config(plugin_config)
    if container is not None:
        setattr(container, "injection_channel_config", defaults)
        setter = getattr(container, "injection_channel_config_setter", None)
        if callable(setter):
            setter(defaults)
        cfg = getattr(container, "plugin_config", None)
        if isinstance(cfg, dict):
            cfg["Channel_Settings"] = {}
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
        setattr(container, "injection_channel_config_effective_since", time.time())
        setattr(container, "injection_channel_config_revision", channel_config_revision(defaults))
    revision = channel_config_revision(defaults)
    return {
        **mutation_response(operation_kind="channel_config.reset", status="succeeded", revision=revision),
        "errors": [],
        "current": current.to_dict(),
        "candidate": defaults.to_dict(),
        "effective": defaults.to_dict(),
        "diff": channel_config_diff(current, defaults),
        "message": "已恢复当前模式默认通道配置",
        "verification_url": f"/observatory?config_revision={revision}",
        "effective_since": getattr(container, "injection_channel_config_effective_since", None),
    }


@channel_config_bp.route("/config/channels", methods=["GET"])
@require_auth
async def get_channel_config():
    c = get_container()
    return jsonify(build_channel_config_payload(
        _plugin_config(c),
        getattr(c, "injection_channel_config", None),
        trace_store=_trace_store_from_container(c),
        effective_since=getattr(c, "injection_channel_config_effective_since", None),
        scope=_request_scope_from_container(c),
    ))


@channel_config_bp.route("/config/channels/validate", methods=["POST"])
@require_auth
async def validate_channel_config():
    c = get_container()
    body = await request.get_json(silent=True) or {}
    expected_scope = _request_scope_from_container(c) if body.get("scope") is not None else None
    if body.get("scope") is not None and expected_scope is None:
        return jsonify({
            "ok": False,
            "error_code": "cross_scope_preview_rejected",
            "errors": ["scoped config validation requires an explicit request Scope"],
        }), 403
    return jsonify(validate_channel_config_patch(
        _plugin_config(c),
        body,
        getattr(c, "injection_channel_config", None),
        expected_scope=expected_scope,
    ))


@channel_config_bp.route("/config/channels", methods=["POST"])
@require_auth
async def update_channel_config():
    c = get_container()
    body = await request.get_json(silent=True) or {}
    if body.get("scope") is not None:
        expected_scope = _request_scope_from_container(c)
        if expected_scope is None:
            return jsonify({
                "ok": False,
                "error_code": "cross_scope_preview_rejected",
                "errors": ["scoped config apply requires an explicit request Scope"],
            }), 403
        scoped_validation = validate_channel_config_patch(
            _plugin_config(c),
            body,
            getattr(c, "injection_channel_config", None),
            expected_scope=expected_scope,
        )
        if not scoped_validation.get("ok"):
            return jsonify(scoped_validation), 403 if scoped_validation.get("error_code") == "cross_scope_preview_rejected" else 400
    result = apply_channel_config_patch(c, body)
    status = 409 if result.get("error_code") == "channel_config_confirmation_required" else 200
    return jsonify(result), status


@channel_config_bp.route("/config/channels/impact-preview", methods=["POST"])
@require_auth
async def preview_channel_config():
    c = get_container()
    body = await request.get_json(silent=True) or {}
    result = await preview_channel_config_impact(c, body, request_scope=_request_scope_from_container(c))
    status = 200 if result.get("ok") else (403 if result.get("error_code") == "cross_scope_preview_rejected" else 400)
    return jsonify(result), status


@channel_config_bp.route("/config/channels/defaults", methods=["POST"])
@require_auth
async def reset_channel_config():
    c = get_container()
    return jsonify(reset_channel_config_to_defaults(c))


__all__ = [
    "channel_config_bp",
    "build_channel_config_payload",
    "validate_channel_config_patch",
    "apply_channel_config_patch",
    "preview_channel_config_impact",
    "reset_channel_config_to_defaults",
]
