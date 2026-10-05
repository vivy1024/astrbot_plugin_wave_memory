"""Bot 心情 v2：纯策略层（画像、情绪词表、事件冲击、衰减合成、情绪词映射）。

心情属于 Bot 本人，一个 Bot 只有一份，延续到所有群与私聊。这里只放纯函数与数据结构，
不读库、不依赖服务层，便于单测与在不同宿主里复用：

- ``MoodSettings``：Bot Profile 里的 ``mood`` 段（基线、敏感度、半衰期、群氛围权重、疲劳、说话倾向）；
- ``score_directed_message``：冲 Bot 来的一句话（@/回复/私聊）只按内容本身（情绪词、否定、笑骂）给出冲击，
  不读关系分数；群聊里「损话是玩笑还是冒犯」由事后记账那次调用回看整段对话给出的心情影响体现；
- ``RELATIONSHIP_EVENT_IMPULSES``：正式关系事件（被夸/被攻击/深谈……）对心情的冲击；
- ``compose_mood``：基线 + 各事件按各自半衰期指数回落后的叠加，软饱和到合法区间；
- ``mood_word``：把 valence/arousal/疲劳映射成通用情绪词（开心/烦躁/委屈/疲惫……），不输出裸数字。

任何角色设定都不写在这里；角色差异只通过 ``MoodSettings`` 配置。
"""

from __future__ import annotations

import math
import re
import heapq
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

POLICY_VERSION = "bot-mood/v2"
MAX_FATIGUE_REPLY_THRESHOLD = 10000
MAX_FATIGUE_WINDOW_MINUTES = 24 * 60
# 在阈值附近连续淡出，而不是把许多微小冲击同时硬截断。
MOOD_RESIDUAL_EPSILON = 1e-6

# ─── 画像 ───────────────────────────────────────────────────────


def _float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(lo, min(hi, parsed))


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = float(value)
        if not math.isfinite(number):
            return default
        parsed = int(number)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, parsed))


def _bool(value: Any, default: bool) -> bool:
    # AstrBot/9876 保存旧 Profile 时可能缺字段或写成字符串：None 一律按默认值处理。
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "否", "关闭"}:
            return False
    return bool(value)


def _words(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set, frozenset)) else re.split(r"[,，\n]", str(value))
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out[:200]


def _hints(value: Any) -> dict[str, str]:
    """说话倾向：{情绪词或类别: 提示}；也接受「开心=话多一点」这样的逐行文本。"""
    pairs: list[tuple[Any, Any]] = []
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, str):
        for line in value.splitlines():
            key, text = _split_hint(line)
            if key:
                pairs.append((key, text))
    out: dict[str, str] = {}
    for key, text in pairs:
        name = str(key or "").strip()
        hint = str(text or "").strip()
        if name and hint:
            out[name[:20]] = hint[:200]
    return out


def _split_hint(line: str) -> tuple[str, str]:
    match = re.match(r"\s*([^=：:]+?)\s*[=：:]\s*(.+)", str(line or ""))
    if not match:
        return "", ""
    return match.group(1), match.group(2)


