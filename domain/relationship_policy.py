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


# ─── 关系档位（读时从五维推导，不改存量数值） ───
#
# attitude_level 只看加权和：熟悉度随互动一路涨，信任/趣味/深度却很少被记，结果所有人都落在
# neutral，档位没有区分度。档位改为按维度组合判断：先看是否有敌意，再从「亲近」往下逐档匹配，
# 第一个命中的即当前档位。档位名与阈值通用、不写死任何角色，可由配置覆盖。
#
# 每个档位的 when 是若干条件组，任意一组全部满足即命中；条件写成 {维度: [下限, 上限]}，
# null 表示不限。除五维外还可用派生维度 rapport（fun + depth，玩得来 + 聊得深）。
DEFAULT_RELATIONSHIP_TIERS: tuple[Mapping[str, Any], ...] = (
    {"key": "hostile", "label": "敌对", "polarity": "negative",
     "when": [{"hostility": [40, None]}, {"trust": [None, -30]}]},
    {"key": "averse", "label": "反感", "polarity": "negative",
     "when": [{"hostility": [20, None], "trust": [None, 20]}, {"trust": [None, -15]}]},
    {"key": "annoyed", "label": "有点烦", "polarity": "negative",
     "when": [{"hostility": [8, None], "trust": [None, 10]}, {"trust": [None, -5]}]},
    {"key": "close", "label": "亲近", "polarity": "positive",
     "when": [{"familiarity": [60, None], "trust": [40, None], "rapport": [30, None]}]},
    {"key": "trusted", "label": "信得过", "polarity": "positive",
     "when": [{"familiarity": [40, None], "trust": [20, None]}]},
    {"key": "congenial", "label": "合得来", "polarity": "positive",
     "when": [{"familiarity": [30, None], "rapport": [12, None]}]},
    {"key": "regular", "label": "常聊", "polarity": "neutral",
     "when": [{"familiarity": [40, None]}]},
    {"key": "familiar", "label": "面熟", "polarity": "neutral",
     "when": [{"familiarity": [10, None]}]},
    {"key": "stranger", "label": "陌生人", "polarity": "neutral", "when": [{}]},
)
DERIVED_DIMENSIONS = frozenset({"rapport"})
_TIER_POLARITIES = frozenset({"positive", "neutral", "negative"})
# 由 main/bootstrap 从 Affinity_Settings.relationship_tiers_json 注入；空表示用默认档位。
_ACTIVE_TIERS: list[dict[str, Any]] = []


def _normalize_tier_clause(clause: Any) -> dict[str, tuple[float | None, float | None]]:
    if not isinstance(clause, Mapping):
        raise ValueError("relationship_tier_clause_invalid")
    normalized: dict[str, tuple[float | None, float | None]] = {}
    for name, bounds in clause.items():
        key = str(name)
        if key not in VALID_DIMENSIONS and key not in DERIVED_DIMENSIONS:
            raise ValueError(f"relationship_tier_dimension_invalid:{key}")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ValueError(f"relationship_tier_bounds_invalid:{key}")
        pair: list[float | None] = []
        for raw in bounds:
            if raw is None:
                pair.append(None)
                continue
            if isinstance(raw, bool):
                raise ValueError(f"relationship_tier_bounds_invalid:{key}")
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(f"relationship_tier_bounds_invalid:{key}")
            pair.append(value)
        normalized[key] = (pair[0], pair[1])
    return normalized


