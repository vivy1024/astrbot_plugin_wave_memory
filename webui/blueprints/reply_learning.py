"""Authenticated, exact-scope human-expression review and reply observation APIs.

Register reply_learning_bp and populate container.expression_policy and
container.reply_feedback_service at startup. No request creates schema or calls
a model. Review uses optimistic revision plus a durable idempotency key.
"""

from __future__ import annotations

import json
import sqlite3

from quart import Blueprint, jsonify, request

from ..api_contract import error_payload, page_response
from ..container import get_container
from ..middleware.auth import require_auth

try:
    from ...domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from ...services.expression_policy import ExpressionReviewError
    from ...services.reply_feedback import group_owner
except ImportError:
    from domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from services.expression_policy import ExpressionReviewError
    from services.reply_feedback import group_owner


reply_learning_bp = Blueprint("reply_learning", __name__, url_prefix="/api/reply-learning")


def _query_scope() -> RuntimeScope:
    values = [request.args.get(key) for key in ("bot_id", "session_id", "visibility")]
    if any(value is None for value in values):
        raise ExpressionReviewError("scope_required")
    bot_id, session_id, visibility = values
    try:
        platform, kind, conversation = session_id.split(":", 2)
    except ValueError as exc:
        raise ExpressionReviewError("invalid_session_id") from exc
    scope = RuntimeScope(bot_id, visibility, SessionRef(session_id, platform, kind, conversation))
    if group_owner(scope) is None:
        raise ExpressionReviewError("group_scope_required")
    return scope


def _body_scope(body: dict) -> RuntimeScope:
    raw = body.get("scope")
    if not isinstance(raw, dict):
        raise ExpressionReviewError("scope_required")
    scope = ScopeCodec.from_dict(raw) if "kind" in raw else RuntimeScope.from_dict(raw)
    if group_owner(scope) is None:
        raise ExpressionReviewError("group_scope_required")
    # If a caller also supplies query scope, contradictory routing is rejected.
    if any(key in request.args for key in ("bot_id", "session_id", "visibility")):
        if group_owner(scope) != group_owner(_query_scope()):
            raise ExpressionReviewError("scope_mismatch")
    return scope


def _page() -> tuple[int, int]:
    try:
        limit, offset = int(request.args.get("limit", 25)), int(request.args.get("offset", 0))
    except ValueError as exc:
        raise ExpressionReviewError("invalid_pagination") from exc
    if not 1 <= limit <= 100 or offset < 0:
        raise ExpressionReviewError("invalid_pagination")
    return limit, offset


def _service(name):
    service = getattr(get_container(), name, None)
    if service is None:
        raise ExpressionReviewError("service_unavailable", 503)
    return service


def _failure(exc):
    if isinstance(exc, sqlite3.Error):
        return jsonify(error_payload("store_unavailable", "Reply learning store is unavailable", retryable=True)), 503
    code = getattr(exc, "code", None) or getattr(exc, "reason_code", "invalid_request")
    status = getattr(exc, "status", 400)
    return jsonify(error_payload(code, code)), status


@reply_learning_bp.route("/expressions", methods=["GET"])
@require_auth
async def list_expressions():
    try:
        scope = _query_scope()
        limit, offset = _page()
        policy = _service("expression_policy")
        items, total = policy.list_candidates(scope, status=request.args.get("status", "pending"), limit=limit, offset=offset)
        payload = page_response(items, total=total, limit=limit, offset=offset)
        payload.update(scope=scope.to_dict(), enabled=policy.enabled)
        return jsonify(payload)
    except (ExpressionReviewError, ScopeValidationError, sqlite3.Error) as exc:
        return _failure(exc)


@reply_learning_bp.route("/expressions/<int:example_id>/review", methods=["POST"])
@require_auth
async def review_expression(example_id: int):
    try:
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ExpressionReviewError("invalid_body")
        scope = _body_scope(body)
        action = body.get("action")
        if action not in ("approve", "reject"):
            raise ExpressionReviewError("invalid_action")
        policy = _service("expression_policy")
        item = policy.review(
            scope, example_id, approved=action == "approve", expected_revision=body.get("revision"),
            reviewer="webui-admin", idempotency_key=body.get("idempotency_key"),
            style_only=body.get("style_only") is True, note=body.get("note", ""),
        )
        return jsonify({"ok": True, "item": item})
    except (ExpressionReviewError, ScopeValidationError, sqlite3.Error) as exc:
        return _failure(exc)


@reply_learning_bp.route("/feedback", methods=["GET"])
@require_auth
async def list_feedback():
    try:
        scope = _query_scope()
        limit, offset = _page()
        service = _service("reply_feedback_service")
        items = service.list_events(scope, limit=limit, offset=offset)
        for item in items:
            item["evidence_available"] = service._evidence_available(scope, item)
            item["artifacts"] = json.loads(item.pop("artifacts_json"))
        total = service.connection.execute(
            "SELECT COUNT(*) FROM reply_feedback_events WHERE bot_id=? AND session_id=? AND visibility=?",
            group_owner(scope),
        ).fetchone()[0]
        payload = page_response(items, total=int(total), limit=limit, offset=offset)
        payload.update(scope=scope.to_dict(), signals=service.decision_signals(scope), enabled=service.enabled)
        return jsonify(payload)
    except (ExpressionReviewError, ScopeValidationError, sqlite3.Error) as exc:
        return _failure(exc)


__all__ = ["reply_learning_bp"]
