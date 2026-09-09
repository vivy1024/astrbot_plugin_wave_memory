from __future__ import annotations

import sys
import types
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from domain.scope import RuntimeScope, SessionRef
from services.belief_engine import BeliefEngine


class RelationshipRepo:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def list_relationships(self, scope, *, subject_principal_id=None):
        self.calls.append((scope, subject_principal_id))
        return [self.row] if self.row is not None else []


def scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )


def evidence():
    return {
        "confidence_policy_version": "evidence-v1",
        "tag_chain_status": "complete",
        "source_tags": [{"memory_id": 1, "tag_id": 1}],
        "evidence": {"memory_ids": [1], "support_memory_ids": [1], "challenge_memory_ids": []},
        "confidence_components": {"confidence": 0.8},
        "confidence_evidence": {"support_windows": 2},
        "activation_eligible": True,
    }


def active_beliefs():
    prov = evidence()
    return [
        {"id": 1, "content": "我会先核实事实再回应", "belief_type": "self_identity", "strength": 0.8, "status": "active", "provenance": prov},
        {"id": 2, "content": "u1 在边界问题上值得认真回应", "belief_type": "person_judgment", "strength": 0.8, "status": "active", "provenance": prov},
        {"id": 3, "content": "边界问题需要保持事实核实", "belief_type": "world_view", "strength": 0.8, "status": "active", "provenance": prov},
    ]


def relationship(*, trust: float, hostility: float, fun: float = 0):
    return {
        "subject_principal_id": "qq:user:u1",
        "revision": 4,
        "dimensions": {"trust": trust, "hostility": hostility, "fun": fun, "familiarity": 50, "depth": 30},
        "values": {},
    }


def test_trusted_subject_allows_person_judgment_and_safe_playfulness():
    repo = RelationshipRepo(relationship(trust=80, hostility=10, fun=50))
    db = SimpleNamespace(list_scoped_beliefs=lambda scope, status=None: active_beliefs())
    engine = BeliefEngine(db, None, bot_id="bot-alpha", soul_repository=repo)

    result = engine.get_injection_details(scope(), sender_id="u1", keywords=["边界"])

    assert "值得认真回应" in result["text"]
    assert "自然承接" not in result["text"]
    assert "轻度玩笑" not in result["text"]
    assert "<relationship_guidance>" not in result["text"]
    assert result["interaction_policy"]["mode"] == "trusted"
    assert repo.calls[0][1] == "qq:user:u1"


def test_hostility_suppresses_person_judgment_and_deescalates():
    repo = RelationshipRepo(relationship(trust=90, hostility=60, fun=80))
    db = SimpleNamespace(list_scoped_beliefs=lambda scope, status=None: active_beliefs())
    engine = BeliefEngine(db, None, bot_id="bot-alpha", soul_repository=repo)

    result = engine.get_injection_details(scope(), sender_id="u1", keywords=["边界"])

    assert "值得认真回应" not in result["text"]
    assert "礼貌" not in result["text"]
    assert "轻度玩笑" not in result["text"]
    assert "<relationship_guidance>" not in result["text"]
    assert result["interaction_policy"]["mode"] == "deescalate"


def test_unknown_subject_keeps_self_and_general_beliefs_but_not_person_judgment():
    repo = RelationshipRepo(None)
    db = SimpleNamespace(list_scoped_beliefs=lambda scope, status=None: active_beliefs())
    engine = BeliefEngine(db, None, bot_id="bot-alpha", soul_repository=repo)

    result = engine.get_injection_details(scope(), sender_id="u1", keywords=["边界"])

    assert "先核实事实" in result["text"]
    assert "边界问题需要保持事实核实" in result["text"]
    assert "值得认真回应" not in result["text"]
    assert "<relationship_guidance>" not in result["text"]
    assert result["interaction_policy"]["reason_code"] == "relationship_unknown"
