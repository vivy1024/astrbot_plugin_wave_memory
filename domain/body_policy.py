"""Bot 的身体：一个 Bot 一副激素状态，纯策略、零 IO、零 LLM。

设计取自社区几套激素引擎并去掉了它们作者自己踩过的坑：
- 向「随时间变化的昼夜基线」回落，而不是固定值（dsh-emotion-engine）；
- 同一件事反复发生按 0.8ⁿ 习惯化，血清素高时压力增量打折（dsh-auralife）；
- 激素之间有耦合，但全部夹在上下限内；
- 只让**真人事件**改变状态，模型自己不能改（dsh-hormone）；
- 时间推进要扣掉插件离线的时长，否则停机 7 小时醒来会以为对方 7 小时没理她；
- 对外只暴露边沿事实，不暴露数值，也不写「你很生气」这类指令。

身体是慢层；现有 ``mood_policy`` 的事件半衰期心情是快层。两层互不写入：
这里只输出一个小的 ``mood_bias`` 和若干行为系数。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

POLICY_VERSION = "bot-body/v1"

HORMONES = ("dopamine", "serotonin", "oxytocin", "cortisol", "melatonin", "boredom")

# 半衰期（分钟）。boredom 单独处理：安静一阵之后才开始向 100 靠拢。
HALF_LIFE_MINUTES: Mapping[str, float] = {
    "dopamine": 25.0,
    "serotonin": 300.0,
    "oxytocin": 120.0,
    "cortisol": 90.0,
    "melatonin": 50.0,
    "boredom": 300.0,
}

BOREDOM_GRACE_MINUTES = 25.0
# 偏离昼夜基线多少（以 30 点为一个单位）才算「刚越过」：0.75 ≈ 比基线高/低 22 点
EDGE_THRESHOLD = 0.75
STEP_MINUTES = 5.0
MAX_ELAPSED_HOURS = 48.0
HABITUATION_BASE = 0.8
HABITUATION_WINDOW_SECONDS = 24 * 3600.0


def wave(hour: float, peak: float) -> float:
    """以 peak 点为峰值的 24 小时余弦波，取值 0..1。"""
    return (math.cos(2.0 * math.pi * (float(hour) - float(peak)) / 24.0) + 1.0) / 2.0


def baseline(hormone: str, hour: float, *, closeness: float = 45.0) -> float:
    """某一激素在这个钟点的昼夜基线（0..100）。"""
    w14, w21, w8, w2 = wave(hour, 14), wave(hour, 21), wave(hour, 8), wave(hour, 2)
    if hormone == "dopamine":
        return 46.0 + 12.0 * w14 + 4.0 * w21
    if hormone == "serotonin":
        return 40.0 + 24.0 * w14
    if hormone == "oxytocin":
        return 44.0 + 8.0 * w14 + 8.0 * w21 + 8.0 * (max(0.0, min(100.0, closeness)) - 50.0) / 50.0
    if hormone == "cortisol":
        return 26.0 + 30.0 * w8
    if hormone == "melatonin":
        return 14.0 + 62.0 * w2
    if hormone == "boredom":
        return 30.0
    raise KeyError(hormone)


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def default_state(hour: float = 12.0) -> dict[str, float]:
    return {name: round(baseline(name, hour), 3) for name in HORMONES}


@dataclass(frozen=True)
class BodyEvent:
    """一次会改变身体的事。``deltas`` 是对各激素的基础增量（还没经过习惯化与亲密缩放）。"""

    kind: str
    deltas: Mapping[str, float]
    # 习惯化键：同一个人的同一类事件反复发生才递减；空表示不习惯化（如被叫到）
    habituate: bool = True


# 事件表：增量刻意取小，社区作者的教训是「一两轮就顶到上限，不叫敏感，叫吵」。
EVENT_TABLE: Mapping[str, BodyEvent] = {
    "addressed": BodyEvent("addressed", {"dopamine": 4.0, "oxytocin": 3.0, "boredom": -30.0}, habituate=False),
    "praised": BodyEvent("praised", {"dopamine": 8.0, "oxytocin": 5.0, "serotonin": 2.0}),
    "scolded": BodyEvent("scolded", {"cortisol": 10.0, "serotonin": -4.0, "oxytocin": -2.0}),
    "joked": BodyEvent("joked", {"dopamine": 4.0, "oxytocin": 2.0}),
    "gift": BodyEvent("gift", {"dopamine": 7.0, "oxytocin": 6.0}),
    "apology": BodyEvent("apology", {"cortisol": -6.0, "oxytocin": 4.0, "serotonin": 2.0}),
    "deep_talk": BodyEvent("deep_talk", {"oxytocin": 6.0, "serotonin": 3.0, "dopamine": 2.0}),
    "boundary": BodyEvent("boundary", {"cortisol": 8.0, "serotonin": -3.0}),
    "reunion": BodyEvent("reunion", {"oxytocin": 4.0, "dopamine": 3.0}, habituate=False),
    "snubbed": BodyEvent("snubbed", {"cortisol": 6.0, "oxytocin": -4.0}, habituate=False),
    "tool_error": BodyEvent("tool_error", {"cortisol": 2.0}, habituate=False),
    "tool_error_streak": BodyEvent("tool_error_streak", {"cortisol": 5.0}, habituate=False),
    "late_night_call": BodyEvent("late_night_call", {"melatonin": 6.0, "cortisol": 2.0}),
}

# 关系事件（relationship_policy.VALID_EVENT_TYPES）到身体事件的映射。
RELATIONSHIP_EVENT_MAP: Mapping[str, str] = {
    "bot_praised": "praised",
    "bot_attacked": "scolded",
    "ignored_boundary": "boundary",
    "gift_or_feed": "gift",
    "joke": "joked",
    "deep_talk": "deep_talk",
    "confession": "deep_talk",
    "correction": "boundary",
}


def advance(
    state: Mapping[str, float],
    *,
    elapsed_seconds: float,
    hour_at: Any,
    start_ts: float,
    closeness: float = 45.0,
    quiet_seconds: float = 0.0,
) -> dict[str, float]:
    """把身体从 ``start_ts`` 往前推进 ``elapsed_seconds``。

    ``elapsed_seconds`` 必须是**已经扣掉离线时长**的有效时间；本函数再封顶到 48 小时。
    ``hour_at(ts) -> float`` 给出该时刻 Bot 所在时区的小时（含小数），按 5 分钟步长积分，
    这样跨过昼夜基线拐点时是连续的。``quiet_seconds`` 是此前已经安静了多久，
    用于决定无聊何时开始累积。
    """
    remaining = max(0.0, min(float(elapsed_seconds), MAX_ELAPSED_HOURS * 3600.0))
    values = {name: clamp(state.get(name, baseline(name, 12.0))) for name in HORMONES}
    if remaining <= 0:
        return values
    step = STEP_MINUTES * 60.0
    cursor = float(start_ts)
    quiet = max(0.0, float(quiet_seconds))
    while remaining > 1e-6:
        dt = min(step, remaining)
        hour = float(hour_at(cursor + dt / 2.0))
        minutes = dt / 60.0
        da, ser, oxy, cort = values["dopamine"], values["serotonin"], values["oxytocin"], values["cortisol"]
        for name in HORMONES:
            if name == "boredom":
                quiet += dt
                if quiet / 60.0 >= BOREDOM_GRACE_MINUTES:
                    target = 100.0
                    half = HALF_LIFE_MINUTES["boredom"]
                    values[name] = target + (values[name] - target) * 0.5 ** (minutes / half)
                else:
                    values[name] = baseline(name, hour) + (values[name] - baseline(name, hour)) * 0.5 ** (
                        minutes / HALF_LIFE_MINUTES[name]
                    )
                continue
            half = HALF_LIFE_MINUTES[name]
            if name == "cortisol":
                # 开心（多巴胺高）时压力消得快
                half = half * (1.0 - 0.35 * clamp(da) / 100.0)
            target = baseline(name, hour, closeness=closeness)
            values[name] = target + (values[name] - target) * 0.5 ** (minutes / max(1.0, half))
        # 压力长期过高会拖累血清素（每小时 -6，按步长折算）
        if cort >= 70.0:
            values["serotonin"] = values["serotonin"] - 6.0 * (minutes / 60.0)
        for name in HORMONES:
            values[name] = clamp(values[name])
        cursor += dt
        remaining -= dt
    return {name: round(value, 3) for name, value in values.items()}


def habituation_factor(prior_count: int) -> float:
    """同一个人同一类事件在 24 小时内已发生 n 次，这一次的增量乘 0.8ⁿ。"""
    return HABITUATION_BASE ** max(0, int(prior_count))


def intimacy_scale(closeness: float, *, negative: bool) -> float:
    """亲近程度改变事件的分量。

    陌生人的示好只按约 40% 计入；亲近的人伤到你更疼（负向额外放大，最多 1.5 倍）。
    ``closeness`` 取 0..100（关系温度，50 为一般熟）。
    """
    c = clamp(closeness) / 100.0
    if negative:
        return 0.6 + 0.9 * c
    return 0.4 + 0.6 * c


def apply_event(
    state: Mapping[str, float],
    event: BodyEvent | str,
    *,
    closeness: float = 50.0,
    prior_count: int = 0,
    gain: float = 1.0,
) -> tuple[dict[str, float], dict[str, float]]:
    """把一次事件作用到身体上，返回 (新状态, 实际增量)。"""
    spec = EVENT_TABLE[event] if isinstance(event, str) else event
    factor = habituation_factor(prior_count) if spec.habituate else 1.0
    values = {name: clamp(state.get(name, baseline(name, 12.0))) for name in HORMONES}
    applied: dict[str, float] = {}
    for name, base_delta in spec.deltas.items():
        delta = float(base_delta) * factor * max(0.0, float(gain))
        negative = delta < 0 if name != "cortisol" else delta > 0
        delta *= intimacy_scale(closeness, negative=negative)
        # 血清素高时压力的正向增量打 7 折：心情稳的时候不容易被激怒
        if name == "cortisol" and delta > 0 and values["serotonin"] >= 70.0:
            delta *= 0.7
        # 正向增量在高位越难涨
        if delta > 0 and name not in {"cortisol", "boredom"}:
            delta *= 1.0 - values[name] / 100.0 * 0.5
        before = values[name]
        values[name] = clamp(before + delta)
        applied[name] = round(values[name] - before, 3)
    return {name: round(value, 3) for name, value in values.items()}, applied


# ─── 读数：派生量与输出 ──────────────────────────────────────────────


@dataclass(frozen=True)
class BodyReading:
    """身体读数。数值只在内部使用；对外只给边沿事实与系数。"""

    hormones: dict[str, float]
    mood_bias: float
    arousal_bias: float
    reply_length_factor: float
    proactive_drive: float
    sleepiness: float
    asleep: bool
    flags: dict[str, bool] = field(default_factory=dict)


def read(
    state: Mapping[str, float],
    *,
    hour: float,
    closeness: float = 45.0,
    sleep_start: int | None = None,
    sleep_end: int | None = None,
) -> BodyReading:
    """从激素状态派生出对行为的偏置。

    ``mood_bias`` 夹在 ±0.2：身体只轻推快层心情，不替它做主。
    ``proactive_drive`` 0..1：无聊与催产素推高，压力与困意压低。
    """
    values = {name: clamp(state.get(name, baseline(name, hour))) for name in HORMONES}
    dev = {name: (values[name] - baseline(name, hour, closeness=closeness)) / 30.0 for name in HORMONES}
    valence = 0.5 * dev["dopamine"] + 0.4 * dev["serotonin"] + 0.3 * dev["oxytocin"] - 0.6 * max(0.0, dev["cortisol"])
    arousal = 0.4 * dev["dopamine"] + 0.5 * max(0.0, dev["cortisol"]) - 0.5 * max(0.0, dev["melatonin"])
    mood_bias = max(-0.2, min(0.2, valence * 0.2))
    arousal_bias = max(-0.2, min(0.2, arousal * 0.2))
    sleepiness = clamp(values["melatonin"]) / 100.0
    in_window = _in_sleep_window(hour, sleep_start, sleep_end)
    asleep = bool(in_window or sleepiness >= 0.8)
    length = 0.7 + 0.45 * (0.5 + arousal_bias * 2.5) + 0.35 * (0.5 + mood_bias * 2.5) - 0.5 * max(0.0, sleepiness - 0.45)
    proactive = (
        0.5 * (values["boredom"] / 100.0)
        + 0.3 * max(0.0, dev["oxytocin"])
        - 0.4 * max(0.0, dev["cortisol"])
        - 0.5 * max(0.0, sleepiness - 0.4)
    )
    length_factor = round(max(0.4, min(1.4, length)), 3)
    stressed = dev["cortisol"] >= EDGE_THRESHOLD
    wired = dev["dopamine"] >= EDGE_THRESHOLD
    low = dev["serotonin"] <= -EDGE_THRESHOLD
    drowsy = sleepiness >= 0.6
    flags = {
        "stressed": stressed,
        "wired": wired,
        "low": low,
        "affectionate": dev["oxytocin"] >= EDGE_THRESHOLD,
        "lonely": dev["oxytocin"] <= -EDGE_THRESHOLD,
        "bored": values["boredom"] >= 80.0,
        "drowsy": drowsy,
        # 长度系数拉低/拉高时给出事实提示；若已经说了「没劲/困/绷着/兴奋」，不重复说长短。
        "terse": length_factor <= 0.85 and not (low or drowsy or stressed),
        "chatty": length_factor >= 1.25 and not wired,
    }
    return BodyReading(
        hormones={name: round(value, 2) for name, value in values.items()},
        mood_bias=round(mood_bias, 4),
        arousal_bias=round(arousal_bias, 4),
        reply_length_factor=length_factor,
        proactive_drive=round(max(0.0, min(1.0, proactive)), 4),
        sleepiness=round(sleepiness, 4),
        asleep=asleep,
        flags=flags,
    )


def in_quiet_window(hour: float, start: int | None, end: int | None) -> bool:
    """是否处在模型服务静默时段（跨午夜自动处理；起止相同或缺一项视为没有静默）。"""
    return _in_sleep_window(hour, start, end)


def quiet_end_label(end: int | None) -> str:
    """「几点回来」的口语写法，用于替换话术里的 {end}；没配置时给一个不带钟点的说法。"""
    if end is None:
        return "天亮"
    return f"{int(end) % 24} 点"


def _in_sleep_window(hour: float, start: int | None, end: int | None) -> bool:
    if start is None or end is None or start == end:
        return False
    h = float(hour) % 24.0
    if start < end:
        return start <= h < end
    return h >= start or h < end


# 边沿事实：只在状态**刚越过**阈值时说一句，状态持续时闭嘴。
_EDGE_LINES: Mapping[str, str] = {
    "stressed": "你这会儿有点绷着，不太想应付麻烦事。",
    "wired": "你这会儿精神头很足，话容易多。",
    "low": "你这会儿提不起劲，懒得说长话。",
    "affectionate": "你这会儿心里软软的，对人比平时亲近。",
    "lonely": "你这会儿有点没着落，想有人说说话。",
    "bored": "你闲了好一阵，有点无聊。",
    "drowsy": "你有点困了。",
    "terse": "这会儿不太想多打字，回短点也没事。",
    "chatty": "这会儿挺顺口的，可以多聊两句。",
}
EDGE_REPEAT_SECONDS = 40 * 60.0
EDGE_MAX_CHARS = 80


def edge_notes(
    flags: Mapping[str, bool],
    last_announced: Mapping[str, float],
    *,
    now: float,
) -> tuple[list[str], dict[str, float]]:
    """返回 (要注入的边沿句子, 更新后的上次播报时间表)。

    规则：某个标志刚变为真才说一次；持续为真时 40 分钟内不重复；变回假则清掉记录，
    下次再越过时可以重新说。总长度不超过 80 字。
    """
    updated = {key: ts for key, ts in last_announced.items() if flags.get(key)}
    lines: list[str] = []
    for key, text in _EDGE_LINES.items():
        if not flags.get(key):
            continue
        last = updated.get(key)
        if last is None or now - float(last) >= EDGE_REPEAT_SECONDS:
            lines.append(text)
            updated[key] = float(now)
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) > EDGE_MAX_CHARS:
            break
        out.append(line)
        used += len(line)
    # 没被采用的（超长）不记为已播报，下一轮还有机会
    for key, text in _EDGE_LINES.items():
        if text in lines and text not in out:
            updated.pop(key, None)
    return out, updated


# ─── 睡眠态：被叫醒的掷骰（确定性，便于测试） ─────────────────────────

WAKE_WINDOW_SECONDS = 600.0
WAKE_CERTAIN_AT = 6
DEEP_SLEEP_FACTOR = 0.45


def wake_probability(calls_in_window: int, *, sleepiness: float) -> float:
    """10 分钟窗口内第 n 次被叫：第 1 次不醒，第 2 次起逐次变高，第 6 次必醒；越困越难醒。"""
    n = int(calls_in_window)
    if n <= 1:
        return 0.0
    if n >= WAKE_CERTAIN_AT:
        return 1.0
    base = (n - 1) / float(WAKE_CERTAIN_AT - 1)
    deep = DEEP_SLEEP_FACTOR if float(sleepiness) >= 0.7 else 1.0
    return max(0.0, min(0.99, base * deep))


__all__ = [
    "BOREDOM_GRACE_MINUTES",
    "BodyEvent",
    "BodyReading",
    "EVENT_TABLE",
    "HORMONES",
    "POLICY_VERSION",
    "RELATIONSHIP_EVENT_MAP",
    "advance",
    "apply_event",
    "baseline",
    "default_state",
    "edge_notes",
    "habituation_factor",
    "in_quiet_window",
    "intimacy_scale",
    "quiet_end_label",
    "read",
    "wake_probability",
    "wave",
]