@dataclass
class MoodSettings:
    """Bot Profile 的 ``mood`` 段；未配置时全部取中性默认值。"""

    # 这个 Bot 是否有心情（关掉后不记情绪事件、不注入心情）。
    enabled: bool = True
    # 没发生任何事时回落到的基线：valence -1~1（愉悦），arousal 0~1（激活）。
    baseline_valence: float = 0.1
    baseline_arousal: float = 0.4
    # 敏感度：所有事件冲击的倍数（0 = 不为外事所动，1 = 默认，最大 3）。
    sensitivity: float = 1.0
    # 恢复半衰期（分钟）：事件影响每过这么久减半；0 = 用全局 Lifecycle_Settings.mood_duration_hours。
    half_life_minutes: float = 0.0
    # 强事件影响更久：半衰期 × (1 + strong_event_boost × 事件强度)。
    strong_event_boost: float = 1.0
    # 群氛围传染权重（0~1）：群里整体气氛对她心情的影响，只作小权重输入。
    atmosphere_weight: float = 0.15
    # 疲劳：一段时间内回复太多、或处于深夜时段时，激活度下降、心情略降。
    fatigue_enabled: bool = True
    fatigue_window_minutes: int = 120
    fatigue_reply_threshold: int = 40
    # 深夜时段 [start, end)（本机时区的小时）；两者相等表示不按深夜算疲劳。
    night_start_hour: int = 1
    night_end_hour: int = 6
    # 疲劳强度（0~1）：疲劳拉低激活度与心情的程度。
    fatigue_strength: float = 0.5
    # 各心情下的说话倾向：键可以是情绪词（开心/烦躁/…）或类别（正面/负面/平静/疲惫），空则不写。
    speaking_hints: dict[str, str] = field(default_factory=dict)
    # 额外的正面 / 负面词（叠加在通用词表之上），用于识别冲她来的话。
    positive_words: list[str] = field(default_factory=list)
    negative_words: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "MoodSettings":
        data = data if isinstance(data, Mapping) else {}
        defaults = cls()
        return cls(
            enabled=_bool(data.get("enabled"), defaults.enabled),
            baseline_valence=_float(data.get("baseline_valence"), defaults.baseline_valence, -1.0, 1.0),
            baseline_arousal=_float(data.get("baseline_arousal"), defaults.baseline_arousal, 0.0, 1.0),
            sensitivity=_float(data.get("sensitivity"), defaults.sensitivity, 0.0, 3.0),
            half_life_minutes=_float(data.get("half_life_minutes"), defaults.half_life_minutes, 0.0, 7 * 24 * 60.0),
            strong_event_boost=_float(data.get("strong_event_boost"), defaults.strong_event_boost, 0.0, 5.0),
            atmosphere_weight=_float(data.get("atmosphere_weight"), defaults.atmosphere_weight, 0.0, 1.0),
            fatigue_enabled=_bool(data.get("fatigue_enabled"), defaults.fatigue_enabled),
            fatigue_window_minutes=_int(data.get("fatigue_window_minutes"), defaults.fatigue_window_minutes, 10, MAX_FATIGUE_WINDOW_MINUTES),
            fatigue_reply_threshold=_int(data.get("fatigue_reply_threshold"), defaults.fatigue_reply_threshold, 1, MAX_FATIGUE_REPLY_THRESHOLD),
            night_start_hour=_int(data.get("night_start_hour"), defaults.night_start_hour, 0, 23),
            night_end_hour=_int(data.get("night_end_hour"), defaults.night_end_hour, 0, 23),
            fatigue_strength=_float(data.get("fatigue_strength"), defaults.fatigue_strength, 0.0, 1.0),
            speaking_hints=_hints(data.get("speaking_hints")),
            positive_words=_words(data.get("positive_words")),
            negative_words=_words(data.get("negative_words")),
        )

    def half_life_seconds(self, default_minutes: float = 120.0) -> float:
        minutes = self.half_life_minutes if self.half_life_minutes > 0 else float(default_minutes or 120.0)
        return max(60.0, minutes * 60.0)


# ─── 通用情绪词表（冲 Bot 来的话） ───────────────────────────────

PRAISE_WORDS = (
    "厉害", "好棒", "真棒", "太棒", "可爱", "聪明", "谢谢", "感谢", "辛苦了", "爱你", "喜欢你",
    "牛逼", "太牛", "真牛", "nb", "太强", "好强", "666", "贴贴", "抱抱", "摸摸头", "乖", "靠谱",
    "真好", "好耶", "天才", "夸夸", "佩服", "优秀", "漂亮", "好看", "温柔", "暖心", "机智",
)
INSULT_WORDS = (
    "傻逼", "sb", "弱智", "智障", "笨蛋", "蠢", "废物", "垃圾", "滚", "闭嘴", "人机", "有病",
    "恶心", "讨厌你", "烦死", "无语", "脑残", "去死", "差劲", "没用", "神经病", "别说话", "难听",
    "丑", "煞笔", "沙比", "傻子", "白痴", "低能",
)
TEASE_WORDS = ("哈哈", "笑死", "xswl", "doge", "狗头", "🤣", "😂", "整活", "逗你", "斗嘴")
# 强攻击（与 MetaThinking 的极端攻击识别同口径）。
EXTREME_ATTACK = re.compile(r"(你[妈马]|nmsl|死[全妈]|全家|操你|fuck\s*you|滚去死|杀了你|弄死你)", re.IGNORECASE)
_NEGATIONS = ("不", "没", "别", "非")


