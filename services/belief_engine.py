"""BeliefEngine — 信念注入（读侧）。

信念是她自己从经历里慢慢形成的看法，不是每轮都要背一遍的设定：只有当前这条消息的话题和某条
已形成 / 稳固的看法相关时，才浮现 1–2 条，措辞明确是她自己的体会，可以参考、不必复述。

相关性只用廉价的办法：反思时顺带产出的触发词 + 看法原文与消息的二元组 / 英文词重叠；
不调用模型，也不做向量检索。看法属于 Bot 本人（跨群、私聊都可用）；「关于某个群」的看法只在
依据日记所在的那些群里浮现。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests
    import logging
    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - direct service imports in focused tests
    from domain.scope import RuntimeScope
from .belief_lifecycle import INJECTABLE_STATUSES, BeliefPolicy, compute_strength
from .identity_safety import is_identity_contamination

MAX_SURFACE = 2
_PARTICLES = set("的了吗呢吧啊呀哦嘛么着过得地")
_STOP_BIGRAMS = frozenset({
    "我们", "你们", "他们", "她们", "大家", "一个", "这个", "那个", "什么", "怎么", "就是", "不是", "没有",
    "可以", "还是", "因为", "所以", "但是", "如果", "这样", "那样", "自己", "觉得", "时候", "一下", "一些",
    "有点", "比较", "知道", "现在", "然后", "其实", "真的", "已经", "可能", "应该", "不会", "不要", "还有",
    "一样", "这么", "那么", "我也", "我是", "你是", "是不", "人家", "起来", "出来", "一起", "今天", "感觉",
})
_STOP_LATIN = frozenset({"ok", "hh", "hhh", "qq", "cq", "at", "http", "https", "www", "com"})
_NOISE = re.compile(r"\[CQ:[^\]]*\]|\[At:[^\]]*\]|@\S+|https?://\S+", re.I)
_CJK_RUN = re.compile(r"[一-鿿]+")
_LATIN = re.compile(r"[a-z0-9]{2,}")


def _clean(text: Any) -> str:
    return _NOISE.sub(" ", str(text or "")).casefold()


def topic_tokens(text: Any) -> tuple[set[str], set[str]]:
    """(中文二元组, 英文/数字词)，去掉虚词二元组。"""
    cleaned = _clean(text)
    grams: set[str] = set()
    for run in _CJK_RUN.findall(cleaned):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram in _STOP_BIGRAMS or any(ch in _PARTICLES for ch in gram):
                continue
            grams.add(gram)
    latin = {word for word in _LATIN.findall(cleaned) if word not in _STOP_LATIN and not word.isdigit()}
    return grams, latin


def relevance(message: str, belief: Mapping[str, Any]) -> tuple[float, list[str]]:
    """返回 (分数, 命中的触发词)；分数为 0 表示与当前话题无关。"""
    cleaned = _clean(message)
    if len(cleaned.strip()) < 2:
        return 0.0, []
    hits = [
        str(trigger) for trigger in belief.get("triggers") or []
        if len(str(trigger).strip()) >= 2 and str(trigger).strip().casefold() in cleaned
    ]
    msg_grams, msg_latin = topic_tokens(cleaned)
    grams, latin = topic_tokens(belief.get("content"))
    shared = grams & msg_grams
    shared_latin = latin & msg_latin
    if not hits and len(shared) < 2 and not shared_latin:
        return 0.0, []
    return 3.0 * len(hits) + len(shared) + 2.0 * len(shared_latin), hits


def _xun(day: int) -> str:
    return "上旬" if day <= 10 else "中旬" if day <= 20 else "下旬"


def describe_experiences(dates: Sequence[str], *, now: float | None = None) -> str:
    """把依据日期说成人话：「来自 9 月下旬的几次经历」。"""
    parsed = sorted(
        datetime.strptime(d, "%Y-%m-%d") for d in {str(v) for v in dates}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
    )
    if not parsed:
        return "来自最近的经历"
    first, last = parsed[0], parsed[-1]
    this_year = datetime.fromtimestamp(now if now is not None else time.time()).year
    year = "" if first.year == this_year and last.year == this_year else f"{first.year} 年 "
    if (first.year, first.month) == (last.year, last.month):
        period = f"{year}{first.month} 月" + (_xun(first.day) if _xun(first.day) == _xun(last.day) else "")
    elif first.year == last.year:
        period = f"{year}{first.month} 月到 {last.month} 月"
    else:
        period = f"{first.year} 年 {first.month} 月到 {last.year} 年 {last.month} 月"
    count = len(parsed)
    times = "一次" if count == 1 else "两次" if count == 2 else "几次"
    return f"来自 {period}的{times}经历"


class BeliefEngine:
    """只读：按话题相关性挑出可以浮现的已形成/稳固看法。"""

    def __init__(
        self,
        db: Any,
        llm_client: Any = None,
        bot_id: str = "",
        max_beliefs: int = 50,
        soul_repository: Any | None = None,
        *,
        repository: Any = None,
        policy: BeliefPolicy | None = None,
        max_items: int = MAX_SURFACE,
        clock: Any = None,
    ):
        # llm_client / soul_repository 只为旧调用方兼容保留：注入不调用模型，也不再按好感门禁。
        self.db = db
        self.llm = llm_client
        self.bot_id = bot_id
        self.max_beliefs = max_beliefs
        self.repository = repository if repository is not None else getattr(db, "bot_beliefs", None)
        self.policy = policy or BeliefPolicy()
        self.max_items = max(1, min(MAX_SURFACE, int(max_items or MAX_SURFACE)))
        self.clock = clock or time.time

    def get_injection_details(
        self,
        scope: RuntimeScope | None,
        sender_id: str | None = None,
        keywords: list[str] | None = None,
        *,
        message: str = "",
        max_items: int | None = None,
    ) -> dict[str, Any]:
        empty = {"text": "", "belief_ids": [], "matches": [], "considered": 0}
        if (
            not isinstance(scope, RuntimeScope)
            or scope.visibility not in {"group", "private"}
            or scope.session is None
        ):
            return {**empty, "reason": "scope_required"}
        if self.repository is None:
            return {**empty, "reason": "belief_repository_unavailable"}
        text = str(message or "").strip() or " ".join(str(k) for k in (keywords or []) if k)
        if not text:
            return {**empty, "reason": "no_topic"}
        try:
            beliefs = self.repository.list_beliefs(scope.bot_id, statuses=sorted(INJECTABLE_STATUSES), limit=self.max_beliefs)
        except Exception:
            logger.debug("[BeliefEngine] 读取看法失败", exc_info=True)
            return {**empty, "reason": "belief_read_failed"}
        now = float(self.clock())
        scored = []
        for belief in beliefs:
            if belief.get("status") not in INJECTABLE_STATUSES or is_identity_contamination(belief.get("content")):
                continue
            if belief.get("category") == "group" and (
                scope.visibility != "group" or scope.session.id not in set(belief.get("session_ids") or [])
            ):
                continue
            score, hits = relevance(text, belief)
            if score <= 0:
                continue
            scored.append((score, compute_strength(belief, now=now, policy=self.policy), belief, hits))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        limit = max(1, min(self.max_items, int(max_items or self.max_items)))
        chosen = scored[:limit]
        if not chosen:
            return {**empty, "considered": len(beliefs), "reason": "no_relevant_belief"}
        lines = ["<beliefs>", "你这段时间的体会（是你自己从经历里慢慢形成的看法，可以参考，不必复述）："]
        for _score, _strength, belief, _hits in chosen:
            lines.append(f"- {belief['content']}（{describe_experiences(belief.get('support_dates') or [], now=now)}）")
        lines.append("</beliefs>")
        return {
            "text": "\n".join(lines),
            "belief_ids": [int(belief["id"]) for _s, _t, belief, _h in chosen],
            "matches": [
                {"id": int(belief["id"]), "score": round(score, 2), "strength": strength, "triggers_hit": hits}
                for score, strength, belief, hits in chosen
            ],
            "considered": len(beliefs),
        }

    def get_injection(
        self,
        scope: RuntimeScope | None,
        sender_id: str | None = None,
        keywords: list[str] | None = None,
        *,
        message: str = "",
    ) -> str:
        return str(self.get_injection_details(scope, sender_id=sender_id, keywords=keywords, message=message).get("text") or "")


__all__ = ["BeliefEngine", "MAX_SURFACE", "describe_experiences", "relevance", "topic_tokens"]
