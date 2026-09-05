"""Scoped belief relationship gating policy.

This module is deliberately independent from SQLite and LLM services.  It turns
formal relationship snapshots into a conservative, auditable decision that can
be reused by extraction and injection paths.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - focused tests import services directly
    from domain.scope import RuntimeScope


POLICY_VERSION = "relationship-gating-v1"
DIRECT_TRUST_MIN = 60.0
DIRECT_HOSTILITY_MAX = 30.0
QUARANTINE_TRUST_BELOW = 30.0
QUARANTINE_HOSTILITY_MIN = 60.0
FUN_PLAYFUL_MIN = 40.0


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def canonical_subject_principal(scope: RuntimeScope, sender_id: Any) -> str | None:
    """Project a message sender into the current session's principal namespace."""
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        return None
    sender = str(sender_id or "").strip()
    if not sender:
        return None
    prefix = f"{scope.session.platform_id}:user:"
    if sender.startswith(prefix):
        return sender if sender != prefix and ":" not in sender[len(prefix):] else None
    # Raw IDs are accepted only as a single namespace-safe component.  A
    # different platform/canonical namespace is never silently rewritten.
    if ":" in sender or sender in {"bot_self", "unknown"}:
        return None
    return f"{prefix}{sender}"


def relationship_dimensions(row: Mapping[str, Any] | None) -> dict[str, float]:
    """Read effective formal values without inventing missing dimensions."""
    if not isinstance(row, Mapping):
        return {}
    raw_values = row.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    raw_dimensions = row.get("dimensions")
    dimensions = raw_dimensions if isinstance(raw_dimensions, Mapping) else {}
    result: dict[str, float] = {}
    for name in ("familiarity", "trust", "fun", "hostility", "depth"):
        value: Any = None
        item = values.get(name)
        if isinstance(item, Mapping):
            value = item.get("effective_value")
        if value is None:
            value = dimensions.get(name)
        numeric = _finite_number(value)
        if numeric is not None:
            result[name] = numeric
    return result


@dataclass(frozen=True)
class RelationshipSnapshot:
    subject_principal_id: str
    dimensions: Mapping[str, float]
    revision: int | None = None
    available: bool = True

    @property
    def trust(self) -> float | None:
        return _finite_number(self.dimensions.get("trust"))

    @property
    def hostility(self) -> float | None:
        return _finite_number(self.dimensions.get("hostility"))

    @property
    def known_for_gating(self) -> bool:
        return self.available and self.trust is not None and self.hostility is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_principal_id": self.subject_principal_id,
            "dimensions": dict(self.dimensions),
            "trust": self.trust,
            "hostility": self.hostility,
            "revision": self.revision,
            "available": self.available,
        }


def snapshot_from_relationship(
    subject_principal_id: str,
    row: Mapping[str, Any] | None,
) -> RelationshipSnapshot:
    revision = None
    if isinstance(row, Mapping):
        raw_revision = _finite_number(row.get("revision"))
        revision = int(raw_revision) if raw_revision is not None else None
    return RelationshipSnapshot(
        subject_principal_id=subject_principal_id,
        dimensions=relationship_dimensions(row),
        revision=revision,
        available=isinstance(row, Mapping),
    )