def _count_hits(text: str, words: Iterable[str]) -> tuple[int, list[str]]:
    """数命中的词；紧跟否定词（不/没/别）的命中不算在这里（见 ``_negated_count``）。"""
    lowered = text.lower()
    hits = 0
    matched: list[str] = []
    for word in words:
        needle = str(word or "").lower()
        if not needle:
            continue
        start = lowered.find(needle)
        while start >= 0:
            if not (start > 0 and lowered[start - 1] in _NEGATIONS):
                hits += 1
                if word not in matched:
                    matched.append(word)
            start = lowered.find(needle, start + len(needle))
    return hits, matched


@dataclass
class MessageImpact:
    kind: str                # praise / insult / banter / tease / extreme / none
    valence: float
    arousal: float
    intensity: float
    matched: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.kind == "none" or (abs(self.valence) < 0.01 and abs(self.arousal) < 0.01)


def _negated_count(text: str, words: Iterable[str]) -> int:
    lowered = text.lower()
    total = 0
    for word in words:
        needle = str(word or "").lower()
        if not needle:
            continue
        start = lowered.find(needle)
        while start >= 0:
            if start > 0 and lowered[start - 1] in _NEGATIONS:
                total += 1
            start = lowered.find(needle, start + len(needle))
    return total


def score_directed_message(
    text: str,
    settings: MoodSettings | None = None,
) -> MessageImpact:
    """冲 Bot 来的一句话对她心情的冲击，只看这句话本身；返回的冲击已乘画像敏感度。

    不按好感分数给损话打折或加重：分数只是给群友玩的数字，不能替 Bot 判断「这是玩笑还是冒犯」。
    群聊里的关系语境由事后记账回看整段对话时给出的心情影响（``parse_bookkeeping_mood``）体现。
    """
    settings = settings or MoodSettings()
    body = str(text or "")[:500]
    if not body.strip():
        return MessageImpact("none", 0.0, 0.0, 0.0)
    praise_words = (*PRAISE_WORDS, *settings.positive_words)
    insult_words = (*INSULT_WORDS, *settings.negative_words)
    praise, praise_matched = _count_hits(body, praise_words)
    insult, insult_matched = _count_hits(body, insult_words)
    tease, tease_matched = _count_hits(body, TEASE_WORDS)
    # 「不可爱」「不厉害」：被否定的夸奖算轻微负面；「不讨厌你」算轻微正面。
    negated_praise = _negated_count(body, praise_words)
    negated_insult = _negated_count(body, insult_words)
    insult += negated_praise
    praise += negated_insult
    extreme = bool(EXTREME_ATTACK.search(body))
    if not (praise or insult or tease or extreme):
        return MessageImpact("none", 0.0, 0.0, 0.0)

    valence = 0.22 * min(praise, 3) - 0.30 * min(insult, 3) + 0.06 * min(tease, 2)
    arousal = 0.06 * min(praise, 3) + 0.15 * min(insult, 3) + 0.08 * min(tease, 2)
    kind = "praise" if valence > 0 else "insult" if valence < 0 else "tease"
    if insult and tease and not extreme:
        # 骂里带笑：多半是斗嘴。
        valence *= 0.6
        kind = "banter"
    if extreme:
        valence -= 0.5
        arousal += 0.25
        kind = "extreme"
    sensitivity = settings.sensitivity
    valence = max(-1.0, min(1.0, valence * sensitivity))
    arousal = max(-1.0, min(1.0, arousal * sensitivity))
    intensity = max(0.0, min(1.0, abs(valence) / 0.5 + (0.5 if extreme else 0.0)))
    matched = [*praise_matched, *insult_matched, *tease_matched][:6]
    return MessageImpact(kind, round(valence, 4), round(arousal, 4), round(intensity, 4), matched)


