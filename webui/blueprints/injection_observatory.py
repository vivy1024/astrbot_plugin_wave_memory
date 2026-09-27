"""Inject Observatory API — 注入 trace 列表与详情。"""

from __future__ import annotations

import time
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
        args: dict[str, Any] = {}
    request = _Request()  # type: ignore[assignment]

try:
    from services.injection.feedback_store import MemoryFeedbackStore
    from services.injection.trace_store import MEMORY_ITEM_CHANNELS, InjectionTraceStore
    from tools.injection_explain import build_injection_explanation
except Exception:  # pragma: no cover - AstrBot 包导入路径
    from ...services.injection.feedback_store import MemoryFeedbackStore
    from ...services.injection.trace_store import MEMORY_ITEM_CHANNELS, InjectionTraceStore
    from ...tools.injection_explain import build_injection_explanation

from ..api_contract import error_payload, not_found_payload, page_response
from ..container import get_container
try:
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时直接放行
    def require_auth(func):
        return func

injection_observatory_bp = Blueprint("injection_observatory", __name__, url_prefix="/api/observatory")


def _float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


# 前端跳转需要、但 build_injection_explanation 的精简条目里没有的原始字段（白名单，不透传正文）
_LINK_FIELD_KEYS = (
    "group_id", "sender_id", "title", "note_id", "community_id", "belief_ids", "example_id",
    "rowid", "subject", "predicate", "object", "word", "subject_principal_id", "block",
)


def _memory_scope_rows(conn: Any, memory_ids: set[int]) -> dict[int, dict[str, Any]]:
    """按 id 批量读取记忆自身的作用域，用来生成精确的记忆页链接（记忆的 session_id 以行为准）。"""
    if conn is None or not memory_ids:
        return {}
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    except Exception:
        return {}
    wanted = [name for name in ("id", "bot_id", "session_id", "visibility", "group_id") if name in columns]
    if "id" not in wanted:
        return {}
    ids = sorted(memory_ids)[:200]
    try:
        rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM memories WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
    except Exception:
        return {}
    return {int(row[0]): dict(zip(wanted, row)) for row in rows}


def _attach_link_fields(payload: dict[str, Any], trace: Mapping[str, Any], conn: Any) -> None:
    """给详情里的 hit_items / filtered_items 补上跳转定位字段 ``link``。"""
    import json

    raw_by_channel: dict[int, tuple[list[Any], list[Any]]] = {}
    for index, channel in enumerate(trace.get("channels") or []):
        try:
            details = json.loads(channel.get("details") or "{}")
        except Exception:
            details = {}
        if not isinstance(details, Mapping):
            details = {}
        items = [item for item in details.get("items") or [] if isinstance(item, Mapping)]
        filtered = [item for item in details.get("filtered") or [] if isinstance(item, Mapping)]
        raw_by_channel[index] = (items, filtered)

    memory_ids: set[int] = set()
    pairs: list[tuple[str, dict[str, Any], Mapping[str, Any]]] = []
    for index, channel in enumerate(payload.get("channels") or []):
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("channel") or "")
        items, filtered = raw_by_channel.get(index, ([], []))
        for key, raw_items in (("hit_items", items), ("filtered_items", filtered)):
            for brief, raw in zip(channel.get(key) or [], raw_items):
                if not isinstance(brief, dict):
                    continue
                pairs.append((name, brief, raw))
                if name in MEMORY_ITEM_CHANNELS and isinstance(raw.get("id"), int) and not isinstance(raw.get("id"), bool):
                    memory_ids.add(int(raw["id"]))

    scopes = _memory_scope_rows(conn, memory_ids)
    for name, brief, raw in pairs:
        link: dict[str, Any] = {key: raw.get(key) for key in _LINK_FIELD_KEYS if raw.get(key) not in (None, "", [])}
        if name in MEMORY_ITEM_CHANNELS and isinstance(raw.get("id"), int):
            link["kind"] = "memory"
            link["memory_id"] = int(raw["id"])
            scope = scopes.get(int(raw["id"]))
            if scope:
                link["memory_scope"] = {k: scope.get(k) for k in ("bot_id", "session_id", "visibility", "group_id")}
        if link:
            brief["link"] = link


