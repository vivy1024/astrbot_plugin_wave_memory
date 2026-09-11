"""Tag 神经云图的 Scope 严格隔离只读 API。"""

from __future__ import annotations

from typing import Any, Iterable

from quart import Blueprint, current_app, jsonify, request

from ..api_contract import current_runtime_scope, error_payload, not_found_payload
from ..container import get_container
from ..graph_projection import TAG_GRAPH_LAYERS, build_tag_graph_projection, find_tag_graph_path
from ..middleware.auth import require_auth

try:
    from ...domain.scope import RuntimeScope, scope_to_dict
except ImportError:  # pragma: no cover - focused tests import webui as top-level
    from domain.scope import RuntimeScope, scope_to_dict


tag_graph_bp = Blueprint("tag_graph", __name__, url_prefix="/api/tag-graph")


def _request_scope() -> RuntimeScope | None:
    try:
        provider = current_app.extensions.get("wave_api_contract", {}).get("request_scope_provider")
    except RuntimeError:
        provider = None
    scope = current_runtime_scope(provider)
    if scope is None or scope.visibility != "group" or scope.session is None:
        return None
    return scope


def _object_refs():
    try:
        return current_app.extensions.get("wave_api_contract", {}).get("object_refs")
    except RuntimeError:
        return None


def _scope_failure():
    return jsonify(error_payload("scope_required", "A canonical group RuntimeScope is required")), 400


def _parse_layers(value: Any, *, default: Iterable[str] = TAG_GRAPH_LAYERS) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if isinstance(value, (list, tuple, set)):
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    raise ValueError("invalid_tag_graph_layers")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _scope_query(scope: RuntimeScope) -> dict[str, str]:
    assert scope.session is not None
    return {"bot_id": scope.bot_id, "session_id": scope.session.id, "visibility": scope.visibility}


def _descriptor(refs, *, kind: str, locator: int | str, scope: RuntimeScope, revision: int) -> dict[str, Any] | None:
    if refs is None:
        return None
    ref = refs.issue(kind=kind, locator=locator, scope=scope, revision=revision)
    return {
        "ref": ref, "kind": kind, "locator": locator,
        "scope_key": scope.session.id if scope.session is not None else "",
        "scope_query": _scope_query(scope), "version": revision,
    }


_LEGEND_KNOWN_TYPES = ("keyword", "entity", "topic", "emotion", "fact", "jargon", "default")


def _legend_settings() -> dict[str, Any]:
    """图例展示配置：类型顺序、显隐、是否计数。仅影响展示，不影响图谱数据。"""
    container = get_container()
    tag_cfg = (getattr(container, "plugin_config", None) or {}).get("Tag_Settings", {}) or {}
    raw = str(tag_cfg.get("graph_legend_types", "") or "").strip()
    # 未配置或留空 => 展示图谱中实际出现的全部类型（按出现顺序，前端兜底）。
    types = [item.strip() for item in raw.split(",") if item.strip()] if raw else []
    enabled = bool(tag_cfg.get("graph_legend_enabled", True))
    show_count = bool(tag_cfg.get("graph_legend_show_count", True))
    # 未知类型名直接丢弃，避免把拼写错误当成一个空图例项渲染出来。
    return {
        "enabled": enabled,
        "types": [t for t in types if t in _LEGEND_KNOWN_TYPES],
        "show_count": show_count,
        "known_types": list(_LEGEND_KNOWN_TYPES),
    }


def _decorate_graph(payload: dict[str, Any], *, scope: RuntimeScope) -> dict[str, Any]:
    refs = _object_refs()
    for node in payload.get("nodes", ()):
        locator = int(node.get("locator"))
        descriptor = _descriptor(refs, kind="tag", locator=locator, scope=scope, revision=int(node.get("revision") or 1))
        if descriptor is not None:
            node["ref"] = descriptor["ref"]
            node["object_ref"] = descriptor
        for memory in node.get("associated_memories", ()):
            memory_descriptor = _descriptor(
                refs, kind="memory", locator=int(memory["id"]), scope=scope,
                revision=int(memory.get("version") or 1),
            )
            if memory_descriptor is not None:
                memory["ref"] = memory_descriptor["ref"]
                memory["object_ref"] = memory_descriptor
    payload["legend"] = _legend_settings()
    return payload