def candidate_fingerprint(
    scope: RuntimeScope,
    *,
    relation: str,
    target_belief_id: int | None,
    content: str,
) -> str:
    """Build a scope- and target-bound idempotency key for gated candidates."""
    if not isinstance(scope, RuntimeScope) or scope.session is None:
        raise ValueError("scope_required")
    normalized_relation = str(relation or "").strip().lower()
    normalized_content = " ".join(str(content or "").casefold().split())
    payload = "\0".join((
        scope.bot_id,
        scope.session.id,
        scope.visibility,
        normalized_relation,
        "" if target_belief_id is None else str(int(target_belief_id)),
        normalized_content,
    ))
    return "belief-candidate-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def evaluate_relationship_gate(
    snapshots: Sequence[RelationshipSnapshot],
    *,
    unknown_subjects: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate one or more speakers using the strictest relationship values."""
    normalized = [item for item in snapshots if isinstance(item, RelationshipSnapshot)]
    unknown = list(dict.fromkeys(str(item) for item in unknown_subjects if str(item)))
    known = [item for item in normalized if item.known_for_gating]
    all_subjects = list(dict.fromkeys([item.subject_principal_id for item in normalized] + unknown))
    trust_values = [item.trust for item in known if item.trust is not None]
    hostility_values = [item.hostility for item in known if item.hostility is not None]
    effective_trust = min(trust_values) if trust_values else None
    effective_hostility = max(hostility_values) if hostility_values else None

    decision = "pending"
    reason_code = "relationship_review_required"
    if any(item.trust is not None and item.trust < QUARANTINE_TRUST_BELOW for item in known):
        decision = "quarantine"
        reason_code = "relationship_low_trust"
    elif any(item.hostility is not None and item.hostility >= QUARANTINE_HOSTILITY_MIN for item in known):
        decision = "quarantine"
        reason_code = "relationship_high_hostility"
    elif unknown or len(known) != len(normalized) or not known:
        decision = "pending"
        reason_code = "relationship_unknown" if unknown or not known else "relationship_review_required"
    elif effective_trust is not None and effective_hostility is not None:
        if effective_trust >= DIRECT_TRUST_MIN and effective_hostility < DIRECT_HOSTILITY_MAX:
            decision = "direct"
            reason_code = "relationship_direct"
        else:
            decision = "pending"
            reason_code = "relationship_review_required"

    return {
        "policy_version": POLICY_VERSION,
        "decision": decision,
        "reason_code": reason_code,
        "effective_trust": effective_trust,
        "effective_hostility": effective_hostility,
        "subjects": all_subjects,
        "relationship_revisions": {
            item.subject_principal_id: item.revision for item in normalized if item.revision is not None
        },
        "snapshots": [item.to_dict() for item in normalized],
        "unknown_subjects": unknown,
        "review_required": decision != "direct",
    }


def interaction_policy(
    snapshot: RelationshipSnapshot | None,
    *,
    subject_available: bool = True,
) -> dict[str, Any]:
    """Return bounded, non-factual response guidance for the current subject."""
    if not subject_available or snapshot is None or not snapshot.known_for_gating:
        return {
            "policy_version": POLICY_VERSION,
            "mode": "cautious",
            "reason_code": "relationship_unknown",
            "allow_person_judgment": False,
            "privacy_openness": "minimal",
            "playfulness": "neutral",
            "boundary": "先核实事实，不要默认彼此熟悉或共享背景",
        }
    trust = float(snapshot.trust or 0.0)
    hostility = float(snapshot.hostility or 0.0)
    fun = float(snapshot.dimensions.get("fun", 0.0))
    if hostility >= QUARANTINE_HOSTILITY_MIN:
        return {
            "policy_version": POLICY_VERSION,
            "mode": "deescalate",
            "reason_code": "relationship_high_hostility",
            "allow_person_judgment": False,
            "privacy_openness": "minimal",
            "playfulness": "none",
            "boundary": "保持礼貌、简短并明确边界，不反击",
        }
    if trust >= DIRECT_TRUST_MIN and hostility < DIRECT_HOSTILITY_MAX:
        return {
            "policy_version": POLICY_VERSION,
            "mode": "trusted",
            "reason_code": "relationship_direct",
            "allow_person_judgment": True,
            "privacy_openness": "safe_public_topics_only",
            "playfulness": "light" if fun >= FUN_PLAYFUL_MIN else "neutral",
            "boundary": "respond naturally only to volunteered or safely public topics; do not disclose private information",
        }
    return {
        "policy_version": POLICY_VERSION,
        "mode": "cautious",
        "reason_code": "relationship_review_required" if trust >= QUARANTINE_TRUST_BELOW else "relationship_low_trust",
        "allow_person_judgment": trust >= QUARANTINE_TRUST_BELOW,
        "privacy_openness": "minimal",
        "playfulness": "light" if fun >= FUN_PLAYFUL_MIN else "neutral",
        "boundary": "keep claims tentative and do not assume trust or shared context",
    }


__all__ = [
    "DIRECT_HOSTILITY_MAX",
    "DIRECT_TRUST_MIN",
    "FUN_PLAYFUL_MIN",
    "POLICY_VERSION",
    "QUARANTINE_HOSTILITY_MIN",
    "QUARANTINE_TRUST_BELOW",
    "RelationshipSnapshot",
    "candidate_fingerprint",
    "canonical_subject_principal",
    "evaluate_relationship_gate",
    "interaction_policy",
    "relationship_dimensions",
    "snapshot_from_relationship",
]
