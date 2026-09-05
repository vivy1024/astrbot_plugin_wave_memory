from services.belief_gating import RelationshipSnapshot
from services.proactive_policy import (
    BEHAVIOR_AMBIENT,
    BEHAVIOR_CONCERN_FOLLOWUP,
    evaluate_proactive_policy,
    relationship_behavior_guidance,
)


def snapshot(
    subject="qq:user:u1",
    *,
    familiarity=50,
    trust=70,
    fun=50,
    hostility=10,
    depth=55,
    revision=1,
):
    return RelationshipSnapshot(
        subject,
        {
            "familiarity": familiarity,
            "trust": trust,
            "fun": fun,
            "hostility": hostility,
            "depth": depth,
        },
        revision=revision,
    )


def decide(*, depth=55, concern=0.0, interesting=False, behavior=BEHAVIOR_AMBIENT, **kwargs):
    return evaluate_proactive_policy(
        [snapshot(depth=depth, **kwargs)],
        concern_score=concern,
        is_interesting=interesting,
        behavior_type=behavior,
    )


def test_low_trust_and_high_hostility_are_hard_blocks():
    low = decide(trust=29, concern=1.0, interesting=True)
    assert low["decision"] == "blocked"
    assert low["reason_code"] == "relationship_low_trust"
    assert low["trigger_weight"] == 0

    hostile = decide(hostility=60, concern=1.0, interesting=True)
    assert hostile["decision"] == "blocked"
    assert hostile["reason_code"] == "relationship_high_hostility"


def test_intermediate_trust_or_hostility_is_pending_and_never_llm_allowed():
    trust_pending = decide(trust=59, depth=80, concern=1.0, interesting=True)
    assert trust_pending["decision"] == "pending"
    assert trust_pending["reason_code"] == "relationship_trust_pending"
    assert trust_pending["fail_closed"] is True

    hostility_pending = decide(hostility=30, concern=1.0, interesting=True)
    assert hostility_pending["decision"] == "pending"
    assert hostility_pending["reason_code"] == "relationship_hostility_pending"


def test_multi_subject_uses_strictest_trust_hostility_and_depth():
    result = evaluate_proactive_policy(
        [
            snapshot("qq:user:u1", trust=80, hostility=2, depth=70),
            snapshot("qq:user:u2", trust=65, hostility=29, depth=25),
        ],
        concern_score=0.4,
        is_interesting=True,
    )
    assert result["decision"] == "allow_llm"
    assert result["effective_trust"] == 65
    assert result["effective_hostility"] == 29
    assert result["effective_depth"] == 25
    assert result["subjects"] == ["qq:user:u1", "qq:user:u2"]


def test_depth_and_concern_table_for_ambient_interjection():
    assert decide(depth=19.99, concern=0.70)["decision"] == "allow_llm"
    assert decide(depth=19.99, concern=0.69)["reason_code"] == "low_depth_concern_insufficient"

    medium = decide(depth=20, concern=0.30, interesting=True)
    assert (medium["decision"], medium["trigger_weight"], medium["reason_code"]) == (
        "allow_llm",
        0.65,
        "medium_depth_interest_with_concern",
    )
    medium_strong = decide(depth=49.99, concern=0.70, interesting=False)
    assert (medium_strong["decision"], medium_strong["trigger_weight"]) == ("allow_llm", 0.70)
    assert decide(depth=20, concern=0.29, interesting=True)["decision"] == "no_trigger"

    high_interest = decide(depth=50, concern=0.0, interesting=True)
    assert (high_interest["decision"], high_interest["trigger_weight"]) == ("allow_llm", 0.65)
    high_concern = decide(depth=50, concern=0.70, interesting=False)
    assert (high_concern["decision"], high_concern["trigger_weight"]) == ("allow_llm", 0.80)


def test_concern_followup_uses_its_own_depth_thresholds():
    assert decide(
        depth=19.99,
        concern=0.69,
        behavior=BEHAVIOR_CONCERN_FOLLOWUP,
    )["decision"] == "no_trigger"
    low = decide(depth=19.99, concern=0.70, behavior=BEHAVIOR_CONCERN_FOLLOWUP)
    assert (low["decision"], low["trigger_weight"]) == ("allow_llm", 0.60)
    medium = decide(depth=20, concern=0.30, behavior=BEHAVIOR_CONCERN_FOLLOWUP)
    assert (medium["decision"], medium["trigger_weight"]) == ("allow_llm", 0.72)
    high = decide(depth=50, concern=0.70, behavior=BEHAVIOR_CONCERN_FOLLOWUP)
    assert (high["decision"], high["trigger_weight"]) == ("allow_llm", 0.88)


def test_missing_formal_relationship_dimensions_fail_closed():
    result = evaluate_proactive_policy(
        [RelationshipSnapshot("qq:user:u1", {"trust": 80, "hostility": 1}, revision=1)],
        concern_score=1.0,
        is_interesting=True,
    )
    assert result["decision"] == "blocked"
    assert result["reason_code"] == "relationship_unknown"


def test_relationship_guidance_distinguishes_trusted_hostile_and_unknown():
    trusted = relationship_behavior_guidance([snapshot(trust=80, hostility=2, fun=50, depth=60)])
    assert trusted["mode"] == "trusted"
    assert trusted["playfulness"] == "light"
    assert trusted["continuity"] == "recent_shared_experiences_only"

    hostile = relationship_behavior_guidance([snapshot(trust=80, hostility=70)])
    assert hostile["mode"] == "deescalate"
    assert hostile["playfulness"] == "none"

    unknown = relationship_behavior_guidance([])
    assert unknown["mode"] == "cautious"
    assert unknown["openness"] == "minimal"
