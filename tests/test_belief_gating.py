"""belief_gating 只剩正式关系快照与阈值（主动关心策略在用）；按好感给信念门禁的逻辑已删除。"""

from __future__ import annotations

import services.belief_gating as gating
from services.belief_gating import RelationshipSnapshot, relationship_dimensions, snapshot_from_relationship


def test_relationship_snapshot_reads_effective_values_without_inventing_missing_dimensions():
    row = {
        "revision": 3,
        "values": {"trust": {"effective_value": 42}, "hostility": {"effective_value": "5"}},
        "dimensions": {"fun": 12, "depth": float("nan")},
    }
    assert relationship_dimensions(row) == {"trust": 42.0, "fun": 12.0, "hostility": 5.0}
    snapshot = snapshot_from_relationship("qq:user:10000001", row)
    assert isinstance(snapshot, RelationshipSnapshot)
    assert (snapshot.trust, snapshot.hostility, snapshot.revision) == (42.0, 5.0, 3)
    assert snapshot.known_for_gating
    assert snapshot.to_dict()["subject_principal_id"] == "qq:user:10000001"
    missing = snapshot_from_relationship("qq:user:10000002", None)
    assert not missing.available and not missing.known_for_gating


def test_trust_based_belief_gates_are_gone():
    for name in ("interaction_policy", "evaluate_relationship_gate", "candidate_fingerprint", "canonical_subject_principal"):
        assert not hasattr(gating, name), f"{name} 只为旧信念服务，应已删除"
    # 主动关心仍使用这些阈值
    assert gating.DIRECT_TRUST_MIN == 60.0 and gating.QUARANTINE_TRUST_BELOW == 30.0
