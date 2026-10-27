"""Soul Blueprint — 按 Scope 读取灵魂状态（心情/关切/经历时间线/关系）与强制自省。

旧表（concerns / time_anchors / bot_mood / mood_snapshots）的读写端点已删除；
非 GET 请求仍由 before_request 统一返回 410。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from quart import Blueprint, current_app, jsonify, request

from ..api_contract import current_runtime_scope, error_payload, page_response
from ..container import get_container
from ..middleware.auth import require_auth

try:
    from ...domain.scope import RuntimeScope, ScopeValidationError
    from ...engine.db.scoped_soul_repo import ScopedSoulScopeError
except ImportError:  # pragma: no cover - plugin root may be imported directly
    from domain.scope import RuntimeScope, ScopeValidationError
    from engine.db.scoped_soul_repo import ScopedSoulScopeError

soul_bp = Blueprint("soul", __name__, url_prefix="/api")


@soul_bp.before_request
async def _reject_unscoped_soul_mutations():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    # 强制自省是唯一允许的 POST：只读重算，不属于 legacy 写路径
    if request.method == "POST" and request.path == "/api/soul/state/refresh":
        return None
    return jsonify({"error": {"code": "legacy_mutation_disabled"}}), 410


def _formal_group_scope() -> RuntimeScope:
    try:
        provider = current_app.extensions.get("wave_api_contract", {}).get("request_scope_provider")
    except RuntimeError:
        provider = None
    scope = current_runtime_scope(provider)
    if scope is None:
        raise ScopedSoulScopeError("scope_required")
    if scope.visibility != "group" or scope.session is None:
        raise ScopedSoulScopeError("soul_scope_visibility_unsupported")
    return scope


def _scope_payload(scope: RuntimeScope) -> dict:
    assert scope.session is not None
    return {
        "kind": "SoulScope",
        "bot_id": scope.bot_id,
        "session_id": scope.session.id,
        "visibility": scope.visibility,
        "platform_id": scope.session.platform_id,
        "conversation_id": scope.session.conversation_id,
        "subject_principal_id": scope.subject_principal_id,
    }


def _scoped_repo(container):
    repo = getattr(container, "soul_repository", None)
    if repo is None:
        repo = getattr(getattr(container, "db", None), "scoped_soul", None)
    return repo


def _object_refs():
    try:
        return current_app.extensions.get("wave_api_contract", {}).get("object_refs")
    except RuntimeError:
        return None


def _scope_key(scope: RuntimeScope) -> str:
    assert scope.session is not None
    return f"{scope.bot_id}:{scope.session.id}:{scope.visibility}"


def _memory_evidence(connection, *, scope: RuntimeScope, memory_id: Any, refs) -> dict[str, Any]:
    try:
        normalized_id = int(memory_id)
    except (TypeError, ValueError):
        return {
            "type": "memory",
            "id": str(memory_id),
            "content_hash": None,
            "captured_at": None,
            "source_scope": _scope_key(scope),
            "availability": "unknown",
            "object_ref": None,
        }
    if connection is None:
        row = None
    else:
        row = connection.execute(
            """SELECT content, timestamp, version, resolution_state, COALESCE(quarantine, 0)
                 FROM memories
                WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (normalized_id, scope.bot_id, scope.session.id, scope.visibility),
        ).fetchone()
    if row is None:
        return {
            "type": "memory",
            "id": str(normalized_id),
            "content_hash": None,
            "captured_at": None,
            "source_scope": _scope_key(scope),
            "availability": "unavailable",
            "object_ref": None,
        }
    content = str(row[0] or "")
    available = str(row[3] or "") == "resolved" and not bool(row[4])
    object_ref = None
    if available and refs is not None:
        ref = refs.issue(kind="memory", locator=normalized_id, scope=scope, revision=int(row[2] or 1))
        object_ref = {
            "ref": ref,
            "kind": "memory",
            "locator": normalized_id,
            "scope_key": scope.session.id,
            "version": int(row[2] or 1),
        }
    return {
        "type": "memory",
        "id": str(normalized_id),
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "captured_at": row[1],
        "source_scope": _scope_key(scope),
        "availability": "available" if available else "quarantined",
        "object_ref": object_ref,
        "summary": content[:240] if available else None,
    }