# ─── 关系事件 → 心情 ────────────────────────────────────────────

# 事件类型: (valence, arousal, 叙述动词)。direct_reply / manual_adjustment 不影响心情。
RELATIONSHIP_EVENT_IMPULSES: Mapping[str, tuple[float, float, str]] = {
    "bot_praised": (0.28, 0.10, "夸了你"),
    "bot_attacked": (-0.38, 0.25, "冲你发难"),
    "joke": (0.07, 0.05, "跟你开玩笑"),
    "deep_talk": (0.15, -0.03, "跟你聊了心里话"),
    "gift_or_feed": (0.30, 0.15, "给你投喂"),
    "confession": (0.22, 0.25, "向你表白"),
    "correction": (-0.08, 0.05, "纠正了你"),
    "ignored_boundary": (-0.25, 0.15, "越了你的边界"),
}


def relationship_event_impulse(event_type: str, delta: Any, settings: MoodSettings | None = None) -> tuple[float, float, float, str] | None:
    """正式关系事件的心情冲击：(valence, arousal, intensity, 动词)；不影响心情的类型返回 None。"""
    spec = RELATIONSHIP_EVENT_IMPULSES.get(str(event_type or "").strip())
    if spec is None:
        return None
    settings = settings or MoodSettings()
    try:
        magnitude = abs(float(delta or 0.0))
    except (TypeError, ValueError):
        magnitude = 0.0
    scale = max(0.6, min(1.4, 0.6 + 0.15 * magnitude))
    valence = spec[0] * scale * settings.sensitivity
    arousal = spec[1] * scale * settings.sensitivity
    intensity = max(0.0, min(1.0, abs(valence) / 0.45))
    return round(valence, 4), round(arousal, 4), round(intensity, 4), spec[2]


# ─── 事后记账 LLM 给出的心情影响 ───────────────────────────────

_UP = {"up", "positive", "better", "+", "好", "变好", "开心", "上升", "正面", "提升"}
_DOWN = {"down", "negative", "worse", "-", "坏", "变差", "难过", "下降", "负面", "低落"}


