"""正式关系快照与信任/敌意阈值（只剩主动关心策略在用）。

历史上这里是「按好感分数给信念做门禁」：对方 trust<30 或 hostility≥60 时不注入对人的判断、
提取信念时按关系决定直通/待审/隔离。信念 v2 只来自 Bot 自己的经历反思，不再有对人的判断，
这些门禁（``interaction_policy``、``evaluate_relationship_gate``、``candidate_fingerprint``、
``canonical_subject_principal``）已删除。模块名保留，免得改动所有导入点。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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


__all__ = [
    "DIRECT_HOSTILITY_MAX",
    "DIRECT_TRUST_MIN",
    "FUN_PLAYFUL_MIN",
    "QUARANTINE_HOSTILITY_MIN",
    "QUARANTINE_TRUST_BELOW",
    "RelationshipSnapshot",
    "relationship_dimensions",
    "snapshot_from_relationship",
]
