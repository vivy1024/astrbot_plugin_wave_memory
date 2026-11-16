"""Message-driven reply rules & multi-feature air-reading engine.

No model, embedding, database or timer dependency.

- 兼容原有 ``ReplyDecisionService.decide(ReplySignals)`` 规则校验（默认信号保持 7 维因子兼容）。
- 当启用 Bot 主动读空气（``proactive_enabled=True``）或传入心智/话题/Bot协同信号时，
  执行多特征意愿打分（参考 DSH 内在动因 + MaiBot 动态单向配额与心流姿态）、
  硬过滤（指令/收尾/点名他人/双人刷屏/疑似Bot）、接话窗口连续对话、以及双 Bot 群级共享锁仲裁。
- 好感度分数永不参与开口打分与拦截。
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


POLICY_VERSION = "reply-rules-v3"
# 主动开口后多久没人说话算一次「无人接话」；退避间隔按 2^n 放大，最长 24 小时。
UNANSWERED_WINDOW_SECONDS = 1800.0
UNANSWERED_MAX_STREAK = 8
UNANSWERED_BACKOFF_CAP_SECONDS = 86400.0
_QUESTION = re.compile(
    r"[?？]|(?:请问|能否|可以帮|请帮|帮忙|怎么|如何|为什么|啥意思|为啥)|\b(?:how|why|what|help|please|could|can you)\b",
    re.I,
)
_NOISE = re.compile(
    r"(?:[哈呵嘿嘻啊哦嗯草额呃]{1,12}|[68８]{3,}|(?:ha){2,}|lol+|ok|好的|收到|行吧|可以|\[(?:表情|图片|动画表情)\])",
    re.I,
)
_FAREWELL = re.compile(
    r"^(?:晚安|睡了|先睡了|睡觉了|88|886|拜拜|下线|回聊|撤了|再见|早点睡|辛苦了|散了)[!！。.~～]*$",
    re.I,
)
_COMMAND_PREFIX = re.compile(r"^\s*[/!！#\\]")
_CHINESE_NAME_CALL = re.compile(r"^@?([\u4e00-\u9fffA-Za-z0-9_]{1,10})[,，:：\s]")
_SYSTEM_OR_ERROR_NOTICE = re.compile(
    r"(?:所有图片均下载失败|图片下载失败|消息已撤回|撤回了一条消息|请求失败|加载失败|"
    r"接口报错|服务不可用|API\s*Error|429\s*Too\s*Many|502\s*Bad\s*Gateway)",
    re.I,
)


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


def detect_message_air_flags(
    message: str,
    *,
    bot_terms: Iterable[str] = (),
    bot_qq_ids: Iterable[str] = (),
    at_qq_ids: Iterable[str] = (),
    quote_sender_id: str = "",
) -> dict[str, bool]:
    """纯本地零 Token 识别消息语境红线：指令、告别收尾、点名/艾特其他人。"""
    text = str(message or "").strip()
    is_command = bool(_COMMAND_PREFIX.match(text))
    is_farewell = bool(_FAREWELL.fullmatch(text))
    is_system_notice = bool(_SYSTEM_OR_ERROR_NOTICE.search(text))

    own_ids = {str(x).strip() for x in bot_qq_ids if str(x or "").strip()}
    terms_lower = {str(t).strip().casefold() for t in bot_terms if str(t or "").strip()}

    addressed_other = False
    # 1. @ 了其他 QQ 号（非自己）
    other_ats = {str(x).strip() for x in at_qq_ids if str(x or "").strip()} - own_ids
    if other_ats:
        addressed_other = True
    # 2. 文本里包含 [At:xxx] 或 @他人
    if not addressed_other and "[At:" in text:
        for match in re.findall(r"\[At:([^\]]+)\]", text):
            if match.strip() not in own_ids:
                addressed_other = True
                break
    # 3. 引用了别人而不是 Bot
    if not addressed_other and quote_sender_id and quote_sender_id.strip() not in own_ids:
        addressed_other = True
    # 4. 句首直接点名他人（未命中 Bot 自称词）
    if not addressed_other and terms_lower:
        m = _CHINESE_NAME_CALL.match(text)
        if m:
            called = m.group(1).strip().casefold()
            if called and called not in terms_lower and len(called) >= 2:
                # 排除常见日常开头副词/代词
                non_name_starts = {
                    "今天", "明天", "昨天", "刚才", "刚刚", "现在", "如果", "因为", "所以",
                    "但是", "不过", "其实", "话说", "对了", "顺便", "大家", "各位", "兄弟",
                    "姐妹", "家人", "救命", "卧槽", "哈哈", "怎么", "为什么", "请问", "那个", "这个",
                }
                if called not in non_name_starts:
                    addressed_other = True

    return {
        "is_command": is_command,
        "is_farewell": is_farewell,
        "is_system_notice": is_system_notice,
        "addressed_other": addressed_other,
    }


def score_topic_division(
    message: str,
    *,
    preferred_topics: Iterable[str] = (),
    interest_keywords: Iterable[str] = (),
    avoid_topics: Iterable[str] = (),
) -> tuple[float, bool, list[str]]:
    """计算消息与当前 Bot 话题分工的契合度 (0~1)、是否命中避让词、以及命中词列表。"""
    text_lower = str(message or "").casefold()
    if not text_lower:
        return 0.0, False, []
    for bad in avoid_topics:
        needle = str(bad or "").strip().casefold()
        if needle and needle in text_lower:
            return 0.0, True, [str(bad)]

    hits: list[str] = []
    score = 0.0
    for kw in preferred_topics:
        needle = str(kw or "").strip()
        if needle and needle.casefold() in text_lower:
            if needle not in hits:
                hits.append(needle)
            score += 0.42
    for kw in interest_keywords:
        needle = str(kw or "").strip()
        if needle and needle.casefold() in text_lower:
            if needle not in hits:
                hits.append(needle)
            score += 0.30
    return min(1.0, round(score, 3)), False, hits[:6]


class GroupProactiveArbiter:
    """多 Bot 同群主动插话协同锁：防双 Bot 抢答、同条消息只让最契合的一只开口。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        # session_id -> (bot_id, spoken_at)
        self._last_group_proactive: dict[str, tuple[str, float]] = {}
        # (session_id, message_key) -> (bot_id, score, decided_at)
        self._message_claims: OrderedDict[tuple[str, str], tuple[str, float, float]] = OrderedDict()

    def shared_cooldown_remaining(
        self,
        *,
        bot_id: str,
        session_id: str,
        cooldown_seconds: float,
        now: float | None = None,
    ) -> float:
        if cooldown_seconds <= 0 or not session_id:
            return 0.0
        stamp = float(self.clock() if now is None else now)
        with self._lock:
            last = self._last_group_proactive.get(session_id)
            if not last:
                return 0.0
            other_bot, spoken_at = last
            if other_bot == bot_id:
                return 0.0
            remaining = (spoken_at + float(cooldown_seconds)) - stamp
            return max(0.0, round(remaining, 3))

    def try_claim_message(
        self,
        *,
        bot_id: str,
        session_id: str,
        message_key: str,
        score: float,
        now: float | None = None,
    ) -> bool:
        """同条消息在同群只允许 1 只 Bot 主动接话；后来者不得抢占已通过的发言权。"""
        if not session_id or not message_key:
            return True
        stamp = float(self.clock() if now is None else now)
        key = (str(session_id), str(message_key))
        with self._lock:
            existing = self._message_claims.get(key)
            if existing is not None and stamp - existing[2] <= 30.0:
                return existing[0] == bot_id
            self._message_claims[key] = (str(bot_id), float(score), stamp)
            self._message_claims.move_to_end(key)
            while len(self._message_claims) > 512:
                self._message_claims.popitem(last=False)
            return True

    def record_group_proactive_spoken(
        self,
        *,
        bot_id: str,
        session_id: str,
        now: float | None = None,
    ) -> None:
        if not bot_id or not session_id:
            return
        stamp = float(self.clock() if now is None else now)
        with self._lock:
            self._last_group_proactive[session_id] = (str(bot_id), stamp)


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
    backlog: int = 0
    cooldown_remaining: float = 0.0
    # Fixed reason codes supplied by the existing followup service.
    candidate_gate: str | None = None
    # Optional explicit stage-3 outcome. Never inferred from silence/timeouts.
    feedback: str | None = None
    has_media: bool = False
    # ─── 扩展读空气 / Bot 协同 / 心智状态信号（默认中性，兼容旧单测） ───
    proactive_enabled: bool = False
    sender_is_bot: bool = False
    bot_collab_exceeded: bool = False
    addressed_other: bool = False
    is_command_or_farewell: bool = False
    is_system_notice: bool = False
    in_duo_pingpong: bool = False
    followup_eligible: bool = False
    topic_match: float | None = None
    avoid_topic_hit: bool = False
    mind_drive: float | None = None
    mind_energy: float | None = None
    mind_stance: str = ""
    mood_influence: float = 1.0
    # 激素身体的主动意愿 0..1（BodyReading.proactive_drive）；None 表示这个 Bot 没开身体，不参与打分
    body_drive: float | None = None
    silence_messages: int = 0
    group_msgs_per_min: int = 0
    ignored_streak: int = 0
    quiet_hours_active: bool = False
    shared_cooldown_remaining: float = 0.0
    proactive_hourly_limit_hit: bool = False
    consecutive_proactive_hit: bool = False


