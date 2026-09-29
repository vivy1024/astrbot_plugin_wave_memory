"""Message-driven reply rules. No model, embedding, database or timer dependency.

This service filters an *existing* reply candidate. A high score never grants
platform permission or creates a wake-up. Only confirmed sends enter frequency
history; missing feedback is neutral, not a failed interaction.
"""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .proactive_policy import reply_relationship_signal


POLICY_VERSION = "reply-rules-v1"
_QUESTION = re.compile(r"[?？]|(?:请问|能否|可以帮|请帮|帮忙|怎么|如何|为什么)|\b(?:how|why|what|help|please|could|can you)\b", re.I)
_NOISE = re.compile(r"(?:[哈呵嘿嘻啊哦嗯]{1,12}|[6８8]{3,}|(?:ha){2,}|lol+|ok|好的|收到|\[(?:表情|图片|动画表情)\])", re.I)


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value) if not isinstance(value, bool) else default
    except (ValueError, TypeError, OverflowError):
        number = default
    return max(low, min(high, number if math.isfinite(number) else default))


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def feedback_polarity(counts: Any) -> str | None:
    """Only explicit acceptance/rejection contributes. Correction is not dislike."""
    if not isinstance(counts, Mapping):
        return None
    accepted, rejected = counts.get("accepted", 0), counts.get("rejected", 0)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (accepted, rejected)):
        return None
    if rejected > accepted:
        return "negative"
    if accepted > rejected:
        return "positive"
    return None


@dataclass(frozen=True)
class ReplySignals:
    bot_id: str
    session_id: str
    visibility: str
    message: str
    direct: bool = False
    candidate: bool = False
    platform_allowed: bool = True
    stopped: bool = False
    self_message: bool = False
    relevance: float | None = None
    concern: bool = False
    relationship: Any = None
    backlog: int = 0
    cooldown_remaining: float = 0.0
    # Fixed reason codes supplied by the existing followup service.
    candidate_gate: str | None = None
    # Optional explicit stage-3 outcome. Never inferred from silence/timeouts.
    feedback: str | None = None
    has_media: bool = False


@dataclass
class _History:
    replies: deque = field(default_factory=lambda: deque(maxlen=256))
    streak: int = 0
    suppressed_until: float = 0.0
    last_evaluated: float = 0.0


