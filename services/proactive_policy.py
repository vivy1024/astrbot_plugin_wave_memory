"""Relationship-driven proactive behavior policy.

The policy is deliberately pure: it consumes already-resolved relationship
snapshots and trigger signals, but never reads SQLite, events, or LLM state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from .belief_gating import (
        DIRECT_HOSTILITY_MAX,
        DIRECT_TRUST_MIN,
        QUARANTINE_HOSTILITY_MIN,
        QUARANTINE_TRUST_BELOW,
        RelationshipSnapshot,
    )
except ImportError:  # pragma: no cover - focused repository tests
    from services.belief_gating import (
        DIRECT_HOSTILITY_MAX,
        DIRECT_TRUST_MIN,
        QUARANTINE_HOSTILITY_MIN,
        QUARANTINE_TRUST_BELOW,
        RelationshipSnapshot,
    )


POLICY_VERSION = "relationship-proactive-v1"
DEPTH_LOW_MAX = 20.0
DEPTH_HIGH_MIN = 50.0
CONCERN_MEDIUM_MIN = 0.30
CONCERN_STRONG_MIN = 0.70
FUN_PLAYFUL_MIN = 40.0

BEHAVIOR_AMBIENT = "ambient_interjection"
BEHAVIOR_CONCERN_FOLLOWUP = "concern_followup"
SUPPORTED_BEHAVIORS = frozenset({BEHAVIOR_AMBIENT, BEHAVIOR_CONCERN_FOLLOWUP})


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp01(value: Any) -> float:
    number = _finite(value)
    if number is None:
        return 0.0
    return max(0.0, min(1.0, number))


def _snapshot_dict(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, RelationshipSnapshot):
        return snapshot.to_dict()
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    return {}


def _snapshot_subject(snapshot: Any) -> str:
    data = _snapshot_dict(snapshot)
    return str(data.get("subject_principal_id") or "").strip()


def _snapshot_dimensions(snapshot: Any) -> dict[str, float]:
    data = _snapshot_dict(snapshot)
    raw_dimensions = data.get("dimensions")
    dimensions = dict(raw_dimensions) if isinstance(raw_dimensions, Mapping) else {}
    raw_values = data.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    result: dict[str, float] = {}
    for name in ("familiarity", "trust", "fun", "hostility", "depth"):
        value = dimensions.get(name)
        item = values.get(name)
        if isinstance(item, Mapping) and item.get("effective_value") is not None:
            value = item.get("effective_value")
        number = _finite(value)
        if number is not None:
            result[name] = number
    return result


def _snapshot_available(snapshot: Any) -> bool:
    data = _snapshot_dict(snapshot)
    if isinstance(snapshot, RelationshipSnapshot):
        return snapshot.available and snapshot.known_for_gating
    if data.get("available") is False:
        return False
    dimensions = _snapshot_dimensions(snapshot)
    return all(name in dimensions for name in ("trust", "hostility", "depth"))


def _base_result(
    snapshots: Sequence[Any],
    *,
    concern_score: float,
    is_interesting: bool,
    behavior_type: str,
    decision: str,
    reason_code: str,
    trigger_weight: float = 0.0,
    fail_closed: bool = True,
) -> dict[str, Any]:
    normalized = [item for item in snapshots if _snapshot_subject(item)]
    dimensions = [_snapshot_dimensions(item) for item in normalized]

    def strict_min(name: str) -> float | None:
        values = [item[name] for item in dimensions if name in item]
        return min(values) if values else None

    def strict_max(name: str) -> float | None:
        values = [item[name] for item in dimensions if name in item]
        return max(values) if values else None

    revisions: dict[str, int] = {}
    payload_snapshots: list[dict[str, Any]] = []
    for item in normalized:
        data = _snapshot_dict(item)
        subject = _snapshot_subject(item)
        revision = _finite(data.get("revision"))
        if revision is not None:
            revisions[subject] = int(revision)
        payload_snapshots.append(data)

    effective_depth = strict_min("depth")
    if effective_depth is None:
        depth_factor = 0.0
    elif effective_depth < DEPTH_LOW_MAX:
        depth_factor = 0.4
    elif effective_depth < DEPTH_HIGH_MIN:
        depth_factor = 0.7
    else:
        depth_factor = 1.0
    return {
        "policy_version": POLICY_VERSION,
        "decision": decision,
        "reason_code": reason_code,
        "behavior_type": behavior_type,
        "trigger_weight": round(max(0.0, min(1.0, float(trigger_weight))), 2),
        "depth_factor": depth_factor,
        "fail_closed": bool(fail_closed),
        "review_required": decision != "allow_llm",
        "concern_score": round(_clamp01(concern_score), 3),
        "is_interesting": bool(is_interesting),
        "subjects": [_snapshot_subject(item) for item in normalized],
        "relationship_revisions": revisions,
        "snapshots": payload_snapshots,
        # Multi-speaker decisions are conservative: trust/depth/familiarity
        # use the least favorable value; hostility uses the worst value.
        "effective_trust": strict_min("trust"),
        "effective_hostility": strict_max("hostility"),
        "effective_depth": strict_min("depth"),
        "effective_fun": strict_min("fun"),
        "effective_familiarity": strict_min("familiarity"),
    }


def evaluate_proactive_policy(
    snapshots: Sequence[Any],
    *,
    concern_score: float,
    is_interesting: bool,
    behavior_type: str = BEHAVIOR_AMBIENT,
    scope_available: bool = True,
    subject_available: bool = True,
) -> dict[str, Any]:
    """Evaluate whether a proactive candidate may reach MetaThinking.

    ``pending`` is intentionally fail-closed for execution: it is returned for
    the auditable intermediate relationship band, but callers must not invoke
    an LLM unless the decision is exactly ``allow_llm``.
    """
    snapshots = tuple(snapshots or ())
    behavior_type = str(behavior_type or BEHAVIOR_AMBIENT).strip()
    concern = _clamp01(concern_score)
    interesting = bool(is_interesting)

    if not scope_available:
        return _base_result(
            snapshots,
            concern_score=concern,
            is_interesting=interesting,
            behavior_type=behavior_type,
            decision="blocked",
            reason_code="scope_required",
        )
    if not subject_available or not snapshots:
        return _base_result(
            snapshots,
            concern_score=concern,
            is_interesting=interesting,
            behavior_type=behavior_type,
            decision="blocked",
            reason_code="subject_missing",
        )
    if any(not _snapshot_available(item) for item in snapshots):
        return _base_result(
            snapshots,
            concern_score=concern,
            is_interesting=interesting,
            behavior_type=behavior_type,
            decision="blocked",
            reason_code="relationship_unknown",
        )

    result = _base_result(
        snapshots,
        concern_score=concern,
        is_interesting=interesting,
        behavior_type=behavior_type,
        decision="no_trigger",
        reason_code="trigger_signal_absent",
    )
    trust = result["effective_trust"]
    hostility = result["effective_hostility"]
    depth = result["effective_depth"]

    if trust is None or hostility is None or depth is None:
        result.update(decision="blocked", reason_code="relationship_unknown", fail_closed=True)
        return result
    if trust < QUARANTINE_TRUST_BELOW:
        result.update(decision="blocked", reason_code="relationship_low_trust", fail_closed=True)
        return result
    if hostility >= QUARANTINE_HOSTILITY_MIN:
        result.update(decision="blocked", reason_code="relationship_high_hostility", fail_closed=True)
        return result
    # The intermediate trust/hostility bands are permanent execution gates
    # until the formal relationship snapshot changes.
    if trust < DIRECT_TRUST_MIN:
        result.update(decision="pending", reason_code="relationship_trust_pending", fail_closed=True)
        return result
    if hostility >= DIRECT_HOSTILITY_MAX:
        result.update(decision="pending", reason_code="relationship_hostility_pending", fail_closed=True)
        return result
    if behavior_type not in SUPPORTED_BEHAVIORS:
        result.update(decision="no_trigger", reason_code="unsupported_behavior_type", fail_closed=True)
        return result

    def allow(weight: float, reason: str) -> dict[str, Any]:
        result.update(decision="allow_llm", reason_code=reason, trigger_weight=weight, fail_closed=False)
        return result

    if behavior_type == BEHAVIOR_CONCERN_FOLLOWUP:
        if concern < CONCERN_MEDIUM_MIN:
            result.update(reason_code="concern_required")
            return result
        if depth < DEPTH_LOW_MAX:
            return allow(0.60, "low_depth_concern_followup") if concern >= CONCERN_STRONG_MIN else result
        if depth < DEPTH_HIGH_MIN:
            return allow(
                0.78 if concern >= CONCERN_STRONG_MIN else 0.72,
                "medium_depth_strong_concern_followup" if concern >= CONCERN_STRONG_MIN else "medium_depth_concern_followup",
            )
        return allow(
            0.88 if concern >= CONCERN_STRONG_MIN else 0.80,
            "high_depth_strong_concern_followup" if concern >= CONCERN_STRONG_MIN else "high_depth_concern_followup",
        )

    # ambient_interjection
    if depth < DEPTH_LOW_MAX:
        if concern >= CONCERN_STRONG_MIN:
            return allow(0.55, "low_depth_strong_concern")
        result.update(reason_code="low_depth_concern_insufficient")
        return result
    if depth < DEPTH_HIGH_MIN:
        if concern >= CONCERN_STRONG_MIN:
            return allow(0.70, "medium_depth_strong_concern")
        if concern >= CONCERN_MEDIUM_MIN and interesting:
            return allow(0.65, "medium_depth_interest_with_concern")
        result.update(reason_code="medium_depth_concern_required")
        return result
    if concern >= CONCERN_STRONG_MIN:
        return allow(0.80, "high_depth_strong_concern")
    if interesting:
        return allow(0.75 if concern >= CONCERN_MEDIUM_MIN else 0.65, "high_depth_interest_with_concern" if concern >= CONCERN_MEDIUM_MIN else "high_depth_interest")
    result.update(reason_code="trigger_signal_absent")
    return result


def relationship_behavior_guidance(snapshots: Sequence[Any]) -> dict[str, Any]:
    """Return bounded, non-injected policy metadata for proactive gating tests."""
    snapshots = tuple(snapshots or ())
    if not snapshots or any(not _snapshot_available(item) for item in snapshots):
        return {
            "policy_version": POLICY_VERSION,
            "mode": "cautious",
            "reason_code": "relationship_unknown",
            "openness": "minimal",
            "playfulness": "neutral",
            "continuity": "none",
            "boundary": "先核实事实，不要默认彼此熟悉或共享背景；不要主动扩展私人话题。",
        }
    result = _base_result(
        snapshots,
        concern_score=0.0,
        is_interesting=False,
        behavior_type=BEHAVIOR_AMBIENT,
        decision="no_trigger",
        reason_code="relationship_guidance",
    )
    trust = float(result["effective_trust"] or 0.0)
    hostility = float(result["effective_hostility"] or 0.0)
    fun = float(result["effective_fun"] or 0.0)
    depth = float(result["effective_depth"] or 0.0)
    familiarity = float(result["effective_familiarity"] or 0.0)
    if hostility >= QUARANTINE_HOSTILITY_MIN:
        mode, reason, openness, playfulness, boundary = (
            "deescalate",
            "relationship_high_hostility",
            "minimal",
            "none",
            "保持礼貌、简短并明确边界，不反击，不主动插话。",
        )
    elif trust >= DIRECT_TRUST_MIN and hostility < DIRECT_HOSTILITY_MAX:
        mode, reason, openness, playfulness, boundary = (
            "trusted",
            "relationship_direct",
            "safe_public_topics_only",
            "light" if fun >= FUN_PLAYFUL_MIN else "neutral",
            "只对用户主动提供的安全公开内容自然承接；不要披露私人信息。",
        )
    else:
        mode, reason, openness, playfulness, boundary = (
            "cautious",
            "relationship_review_required",
            "minimal",
            "light" if fun >= FUN_PLAYFUL_MIN else "neutral",
            "保持谨慎和试探，不假定信任、熟悉度或共享背景。",
        )
    return {
        "policy_version": POLICY_VERSION,
        "mode": mode,
        "reason_code": reason,
        "openness": openness,
        "playfulness": playfulness,
        "continuity": "recent_shared_experiences_only" if depth >= DEPTH_HIGH_MIN else "basic_context_only" if depth >= DEPTH_LOW_MAX else "none",
        "familiarity": "continuity_only" if familiarity >= 40.0 else "neutral",
        "boundary": boundary,
        "effective_depth": depth,
        "effective_fun": fun,
    }


__all__ = [
    "BEHAVIOR_AMBIENT",
    "BEHAVIOR_CONCERN_FOLLOWUP",
    "CONCERN_MEDIUM_MIN",
    "CONCERN_STRONG_MIN",
    "DEPTH_HIGH_MIN",
    "DEPTH_LOW_MAX",
    "POLICY_VERSION",
    "SUPPORTED_BEHAVIORS",
    "evaluate_proactive_policy",
    "relationship_behavior_guidance",
]
