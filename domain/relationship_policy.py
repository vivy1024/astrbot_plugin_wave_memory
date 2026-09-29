"""Formal relationship dimensions, event validation, and automatic cap policy."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# 历史关系模型的四个正向维度与一个负向维度。hostility 不能被
# 当作 trust 的负值折叠：旧账本和现有工具都把它作为独立证据维度。
DIMENSION_WEIGHTS: Mapping[str, float] = {
    "familiarity": 0.25,
    "trust": 0.30,
    "fun": 0.20,
    "depth": 0.25,
}
HOSTILITY_WEIGHT = 0.50
DIMENSION_RANGES: Mapping[str, tuple[float, float]] = {
    "familiarity": (0.0, 100.0),
    "trust": (-50.0, 100.0),
    "fun": (0.0, 80.0),
    "hostility": (0.0, 100.0),
    "depth": (0.0, 80.0),
}
VALID_DIMENSIONS = frozenset(DIMENSION_RANGES)
VALID_EVENT_TYPES = frozenset({
    "direct_reply",
    "bot_praised",
    "bot_attacked",
    "correction",
    "gift_or_feed",
    "confession",
    "joke",
    "deep_talk",
    "ignored_boundary",
    "manual_adjustment",
})
NOISY_EVENT_TYPES = frozenset({"message_seen"})
NOISY_EVENT_REASONS = frozenset({
    "看见一条群友消息",
    "消息带来趣味感",
    "行为统计关系变化",
})
SINGLE_DELTA_CAP = 5.0
HOSTILITY_DELTA_CAP = 8.0
DAILY_DELTA_CAP = 15.0
MANUAL_ADJUSTMENT_DELTA_CAP = 20.0


def clamp_dimension(dimension: str, value: float) -> float:
    lo, hi = DIMENSION_RANGES[dimension]
    return max(lo, min(hi, float(value)))


def compute_affinity(dimensions: Mapping[str, float]) -> int:
    score = sum(float(dimensions.get(key, 0.0)) * weight for key, weight in DIMENSION_WEIGHTS.items())
    score -= float(dimensions.get("hostility", 0.0)) * HOSTILITY_WEIGHT
    return int(max(-100.0, min(100.0, score)))


def attitude_level(affection: int) -> str:
    if affection >= 60:
        return "intimate"
    if affection >= 30:
        return "friendly"
    if affection >= 0:
        return "neutral"
    if affection >= -30:
        return "cold"
    return "hostile"


def is_noisy_relationship_event(event_type: Any, reason: Any = "") -> bool:
    """路过触达与关键词空跑不算正式关系事件。"""
    normalized_event = str(event_type or "").strip()
    normalized_reason = str(reason or "").strip()
    return normalized_event in NOISY_EVENT_TYPES or normalized_reason in NOISY_EVENT_REASONS


def validate_event(event_type: Any, dimension: Any, reason: Any, delta: Any) -> tuple[str, str, str, float]:
    normalized_event = str(event_type or "").strip()
    normalized_dimension = str(dimension or "").strip()
    normalized_reason = str(reason or "").strip()
    if normalized_event not in VALID_EVENT_TYPES or is_noisy_relationship_event(normalized_event, normalized_reason):
        raise ValueError("invalid_relationship_event_type")
    if normalized_dimension not in VALID_DIMENSIONS:
        raise ValueError("invalid_relationship_dimension")
    if not normalized_reason:
        raise ValueError("relationship_reason_required")
    if len(normalized_reason) > 1000:
        raise ValueError("relationship_reason_too_long")
    try:
        normalized_delta = float(delta)
    except (TypeError, ValueError) as exc:
        raise ValueError("relationship_delta_invalid") from exc
    if not math.isfinite(normalized_delta):
        raise ValueError("relationship_delta_invalid")
    return normalized_event, normalized_dimension, normalized_reason, normalized_delta


def cap_automatic_delta(
    *,
    dimension: str,
    requested_delta: float,
    daily_total: float,
    single_delta_cap: float = SINGLE_DELTA_CAP,
    daily_delta_cap: float = DAILY_DELTA_CAP,
) -> float:
    cap = HOSTILITY_DELTA_CAP if dimension == "hostility" and float(requested_delta) > 0 else single_delta_cap
    delta = max(-abs(cap), min(abs(cap), float(requested_delta)))
    if delta > 0:
        remaining = daily_delta_cap - max(float(daily_total), 0.0)
        return round(min(delta, max(0.0, remaining)), 2)
    if delta < 0:
        remaining = daily_delta_cap - max(-float(daily_total), 0.0)
        return round(max(delta, -max(0.0, remaining)), 2)
    return 0.0


def cap_manual_adjustment_delta(requested_delta: float, cap: float = MANUAL_ADJUSTMENT_DELTA_CAP) -> float:
    try:
        value = float(requested_delta)
    except (TypeError, ValueError) as exc:
        raise ValueError("relationship_delta_invalid") from exc
    if not math.isfinite(value):
        raise ValueError("relationship_delta_invalid")
    return round(max(-abs(cap), min(abs(cap), value)), 2)


__all__ = [
    "DAILY_DELTA_CAP",
    "DIMENSION_RANGES",
    "DIMENSION_WEIGHTS",
    "HOSTILITY_DELTA_CAP",
    "HOSTILITY_WEIGHT",
    "MANUAL_ADJUSTMENT_DELTA_CAP",
    "SINGLE_DELTA_CAP",
    "VALID_DIMENSIONS",
    "VALID_EVENT_TYPES",
    "attitude_level",
    "cap_automatic_delta",
    "cap_manual_adjustment_delta",
    "clamp_dimension",
    "compute_affinity",
    "validate_event",
]