class ReplyDecisionService:
    def __init__(self, config: Mapping | None = None, *, clock: Callable[[], float] = time.time):
        self.config = dict(_mapping(config))
        self.clock = clock
        self._history: OrderedDict[tuple[str, str, str], _History] = OrderedDict()
        # Includes vetoed candidates which never reach injection. Process-local,
        # bounded, and deliberately unrelated to response-quality statistics.
        self.diagnostics: deque[dict] = deque(maxlen=128)

    def list_diagnostics(
        self, *, limit: int = 50, bot_id: str | None = None,
        session_id: str | None = None, visibility: str | None = None,
    ) -> list[dict]:
        """Newest first, copied and bounded. Includes decisions with no injection.

        UI callers retain their existing authorization and scope resolution;
        these optional exact-match filters are not an authorization mechanism.
        """
        count = int(_number(limit, 50, 0, 128))
        items = [
            item for item in reversed(tuple(self.diagnostics))
            if (bot_id is None or item.get("bot_id") == bot_id)
            and (session_id is None or item.get("session_id") == session_id)
            and (visibility is None or item.get("visibility") == visibility)
        ]
        return deepcopy(items[:count])

    def settings(self, bot_id: str, session_id: str) -> dict:
        """Global < bot < session < bot/session. Keys never use raw group IDs."""
        config = self.config
        bot = _mapping(_mapping(config.get("bots")).get(bot_id))
        result = dict(config)
        result.update(bot)
        result.update(_mapping(_mapping(config.get("groups")).get(session_id)))
        result.update(_mapping(_mapping(bot.get("groups")).get(session_id)))
        mode = str(result.get("mode") or "off").strip().lower()
        result["mode"] = mode if mode in {"off", "shadow", "enforce"} else "off"
        for key, default, low, high in (
            ("score_threshold", 45, 0, 100),
            ("reply_window_seconds", 60, 1, 3600),
            ("max_recent_replies", 3, 1, 256),
            ("backoff_base_seconds", 5, 0, 3600),
            ("backoff_max_seconds", 60, 0, 3600),
        ):
            result[key] = _number(result.get(key), default, low, high)
        return result

    def _state(self, key: tuple[str, str, str]) -> _History:
        state = self._history.setdefault(key, _History())
        self._history.move_to_end(key)
        while len(self._history) > 1024:
            self._history.popitem(last=False)
        return state

    def note_reply(self, bot_id: str, session_id: str, visibility: str, *, now: float | None = None) -> None:
        """Called only by the send hook, not by generation or a rule decision."""
        if bot_id and session_id and visibility in {"group", "private"}:
            self._state((bot_id, visibility, session_id)).replies.append(float(self.clock() if now is None else now))

    def decide(self, signals: ReplySignals, *, now: float | None = None) -> dict:
        cfg = self.settings(signals.bot_id, signals.session_id)
        stamp = float(self.clock() if now is None else now)
        mode = cfg["mode"]
        reasons: list[str] = []
        factors: dict[str, float] = {}
        score = 0.0
        retry_after = 0.0
        text = str(signals.message or "").strip()
        # A candidate flag is eligibility, not a permission override.
        eligible = bool(signals.direct or signals.candidate)
        result = {
            "policy_version": POLICY_VERSION, "mode": mode,
            "bot_id": str(signals.bot_id)[:128], "session_id": str(signals.session_id)[:256],
            "visibility": signals.visibility, "score": 0.0,
            "threshold": cfg["score_threshold"], "factors": factors, "reasons": reasons,
            "candidate": eligible, "direct": signals.direct,
            "would_reply": False, "allow": False, "enforced": False,
            "retry_after_seconds": 0.0, "recent_replies": 0,
        }

        def finish(allow: bool, *, hard: bool = False) -> dict:
            result.update(
                decision="reply" if allow else "suppress" if eligible else "skip",
                score=round(max(0.0, min(100.0, score)), 2),
                would_reply=allow,
                allow=allow if hard or mode == "enforce" else eligible,
                enforced=mode == "enforce" and eligible and not allow,
                retry_after_seconds=round(retry_after, 3),
            )
            self.diagnostics.append({**result, "timestamp": stamp})
            return result

        if not signals.bot_id or not signals.session_id or signals.visibility not in {"group", "private"}:
            reasons.append("scope_missing")
            return finish(False, hard=True)
        if signals.stopped or signals.self_message or signals.platform_allowed is not True:
            reasons.append("event_stopped" if signals.stopped else "self_message" if signals.self_message else "platform_denied")
            return finish(False, hard=True)
        if not text and not signals.has_media:
            reasons.append("no_message")
            return finish(False, hard=True)
        if mode == "off":
            reasons.append("mode_off")
            return finish(eligible)
        if signals.direct:
            score = 100.0
            reasons.append("direct_priority")
            return finish(True)

        relevance = _number(signals.relevance, 0, 0, 1)
        factors["relevance"] = 45.0 * relevance
        reasons.append("relevance_unknown" if signals.relevance is None else "relevance")
        question = bool(_QUESTION.search(text))
        factors["question_request"] = 20.0 if question else 0.0
        if question:
            reasons.append("question_request")
        noise = not signals.has_media and (not any(c.isalnum() for c in text) or bool(_NOISE.fullmatch(text)))
        factors["noise"] = -60.0 if noise else 0.0
        if noise:
            reasons.append("pure_noise")
        factors["concern"] = 45.0 if signals.concern else 0.0
        if signals.concern:
            reasons.append("concern_candidate")
        relationship = reply_relationship_signal(signals.relationship)
        factors["relationship"] = relationship["score"]
        reasons.append(relationship["reason_code"])
        feedback = signals.feedback if signals.feedback in ("positive", "negative") else None
        factors["feedback"] = 10.0 if feedback == "positive" else -15.0 if feedback == "negative" else 0.0
        reasons.append(f"feedback_{feedback}" if feedback else "feedback_unknown")
        factors["backlog"] = -min(40.0, _number(signals.backlog, 0, 0, 100) * 10.0)
        if factors["backlog"]:
            reasons.append("backlog")
        state = self._state((signals.bot_id, signals.visibility, signals.session_id))
        # Trim to the maximum supported window, preserving history across overrides.
        while state.replies and stamp - state.replies[0] >= 3600:
            state.replies.popleft()
        recent = sum(0 <= stamp - t < cfg["reply_window_seconds"] for t in state.replies)
        result["recent_replies"] = recent
        factors["recent_replies"] = -min(48.0, recent * 12.0)
        if recent:
            reasons.append("recent_bot_replies")
        score = sum(factors.values())
        retry_after = _number(signals.cooldown_remaining, 0, 0, 86400)
        if signals.candidate_gate in {"concern_cooldown", "concern_quiet_hours", "concern_group_gap", "concern_hourly_limit"}:
            reasons.append(signals.candidate_gate)
        if not eligible:
            # Merely overhearing a group question never creates a candidate or backoff.
            reasons.append("no_existing_candidate")
            return finish(False)
        if stamp < state.last_evaluated or stamp - state.last_evaluated > cfg["backoff_max_seconds"]:
            state.streak = 0
            state.suppressed_until = 0
        state.last_evaluated = stamp
        retry_after = max(retry_after, state.suppressed_until - stamp)
        if retry_after:
            reasons.append("cooldown" if signals.cooldown_remaining > 0 else "suppression_backoff")
            # Arrivals inside the interval never slide its expiry forward.
            return finish(False)
        allow = not noise and not relationship["blocked"] and recent < cfg["max_recent_replies"] and score >= cfg["score_threshold"]
        reasons.append("score_pass" if allow else "recent_reply_limit" if recent >= cfg["max_recent_replies"] else "score_below_threshold" if score < cfg["score_threshold"] else "rule_suppressed")
        if allow:
            state.streak = 0
            state.suppressed_until = 0
        else:
            state.streak = min(16, state.streak + 1)
            retry_after = min(cfg["backoff_max_seconds"], cfg["backoff_base_seconds"] * 2 ** (state.streak - 1))
            state.suppressed_until = stamp + retry_after
        return finish(allow)


def attach_reply_decision(event: Any, decision: Mapping) -> None:
    """Bounded rule diagnostics; no message text or fabricated response outcomes."""
    payload = dict(decision)
    try:
        event._wave_memory_reply_decision = payload
    except (AttributeError, TypeError):
        pass
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        setter("wave_memory_reply_decision", payload)


__all__ = ["ReplyDecisionService", "ReplySignals", "attach_reply_decision", "feedback_polarity"]
