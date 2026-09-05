from __future__ import annotations

from domain.scope import RuntimeScope, SessionRef
from services.belief_gating import (
    RelationshipSnapshot,
    candidate_fingerprint,
    canonical_subject_principal,
    evaluate_relationship_gate,
    interaction_policy,
)


def scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
    )


def snapshot(subject: str, *, trust: float | None, hostility: float | None, fun: float = 0) -> RelationshipSnapshot:
    dimensions = {"fun": fun}
    if trust is not None:
        dimensions["trust"] = trust
    if hostility is not None:
        dimensions["hostility"] = hostility
    return RelationshipSnapshot(subject, dimensions, revision=7)


def test_gate_boundaries_are_conservative():
    assert evaluate_relationship_gate([snapshot("qq:user:u1", trust=60, hostility=29)])[
        "decision"
    ] == "direct"
    assert evaluate_relationship_gate([snapshot("qq:user:u1", trust=60, hostility=30)])[
        "decision"
    ] == "pending"
    assert evaluate_relationship_gate([snapshot("qq:user:u1", trust=29, hostility=0)])[
        "decision"
    ] == "quarantine"
    assert evaluate_relationship_gate([snapshot("qq:user:u1", trust=60, hostility=60)])[
        "decision"
    ] == "quarantine"


def test_multi_speaker_uses_strictest_trust_and_hostility():
    result = evaluate_relationship_gate([
        snapshot("qq:user:trusted", trust=90, hostility=1),
        snapshot("qq:user:review", trust=55, hostility=20),
    ])
    assert result["decision"] == "pending"
    assert result["effective_trust"] == 55
    assert result["effective_hostility"] == 20

    result = evaluate_relationship_gate([
        snapshot("qq:user:trusted", trust=90, hostility=1),
        snapshot("qq:user:hostile", trust=80, hostility=60),
    ])
    assert result["decision"] == "quarantine"
    assert result["effective_trust"] == 80
    assert result["effective_hostility"] == 60


def test_unknown_relationship_never_direct():
    result = evaluate_relationship_gate(
        [snapshot("qq:user:u1", trust=90, hostility=1)],
        unknown_subjects=["qq:user:u2"],
    )
    assert result["decision"] == "pending"
    assert result["reason_code"] == "relationship_unknown"
    assert result["review_required"] is True


def test_principal_and_fingerprint_are_scope_bound():
    current = scope()
    assert canonical_subject_principal(current, "u1") == "qq:user:u1"
    assert canonical_subject_principal(current, "qq:user:u1") == "qq:user:u1"
    assert canonical_subject_principal(current, "wx:user:u1") is None
    assert canonical_subject_principal(current, "qq:user:u1:extra") is None
    assert canonical_subject_principal(current, "") is None

    first = candidate_fingerprint(current, relation="reinforce", target_belief_id=5, content="  同一 判断 ")
    retry = candidate_fingerprint(current, relation="reinforce", target_belief_id=5, content="同一 判断")
    other_scope = RuntimeScope(
        bot_id="bot-beta",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
    )
    assert first == retry
    assert first != candidate_fingerprint(other_scope, relation="reinforce", target_belief_id=5, content="同一 判断")
    assert first.startswith("belief-candidate-v1:")
    assert len(first) == len("belief-candidate-v1:") + 32


def test_interaction_policy_prioritizes_hostility_and_trust():
    hostile = interaction_policy(snapshot("qq:user:u1", trust=90, hostility=60))
    assert hostile["mode"] == "deescalate"
    assert hostile["allow_person_judgment"] is False
    assert hostile["playfulness"] == "none"

    trusted = interaction_policy(snapshot("qq:user:u1", trust=60, hostility=29, fun=40))
    assert trusted["mode"] == "trusted"
    assert trusted["allow_person_judgment"] is True
    assert trusted["playfulness"] == "light"

    unknown = interaction_policy(None, subject_available=False)
    assert unknown["mode"] == "cautious"
    assert unknown["allow_person_judgment"] is False
