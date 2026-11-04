"""全局待审计数：侧栏徽标与仪表盘待办用。

线上 2026-11-04：羽书待审事实 782、黑话 214、表达候选 1481，但各页只显示「本页待审核」，
不进页面就不知道积压了多少。这里按 Bot 跨会话合计各类待审数，只读、带 30 秒缓存。

口径与各页「待审」筛选一致：
- facts：scoped_facts.status ∈ {pending, quarantined, conflict}（事实页 _REVIEWABLE_STATUSES）
- jargon：scoped_jargon.status = 'pending'
- expressions：reply_expression_candidates.status = 'pending'
- beliefs：bot_beliefs.status = 'sprout'（萌芽中、等待确认的看法）
- game_drafts：game_knowledge_cards.status = 'draft'，含所有 Bot 共用的攻略（bot_id=''）
"""

from __future__ import annotations

import time
from typing import Any, Callable

from quart import Blueprint, jsonify, request

from ..api_contract import error_payload
from ..container import get_container
from ..middleware.auth import require_auth

review_summary_bp = Blueprint("review_summary", __name__, url_prefix="/api/review")

CACHE_SECONDS = 30.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_COUNT_SQL: dict[str, tuple[str, Callable[[str], tuple]]] = {
    "facts": (
        "SELECT COUNT(*) FROM scoped_facts WHERE bot_id=? AND status IN ('pending','quarantined','conflict')",
        lambda bot: (bot,),
    ),
    "jargon": ("SELECT COUNT(*) FROM scoped_jargon WHERE bot_id=? AND status='pending'", lambda bot: (bot,)),
    "expressions": (
        "SELECT COUNT(*) FROM reply_expression_candidates WHERE bot_id=? AND status='pending'",
        lambda bot: (bot,),
    ),
    "beliefs": ("SELECT COUNT(*) FROM bot_beliefs WHERE bot_id=? AND status='sprout'", lambda bot: (bot,)),
    "game_drafts": (
        "SELECT COUNT(*) FROM game_knowledge_cards WHERE status='draft' AND bot_id IN ('', ?)",
        lambda bot: (bot,),
    ),
}


def _reader(container: Any) -> Callable[[str, tuple], Any] | None:
    db = getattr(container, "db", None)
    cm = getattr(db, "_cm", None) or getattr(getattr(db, "scoped_knowledge", None), "cm", None)
    execute_read = getattr(cm, "execute_read", None)
    if callable(execute_read):
        return execute_read
    conn = getattr(db, "conn", None)
    execute = getattr(conn, "execute", None)
    return execute if callable(execute) else None


def build_review_summary(container: Any, bot_id: str, *, now: float | None = None) -> dict[str, Any]:
    """按 Bot 合计各类待审数；某一项查不了（表不存在、库忙）记为 None 并写进 errors，不整体失败。"""
    execute = _reader(container)
    counts: dict[str, int | None] = {}
    errors: dict[str, str] = {}
    for key, (sql, params) in _COUNT_SQL.items():
        if execute is None:
            counts[key] = None
            errors[key] = "database_unavailable"
            continue
        try:
            row = execute(sql, params(bot_id)).fetchone()
            counts[key] = int(row[0] or 0) if row else 0
        except Exception as exc:  # noqa: BLE001 - 单项失败只降级该项
            counts[key] = None
            errors[key] = type(exc).__name__
    return {
        "bot_id": bot_id,
        "counts": counts,
        "total": sum(value for value in counts.values() if isinstance(value, int)),
        "generated_at": time.time() if now is None else now,
        "errors": errors,
    }


def cached_review_summary(container: Any, bot_id: str, *, now: float | None = None) -> dict[str, Any]:
    stamp = time.monotonic() if now is None else now
    hit = _cache.get(bot_id)
    if hit is not None and stamp - hit[0] < CACHE_SECONDS:
        return hit[1]
    payload = build_review_summary(container, bot_id)
    _cache[bot_id] = (stamp, payload)
    return payload


def clear_review_summary_cache() -> None:
    _cache.clear()


@review_summary_bp.route("/summary", methods=["GET"])
@require_auth
async def review_summary():
    bot_id = str(request.args.get("bot_id") or "").strip()
    if not bot_id:
        return jsonify(error_payload("bot_id_required", "bot_id 必填（BotProfile.db_id）")), 400
    return jsonify(cached_review_summary(get_container(), bot_id))


__all__ = [
    "CACHE_SECONDS",
    "build_review_summary",
    "cached_review_summary",
    "clear_review_summary_cache",
    "review_summary_bp",
]
