"""Agent Feedback API — 反馈记录、配置建议、审查候选与人工处理入口。"""

from __future__ import annotations

from typing import Any

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

    request = None  # type: ignore[assignment]

try:
    from ..container import get_container
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover
    def get_container():  # type: ignore[no-redef]
        return None

    def require_auth(func):  # type: ignore[no-redef]
        return func

try:
    from services.injection.feedback_store import MemoryFeedbackStore
    from services.injection.config_suggestion_store import ConfigSuggestionStore
    from services.review.candidate_store import ReviewCandidateStore
except Exception:  # pragma: no cover - AstrBot 包导入路径
    from ...services.injection.feedback_store import MemoryFeedbackStore
    from ...services.injection.config_suggestion_store import ConfigSuggestionStore
    from ...services.review.candidate_store import ReviewCandidateStore


agent_feedback_bp = Blueprint("agent_feedback", __name__, url_prefix="/api/agent-feedback")

_ACTION_TO_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "ignore": "ignored",
}


def _stores(conn: Any) -> tuple[MemoryFeedbackStore, ConfigSuggestionStore, ReviewCandidateStore]:
    feedback_store = MemoryFeedbackStore(conn)
    suggestion_store = ConfigSuggestionStore(conn)
    candidate_store = ReviewCandidateStore(conn)
    feedback_store.ensure_schema()
    suggestion_store.ensure_schema()
    candidate_store.ensure_schema()
    return feedback_store, suggestion_store, candidate_store


def build_agent_feedback_payload(conn: Any, *, limit: int = 100, bot_id: str = "") -> dict[str, Any]:
    """构造 Agent 反馈页面 payload。

    审核台是跨 Scope 的管理员视图，因此主列表保留全部候选（含无归属的历史
    legacy 行），避免旧调用方提交的候选被隐藏而漏审；归属通过每条的
    ``scope`` / ``legacy`` 字段显式暴露。``bot_id`` 非空时才收窄到该 Bot，
    真正的 Scope 隔离由 store 层的带 scope 查询保证。
    """
    feedback_store, suggestion_store, candidate_store = _stores(conn)
    feedback_records = feedback_store.list_recent(limit=limit)
    suggestions = suggestion_store.list_all(limit=limit)
    wanted_bot = str(bot_id or "").strip()
    candidates = candidate_store.list_all(limit=limit, include_legacy=True)
    if wanted_bot:
        candidates = [
            item for item in candidates if str(item.get("bot_id") or "") == wanted_bot
        ]
    suggestion_history = [item for item in suggestions if item.get("review_status") != "pending"]
    candidate_history = [item for item in candidates if item.get("review_status") != "pending"]
    pending_suggestions = [item for item in suggestions if item.get("review_status") == "pending"]
    pending_candidates = [item for item in candidates if item.get("review_status") == "pending"]
    legacy_pending = [
        item for item in pending_candidates if item.get("legacy")
    ]
    return {
        "feedback_records": feedback_records,
        "config_suggestions": pending_suggestions,
        "review_candidates": pending_candidates,
        "history": {
            "config_suggestions": suggestion_history,
            "review_candidates": candidate_history,
        },
        "legacy_review_candidates": legacy_pending,
        "summary": {
            "feedback_records": len(feedback_records),
            "pending_suggestions": len(pending_suggestions),
            "pending_candidates": len(pending_candidates),
            "legacy_pending_candidates": len(legacy_pending),
            "history_items": len(suggestion_history) + len(candidate_history),
        },
        "safety_note": "危险建议不会自动生效；批准只会记录人工状态，实际配置仍需在通道配置页面显式应用。",
    }


def review_config_suggestion(store: ConfigSuggestionStore, suggestion_id: int, action: str) -> dict[str, Any]:
    status = _ACTION_TO_STATUS.get(str(action or "").strip().lower())
    if not status:
        raise ValueError(f"invalid action: {action}")
    row = store.update_review_status(int(suggestion_id), status, applied=False)
    if not row:
        raise ValueError(f"config suggestion not found: {suggestion_id}")
    row["message"] = "配置建议已记录人工状态；危险建议不会自动应用。"
    return row


def review_review_candidate(
    store: ReviewCandidateStore,
    candidate_id: int,
    action: str,
    *,
    bot_id: str = "",
    reviewer: str = "",
) -> dict[str, Any]:
    """记录人工裁决。带 ``bot_id`` 时禁止跨该 Bot Scope 操作候选。"""
    status = _ACTION_TO_STATUS.get(str(action or "").strip().lower())
    if not status:
        raise ValueError(f"invalid action: {action}")
    current = store.get(int(candidate_id))
    if current is None:
        raise ValueError(f"review candidate not found: {candidate_id}")
    wanted_bot = str(bot_id or "").strip()
    owner_bot = str(current.get("bot_id") or "").strip()
    if wanted_bot and owner_bot and owner_bot != wanted_bot:
        raise ValueError("review candidate is outside the requested bot scope")
    row = store.update_review_status(int(candidate_id), status, promoted=False, reviewer=reviewer)
    if not row:
        raise ValueError(f"review candidate not found: {candidate_id}")
    row["message"] = "审查候选已记录人工状态；不会自动写入高风险对象。"
    return row