def _normalize_evidence_items(connection, *, scope: RuntimeScope, values: Any, refs) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        memory_id = raw.get("memory_id")
        if memory_id is None and str(raw.get("kind") or raw.get("type") or "").strip().lower() == "memory":
            memory_id = raw.get("id")
        if memory_id is not None:
            item = _memory_evidence(connection, scope=scope, memory_id=memory_id, refs=refs)
            supplied_hash = str(raw.get("content_hash") or "").strip()
            if supplied_hash and item.get("content_hash") and supplied_hash != item["content_hash"]:
                item["availability"] = "unavailable"
                item["object_ref"] = None
            # Memory evidence defaults to the memory text, but manual calibration
            # evidence may carry an explicit audit summary that must survive reads.
            supplied_summary = raw.get("summary")
            if isinstance(supplied_summary, str) and supplied_summary.strip():
                item["summary"] = supplied_summary.strip()
            normalized.append(item)
            continue
        evidence_type = str(raw.get("type") or raw.get("kind") or "evidence").strip() or "evidence"
        evidence_id = str(raw.get("id") or raw.get(f"{evidence_type}_id") or "").strip()
        if not evidence_id:
            continue
        item = {
            "type": evidence_type,
            "id": evidence_id,
            "content_hash": raw.get("content_hash"),
            "captured_at": raw.get("captured_at"),
            "source_scope": _scope_key(scope),
            "availability": "unknown",
            "object_ref": None,
            "summary": raw.get("summary"),
        }
        if evidence_type in {"relationship_event", "relationship_calibration"} and refs is not None:
            ref = refs.issue(kind=evidence_type, locator=evidence_id, scope=scope, revision=int(raw.get("revision") or 1))
            item["object_ref"] = {
                "ref": ref,
                "kind": evidence_type,
                "locator": evidence_id,
                "scope_key": scope.session.id,
                "version": int(raw.get("revision") or 1),
            }
        normalized.append(item)
    return normalized


def _normalize_soul_state_evidence(state: dict[str, Any], *, scope: RuntimeScope, connection, refs) -> dict[str, Any]:
    normalized = dict(state)
    for key in ("mood", "relationship"):
        if isinstance(normalized.get(key), Mapping):
            normalized[key] = {**normalized[key], "evidence": _normalize_evidence_items(connection, scope=scope, values=normalized[key].get("evidence", []), refs=refs)}
    for collection_name in ("concerns", "timeline"):
        collection = normalized.get(collection_name)
        if isinstance(collection, Mapping):
            collection = dict(collection)
            collection["items"] = [
                {**item, "evidence": _normalize_evidence_items(connection, scope=scope, values=item.get("evidence", []), refs=refs)}
                if isinstance(item, Mapping) else item
                for item in collection.get("items", [])
            ]
            normalized[collection_name] = collection
    history = normalized.get("relationship_history")
    if isinstance(history, Mapping):
        history = dict(history)
        history["items"] = [
            {**item, "evidence": _normalize_evidence_items(connection, scope=scope, values=item.get("evidence", []), refs=refs)}
            if isinstance(item, Mapping) else item
            for item in history.get("items", [])
        ]
        normalized["relationship_history"] = history
    return normalized


def _normalized_state(state: dict[str, Any], scope: RuntimeScope) -> dict[str, Any]:
    container = get_container()
    connection = getattr(getattr(container, "db", None), "conn", None)
    return _normalize_soul_state_evidence(
        state,
        scope=scope,
        connection=connection,
        refs=_object_refs(),
    )


def _state_success_payload(
    state: dict[str, Any],
    scope: RuntimeScope,
    *,
    limit: int,
    offset: int,
    refreshed_at: float | None = None,
) -> dict[str, Any]:
    """把仓储聚合状态呈现为正式 Soul payload；GET 与强制自省（refresh）共用同一组装。"""
    concerns = state["concerns"]
    timeline = state["timeline"]
    relationship_history = state.get("relationship_history", {"items": [], "total": 0, "revision": None})
    historical_audit = state.get("historical_audit") or {
        "available": False,
        "total": 0,
        "by_type": [],
        "recent": [],
        "readonly": True,
        "affects_affinity": False,
    }
    if isinstance(historical_audit, dict):
        historical_audit = {
            **historical_audit,
            "readonly": True,
            "affects_affinity": False,
        }
    relationship = dict(state["relationship"])
    refs = _object_refs()
    if relationship.get("revision") is not None and relationship.get("people_ref") and refs is not None:
        ref = refs.issue(kind="relationship", locator=relationship["people_ref"], scope=scope, revision=int(relationship["revision"]))
        relationship["people_ref"] = {"ref": ref, "kind": "relationship", "locator": relationship["people_ref"], "scope_key": scope.session.id, "version": int(relationship["revision"])}
    runtime_refresh: dict[str, Any] = {
        "status": "refreshed" if refreshed_at is not None else "available",
        "operation": None,
        "reason_code": None,
    }
    if refreshed_at is not None:
        runtime_refresh["refreshed_at"] = refreshed_at
    return {
        "scope": _scope_payload(scope),
        "source": {"health": "ready", "reason_code": None},
        "revision": state.get("revision"),
        "evidence": state.get("evidence", []),
        "mood": state["mood"],
        "concerns": {
            **page_response(concerns["items"], total=concerns["total"], limit=limit, offset=offset),
            "revision": concerns.get("revision"),
        },
        "timeline": {
            **page_response(timeline["items"], total=timeline["total"], limit=limit, offset=offset),
            "revision": timeline.get("revision"),
        },
        "relationship_history": {
            **page_response(relationship_history["items"], total=relationship_history["total"], limit=limit, offset=offset),
            "revision": relationship_history.get("revision"),
        },
        "historical_audit": historical_audit,
        "relationship": relationship,
        "soul_context": state.get("soul_context", {"status": "unavailable", "reason_code": "formal_soul_context_unavailable", "timezone": None, "circadian": None, "energy": None, "sleepiness": None}),
        "capabilities": {
            "mutate": {"available": bool(relationship.get("calibration", {}).get("available")), "reason_code": None if relationship.get("calibration", {}).get("available") else "relationship_calibration_unavailable"},
            "calibration": relationship.get("calibration", {"available": False, "reason_code": "relationship_unknown"}),
            "runtime_refresh": {"available": True, "reason_code": None},
        },
        "runtime_refresh": runtime_refresh,
    }