def build_trace_list_payload(trace_store: InjectionTraceStore, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造 trace 列表响应。"""
    filters = filters or {}
    now = time.time()
    from_ts = _float(filters.get("from_ts") or filters.get("from"), 0.0)
    to_ts = _float(filters.get("to_ts") or filters.get("to"), now)
    limit = max(1, min(_int(filters.get("limit"), 100), 500))
    traces = trace_store.query(
        from_ts=from_ts,
        to_ts=to_ts,
        group_id=filters.get("group_id") or None,
        sender_id=filters.get("sender_id") or None,
        bot_id=filters.get("bot_id") or None,
        channel=filters.get("channel") or None,
        status=filters.get("status") or None,
        has_error=_bool_or_none(filters.get("has_error")),
        scope=filters.get("scope") or filters.get("chat_type") or None,
        session_id=filters.get("session_id") or None,
        config_revision=filters.get("config_revision") or None,
        source=filters.get("source") or None,
        limit=limit,
    )
    return {"traces": traces, "count": len(traces), "limit": limit}


def build_trace_detail_payload(
    trace_store: InjectionTraceStore,
    feedback_store: MemoryFeedbackStore | None,
    trace_id: str,
) -> dict[str, Any] | None:
    """构造 trace 详情响应，包含通道命中/过滤和反馈记录。"""
    trace = trace_store.get(trace_id)
    if not trace:
        return None
    payload = build_injection_explanation(trace)
    raw_payload = str(trace.get("payload_json") or "")
    if raw_payload:
        payload["raw_payload"] = raw_payload
        try:
            import json
            decoded = json.loads(raw_payload)
            raw_trace = decoded.get("trace") if isinstance(decoded, Mapping) else None
            if isinstance(raw_trace, Mapping):
                payload["request"] = {
                    **dict(payload.get("request") or {}),
                    "message": raw_trace.get("message"),
                    "metadata": raw_trace.get("metadata"),
                }
                payload["final_text"] = raw_trace.get("final_text") or raw_trace.get("final_injection") or ""
            raw_channels = decoded.get("channels") if isinstance(decoded, Mapping) else None
            if isinstance(raw_channels, list):
                payload["raw_channels"] = raw_channels
        except Exception:
            payload["raw_payload_status"] = "invalid_json"
    _attach_link_fields(payload, trace, getattr(trace_store, "conn", None))
    payload["feedback"] = feedback_store.list_for_trace(trace_id) if feedback_store else []
    payload["feedback_status"] = "present" if payload["feedback"] else "none"
    return payload


def build_memory_traces_payload(
    trace_store: InjectionTraceStore,
    memory_id: int,
    filters: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """构造「这条记忆最近被哪些回复用到」响应；必须带 bot_id，只返回该 Bot 的 trace。"""
    filters = filters or {}
    bot_id = str(filters.get("bot_id") or "").strip()
    if not bot_id:
        return error_payload("scope_required", "bot_id is required"), 400
    limit = max(1, min(_int(filters.get("limit"), 10), 50))
    started = time.perf_counter()
    items = trace_store.find_traces_for_memory(int(memory_id), bot_id=bot_id, limit=limit)
    for item in items:
        item["detail_url"] = f"/api/observatory/traces/{item['trace_id']}"
    return {
        "memory_id": int(memory_id),
        "bot_id": bot_id,
        "items": items,
        "count": len(items),
        "limit": limit,
        "channels": list(MEMORY_ITEM_CHANNELS),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }, 200


def _stores_from_container() -> tuple[InjectionTraceStore | None, MemoryFeedbackStore | None]:
    c = get_container()
    db = getattr(c, "db", None)
    if not db:
        return None, None
    if getattr(db, "closed", False):
        try:
            db.reopen()
        except Exception:
            return None, None
    conn = getattr(db, "conn", None)
    if not conn:
        return None, None
    trace_store = InjectionTraceStore(conn)
    trace_store.ensure_schema()
    feedback_store = MemoryFeedbackStore(conn)
    feedback_store.ensure_schema()
    return trace_store, feedback_store


@injection_observatory_bp.route("/traces", methods=["GET"])
@require_auth
async def list_traces():
    trace_store, _ = _stores_from_container()
    if not trace_store:
        return jsonify(error_payload("service_unavailable", "Trace store is unavailable", retryable=True)), 503
    filters = dict(getattr(request, "args", {}) or {})
    now = time.time()
    limit = max(1, min(_int(filters.get("limit"), 100), 500))
    offset = max(0, _int(filters.get("offset"), 0))
    query_filters = {
        "from_ts": _float(filters.get("from_ts") or filters.get("from"), 0.0),
        "to_ts": _float(filters.get("to_ts") or filters.get("to"), now),
        "group_id": filters.get("group_id") or None,
        "sender_id": filters.get("sender_id") or None,
        "bot_id": filters.get("bot_id") or None,
        "channel": filters.get("channel") or None,
        "status": filters.get("status") or None,
        "has_error": _bool_or_none(filters.get("has_error")),
        "scope": filters.get("scope") or filters.get("chat_type") or None,
        "session_id": filters.get("session_id") or None,
        "config_revision": filters.get("config_revision") or None,
        "source": filters.get("source") or None,
    }
    try:
        traces = trace_store.query(**query_filters, limit=limit, offset=offset)
        total = trace_store.count(**query_filters)
    except Exception:
        return jsonify(error_payload("service_unavailable", "Trace store is unavailable", retryable=True)), 503
    for item in traces:
        item["detail_url"] = f"/api/observatory/traces/{item['trace_id']}"
    return jsonify(page_response(traces, total=total, limit=limit, offset=offset))


@injection_observatory_bp.route("/traces/<trace_id>", methods=["GET"])
@require_auth
async def get_trace_detail(trace_id: str):
    trace_store, feedback_store = _stores_from_container()
    if not trace_store:
        return jsonify(error_payload("service_unavailable", "Trace store is unavailable", retryable=True)), 503
    payload = build_trace_detail_payload(trace_store, feedback_store, trace_id)
    if payload is None:
        return jsonify(not_found_payload()), 404
    return jsonify(payload)



@injection_observatory_bp.route("/memories/<int:memory_id>/traces", methods=["GET"])
@require_auth
async def list_memory_traces(memory_id: int):
    trace_store, _ = _stores_from_container()
    if not trace_store:
        return jsonify(error_payload("service_unavailable", "Trace store is unavailable", retryable=True)), 503
    try:
        payload, status = build_memory_traces_payload(trace_store, memory_id, dict(getattr(request, "args", {}) or {}))
    except Exception:
        return jsonify(error_payload("service_unavailable", "Trace store is unavailable", retryable=True)), 503
    return jsonify(payload), status


__all__ = [
    "injection_observatory_bp",
    "build_trace_list_payload",
    "build_trace_detail_payload",
    "build_memory_traces_payload",
]