@dataclass
class _History:
    replies: deque = field(default_factory=lambda: deque(maxlen=256))
    proactive_replies: deque = field(default_factory=lambda: deque(maxlen=64))
    consecutive_proactive: int = 0
    # 主动开口后无人接话的退避：同一次主动只记一次冷落；仅在有人搭理该 Bot 时清零。
    pending_proactive_at: float = 0.0
    unanswered_chatter: int = 0
    unanswered_proactive: int = 0
    streak: int = 0
    suppressed_until: float = 0.0
    last_evaluated: float = 0.0


class ReplyDecisionService:
    def __init__(
        self,
        config: Mapping | None = None,
        *,
        clock: Callable[[], float] = time.time,
        arbiter: GroupProactiveArbiter | None = None,
    ):
        self.config = dict(_mapping(config))
        self.clock = clock
        self.arbiter = arbiter or GroupProactiveArbiter(clock=clock)
        self._history: OrderedDict[tuple[str, str, str], _History] = OrderedDict()
        # Includes vetoed candidates which never reach injection. Process-local,
        # bounded, and deliberately unrelated to response-quality statistics.
        self.diagnostics: deque[dict] = deque(maxlen=128)
        self._group_recent_senders: dict[str, deque[tuple[str, float]]] = {}

    def list_diagnostics(
        self, *, limit: int = 50, bot_id: str | None = None,
        session_id: str | None = None, visibility: str | None = None,
    ) -> list[dict]:
        """Newest first, copied and bounded. Includes decisions with no injection."""
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

    def observe_group_speaker(self, session_id: str, sender_id: str, *, now: float | None = None) -> bool:
        """记录群内说话人顺序，判断当前是否处于两人快速双人拉锯战（A->B->A->B）。"""
        if not session_id or not sender_id:
            return False
        stamp = float(self.clock() if now is None else now)
        ring = self._group_recent_senders.setdefault(session_id, deque(maxlen=12))
        ring.append((str(sender_id), stamp))
        recent = [uid for uid, ts in ring if stamp - ts <= 90.0]
        if len(recent) >= 4:
            tail4 = recent[-4:]
            if len(set(tail4)) == 2 and tail4[0] == tail4[2] and tail4[1] == tail4[3] and tail4[0] != tail4[1]:
                return True
        return False

    def note_reply(
        self,
        bot_id: str,
        session_id: str,
        visibility: str,
        *,
        proactive: bool = False,
        now: float | None = None,
    ) -> None:
        """Called only by the send hook, not by generation or a rule decision."""
        if bot_id and session_id and visibility in {"group", "private"}:
            stamp = float(self.clock() if now is None else now)
            state = self._state((bot_id, visibility, session_id))
            state.replies.append(stamp)
            if proactive:
                self._settle_unanswered(state, stamp)
                state.proactive_replies.append(stamp)
                state.consecutive_proactive += 1
                state.pending_proactive_at = stamp
                state.unanswered_chatter = 0
                self.arbiter.record_group_proactive_spoken(bot_id=bot_id, session_id=session_id, now=stamp)
            else:
                state.consecutive_proactive = 0

    def _settle_unanswered(self, state: "_History", stamp: float) -> bool:
        """上一次主动开口若超过无人接话窗口仍没人理，记一次冷落（每次主动只记一次）。"""
        if state.pending_proactive_at and stamp - state.pending_proactive_at >= UNANSWERED_WINDOW_SECONDS:
            state.unanswered_proactive = min(UNANSWERED_MAX_STREAK, state.unanswered_proactive + 1)
            state.pending_proactive_at = 0.0
            state.unanswered_chatter = 0
            return True
        return False

    def note_bot_engaged(
        self,
        bot_id: str,
        session_id: str,
        visibility: str = "group",
        *,
        now: float | None = None,
    ) -> None:
        """群友明确点名/@/引用该 Bot，或在接话窗口内顺着该 Bot 继续对话：清零该 Bot 的冷场与连续主动计数。"""
        if not bot_id or not session_id or visibility not in {"group", "private"}:
            return
        stamp = float(self.clock() if now is None else now)
        state = self._state((bot_id, visibility, session_id))
        if state.pending_proactive_at and stamp - state.pending_proactive_at >= UNANSWERED_WINDOW_SECONDS:
            self._settle_unanswered(state, stamp)
        state.pending_proactive_at = 0.0
        state.unanswered_chatter = 0
        state.unanswered_proactive = 0
        state.consecutive_proactive = 0

    def observe_unanswered_chatter(
        self,
        bot_id: str,
        session_id: str,
        visibility: str = "group",
        *,
        min_messages: int = 4,
        min_elapsed_seconds: float = 120.0,
        now: float | None = None,
    ) -> bool:
        """记录一条未搭理该 Bot 的普通群聊；若主动开口后已刷过多条无关消息且超时，结算一次冷落。"""
        if not bot_id or not session_id or visibility != "group":
            return False
        stamp = float(self.clock() if now is None else now)
        state = self._state((bot_id, visibility, session_id))
        if not state.pending_proactive_at:
            return False
        state.unanswered_chatter += 1
        if (
            stamp - state.pending_proactive_at >= max(10.0, float(min_elapsed_seconds))
            and state.unanswered_chatter >= max(1, int(min_messages))
        ):
            state.unanswered_proactive = min(UNANSWERED_MAX_STREAK, state.unanswered_proactive + 1)
            state.pending_proactive_at = 0.0
            state.unanswered_chatter = 0
            return True
        return self._settle_unanswered(state, stamp)

    def note_human_activity(self, session_id: str, *, now: float | None = None) -> None:
        """显式会话级活跃重置（保留兼容单测与手动重置入口）。"""
        if not session_id:
            return
        stamp = float(self.clock() if now is None else now)
        for (_bot, _vis, sid), state in self._history.items():
            if sid != session_id:
                continue
            if state.pending_proactive_at and stamp - state.pending_proactive_at < UNANSWERED_WINDOW_SECONDS:
                state.pending_proactive_at = 0.0
            else:
                self._settle_unanswered(state, stamp)
            state.unanswered_chatter = 0
            state.unanswered_proactive = 0
            state.consecutive_proactive = 0

    def proactive_backoff_seconds(
        self,
        *,
        bot_id: str,
        session_id: str,
        visibility: str,
        base_gap_seconds: float,
        now: float | None = None,
    ) -> float:
        """无人接话的指数退避：每连续冷落一次，主动最小间隔翻倍，上限 24 小时。"""
        stamp = float(self.clock() if now is None else now)
        state = self._state((bot_id, visibility, session_id))
        self._settle_unanswered(state, stamp)
        streak = state.unanswered_proactive
        if streak <= 0:
            return 0.0
        last_ts = state.proactive_replies[-1] if state.proactive_replies else 0.0
        gap = min(UNANSWERED_BACKOFF_CAP_SECONDS, max(1.0, float(base_gap_seconds)) * (2 ** streak))
        return round(max(0.0, last_ts + gap - stamp), 3) if last_ts else 0.0

    def proactive_quota_status(
        self,
        *,
        bot_id: str,
        session_id: str,
        visibility: str,
        min_gap_seconds: float,
        max_per_hour: int,
        max_consecutive_turns: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        """单向收紧动态配额检查（只紧不松）。"""
        stamp = float(self.clock() if now is None else now)
        state = self._state((bot_id, visibility, session_id))
        while state.proactive_replies and stamp - state.proactive_replies[0] >= 3600.0:
            state.proactive_replies.popleft()
        hourly_count = len(state.proactive_replies)
        last_ts = state.proactive_replies[-1] if state.proactive_replies else 0.0
        gap_remaining = max(0.0, (last_ts + float(min_gap_seconds)) - stamp) if last_ts > 0 else 0.0
        return {
            "hourly_count": hourly_count,
            "hourly_limit_hit": hourly_count >= max(1, int(max_per_hour)),
            "gap_remaining": round(gap_remaining, 3),
            "consecutive_hit": state.consecutive_proactive >= max(1, int(max_consecutive_turns)),
        }

    def decide(self, signals: ReplySignals, *, now: float | None = None) -> dict:
        cfg = self.settings(signals.bot_id, signals.session_id)
        stamp = float(self.clock() if now is None else now)
        mode = cfg["mode"]
        reasons: list[str] = []
        factors: dict[str, float] = {}
        score = 0.0
        retry_after = 0.0
        text = str(signals.message or "").strip()
        # 当启用主动读空气时，普通群消息也能作为「待评估的主动候选」进入意愿打分；
        # 未开启主动读空气时保持原规则（只有关切/直呼才算候选）。
        proactive_wake_candidate = bool(signals.proactive_enabled and not signals.direct and signals.visibility == "group")
        eligible = bool(signals.direct or signals.candidate or proactive_wake_candidate)
        result = {
            "policy_version": POLICY_VERSION, "mode": mode,
            "bot_id": str(signals.bot_id)[:128], "session_id": str(signals.session_id)[:256],
            "visibility": signals.visibility, "score": 0.0,
            "threshold": cfg["score_threshold"], "factors": factors, "reasons": reasons,
            "candidate": eligible, "direct": signals.direct,
            "proactive_wake": False,
            "would_reply": False, "allow": False, "enforced": False,
            "retry_after_seconds": 0.0, "recent_replies": 0,
        }

        def finish(allow: bool, *, hard: bool = False) -> dict:
            is_proactive_wake = bool(allow and proactive_wake_candidate and not signals.candidate and not signals.direct)
            result.update(
                decision="reply" if allow else "suppress" if eligible else "skip",
                score=round(max(0.0, min(100.0, score)), 2),
                proactive_wake=is_proactive_wake,
                would_reply=allow,
                allow=allow if hard or mode == "enforce" else (signals.direct or signals.candidate),
                enforced=mode == "enforce" and (signals.direct or signals.candidate) and not allow,
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
            if signals.quiet_hours_active and not signals.direct:
                reasons.append("proactive_quiet_hours")
                return finish(False, hard=True)
            return finish(bool(signals.direct or signals.candidate))

        # Bot 协同防死循环：即使被 Bot @ 了，超过协同上限也硬拦；未被 @ 时绝不主动回 Bot
        if signals.sender_is_bot:
            if not signals.direct:
                reasons.append("sender_is_bot_no_proactive")
                return finish(False, hard=True)
            if signals.bot_collab_exceeded:
                reasons.append("bot_collab_limit_exceeded")
                return finish(False, hard=True)

        if signals.direct:
            score = 100.0
            reasons.append("direct_priority")
            return finish(True)

        # ─── 基础 7 维因子（向后兼容已有契约） ───
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
        feedback = signals.feedback if signals.feedback in ("positive", "negative") else None
        factors["feedback"] = 10.0 if feedback == "positive" else -15.0 if feedback == "negative" else 0.0
        reasons.append(f"feedback_{feedback}" if feedback else "feedback_unknown")
        factors["backlog"] = -min(40.0, _number(signals.backlog, 0, 0, 100) * 10.0)
        if factors["backlog"]:
            reasons.append("backlog")
        state = self._state((signals.bot_id, signals.visibility, signals.session_id))
        while state.replies and stamp - state.replies[0] >= 3600:
            state.replies.popleft()
        recent = sum(0 <= stamp - t < cfg["reply_window_seconds"] for t in state.replies)
        result["recent_replies"] = recent
        engaged_followup = bool(proactive_wake_candidate and signals.followup_eligible)
        # 刚主动搭话后对方 60 秒内顺着接话（recent=1 是自己刚才那句触发的），不应被自己那 1 条反扣 -12 分卡死；
        # 超过 1 条仍逐步降权，且 recent >= max_recent_replies 硬上限不变。
        penalized_recent = max(0, recent - 1) if engaged_followup else recent
        factors["recent_replies"] = -min(48.0, penalized_recent * 12.0)
        if penalized_recent:
            reasons.append("recent_bot_replies")

        # ─── 扩展读空气 / 话题分工 / 心智调节因子（仅在相关信号启用或命中时写入） ───
        if signals.is_command_or_farewell:
            factors["command_or_farewell"] = -80.0
            reasons.append("command_or_farewell")
        if signals.is_system_notice:
            factors["system_notice"] = -80.0
            reasons.append("system_or_error_notice")
        if signals.addressed_other:
            factors["addressed_other"] = -60.0
            reasons.append("addressed_other")
        if signals.in_duo_pingpong:
            factors["duo_pingpong"] = -35.0
            reasons.append("duo_pingpong")
        if signals.avoid_topic_hit:
            factors["avoid_topic"] = -55.0
            reasons.append("avoid_topic_hit")
        if signals.topic_match is not None and signals.topic_match > 0:
            factors["topic_match"] = round(38.0 * _number(signals.topic_match, 0, 0, 1), 2)
            reasons.append("topic_match")
        if signals.followup_eligible:
            # 刚主动搭话后对方顺着回话（engaged 窗口），即使消息本身不带关键词、也不带问号，
            # 也应稳稳跨过默认 balanced 门槛（55），让对话自然接完；如命中避让词/指令/告别/点名他人仍会压住。
            factors["followup_window"] = 58.0 if proactive_wake_candidate else 26.0
            reasons.append("followup_window")
        if signals.mind_drive is not None or signals.mind_energy is not None:
            drive = _number(signals.mind_drive, 0.5, 0.0, 1.0)
            energy = _number(signals.mind_energy, 0.7, 0.0, 1.0)
            influence = _number(signals.mood_influence, 1.0, 0.0, 2.0)
            # drive=0.5 中性为 0；高精力+高倾诉欲最多 +18，低精力/休眠最多 -28
            mind_delta = ((drive - 0.5) * 28.0 + (energy - 0.5) * 16.0) * influence
            if signals.mind_stance == "quiet" or energy < 0.25:
                mind_delta -= 25.0 * max(0.5, influence)
                reasons.append("mind_low_energy_quiet")
            elif signals.mind_stance == "cooling":
                mind_delta -= 12.0 * max(0.5, influence)
                reasons.append("mind_cooling")
            factors["mind_state"] = round(mind_delta, 2)
        if signals.body_drive is not None:
            # 身体一天里的常态主动意愿约 0.15（0 = 困/压力大，0.45 = 很无聊或很想说话）；偏离常态才推一把。
            # 量级刻意比话题（最多 +38）和心智（+18/-28）小：身体只轻推，不替读空气做主。
            body_delta = (_number(signals.body_drive, 0.15, 0.0, 1.0) - 0.15) * 40.0
            body_delta *= _number(signals.mood_influence, 1.0, 0.0, 2.0)
            body_delta = max(-15.0, min(15.0, body_delta))
            if abs(body_delta) >= 0.5:
                factors["body_drive"] = round(body_delta, 2)
                reasons.append("body_drive_up" if body_delta > 0 else "body_drive_down")
        if signals.silence_messages > 0 and signals.proactive_enabled:
            factors["silence_buildup"] = round(min(14.0, signals.silence_messages * 1.4), 2)
        if signals.group_msgs_per_min >= 12:
            factors["group_flood_suppress"] = -min(30.0, (signals.group_msgs_per_min - 10) * 3.0)
            reasons.append("group_flood_suppress")
        if signals.ignored_streak > 0:
            factors["ignored_backoff"] = -min(36.0, signals.ignored_streak * 14.0)
            reasons.append("ignored_backoff")

        score = sum(factors.values())
        # 无实质锚点封顶门禁：非接话窗口、无话题匹配、无相关度、无关切跟进时，
        # 不允许单靠「心智精力 + 身体推力 + 沉默蓄能 + 泛问号」破门强插无关闲聊。
        has_topic_anchor = bool(
            signals.followup_eligible
            or (signals.topic_match is not None and signals.topic_match > 0)
            or relevance > 0
            or signals.concern
        )
        if proactive_wake_candidate and not has_topic_anchor:
            unanchored_cap = max(0.0, cfg["score_threshold"] - 5.0)
            if score > unanchored_cap:
                factors["unanchored_cap"] = round(unanchored_cap - score, 2)
                score = unanchored_cap
            reasons.append("no_topic_anchor")

        # 明确点名/引用他人或第三方机器报错回执：非直接召唤时一律硬拦截，绝不被正分冲穿
        if signals.addressed_other or signals.is_system_notice:
            return finish(False, hard=True)

        effective_cooldown = 0.0 if engaged_followup else _number(signals.cooldown_remaining, 0, 0, 86400)
        retry_after = max(
            effective_cooldown,
            _number(signals.shared_cooldown_remaining, 0, 0, 86400),
        )
        if signals.shared_cooldown_remaining > 0:
            reasons.append("shared_bot_cooldown")
        if signals.candidate_gate in {"concern_cooldown", "concern_quiet_hours", "concern_group_gap", "concern_hourly_limit"}:
            reasons.append(signals.candidate_gate)
        if signals.quiet_hours_active and (proactive_wake_candidate or signals.candidate):
            reasons.append("proactive_quiet_hours")
            return finish(False, hard=True)
        if signals.proactive_hourly_limit_hit and proactive_wake_candidate and not engaged_followup:
            reasons.append("proactive_hourly_limit")
            return finish(False)
        if signals.consecutive_proactive_hit and proactive_wake_candidate and not signals.followup_eligible:
            reasons.append("proactive_consecutive_limit")
            return finish(False)
        if not eligible:
            reasons.append("no_existing_candidate")
            return finish(False)
        if stamp < state.last_evaluated or stamp - state.last_evaluated > cfg["backoff_max_seconds"]:
            state.streak = 0
            state.suppressed_until = 0
        state.last_evaluated = stamp
        if not proactive_wake_candidate:
            retry_after = max(retry_after, state.suppressed_until - stamp)
        if retry_after:
            reasons.append(
                "shared_bot_cooldown" if signals.shared_cooldown_remaining > 0 and retry_after == signals.shared_cooldown_remaining
                else "cooldown" if effective_cooldown > 0
                else "suppression_backoff"
            )
            return finish(False)
        allow = (
            not noise
            and not signals.is_command_or_farewell
            and not signals.avoid_topic_hit
            and recent < cfg["max_recent_replies"]
            and score >= cfg["score_threshold"]
        )
        reasons.append(
            "score_pass" if allow
            else "recent_reply_limit" if recent >= cfg["max_recent_replies"]
            else "score_below_threshold" if score < cfg["score_threshold"]
            else "rule_suppressed"
        )
        if allow:
            state.streak = 0
            state.suppressed_until = 0
        elif not proactive_wake_candidate:
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


__all__ = [
    "GroupProactiveArbiter",
    "ReplyDecisionService",
    "ReplySignals",
    "attach_reply_decision",
    "detect_message_air_flags",
    "feedback_polarity",
    "score_topic_division",
]