@soul_bp.route("/soul/state", methods=["GET"])
@require_auth
async def soul_state():
    """读取正式 Scoped Soul 聚合；仓储不可用时绝不投影 legacy 数据。"""
    try:
        scope = _formal_group_scope()
        limit = int(request.args.get("limit", 25))
        offset = int(request.args.get("offset", 0))
        from_raw = request.args.get("from_ts")
        to_raw = request.args.get("to_ts")
        from_ts = None if from_raw in {None, ""} else float(from_raw)
        to_ts = None if to_raw in {None, ""} else float(to_raw)
        if limit not in {25, 50, 100} or offset < 0:
            raise ValueError("invalid_pagination")
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise ValueError("invalid_time_range")
    except (ScopedSoulScopeError, ScopeValidationError, TypeError, ValueError) as exc:
        code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or str(exc)
        return jsonify({"error": {"code": code}}), 400

    repo = _scoped_repo(get_container())
    if repo is None:
        reason = "soul_scoped_repository_unavailable"
        unavailable_page = page_response([], total=None, limit=limit, offset=offset, unavailable_reason=reason)
        return jsonify({
            "scope": _scope_payload(scope),
            "source": {"health": "unavailable", "reason_code": reason},
            "revision": None,
            "evidence": [],
            "mood": {"value": None, "state": "unknown", "components": None,
                     "policy_version": None, "revision": None, "evidence": []},
            "concerns": unavailable_page,
            "timeline": unavailable_page,
            "relationship_history": unavailable_page,
            "historical_audit": {
                "available": False,
                "total": 0,
                "by_type": [],
                "recent": [],
                "readonly": True,
                "affects_affinity": False,
                "reason_code": reason,
            },
            "relationship": {"affinity": None, "state": "unknown", "revision": None,
                             "evidence": [], "people_ref": None, "values": None,
                             "calibration": {"available": False, "reason_code": "relationship_repository_unavailable"}},
            "soul_context": {"status": "unavailable", "reason_code": "formal_soul_context_unavailable",
                             "timezone": None, "circadian": None, "energy": None, "sleepiness": None},
            "capabilities": {
                "mutate": {"available": False, "reason_code": "relationship_calibration_unavailable"},
                "calibration": {"available": False, "reason_code": "relationship_repository_unavailable"},
                "runtime_refresh": {"available": False, "reason_code": "soul_runtime_refresh_unavailable"},
            },
            "runtime_refresh": {"status": "unavailable", "operation": None, "reason_code": reason},
        })

    try:
        state = repo.get_state(
            scope,
            subject_principal_id=scope.subject_principal_id,
            limit=limit,
            offset=offset,
            from_ts=from_ts,
            to_ts=to_ts,
        )
    except (ScopedSoulScopeError, ScopeValidationError, TypeError, ValueError) as exc:
        code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or str(exc)
        return jsonify({"error": {"code": code}}), 400

    state = _normalized_state(state, scope)
    return jsonify(_state_success_payload(state, scope, limit=limit, offset=offset))


@soul_bp.route("/soul/state/refresh", methods=["POST"])
@require_auth
async def soul_state_refresh():
    """手动触发当前 Scoped Soul 的立即自省重构。

    与 GET 同为只读投影：不写数据、不调用 LLM、不新增事件，revision 不变；
    响应携带 refreshed_at 供前端提示本次重算时刻。
    """
    try:
        scope = _formal_group_scope()
        limit = int(request.args.get("limit", 25))
        offset = int(request.args.get("offset", 0))
        from_raw = request.args.get("from_ts")
        to_raw = request.args.get("to_ts")
        from_ts = None if from_raw in {None, ""} else float(from_raw)
        to_ts = None if to_raw in {None, ""} else float(to_raw)
        if limit not in {25, 50, 100} or offset < 0:
            raise ValueError("invalid_pagination")
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise ValueError("invalid_time_range")
    except (ScopedSoulScopeError, ScopeValidationError, TypeError, ValueError) as exc:
        code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or str(exc)
        return jsonify({"error": {"code": code}}), 400

    repo = _scoped_repo(get_container())
    if repo is None:
        return jsonify({"error": {"code": "soul_scoped_repository_unavailable"}}), 503

    try:
        state = repo.refresh_state(
            scope,
            subject_principal_id=scope.subject_principal_id,
            limit=limit,
            offset=offset,
            from_ts=from_ts,
            to_ts=to_ts,
        )
    except (ScopedSoulScopeError, ScopeValidationError, TypeError, ValueError) as exc:
        code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or str(exc)
        return jsonify({"error": {"code": code}}), 400

    state = _normalized_state(state, scope)
    return jsonify(_state_success_payload(state, scope, limit=limit, offset=offset, refreshed_at=time.time()))
