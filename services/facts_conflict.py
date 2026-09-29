"""Facts 冲突分类与 scoped observation 记录。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from .fact_policy import get_fact_policy, normalized_text, valid_interval


@dataclass(frozen=True)
class FactConflict:
    relation: str
    reason: str
    existing_id: int | None = None


class FactConflictClassifier:
    """按同主体/谓词和有效期判断正式 Facts 关系。"""

    RELATIONS = frozenset({"compatible", "scoped", "conflicts", "supersedes"})

    def __init__(self, *, policy=None):
        self.policy = policy or get_fact_policy()

    @staticmethod
    def _overlaps(candidate: Mapping[str, Any], existing: Mapping[str, Any]) -> bool:
        try:
            c_start, c_end = valid_interval(candidate.get("valid_from"), candidate.get("valid_until"))
            e_start, e_end = valid_interval(existing.get("valid_from"), existing.get("valid_until"))
        except ValueError:
            return True  # Bad bounds must not bypass conflict review.
        # 缺失边界代表开放区间；两个无边界版本默认同时有效。
        return (e_end is None or c_start is None or float(e_end) > float(c_start)) and (
            c_end is None or e_start is None or float(c_end) > float(e_start)
        )

    def classify(self, subject: str | Mapping[str, Any], predicate: str | Mapping[str, Any], object: str | None = None, existing: Any = None) -> FactConflict:
        candidate: Mapping[str, Any]
        if isinstance(subject, Mapping):
            candidate = subject
            rows = predicate if isinstance(predicate, (list, tuple)) else ([predicate] if isinstance(predicate, Mapping) else [])
        else:
            candidate = {"subject": subject, "predicate": predicate, "object": object}
            rows = existing or []
        decisions = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if row.get("status") not in (None, "active", "approved"):
                continue
            if any(candidate.get(k) is not None and row.get(k) is not None and candidate[k] != row[k]
                   for k in ("bot_id", "session_id", "visibility")):
                continue
            if any(normalized_text(row.get(k)) != normalized_text(candidate.get(k)) for k in ("subject", "predicate")):
                continue
            row_id = row.get("id")
            if row.get("object") == candidate.get("object"):
                decision = FactConflict("compatible", "same triple", int(row_id) if row_id is not None else None)
            elif not self._overlaps(candidate, row):
                decision = FactConflict("scoped", "non-overlapping validity intervals", int(row_id) if row_id is not None else None)
            elif self.policy.is_single_value(str(candidate.get("predicate") or "")):
                decision = FactConflict("conflicts", "overlapping single-value slot", int(row_id) if row_id is not None else None)
            else:
                decision = FactConflict("compatible", "multi-value relation", int(row_id) if row_id is not None else None)
            decisions.append(decision)
        # Inspect every row: an earlier duplicate/disjoint interval cannot hide a conflict.
        priorities = {"compatible": 0, "scoped": 1, "conflicts": 2}
        return max(decisions, key=lambda d: priorities[d.relation]) if decisions else FactConflict("compatible", "no same subject/predicate fact")

    def __call__(self, subject: Any, predicate: Any, object: Any = None, existing: Any = None) -> str:
        return self.classify(subject, predicate, object, existing).relation

__all__ = ['FactConflict', 'FactConflictClassifier']
