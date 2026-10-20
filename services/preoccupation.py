"""心事 v1：把未了的关切、对方最近的反应和她此刻的疲累，合成一小段内心活动。

这不是任务清单，也不是配额：这些事没有谁要求她跟进，她可以一直放在心里、可以犹豫、可以不说。
只在她考虑要不要对某个人开口时注入，最多两条，并且不重复「你惦记的事」里已经提到的那条。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

MAX_PREOCCUPATIONS = 2
_TOPIC_LIMIT = 80


@dataclass(frozen=True)
class Preoccupation:
    concern_id: int
    topic: str
    days: int
    due: str = ""
    status: str = "active"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else default


def _clean_topic(topic: Any, sender_name: str = "") -> str:
    text = re.sub(r"\s+", " ", str(topic or "")).strip()
    if sender_name and text.startswith(f"{sender_name}："):
        text = text[len(sender_name) + 1:].strip()
    elif sender_name and text.startswith(f"{sender_name}:"):
        text = text[len(sender_name) + 1:].strip()
    return text if len(text) <= _TOPIC_LIMIT else text[:_TOPIC_LIMIT] + "…"


def _days_since(created_at: Any, now: float) -> int:
    stamp = _number(created_at)
    if stamp <= 0:
        return 0
    return max(0, int((now - stamp) // 86400))


def select_preoccupations(
    concerns: Iterable[Mapping[str, Any]],
    *,
    exclude_ids: Iterable[int] = (),
    sender_name: str = "",
    limit: int = MAX_PREOCCUPATIONS,
    now: float | None = None,
) -> list[Preoccupation]:
    """按强度挑出还没被本次提示提起的关切；同一件事只说一次，最多 ``limit`` 条。"""
    stamp = float(now if now is not None else time.time())
    excluded = {int(item) for item in exclude_ids if item is not None}
    ordered = sorted(
        (item for item in concerns if isinstance(item, Mapping)),
        key=lambda item: (
            -_number(item.get("intensity")),
            -_number(item.get("last_progress_at") or item.get("created_at")),
        ),
    )
    picked: list[Preoccupation] = []
    seen: set[str] = set()
    for item in ordered:
        try:
            concern_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if concern_id <= 0 or concern_id in excluded:
            continue
        topic = _clean_topic(item.get("topic"), sender_name)
        if not topic or topic in seen:
            continue
        seen.add(topic)
        picked.append(Preoccupation(
            concern_id=concern_id,
            topic=topic,
            days=_days_since(item.get("created_at"), stamp),
            due=str(item.get("due") or ""),
            status=str(item.get("status") or "active"),
        ))
        if len(picked) >= max(1, int(limit)):
            break
    return picked


def hesitation_line(
    *,
    counts: Mapping[str, int] | None = None,
    fatigue: float = 0.0,
) -> str:
    """从对方最近的回应和她自己的疲累里，得出一句「她为什么犹豫」。"""
    counts = counts or {}
    accepted = int(counts.get("accepted") or 0)
    corrected = int(counts.get("corrected") or 0)
    rejected = int(counts.get("rejected") or 0)
    if rejected and accepted:
        return "ta 有时接你的话，有时又不太接；你还在摸 ta 的脾气，拿不准这次该进还是该退。"
    if rejected:
        return "ta 最近没太接你的话，你有点犹豫：这次再提，会不会显得你太在意。"
    if corrected:
        return "ta 纠正过你，你还记得，开口前会多想一想怎么说。"
    if _number(fatigue) >= 0.6:
        return "你有点累，不想为了找话说而找话说。"
    if accepted:
        return "ta 最近接过你的话，你心里踏实一点。"
    return ""


def build_heart_prompt(
    preoccupations: Sequence[Preoccupation],
    *,
    sender_name: str = "",
    hesitation: str = "",
) -> str:
    """内心独白式提示：说清楚这些事只是她自己的，不附带任何必须完成的动作。"""
    items = list(preoccupations)[:MAX_PREOCCUPATIONS]
    hesitation = str(hesitation or "").strip()
    if not items and not hesitation:
        return ""
    who = sender_name or "这个人"
    lines = ["【你心里的事】"]
    if items:
        lines.append(f"除了眼前这句话，你心里还搁着{who}的几件事：")
        for item in items:
            age = f"已经 {item.days} 天" if item.days > 0 else "刚记下"
            due = f"，{item.due} 的事" if item.due else ""
            lines.append(f"- {item.topic}（{age}{due}）")
        lines.append("这些不是待办，也没有人要求你跟进；你只是一直记着。")
    else:
        lines.append(f"你心里对{who}还有一点没说出口的挂念。")
    if hesitation:
        lines.append(hesitation)
    lines.append("说不说都由你；不提也不代表你没放在心上，不用为了主动而主动。")
    return "\n".join(lines)


__all__ = [
    "MAX_PREOCCUPATIONS",
    "Preoccupation",
    "build_heart_prompt",
    "hesitation_line",
    "select_preoccupations",
]
