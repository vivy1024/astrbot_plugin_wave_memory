"""把关系五维、印象时间线与近期互动渲染成给模型看的自然语言。

过去的关系注入是「印象时间线 8 条（6 位小数权重 + 群号 + 事件编号）+ 当前印象 + 每轮写标签的指令
+ 态度=neutral 综合值=18 familiarity=63.3…」：裸数字没有量纲，时间线机械冗长，同一个外号重复五次。
这里只做读侧渲染，不写库、不改分数：

- 关系一句话：档位 + 依据（认识多久、互动多少、各维度的文字强弱）；
- 印象压缩：当前看法 + 反复出现的称呼合并计数 + 3–4 件代表性的事 + 一句好感走势；
- 当下感受：最近一两天的短期感受（被夸/被怼/被帮/被冒犯…），随时间衰减，与长期关系分开；
- 共同经历：只取与此人相关的经历，取不到就不写。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from ..domain.relationship_policy import NOISY_EVENT_REASONS, classify_relationship_tier
    from .impression_timeline import (
        _item_timestamp,
        normalize_timeline_half_life,
        timeline_decay_weight,
    )
except ImportError:  # pragma: no cover
    from domain.relationship_policy import NOISY_EVENT_REASONS, classify_relationship_tier
    from services.impression_timeline import (
        _item_timestamp,
        normalize_timeline_half_life,
        timeline_decay_weight,
    )

# 印象压缩：代表性事件条数、单条字数、称呼合并展示个数
NARRATIVE_MAX_EVENTS = 4
NARRATIVE_EVENT_CHARS = 36
NARRATIVE_IMPRESSION_CHARS = 60
NARRATIVE_MAX_EPITHETS = 3
# 当下感受：只看最近这么多小时，按半衰期衰减
CURRENT_FEELING_WINDOW_HOURS = 48.0
CURRENT_FEELING_HALF_LIFE_HOURS = 12.0

_KIND_WEIGHT = {"affinity": 2.0, "milestone": 2.0, "social_anchor": 1.8, "impression": 1.0}
_IMPRESSION_KINDS = frozenset(_KIND_WEIGHT)
_AFFINITY_TAIL = re.compile(r"[，,]\s*好感从\s*(-?\d+)\s*(?:跃迁)?(?:升至|降至|保持在)\s*(-?\d+)\s*$")
_IMPACT_TAIL = re.compile(r"\s*[|｜]\s*impact\s*[:：]?\s*[\d.]+\s*$", re.I)
_BRACKET_PREFIX = re.compile(r"^【[^】]{1,8}】")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _bigrams(text: str) -> set[str]:
    chars = re.sub(r"\s+", "", str(text or ""))
    return {chars[i:i + 2] for i in range(max(0, len(chars) - 1))}


def _similar(left: str, right: str, threshold: float = 0.5) -> bool:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return left == right
    return len(a & b) / max(1, min(len(a), len(b))) >= threshold


# ─── 维度的文字强弱 ───


def familiarity_word(value: float) -> str:
    if value >= 70:
        return "非常熟"
    if value >= 45:
        return "很熟"
    if value >= 25:
        return "比较熟"
    if value >= 10:
        return "有点面熟"
    return "还不太熟"


def trust_word(value: float) -> str:
    if value >= 50:
        return "非常信任"
    if value >= 25:
        return "很信任"
    if value >= 8:
        return "比较信任"
    if value > -3:
        return "信任一般"
    if value > -15:
        return "有点提防"
    return "不太信得过"


def fun_word(value: float) -> str:
    if value >= 25:
        return "很合得来"
    if value >= 10:
        return "挺合得来"
    if value >= 3:
        return "偶尔能玩到一块"
    return ""


def depth_word(value: float) -> str:
    if value >= 25:
        return "交过心"
    if value >= 10:
        return "有过几次深聊"
    if value >= 3:
        return "聊过些正经话题"
    return ""


def hostility_word(value: float) -> str:
    if value >= 40:
        return "敌意很重"
    if value >= 20:
        return "积了不少怨气"
    if value >= 8:
        return "有些小过节"
    if value >= 3:
        return "有过一点摩擦"
    return ""


def dimension_phrases(dimensions: Mapping[str, Any] | None) -> list[str]:
    dims = dimensions if isinstance(dimensions, Mapping) else {}

    def value(name: str) -> float:
        return _finite(dims.get(name)) or 0.0

    phrases = [familiarity_word(value("familiarity")), trust_word(value("trust"))]
    phrases.extend(word for word in (fun_word(value("fun")), depth_word(value("depth")), hostility_word(value("hostility"))) if word)
    return phrases


def _known_since_phrase(first_seen: float | None, now: float) -> str:
    stamp = _finite(first_seen)
    if stamp is None or stamp <= 1e9 or stamp > now:
        return ""
    days = (now - stamp) / 86400.0
    if days < 1:
        return "今天才认识"
    if days < 7:
        return "认识没几天"
    if days < 30:
        return f"认识约 {max(1, round(days / 7))} 周"
    if days < 365:
        return f"认识约 {max(1, round(days / 30))} 个月"
    return f"认识 {int(days // 365)} 年多"


def _interaction_phrase(count: Any) -> str:
    number = _finite(count)
    if number is None or number <= 0:
        return ""
    n = int(number)
    if n < 10:
        return f"打过 {n} 次交道"
    if n < 100:
        return f"打过 {n // 10 * 10} 多次交道"
    if n < 1000:
        return f"打过 {n // 100 * 100} 多次交道"
    return f"打过 {n // 1000} 千多次交道"


def relationship_sentence(
    dimensions: Mapping[str, Any] | None,
    *,
    tier: Mapping[str, Any] | None = None,
    first_seen: float | None = None,
    interaction_count: Any = None,
    now: float | None = None,
) -> str:
    """一句话讲清档位与依据；不含裸分数。"""
    stamp = _finite(now) or time.time()
    stance = dict(tier) if isinstance(tier, Mapping) else classify_relationship_tier(dimensions)
    basis = [part for part in (_known_since_phrase(first_seen, stamp), _interaction_phrase(interaction_count)) if part]
    phrases = dimension_phrases(dimensions)
    body = "，".join(basis + phrases[:1]) if basis else phrases[0]
    rest = "、".join(phrases[1:])
    sentence = f"【你和对方】算「{stance.get('tier_label') or '陌生人'}」：{body}"
    if rest:
        sentence += f"；{rest}"
    return sentence + "。（仅用于调整语气与分寸，除非相关不要主动提及或复述）"


# ─── 印象压缩 ───


def _clean_summary(summary: str) -> tuple[str, tuple[int, int] | None]:
    text = _IMPACT_TAIL.sub("", " ".join(str(summary or "").split()))
    text = _BRACKET_PREFIX.sub("", text)
    shift = None
    match = _AFFINITY_TAIL.search(text)
    if match:
        shift = (int(match.group(1)), int(match.group(2)))
        text = text[: match.start()].rstrip("，, ")
    return text, shift


_MILESTONE = re.compile(r"^(?P<label>[^（(：:]{2,12})[（(](?P<body>.*)[）)]$")


def _milestone_parts(text: str) -> tuple[str, str, str]:
    """「日常直接互动（理由：……的某某）」→（标签, 理由, 当时的看法）；不是这种结构时标签为空。"""
    match = _MILESTONE.match(text)
    if not match:
        return "", text, ""
    body = match.group("body").strip()
    reason, _sep, view = body.partition("：")
    if not _sep:
        reason, _sep, view = body.partition(":")
    return match.group("label").strip(), reason.strip(), view.strip()


def _is_noise(text: str) -> bool:
    return any(text.endswith(reason) for reason in NOISY_EVENT_REASONS)


def _epithet(text: str) -> str:
    """「……的老搭子阿木」→「老搭子阿木」；没有这种结构时返回空。"""
    if "的" not in text:
        return ""
    tail = text.rsplit("的", 1)[1].strip(" ，,。.、")
    if not (2 <= len(tail) <= 12) or re.search(r"[，,。；;！!？?（）()：:「」“”\"]", tail):
        return ""
    return tail


def _without_epithet(text: str, epithet: str) -> str:
    if epithet and text.endswith(epithet):
        trimmed = text[: -len(epithet)].rstrip()
        if trimmed.endswith("的"):
            trimmed = trimmed[:-1]
        return trimmed.strip(" ，,") or text
    return text


def merge_epithets(epithets: Sequence[str]) -> list[tuple[str, int]]:
    """合并相似称呼：带修饰的长称呼并入它结尾的短称呼（「老油条诸葛匹夫」计入「诸葛匹夫」）。"""
    counts: dict[str, int] = {}
    for item in epithets:
        if item:
            counts[item] = counts.get(item, 0) + 1
    merged: dict[str, int] = {}
    for name in sorted(counts, key=len):
        # 太短的尾巴（如两个字母的昵称）会把所有称呼吞掉，至少 3 个字才作为合并基准
        base = next((kept for kept in merged if name.endswith(kept) and len(kept) >= 3), None)
        if base is None:
            merged[name] = counts[name]
        else:
            merged[base] += counts[name]
    return sorted(merged.items(), key=lambda pair: (-pair[1], -len(pair[0])))


def _affinity_shift(item: Mapping[str, Any], cleaned_shift: tuple[int, int] | None) -> tuple[int, int] | None:
    provenance = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
    before, after = _finite(provenance.get("before_affinity")), _finite(provenance.get("after_affinity"))
    if before is not None and after is not None:
        return int(before), int(after)
    return cleaned_shift


def trend_phrase(shifts: Sequence[tuple[int, int]]) -> str:
    if not shifts:
        return ""
    start, end = shifts[0][0], shifts[-1][1]
    diff = end - start
    if diff >= 3:
        word = "明显变好"
    elif diff > 0:
        word = "慢慢变好"
    elif diff == 0:
        word = "有起有落，基本持平" if len(shifts) > 1 else "基本持平"
    elif diff > -3:
        word = "略有疏远"
    else:
        word = "明显变差"
    return f"好感 {start}→{end}，{word}（{len(shifts)} 次变动）"


def _short_when(item: Mapping[str, Any], now: float) -> str:
    ts = _item_timestamp(item)
    if ts <= 0 or ts > now:
        return ""
    days = (now - ts) / 86400.0
    if days < 1:
        return "今天"
    if days < 2:
        return "昨天"
    if days < 7:
        return f"{int(days)}天前"
    if days < 45:
        return f"{max(1, round(days / 7))}周前"
    return f"{max(1, round(days / 30))}个月前"


def compress_impressions(
    events: Sequence[Any] | None,
    *,
    now: float | None = None,
    half_life_days: Any = None,
    max_events: int = NARRATIVE_MAX_EVENTS,
) -> dict[str, Any]:
    """从印象时间线挑出：当前看法、合并后的称呼计数、代表性事件、好感走势。"""
    stamp = _finite(now) or time.time()
    half_life = normalize_timeline_half_life(half_life_days)
    rows = [item for item in (events or ()) if isinstance(item, Mapping) and str(item.get("summary") or "").strip()]
    rows.sort(key=lambda row: (_item_timestamp(row), str(row.get("id") or "")))
    current: dict[str, Any] | None = None
    epithets: list[str] = []
    shifts: list[tuple[int, int]] = []
    candidates: list[tuple[float, float, str, Mapping[str, Any]]] = []
    fallback_current: dict[str, Any] | None = None
    for item in rows:
        kind = str(item.get("kind") or "")
        if kind not in _IMPRESSION_KINDS:
            # 人物事实（别名、说过的话）归事实通道，不算「印象」
            continue
        text, shift = _clean_summary(str(item.get("summary") or ""))
        if not text or _is_noise(text):
            continue
        if kind == "affinity":
            moved = _affinity_shift(item, shift)
            if moved is not None:
                shifts.append(moved)
        label, reason, view = _milestone_parts(text)
        epithet = _epithet(view or text)
        if epithet:
            epithets.append(epithet)
        if kind == "impression":
            current = {"text": text, "item": item}
        elif view:
            fallback_current = {"text": view, "item": item}
        weight = (timeline_decay_weight(item, now=stamp, half_life_days=half_life) or 0.0) * _KIND_WEIGHT.get(kind, 1.0)
        if label:
            short = f"{label}：{reason}" if reason else label
        else:
            short = _without_epithet(text, epithet)
        candidates.append((weight, _item_timestamp(item), short, item))
    if current is None:
        current = fallback_current
    current_text = str((current or {}).get("text") or "")
    picked: list[tuple[float, str, Mapping[str, Any]]] = []
    for weight, ts, short, item in sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True):
        if len(picked) >= max(0, int(max_events)):
            break
        if current is not None and item is current["item"]:
            continue
        if current_text and _similar(short, current_text):
            continue
        if any(_similar(short, other) for _, other, _ in picked):
            continue
        picked.append((ts, short, item))
    picked.sort(key=lambda row: row[0])
    return {
        "current": current_text,
        "current_when": _short_when(current["item"], stamp) if current else "",
        "epithets": merge_epithets(epithets),
        "events": [
            {"when": _short_when(item, stamp), "text": _clip(short, NARRATIVE_EVENT_CHARS), "kind": str(item.get("kind") or "")}
            for _, short, item in picked
        ],
        "trend": trend_phrase(shifts),
        "total": len(rows),
    }


def history_events(history: Sequence[Any] | None, *, now: float | None = None, limit: int = 3) -> list[dict[str, str]]:
    """没有印象时间线时，用关系事件的理由充当「印象里的几件事」。"""
    stamp = _finite(now) or time.time()
    out: list[dict[str, str]] = []
    for item in history or ():
        if not isinstance(item, Mapping) or len(out) >= limit:
            continue
        event_type = str(item.get("event_type") or "")
        reason = str(item.get("reason") or "").strip()
        if not reason or event_type == "message_seen" or reason == "看见一条群友消息":
            continue
        if any(_similar(reason, other["text"]) for other in out):
            continue
        out.append({"when": _short_when({"at": item.get("timestamp")}, stamp), "text": _clip(reason, NARRATIVE_EVENT_CHARS)})
    out.reverse()
    return out


def impression_lines(compressed: Mapping[str, Any], *, fallback_events: Sequence[Mapping[str, str]] = ()) -> list[str]:
    lines: list[str] = []
    current = str(compressed.get("current") or "")
    epithets = [(name, count) for name, count in compressed.get("epithets") or () if count >= 2][:NARRATIVE_MAX_EPITHETS]
    if current:
        when = str(compressed.get("current_when") or "")
        line = f"你对对方的看法：{_clip(current, NARRATIVE_IMPRESSION_CHARS)}" + (f"（{when}）" if when else "")
        if epithets:
            line += "；常这样叫对方：" + "、".join(f"{name}×{count}" for name, count in epithets)
        lines.append(line + "。")
    events = list(compressed.get("events") or ()) or list(fallback_events)
    if events:
        lines.append("印象里的几件事：" + "；".join(
            (f"{item['when']}，" if item.get("when") else "") + item["text"] for item in events
        ) + "。")
    trend = str(compressed.get("trend") or "")
    if trend:
        lines.append(f"走势：{trend}。")
    return lines


# ─── 当下感受 ───

_POSITIVE_EVENTS = {
    "bot_praised": "被对方夸了",
    "gift_or_feed": "收到对方的心意",
    "confession": "对方跟你说了心里话",
    "deep_talk": "和对方聊得很深",
    "joke": "和对方一起玩梗",
}
_NEGATIVE_EVENTS = {
    "bot_attacked": "被对方怼了",
    "ignored_boundary": "被对方冒犯了",
}


def _signal_from_history(item: Mapping[str, Any]) -> tuple[float, str]:
    """关系事件 → (情感方向, 事件标签)。

    线上模型常把「被冒犯」记成 hostility 的负增量，所以 hostility 维度一律按负向看（除非事件本身是正向类型）。
    """
    event_type = str(item.get("event_type") or "")
    dimension = str(item.get("dimension") or "")
    delta = _finite(item.get("delta")) or 0.0
    magnitude = max(0.5, min(3.0, abs(delta))) if delta else 0.5
    if event_type in _NEGATIVE_EVENTS:
        return -magnitude, _NEGATIVE_EVENTS[event_type]
    if event_type in _POSITIVE_EVENTS and not (dimension != "hostility" and delta < 0):
        return magnitude, _POSITIVE_EVENTS[event_type]
    if dimension == "hostility":
        return -magnitude, "和对方有点摩擦"
    if event_type == "correction":
        return (magnitude if delta >= 0 else -magnitude), "被对方纠正了"
    if delta > 0:
        return magnitude, "和对方聊得不错"
    if delta < 0:
        return -magnitude, "对对方有点失望"
    return 0.0, ""


# 当下的互动姿态（与长期档位分开）：受伤/戒备/复杂/放松/活泼/温暖
def feeling_posture(net: float, *, positive: float = 0.0, negative: float = 0.0, playful: bool = False) -> str:
    if positive >= 0.8 and negative >= 0.8:
        return "心情有点复杂"
    if net >= 2.0:
        return "挺温暖的"
    if net >= 0.6:
        return "比较活泼放得开" if playful else "比较放松"
    if net <= -2.0:
        return "有点受伤，想躲开一点"
    if net <= -0.6:
        return "有点戒备"
    return ""


def _age_label(age_hours: float) -> str:
    if age_hours < 1:
        return "刚才"
    if age_hours < 6:
        return "几小时前"
    if age_hours < 24:
        return "一天内"
    return "昨天"


def current_feeling_line(
    *,
    history: Sequence[Any] | None = None,
    traces: Sequence[Any] | None = None,
    timeline_events: Sequence[Any] | None = None,
    now: float | None = None,
    window_hours: float = CURRENT_FEELING_WINDOW_HOURS,
    half_life_hours: float = CURRENT_FEELING_HALF_LIFE_HOURS,
) -> str:
    """最近一两天对此人的短期感受，按时间衰减；没有近期互动返回空串。"""
    stamp = _finite(now) or time.time()
    window = max(1.0, float(window_hours)) * 3600.0
    half_life = max(0.5, float(half_life_hours)) * 3600.0
    net = positive = negative = 0.0
    labels: list[tuple[float, str]] = []
    notes: list[tuple[float, str]] = []
    newest_age: float | None = None

    def fresh(ts: Any) -> float | None:
        value = _finite(ts)
        if value is None or value <= 1e9 or value > stamp + 60 or stamp - value > window:
            return None
        return max(0.0, stamp - value)

    for item in history or ():
        if not isinstance(item, Mapping):
            continue
        age = fresh(item.get("timestamp") or item.get("created_at"))
        if age is None:
            continue
        valence, label = _signal_from_history(item)
        decay = 0.5 ** (age / half_life)
        net += valence * decay
        if valence > 0:
            positive += valence * decay
        elif valence < 0:
            negative += -valence * decay
        if label:
            labels.append((age, label))
        reason = str(item.get("reason") or "").strip()
        if reason:
            notes.append((age, reason))
        newest_age = age if newest_age is None else min(newest_age, age)
    recent_items = [item for item in traces or () if isinstance(item, Mapping)]
    recent_items += [
        item for item in timeline_events or ()
        if isinstance(item, Mapping) and str(item.get("kind") or "") in {"impression", "affinity"}
    ]
    for item in recent_items:
        age = fresh(item.get("ts") or item.get("occurred_at") or item.get("at"))
        if age is None:
            continue
        text, _shift = _clean_summary(str(item.get("text") or item.get("summary") or ""))
        label, reason, view = _milestone_parts(text)
        text = view or reason if label else text
        if text and not _is_noise(text):
            notes.append((age, text))
            newest_age = age if newest_age is None else min(newest_age, age)
    if newest_age is None:
        return ""
    seen_labels: list[str] = []
    for _age, label in sorted(labels):
        if label not in seen_labels:
            seen_labels.append(label)
    picked_notes: list[str] = []
    for _age, text in sorted(notes):
        epithet = _epithet(text)
        short = _clip(_without_epithet(text, epithet), 30)
        if short and not any(_similar(short, other) for other in picked_notes):
            picked_notes.append(short)
        if len(picked_notes) >= 2:
            break
    feeling = feeling_posture(net, positive=positive, negative=negative, playful="和对方一起玩梗" in seen_labels)
    if not seen_labels and not picked_notes:
        return ""
    when = _age_label(newest_age / 3600.0)
    parts = ["【当下感受】"]
    if seen_labels:
        parts.append(f"{when}" + "、".join(seen_labels[:3]) + "；")
    if picked_notes:
        parts.append("这一两天对对方的观感：" + "；".join(picked_notes) + "。")
    else:
        parts[-1] = parts[-1].rstrip("；") + "。"
    parts.append(f"现在对对方{feeling}，" if feeling else "")
    parts.append("这是一时的感受，会慢慢淡去，不等于长期关系。")
    return "".join(parts)


# ─── 共同经历 ───


_ANCHOR_WORDS = {
    "bot_helped_user": "你帮过对方",
    "user_helped_bot": "对方帮过你",
    "bot_promised_user": "你答应过对方",
    "boundary_hit": "对方越过界",
}


def shared_experience_lines(
    soul_timeline_items: Sequence[Any] | None,
    *,
    subject_principal_id: str,
    display_names: Sequence[str] = (),
    limit: int = 2,
    snippet_chars: int = 48,
) -> list[str]:
    """只认主体或结构化 participant_principal_ids；昵称不作为人物归属证据。

    参与者可直接在条目上，或在 evidence 的结构化记录中。保留 display_names 参数兼容旧调用。
    """
    if not subject_principal_id:
        return []
    out: list[str] = []
    for item in soul_timeline_items or ():
        if not isinstance(item, Mapping) or len(out) >= limit:
            continue
        summary = " ".join(str(item.get("event_summary") or "").split())
        for code, words in _ANCHOR_WORDS.items():
            summary = summary.replace(f"（{code}）", f"（{words}）")
        if not summary:
            continue
        subject = str(item.get("subject_principal_id") or "")
        evidence = item.get("evidence")
        records = [item]
        if isinstance(evidence, Mapping):
            records.append(evidence)
        elif isinstance(evidence, (list, tuple)):
            records.extend(record for record in evidence if isinstance(record, Mapping))
        participant = any(
            isinstance(record.get("participant_principal_ids"), (list, tuple))
            and subject_principal_id in record["participant_principal_ids"]
            for record in records
        )
        if subject != subject_principal_id and not participant:
            continue
        text = _clip(summary, snippet_chars)
        if not any(_similar(text, other) for other in out):
            out.append(text)
    return out


__all__ = [
    "CURRENT_FEELING_HALF_LIFE_HOURS",
    "CURRENT_FEELING_WINDOW_HOURS",
    "compress_impressions",
    "current_feeling_line",
    "dimension_phrases",
    "feeling_posture",
    "history_events",
    "impression_lines",
    "merge_epithets",
    "relationship_sentence",
    "shared_experience_lines",
    "trend_phrase",
]