def _conn_from_container() -> Any | None:
    c = get_container()
    db = getattr(c, "db", None)
    if not db:
        return None
    if getattr(db, "closed", False):
        try:
            db.reopen()
        except Exception:
            return None
    return getattr(db, "conn", None)


@agent_feedback_bp.route("", methods=["GET"])
@require_auth
async def get_agent_feedback():
    conn = _conn_from_container()
    if not conn:
        return jsonify({"error": "agent_feedback_store_unavailable", "feedback_records": [], "config_suggestions": [], "review_candidates": [], "history": {}, "summary": {}})
    wanted_bot = ""
    if request is not None:
        wanted_bot = str(request.args.get("bot_id") or "").strip()
    payload = build_agent_feedback_payload(conn, bot_id=wanted_bot)
    return jsonify(payload)


@agent_feedback_bp.route("/config-suggestions/<int:suggestion_id>/<action>", methods=["POST"])
@require_auth
async def update_config_suggestion(suggestion_id: int, action: str):
    conn = _conn_from_container()
    if not conn:
        return jsonify({"ok": False, "error": "agent_feedback_store_unavailable"}), 503
    try:
        _, suggestion_store, _ = _stores(conn)
        row = review_config_suggestion(suggestion_store, suggestion_id, action)
        row["ok"] = True
        return jsonify(row)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _learning_compat_review(candidate_id: int, action: str, bot_id: str, reviewer: str):
    """兼容旧 Agent 路径：命中新候选时只代理 LearningReviewService。"""
    try:
        from engine.db.learning_repository import LearningRepositories
        from services.learning.review import LearningReviewService
    except Exception:  # pragma: no cover
        from ...engine.db.learning_repository import LearningRepositories
        from ...services.learning.review import LearningReviewService
    container = get_container()
    repositories = getattr(container, "learning_repositories", None)
    if repositories is None:
        db = getattr(container, "db", None)
        repositories = getattr(db, "learning", None) if db is not None else None
        if repositories is None:
            conn = _conn_from_container()
            if conn is not None:
                repositories = LearningRepositories.from_connection(conn)
                container.learning_repositories = repositories
    if repositories is None:
        return None
    candidate = repositories.candidates.get(int(candidate_id), bot_id=str(bot_id or "").strip())
    if candidate is None:
        return None
    review = getattr(container, "learning_review_service", None) or LearningReviewService(repositories)
    container.learning_review_service = review
    result = review.review(
        int(candidate_id), bot_id=str(bot_id).strip(), action=action,
        reviewer=str(reviewer or "legacy-agent-feedback"),
    )
    return result


@agent_feedback_bp.route("/review-candidates/<int:candidate_id>/<action>", methods=["POST"])
@require_auth
async def update_review_candidate(candidate_id: int, action: str):
    # 兼容期新客户端可显式携带 bot_id；未携带时继续读取旧 review_candidates。
    body = {}
    if request is not None:
        try:
            body = await request.get_json(silent=True) or {}
        except Exception:
            body = {}
    bot_id = str((request.args.get("bot_id") if request is not None else None) or body.get("bot_id") or "").strip()
    if bot_id:
        try:
            result = _learning_compat_review(
                candidate_id, action, bot_id,
                str(body.get("reviewer") or body.get("actor") or "legacy-agent-feedback"),
            )
            if result is not None:
                candidate = dict(result.get("candidate") or {})
                # 保留旧 API 的顶层候选字段，同时附带新晋升观察视图。
                payload = {"ok": True, **candidate, "candidate": candidate, "promotions": result.get("promotions", [])}
                return jsonify(payload)
        except Exception as exc:
            return jsonify({"ok": False, "error": {"code": getattr(exc, "code", "learning_review_error"), "message": str(exc)[:300], "retryable": False}}), 400
    conn = _conn_from_container()
    if not conn:
        return jsonify({"ok": False, "error": "agent_feedback_store_unavailable"}), 503
    try:
        _, _, candidate_store = _stores(conn)
        row = review_review_candidate(
            candidate_store,
            candidate_id,
            action,
            bot_id=bot_id,
            reviewer=str(body.get("reviewer") or body.get("actor") or "webui-reviewer"),
        )
        row["ok"] = True
        return jsonify(row)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


__all__ = [
    "agent_feedback_bp",
    "build_agent_feedback_payload",
    "review_config_suggestion",
    "review_review_candidate",
]
