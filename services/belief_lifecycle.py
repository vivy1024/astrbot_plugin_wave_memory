"""信念 v2 生命周期：看法怎样从她自己的经历里慢慢形成、动摇、淡出。

状态：
- ``sprout`` 萌芽：第一次从经历里冒出来，只出现过一次，不注入；
- ``formed`` 形成：在至少 ``form_min_days`` 个不同日子的经历里得到支持，且被后来的反思再次印证；
- ``stable`` 稳固：持续被印证（不同日子数达到 ``2 × form_min_days``、被至少 3 次反思印证）；
- ``shaken`` 动摇：形成/稳固后遇到反例，或长期没再被印证而变弱；不注入；
- ``faded`` 已淡出：动摇后再被推翻、长期不再被印证、或超出数量上限时最弱的；
- ``vetoed`` 已否决：管理员否决；内容被记住，之后不再生成相似的看法。

每一次变化都写一条历史事件（带依据日记）。本模块只做计算与编排，SQL 在
``engine/db/bot_belief_repo.py``。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from ..engine.db.bot_belief_repo import BeliefRevisionConflict, BeliefWrite
except ImportError:  # pragma: no cover - 仓库根目录直接导入
    from engine.db.bot_belief_repo import BeliefRevisionConflict, BeliefWrite

SPROUT = "sprout"
FORMED = "formed"
STABLE = "stable"
SHAKEN = "shaken"
FADED = "faded"
VETOED = "vetoed"
LEGACY_ARCHIVED = "legacy_archived"

STATUSES = (SPROUT, FORMED, STABLE, SHAKEN, FADED, VETOED)
# 还在她心里的看法（参与反思、会衰减）
LIVE_STATUSES = frozenset({SPROUT, FORMED, STABLE, SHAKEN})
# 已经形成、计入数量上限的看法
HELD_STATUSES = frozenset({FORMED, STABLE, SHAKEN})
# 可以在话题相关时浮现的看法
INJECTABLE_STATUSES = frozenset({FORMED, STABLE})

STATUS_LABELS = {
    SPROUT: "萌芽",
    FORMED: "已形成",
    STABLE: "稳固",
    SHAKEN: "动摇中",
    FADED: "已淡出",
    VETOED: "已否决",
    LEGACY_ARCHIVED: "已归档（旧版人物判断）",
}

CATEGORIES = ("self", "group", "thing", "world")
CATEGORY_LABELS = {"self": "关于自己", "group": "关于群", "thing": "关于事物", "world": "关于世界"}

EVENT_LABELS = {
    "sprouted": "萌芽",
    "resprouted": "重新冒出",
    "confirmed": "被印证",
    "challenged": "遇到反例",
    "fade_suggested": "反思时觉得不再适用",
    "decayed": "长期没被印证而变弱",
    "cap_evicted": "超出数量上限，最弱的淡出",
    "vetoed": "管理员否决",
    "restored": "管理员恢复",
}

_SECONDS_PER_DAY = 86400.0


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


@dataclass(frozen=True)
class BeliefPolicy:
    # 形成：至少在这么多个不同日子的经历里得到支持（萌芽时的依据也算），且被至少 form_min_runs 次反思印证
    form_min_days: int = 2
    # 多少天不再被印证就降一级（稳固→形成→动摇→淡出；萌芽直接淡出）
    decay_days: int = 30
    # 同时有效（已形成/稳固/动摇）的看法上限；萌芽另按同一上限限制条数
    max_active: int = 20
    form_min_runs: int = 2
    stable_min_runs: int = 3

    @property
    def stable_min_days(self) -> int:
        return max(self.form_min_days + 1, self.form_min_days * 2)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "BeliefPolicy":
        """配置项留空、写错时回到默认值。"""
        source = cfg if isinstance(cfg, Mapping) else {}

        def pick(key: str, default: int, lo: int, hi: int) -> int:
            value = source.get(key)
            if value is None or value == "":
                return default
            return _int(value, default, lo, hi)

        return cls(
            form_min_days=pick("belief_form_min_days", 2, 1, 30),
            decay_days=pick("belief_decay_days", 30, 3, 365),
            max_active=pick("belief_max_active", 20, 1, 200),
        )


# ─── 小工具 ───


def normalize_text(text: Any) -> str:
    return re.sub(r"[\s，。,.!！?？、；;：:“”\"'‘’（）()【】\[\]…—\-~～]+", "", str(text or "")).casefold()


def bigrams(text: Any) -> set[str]:
    chars = normalize_text(text)
    return {chars[i:i + 2] for i in range(max(0, len(chars) - 1))}


def similarity(left: Any, right: Any) -> float:
    """与关系小传同一写法的二元组重合度：交集 / 较短一方的二元组数。"""
    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 1.0 if normalize_text(left) and normalize_text(left) == normalize_text(right) else 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def most_similar(content: str, beliefs: Iterable[Mapping[str, Any]], threshold: float) -> Mapping[str, Any] | None:
    best: tuple[float, Mapping[str, Any]] | None = None
    for belief in beliefs:
        score = similarity(content, belief.get("content"))
        if score >= threshold and (best is None or score > best[0]):
            best = (score, belief)
    return best[1] if best else None


def _dates(values: Iterable[Any]) -> list[str]:
    return sorted({str(v) for v in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v or ""))})


def compute_strength(belief: Mapping[str, Any], *, now: float, policy: BeliefPolicy) -> float:
    """支持强度（0–1）：不同日子的支持越多越强，反例与长期不被印证会削弱。只用于排序与上限淘汰。"""
    days = len(_dates(belief.get("support_dates") or []))
    runs = int(belief.get("confirm_runs") or 0)
    base = 1.0 - 0.5 ** max(0, days)
    base = min(1.0, base + 0.03 * max(0, runs - 1))
    penalty = min(0.6, 0.15 * int(belief.get("challenge_streak") or 0) + 0.05 * int(belief.get("challenge_count") or 0))
    reference = float(belief.get("last_confirmed_at") or belief.get("first_seen_at") or now)
    idle_days = max(0.0, (now - reference) / _SECONDS_PER_DAY)
    freshness = 0.6 + 0.4 * math.exp(-idle_days / max(1.0, float(policy.decay_days)))
    return round(max(0.0, min(1.0, (base - penalty) * freshness)), 4)


# ─── 状态转移（纯函数：输入当前看法，输出要写的字段与事件） ───


@dataclass
class Change:
    fields: dict[str, Any]
    event: dict[str, Any]


def _event(
    belief: Mapping[str, Any], event_type: str, to_status: str, *, reason: str, evidence: Sequence[Mapping[str, Any]] = (),
    strength_after: float | None = None, actor: str = "reflection", run_id: int | None = None, now: float,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "from_status": str(belief.get("status") or ""),
        "to_status": to_status,
        "strength_before": belief.get("strength"),
        "strength_after": strength_after,
        "reason": str(reason or "")[:300],
        "evidence": [dict(item) for item in evidence],
        "actor": actor,
        "run_id": run_id,
        "occurred_at": now,
    }


def _finish(belief: Mapping[str, Any], fields: dict[str, Any], event_type: str, *, policy: BeliefPolicy, now: float,
            reason: str, evidence: Sequence[Mapping[str, Any]] = (), actor: str = "reflection",
            run_id: int | None = None) -> Change:
    merged = {**belief, **fields}
    fields["strength"] = compute_strength(merged, now=now, policy=policy)
    to_status = str(fields.get("status") or belief.get("status") or "")
    if to_status != belief.get("status"):
        fields["status_changed_at"] = now
    return Change(fields, _event(belief, event_type, to_status, reason=reason, evidence=evidence,
                                 strength_after=fields["strength"], actor=actor, run_id=run_id, now=now))


def new_sprout(
    *, content: str, category: str, triggers: Sequence[str], evidence: Sequence[Mapping[str, Any]],
    reason: str, now: float, policy: BeliefPolicy, run_id: int | None = None,
) -> Change:
    dates = _dates(item.get("date") for item in evidence)
    sessions = sorted({str(item.get("session_id")) for item in evidence if item.get("session_id")})
    fields = {
        "content": content,
        "category": category if category in CATEGORIES else "world",
        "status": SPROUT,
        "triggers": list(triggers),
        "support_dates": dates,
        "session_ids": sessions,
        "confirm_runs": 1,
        "challenge_count": 0,
        "challenge_streak": 0,
        "first_seen_at": now,
        "last_confirmed_at": now,
        "status_changed_at": now,
    }
    fields["strength"] = compute_strength(fields, now=now, policy=policy)
    event = _event({"status": ""}, "sprouted", SPROUT, reason=reason, evidence=evidence,
                   strength_after=fields["strength"], run_id=run_id, now=now)
    return Change(fields, event)


def resprout(
    belief: Mapping[str, Any], *, triggers: Sequence[str], evidence: Sequence[Mapping[str, Any]], reason: str,
    now: float, policy: BeliefPolicy, run_id: int | None = None,
) -> Change:
    """已淡出的看法又从新的经历里冒出来：重新从萌芽开始（旧历史保留）。"""
    fields = {
        "status": SPROUT,
        "triggers": list(triggers) or list(belief.get("triggers") or []),
        "support_dates": _dates(item.get("date") for item in evidence),
        "session_ids": sorted({str(item.get("session_id")) for item in evidence if item.get("session_id")}),
        "confirm_runs": 1,
        "challenge_count": 0,
        "challenge_streak": 0,
        "first_seen_at": now,
        "last_confirmed_at": now,
        "last_decay_at": None,
    }
    return _finish(belief, fields, "resprouted", policy=policy, now=now, reason=reason, evidence=evidence, run_id=run_id)


def confirm(
    belief: Mapping[str, Any], *, evidence: Sequence[Mapping[str, Any]], reason: str, now: float,
    policy: BeliefPolicy, run_id: int | None = None, triggers: Sequence[str] = (),
) -> Change:
    status = str(belief.get("status") or SPROUT)
    dates = _dates([*(belief.get("support_dates") or []), *(item.get("date") for item in evidence)])
    sessions = sorted({*(str(s) for s in belief.get("session_ids") or []),
                       *(str(item.get("session_id")) for item in evidence if item.get("session_id"))})
    runs = int(belief.get("confirm_runs") or 0) + 1
    target = status
    if status == SPROUT and len(dates) >= policy.form_min_days and runs >= policy.form_min_runs:
        target = FORMED
    elif status == FORMED and len(dates) >= policy.stable_min_days and runs >= policy.stable_min_runs:
        target = STABLE
    elif status == SHAKEN:
        target = FORMED
    fields: dict[str, Any] = {
        "status": target,
        "support_dates": dates,
        "session_ids": sessions,
        "confirm_runs": runs,
        "challenge_streak": 0,
        "last_confirmed_at": now,
        "last_decay_at": None,
    }
    if triggers:
        merged = list(dict.fromkeys([*(belief.get("triggers") or []), *triggers]))[:8]
        fields["triggers"] = merged
    return _finish(belief, fields, "confirmed", policy=policy, now=now, reason=reason, evidence=evidence, run_id=run_id)


def challenge(
    belief: Mapping[str, Any], *, evidence: Sequence[Mapping[str, Any]], reason: str, now: float,
    policy: BeliefPolicy, run_id: int | None = None,
) -> Change:
    status = str(belief.get("status") or SPROUT)
    target = {SPROUT: FADED, FORMED: SHAKEN, STABLE: SHAKEN, SHAKEN: FADED}.get(status, status)
    fields = {
        "status": target,
        "challenge_count": int(belief.get("challenge_count") or 0) + 1,
        "challenge_streak": int(belief.get("challenge_streak") or 0) + 1,
        "last_challenged_at": now,
    }
    return _finish(belief, fields, "challenged", policy=policy, now=now, reason=reason, evidence=evidence, run_id=run_id)


def suggest_fade(
    belief: Mapping[str, Any], *, reason: str, now: float, policy: BeliefPolicy, run_id: int | None = None,
) -> Change:
    """反思时她自己觉得不再适用：没形成或已动摇的直接淡出，已形成/稳固的先动摇。"""
    status = str(belief.get("status") or SPROUT)
    target = {SPROUT: FADED, SHAKEN: FADED, FORMED: SHAKEN, STABLE: SHAKEN}.get(status, status)
    return _finish(belief, {"status": target}, "fade_suggested", policy=policy, now=now, reason=reason, run_id=run_id)


def decay(belief: Mapping[str, Any], *, now: float, policy: BeliefPolicy) -> Change | None:
    """长期不再被印证：每过 ``decay_days`` 天降一级（稳固→形成→动摇→淡出；萌芽直接淡出）。"""
    status = str(belief.get("status") or "")
    if status not in LIVE_STATUSES:
        return None
    reference = max(
        float(belief.get("last_confirmed_at") or belief.get("first_seen_at") or now),
        float(belief.get("last_decay_at") or 0.0),
    )
    if now - reference < policy.decay_days * _SECONDS_PER_DAY:
        return None
    target = {STABLE: FORMED, FORMED: SHAKEN, SHAKEN: FADED, SPROUT: FADED}[status]
    idle = int((now - float(belief.get("last_confirmed_at") or belief.get("first_seen_at") or now)) // _SECONDS_PER_DAY)
    return _finish(belief, {"status": target, "last_decay_at": now}, "decayed", policy=policy, now=now,
                   reason=f"已经 {idle} 天没有在经历里被印证", actor="lifecycle")


def veto(belief: Mapping[str, Any], *, reason: str, now: float, policy: BeliefPolicy, actor: str = "webui") -> Change:
    return _finish(belief, {"status": VETOED}, "vetoed", policy=policy, now=now,
                   reason=reason or "管理员否决", actor=actor)


def restore(
    belief: Mapping[str, Any], *, previous_status: str, now: float, policy: BeliefPolicy, actor: str = "webui",
) -> Change:
    target = previous_status if previous_status in LIVE_STATUSES | {FADED} else SPROUT
    return _finish(belief, {"status": target}, "restored", policy=policy, now=now,
                   reason="管理员撤销否决", actor=actor)


def cap_evictions(
    beliefs: Sequence[Mapping[str, Any]], *, policy: BeliefPolicy, now: float | None = None,
) -> list[Mapping[str, Any]]:
    """超出上限时要淡出的看法：已形成的按当前强度最弱先出；萌芽按最早出现的先出。"""
    stamp = float(now if now is not None else time.time())
    held = [b for b in beliefs if b.get("status") in HELD_STATUSES]
    sprouts = [b for b in beliefs if b.get("status") == SPROUT]
    out: list[Mapping[str, Any]] = []
    if len(held) > policy.max_active:
        held.sort(key=lambda b: (compute_strength(b, now=stamp, policy=policy),
                                 float(b.get("last_confirmed_at") or 0.0), int(b.get("id") or 0)))
        out.extend(held[: len(held) - policy.max_active])
    if len(sprouts) > policy.max_active:
        sprouts.sort(key=lambda b: (float(b.get("first_seen_at") or 0.0), int(b.get("id") or 0)))
        out.extend(sprouts[: len(sprouts) - policy.max_active])
    return out


def evict(belief: Mapping[str, Any], *, now: float, policy: BeliefPolicy, run_id: int | None = None) -> Change:
    return _finish(belief, {"status": FADED}, "cap_evicted", policy=policy, now=now,
                   reason=f"同时有效的看法超过上限 {policy.max_active} 条，这条最弱", actor="lifecycle", run_id=run_id)


def as_write(belief: Mapping[str, Any] | None, change: Change, *, check_revision: bool = False) -> BeliefWrite:
    if belief is None:
        return BeliefWrite(None, dict(change.fields), events=[change.event])
    return BeliefWrite(
        int(belief["id"]), dict(change.fields),
        expected_revision=int(belief["revision"]) if check_revision and belief.get("revision") is not None else None,
        events=[change.event],
    )


class BeliefAdminError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        self.reason_code = code
        super().__init__(code)


class BeliefLifecycleService:
    """管理台操作与无模型的日常维护：否决 / 恢复 / 衰减 / 数量上限。"""

    def __init__(self, repository: Any, policy: BeliefPolicy | None = None, *, clock=None):
        self.repository = repository
        self.policy = policy or BeliefPolicy()
        self.clock = clock or time.time

    def _current(self, bot_id: str, belief_id: int, revision: Any) -> Mapping[str, Any]:
        belief = self.repository.get_belief(bot_id, int(belief_id))
        if belief is None:
            raise BeliefAdminError("belief_not_found")
        try:
            expected = int(revision)
        except (TypeError, ValueError):
            raise BeliefAdminError("belief_revision_required") from None
        if int(belief.get("revision") or 0) != expected:
            raise BeliefRevisionConflict(int(belief_id))
        return belief

    def veto(self, bot_id: str, belief_id: int, *, revision: Any, reason: str = "", actor: str = "webui") -> dict:
        belief = self._current(bot_id, belief_id, revision)
        if belief.get("status") == VETOED:
            raise BeliefAdminError("belief_already_vetoed")
        now = float(self.clock())
        change = veto(belief, reason=reason, now=now, policy=self.policy, actor=actor)
        self.repository.apply(bot_id, [as_write(belief, change, check_revision=True)], now=now)
        return self.repository.get_belief(bot_id, int(belief_id)) or {}

    def restore(self, bot_id: str, belief_id: int, *, revision: Any, actor: str = "webui") -> dict:
        belief = self._current(bot_id, belief_id, revision)
        if belief.get("status") != VETOED:
            raise BeliefAdminError("belief_not_vetoed")
        previous = SPROUT
        for event in reversed(self.repository.list_events(bot_id, belief_ids=[int(belief_id)])):
            if event.get("event_type") == "vetoed":
                previous = str(event.get("from_status") or SPROUT)
                break
        now = float(self.clock())
        change = restore(belief, previous_status=previous, now=now, policy=self.policy, actor=actor)
        self.repository.apply(bot_id, [as_write(belief, change, check_revision=True)], now=now)
        self.enforce_cap(bot_id, now=now)
        return self.repository.get_belief(bot_id, int(belief_id)) or {}

    def apply_decay(self, bot_id: str, *, now: float | None = None) -> int:
        stamp = float(now if now is not None else self.clock())
        writes = []
        for belief in self.repository.list_beliefs(bot_id, statuses=sorted(LIVE_STATUSES)):
            change = decay(belief, now=stamp, policy=self.policy)
            if change is not None:
                writes.append(as_write(belief, change, check_revision=True))
        if writes:
            self.repository.apply(bot_id, writes, now=stamp)
        return len(writes)

    def enforce_cap(self, bot_id: str, *, now: float | None = None, run_id: int | None = None) -> int:
        stamp = float(now if now is not None else self.clock())
        live = self.repository.list_beliefs(bot_id, statuses=sorted(LIVE_STATUSES))
        evicted = cap_evictions(live, policy=self.policy, now=stamp)
        if evicted:
            self.repository.apply(
                bot_id, [as_write(b, evict(b, now=stamp, policy=self.policy, run_id=run_id), check_revision=True) for b in evicted],
                now=stamp,
            )
        return len(evicted)


__all__ = [
    "BeliefAdminError",
    "BeliefLifecycleService",
    "BeliefPolicy",
    "CATEGORIES",
    "CATEGORY_LABELS",
    "Change",
    "EVENT_LABELS",
    "FADED",
    "FORMED",
    "HELD_STATUSES",
    "INJECTABLE_STATUSES",
    "LEGACY_ARCHIVED",
    "LIVE_STATUSES",
    "SHAKEN",
    "SPROUT",
    "STABLE",
    "STATUSES",
    "STATUS_LABELS",
    "VETOED",
    "as_write",
    "bigrams",
    "cap_evictions",
    "challenge",
    "compute_strength",
    "confirm",
    "decay",
    "evict",
    "most_similar",
    "new_sprout",
    "normalize_text",
    "restore",
    "resprout",
    "similarity",
    "suggest_fade",
    "veto",
]