def _resolve_tag_ref(value: Any, *, scope: RuntimeScope):
    refs = _object_refs()
    if refs is None:
        return None, "not-found"
    return refs.resolve_with_state(value, kind="tag", request_scope=scope)


def _graph_from_request(scope: RuntimeScope, *, layers: tuple[str, ...] | None = None, max_nodes: int | None = None) -> dict[str, Any]:
    container = get_container()
    conn = getattr(getattr(container, "db", None), "conn", None)
    selected_layers = layers if layers is not None else _parse_layers(request.args.get("layers"))
    return build_tag_graph_projection(
        conn=conn,
        scope=scope,
        layers=selected_layers,
        min_confidence=_bounded_float(request.args.get("min_confidence"), 0.0, 0.0, 1.0),
        max_nodes=max_nodes or _bounded_int(request.args.get("max_nodes"), 300, 1, 1000),
        include_pulse=_as_bool(request.args.get("include_pulse")),
        pulse_half_life_hours=_bounded_float(request.args.get("pulse_half_life_hours"), 72.0, 1.0, 24.0 * 365.0),
    )


@tag_graph_bp.route("", methods=["GET"])
@tag_graph_bp.route("/", methods=["GET"])
@require_auth
async def graph():
    scope = _request_scope()
    if scope is None:
        return _scope_failure()
    try:
        payload = _decorate_graph(_graph_from_request(scope), scope=scope)
        return jsonify(payload)
    except ValueError as exc:
        return jsonify(error_payload(str(exc), "Invalid Tag graph request")), 400


@tag_graph_bp.route("/tag", methods=["GET"])
@require_auth
async def tag_detail():
    scope = _request_scope()
    if scope is None:
        return _scope_failure()
    binding, state = _resolve_tag_ref(request.args.get("ref"), scope=scope)
    if binding is None:
        status = 409 if state == "scope-mismatch" else 404
        return jsonify(error_payload(state.replace("-", "_"), "Tag reference is unavailable")), status
    try:
        payload = _decorate_graph(_graph_from_request(scope, max_nodes=10_000), scope=scope)
    except ValueError as exc:
        return jsonify(error_payload(str(exc), "Invalid Tag graph request")), 400
    node = next((item for item in payload.get("nodes", ()) if int(item.get("locator")) == int(binding.locator)), None)
    if node is None:
        return jsonify(not_found_payload()), 404
    return jsonify({"item": node, "scope": scope_to_dict(scope), "read_only": True})


@tag_graph_bp.route("/path", methods=["POST"])
@require_auth
async def path():
    scope = _request_scope()
    if scope is None:
        return _scope_failure()
    body = await request.get_json(silent=True) or {}
    source_binding, source_state = _resolve_tag_ref(body.get("source_ref"), scope=scope)
    target_binding, target_state = _resolve_tag_ref(body.get("target_ref"), scope=scope)
    if source_binding is None or target_binding is None:
        state = "scope_mismatch" if "scope-mismatch" in {source_state, target_state} else "not_found"
        return jsonify(error_payload(state, "Tag path endpoint requires two valid scoped ObjectRefs")), 409 if state == "scope_mismatch" else 404
    try:
        layers = _parse_layers(body.get("layers"))
        graph_payload = _graph_from_request(scope, layers=layers, max_nodes=10_000)
        result = find_tag_graph_path(
            graph_payload,
            source_id=f"tag:{int(source_binding.locator)}",
            target_id=f"tag:{int(target_binding.locator)}",
            layers=layers,
            max_depth=_bounded_int(body.get("max_depth"), 6, 1, 12),
        )
        result["scope"] = scope_to_dict(scope)
        return jsonify(_decorate_graph(result, scope=scope))
    except ValueError as exc:
        return jsonify(error_payload(str(exc), "Invalid Tag path request")), 400


__all__ = ["tag_graph_bp"]