def _normalize_tier(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError("relationship_tier_invalid")
    key = str(item.get("key") or "").strip()
    label = str(item.get("label") or "").strip()
    if not key or not label:
        raise ValueError("relationship_tier_key_and_label_required")
    polarity = str(item.get("polarity") or "neutral").strip()
    if polarity not in _TIER_POLARITIES:
        raise ValueError(f"relationship_tier_polarity_invalid:{key}")
    when = item.get("when")
    if not isinstance(when, (list, tuple)) or not when:
        raise ValueError(f"relationship_tier_when_required:{key}")
    return {"key": key, "label": label[:12], "polarity": polarity,
            "when": [_normalize_tier_clause(clause) for clause in when]}


def parse_relationship_tiers(spec: Any) -> list[dict[str, Any]]:
    """解析档位配置：列表 = 整体替换；对象 = 按 key 覆盖默认档位的 label/polarity/when。

    非法配置抛 ValueError，由调用方记录并回退默认，不让一处笔误把档位全部弄坏。
    """
    defaults = [_normalize_tier(item) for item in DEFAULT_RELATIONSHIP_TIERS]
    if spec is None or spec == "" or spec == {} or spec == []:
        return defaults
    if isinstance(spec, str):
        import json

        spec = json.loads(spec)
    if isinstance(spec, (list, tuple)):
        tiers = [_normalize_tier(item) for item in spec]
    elif isinstance(spec, Mapping):
        patched = {tier["key"]: dict(tier) for tier in defaults}
        for key, patch in spec.items():
            if str(key) not in patched or not isinstance(patch, Mapping):
                raise ValueError(f"relationship_tier_unknown:{key}")
            merged = {**patched[str(key)], **{k: v for k, v in patch.items() if k in {"label", "polarity", "when"}}}
            if "when" not in patch:
                # 已规范化的默认条件转回可再解析的列表形式
                merged["when"] = [{dim: list(bounds) for dim, bounds in clause.items()} for clause in patched[str(key)]["when"]]
            patched[str(key)] = _normalize_tier({**merged, "key": str(key)})
        tiers = [patched[tier["key"]] for tier in defaults]
    else:
        raise ValueError("relationship_tiers_spec_invalid")
    if len({tier["key"] for tier in tiers}) != len(tiers):
        raise ValueError("relationship_tier_key_duplicated")
    return tiers


def configure_relationship_tiers(spec: Any = None) -> list[dict[str, Any]]:
    """由外部配置设置当前档位；非法时保持默认并把异常抛给调用方记录。"""
    tiers = parse_relationship_tiers(spec)
    _ACTIVE_TIERS[:] = tiers
    return tiers


def active_relationship_tiers() -> list[dict[str, Any]]:
    return list(_ACTIVE_TIERS) if _ACTIVE_TIERS else parse_relationship_tiers(None)


def _tier_values(dimensions: Mapping[str, Any] | None) -> dict[str, float]:
    values: dict[str, float] = {}
    for name in DIMENSION_RANGES:
        raw = (dimensions or {}).get(name) if isinstance(dimensions, Mapping) else None
        try:
            number = float(raw) if raw is not None and not isinstance(raw, bool) else 0.0
        except (TypeError, ValueError):
            number = 0.0
        values[name] = clamp_dimension(name, number if math.isfinite(number) else 0.0)
    values["rapport"] = values["fun"] + values["depth"]
    return values


def _clause_matches(clause: Mapping[str, tuple[float | None, float | None]], values: Mapping[str, float]) -> bool:
    for name, (low, high) in clause.items():
        value = values.get(name, 0.0)
        if low is not None and value < low:
            return False
        if high is not None and value > high:
            return False
    return True


def classify_relationship_tier(
    dimensions: Mapping[str, Any] | None,
    *,
    tiers: list[Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """从五维推导关系档位；返回 {tier, tier_label, polarity}。缺失维度按 0 处理。"""
    values = _tier_values(dimensions)
    for tier in tiers if tiers is not None else active_relationship_tiers():
        if any(_clause_matches(clause, values) for clause in tier["when"]):
            return {"tier": tier["key"], "tier_label": tier["label"], "polarity": tier["polarity"]}
    return {"tier": "stranger", "tier_label": "陌生人", "polarity": "neutral"}


def relationship_closeness(dimensions: Mapping[str, Any] | None) -> float:
    """亲疏权重 -1~1：正值越大越亲近（损话更像玩笑），负值越大越有敌意（更像冒犯）。

    熟悉度只贡献有限的正向分量：光是常见面并不等于亲近；信任与「玩得来/聊得深」才拉开差距。
    敌意与负信任直接扣分。
    """
    values = _tier_values(dimensions)
    positive = (
        0.35 * min(values["familiarity"] / 100.0, 1.0)
        + 0.35 * max(0.0, min(values["trust"] / 60.0, 1.0))
        + 0.30 * min(values["rapport"] / 40.0, 1.0)
    )
    negative = min(1.0, values["hostility"] / 40.0 + max(0.0, -values["trust"]) / 30.0)
    return round(max(-1.0, min(1.0, positive - negative)), 3)


def relationship_stance(dimensions: Mapping[str, Any] | None) -> dict[str, Any]:
    """档位 + 亲疏权重，供注入与心情评估共用。"""
    return {**classify_relationship_tier(dimensions), "closeness": relationship_closeness(dimensions)}


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
    "DEFAULT_RELATIONSHIP_TIERS",
    "DIMENSION_RANGES",
    "DIMENSION_WEIGHTS",
    "HOSTILITY_DELTA_CAP",
    "HOSTILITY_WEIGHT",
    "MANUAL_ADJUSTMENT_DELTA_CAP",
    "SINGLE_DELTA_CAP",
    "VALID_DIMENSIONS",
    "VALID_EVENT_TYPES",
    "active_relationship_tiers",
    "attitude_level",
    "cap_automatic_delta",
    "cap_manual_adjustment_delta",
    "clamp_dimension",
    "classify_relationship_tier",
    "compute_affinity",
    "configure_relationship_tiers",
    "parse_relationship_tiers",
    "relationship_closeness",
    "relationship_stance",
    "validate_event",
]