def parse_bookkeeping_mood(raw: Any, settings: MoodSettings | None = None) -> tuple[float, float, float, str] | None:
    """解析 ``{"direction": "up|down|none", "strength": 0~1, "reason": "一句话"}``；不合法返回 None。"""
    if not isinstance(raw, Mapping):
        return None
    direction = str(raw.get("direction") or "").strip().lower()
    if direction in _UP:
        sign = 1.0
    elif direction in _DOWN:
        sign = -1.0
    else:
        return None
    try:
        strength = float(raw.get("strength"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(strength):
        return None
    strength = max(0.0, min(1.0, strength))
    if strength < 0.05:
        return None
    reason = re.sub(r"\s+", " ", str(raw.get("reason") or "")).strip()[:80]
    if not reason:
        return None
    settings = settings or MoodSettings()
    # 记账回看整段对话后判断的「冲着她来的事」是心情最可靠的来源，权重明显高于群氛围（0.15 × 0.5）
    valence = sign * (0.50 if sign > 0 else 0.55) * strength * settings.sensitivity
    arousal = (0.12 if sign > 0 else 0.18) * strength * settings.sensitivity
    return round(valence, 4), round(arousal, 4), round(strength, 4), reason


# ─── 合成与衰减 ────────────────────────────────────────────────


@dataclass
class MoodEventView:
    """合成用的一条情绪事件（来自存储层）。"""

    id: int
    source: str
    valence: float
    arousal: float
    intensity: float
    created_at: float
    cause: str = ""
    subject_name: str = ""
    session_id: str = ""
    visibility: str = "group"
    subject_principal_id: str = ""


@dataclass
class MoodContribution:
    event: MoodEventView
    valence: float
    arousal: float

    @property
    def weight(self) -> float:
        return abs(self.valence) + 0.5 * abs(self.arousal)


@dataclass
class MoodReading:
    valence: float
    arousal: float
    fatigue: float
    word: str
    category: str
    contributions: list[MoodContribution]
    baseline_valence: float
    baseline_arousal: float
    active_events: int = 0


def event_half_life(settings: MoodSettings, intensity: float, default_minutes: float = 120.0) -> float:
    base = settings.half_life_seconds(default_minutes)
    return base * (1.0 + settings.strong_event_boost * max(0.0, min(1.0, float(intensity or 0.0))))


def mood_event_horizon(settings: MoodSettings, default_minutes: float = 120.0) -> float:
    """仓储冲击绝对值不超过 1；此时间外连最强、最慢事件也已淡出。"""
    return event_half_life(settings, 1.0, default_minutes) * math.log2(1.0 / MOOD_RESIDUAL_EPSILON)


def decay_factor(age_seconds: float, half_life_seconds: float) -> float:
    if age_seconds <= 0:
        return 1.0
    return 0.5 ** (age_seconds / max(1.0, half_life_seconds))


def _saturate(baseline: float, offset: float, lo: float, hi: float) -> float:
    """基线 + 软饱和偏移：偏移越大越接近边界，但永远不越界。"""
    squashed = math.tanh(offset)
    if squashed >= 0:
        return baseline + (hi - baseline) * squashed
    return baseline + (baseline - lo) * squashed


def night_active(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def fatigue_level(settings: MoodSettings, *, recent_replies: int, hour: int) -> float:
    """0~1：回复太多（超过阈值开始累，两倍阈值最累）或处于深夜时段。"""
    if not settings.fatigue_enabled or settings.fatigue_strength <= 0:
        return 0.0
    threshold = max(1, int(settings.fatigue_reply_threshold))
    by_replies = max(0.0, min(1.0, (int(recent_replies) - threshold) / threshold))
    by_night = 1.0 if night_active(int(hour), settings.night_start_hour, settings.night_end_hour) else 0.0
    return round(max(by_replies, by_night) * settings.fatigue_strength, 4)


def compose_mood(
    events: Iterable[MoodEventView],
    settings: MoodSettings,
    *,
    now: float,
    fatigue: float = 0.0,
    default_half_life_minutes: float = 120.0,
    max_contributions: int | None = None,
) -> MoodReading:
    """基线 + Σ 事件冲击 × 0.5^(经过时间/该事件半衰期)，再软饱和并叠加疲劳。"""
    contributions: list[MoodContribution] = []
    top: list[tuple[float, int, MoodContribution]] = []
    active_events = 0
    offset_v = 0.0
    offset_a = 0.0
    for event in events:
        half_life = event_half_life(settings, event.intensity, default_half_life_minutes)
        factor = decay_factor(float(now) - float(event.created_at), half_life)
        v = float(event.valence) * factor
        a = float(event.arousal) * factor
        peak = max(abs(v), abs(a))
        if peak <= MOOD_RESIDUAL_EPSILON:
            continue
        taper = min(1.0, (peak - MOOD_RESIDUAL_EPSILON) / MOOD_RESIDUAL_EPSILON)
        v, a = v * taper, a * taper
        offset_v += v
        offset_a += a
        active_events += 1
        contribution = MoodContribution(event, v, a)
        if max_contributions is None:
            contributions.append(contribution)
        elif max_contributions > 0:
            entry = (contribution.weight, -active_events, contribution)
            if len(top) < max_contributions:
                heapq.heappush(top, entry)
            elif entry[:2] > top[0][:2]:
                heapq.heapreplace(top, entry)
    valence = _saturate(settings.baseline_valence, offset_v, -1.0, 1.0)
    arousal = _saturate(settings.baseline_arousal, offset_a, 0.0, 1.0)
    fatigue = max(0.0, min(1.0, float(fatigue or 0.0)))
    if fatigue > 0:
        arousal = max(0.0, arousal - 0.35 * fatigue)
        valence = max(-1.0, valence - 0.10 * fatigue)
    if max_contributions is not None:
        contributions = [item[2] for item in sorted(top, reverse=True)]
    else:
        contributions.sort(key=lambda item: item.weight, reverse=True)
    word, category = mood_word(valence, arousal, fatigue)
    return MoodReading(
        valence=round(valence, 4),
        arousal=round(arousal, 4),
        fatigue=round(fatigue, 4),
        word=word,
        category=category,
        contributions=contributions,
        baseline_valence=settings.baseline_valence,
        baseline_arousal=settings.baseline_arousal,
        active_events=active_events,
    )


def _core_word(valence: float, arousal: float) -> tuple[str, str]:
    if valence >= 0.45:
        return ("兴奋" if arousal >= 0.62 else "开心"), "正面"
    if valence >= 0.15:
        return ("愉快" if arousal >= 0.55 else "放松"), "正面"
    if valence > -0.15:
        if arousal < 0.22:
            return "无聊", "平静"
        if arousal >= 0.68:
            return "紧绷", "平静"
        return "平静", "平静"
    if valence > -0.4:
        return ("烦躁" if arousal >= 0.55 else "郁闷"), "负面"
    if arousal >= 0.65:
        return "生气", "负面"
    return ("委屈" if arousal >= 0.35 else "低落"), "负面"


def mood_word(valence: float, arousal: float, fatigue: float = 0.0) -> tuple[str, str]:
    """(情绪词, 类别)。类别：正面 / 负面 / 平静 / 疲惫。"""
    core, category = _core_word(float(valence), float(arousal))
    magnitude = abs(float(valence))
    if category != "平静":
        if magnitude < 0.25:
            core = "有点" + core
        elif magnitude > 0.7:
            core = "很" + core
    if fatigue >= 0.6:
        return ("疲惫" if category == "平静" else f"{core}，也很累"), "疲惫"
    if fatigue >= 0.3:
        return ("有点累" if category == "平静" else f"{core}，有点累"), category if category != "平静" else "疲惫"
    return core, category


# ─── 叙述 ───────────────────────────────────────────────────────


def describe_age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 300:
        return "刚才"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 2 * 86400:
        return "昨天"
    return f"{int(seconds // 86400)} 天前"


def effect_phrase(valence: float, arousal: float) -> str:
    if valence >= 0.05:
        return "让你开心" if valence >= 0.12 else "让你心情好了点"
    if valence <= -0.05:
        return "让你不痛快" if valence <= -0.12 else "让你有点不舒服"
    return "让你有点起伏" if arousal >= 0 else "让你平静下来"


def speaking_hint(settings: MoodSettings, word: str, category: str) -> str:
    hints = settings.speaking_hints or {}
    if not hints:
        return ""
    for key in (word, word.replace("有点", "").replace("很", "").split("，")[0], category):
        if key and key in hints:
            return hints[key]
    return ""


__all__ = [
    "EXTREME_ATTACK",
    "INSULT_WORDS",
    "MessageImpact",
    "MoodContribution",
    "MoodEventView",
    "MoodReading",
    "MoodSettings",
    "POLICY_VERSION",
    "PRAISE_WORDS",
    "RELATIONSHIP_EVENT_IMPULSES",
    "TEASE_WORDS",
    "compose_mood",
    "decay_factor",
    "describe_age",
    "effect_phrase",
    "event_half_life",
    "fatigue_level",
    "mood_word",
    "night_active",
    "parse_bookkeeping_mood",
    "relationship_event_impulse",
    "score_directed_message",
    "speaking_hint",
]
