"""Evidence-v1 confidence and scoped Belief observation regression tests."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None)
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.belief_confidence import (
    ACTIVATION_MIN_CONFIDENCE,
    ACTIVATION_MIN_SUPPORT_WINDOWS,
    POLICY_VERSION,
    calculate_confidence,
)
from services.belief_engine import BeliefEngine
from services.belief_lifecycle import BeliefLifecycleService


class _Completion:
    def __init__(self, completion_text: str):
        self.completion_text = completion_text


class _BeliefLLM:
    def __init__(self, outputs: list[dict]):
        self.outputs = list(outputs)

    async def text_chat(self, **_kwargs):
        return _Completion(json.dumps([self.outputs.pop(0)], ensure_ascii=False))


def _scope(bot_id: str = "bot-alpha", group_id: str = "group-1") -> RuntimeScope:
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(f"qq:group:{group_id}", "qq", "group", group_id),
    )


def _add_message(db: WaveMemoryDB, scope: RuntimeScope, *, content: str, sender_id: str, timestamp: float) -> int:
    return db.add_memory(
        scope.session.conversation_id,
        content,
        sender_id=sender_id,
        sender_name=sender_id,
        timestamp=timestamp,
        scope=scope,
    )


def _tag(db: WaveMemoryDB, scope: RuntimeScope, memory_id: int) -> None:
    tag_id = db.upsert_scoped_tag(scope, name=f"证据-{memory_id}")
    db.link_scoped_memory_tag(scope, memory_id=memory_id, tag_id=tag_id)


def test_confidence_uses_independent_windows_and_challenge_evidence():
    first = {
        "polarity": "support", "window_key": "window-1", "memory_ids": [1],
        "participants": ["u1"], "window_started_at": 1_000.0, "window_ended_at": 1_000.0,
    }
    one = calculate_confidence([first], now=1_000.0)
    assert one["policy_version"] == POLICY_VERSION
    assert one["summary"]["support_windows"] == 1
    assert one["components"]["confidence"] < ACTIVATION_MIN_CONFIDENCE
    assert not one["activation_eligible"]

    second = {
        "polarity": "support", "window_key": "window-2", "memory_ids": [2],
        "participants": ["u2"], "window_started_at": 1_000.0 + 86_400, "window_ended_at": 1_000.0 + 86_400,
    }
    two = calculate_confidence([first, second], now=1_000.0 + 86_400)
    assert two["summary"]["support_windows"] >= ACTIVATION_MIN_SUPPORT_WINDOWS
    assert two["components"]["confidence"] >= ACTIVATION_MIN_CONFIDENCE
    assert two["activation_eligible"]

    repeated = calculate_confidence([first, second, second], now=1_000.0 + 86_400)
    assert repeated["summary"]["support_windows"] == two["summary"]["support_windows"]
    assert repeated["summary"]["support_messages"] == two["summary"]["support_messages"]
    assert repeated["components"]["confidence"] == two["components"]["confidence"]

    challenge = {
        "polarity": "challenge", "window_key": "window-3", "memory_ids": [3],
        "participants": ["u3"], "window_started_at": 1_000.0 + 172_800, "window_ended_at": 1_000.0 + 172_800,
    }
    challenged = calculate_confidence([first, second, challenge], now=1_000.0 + 172_800)
    assert challenged["summary"]["challenge_windows"] == 1
    assert challenged["components"]["consistency"] < two["components"]["consistency"]
    assert challenged["components"]["confidence"] < two["components"]["confidence"]


def test_belief_engine_persists_idempotent_evidence_observations_and_gates_activation(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        scope = _scope()
        first_id = _add_message(
            db, scope, content="我会先核实事实，再认真回应边界问题。", sender_id="u1", timestamp=1_000.0,
        )
        second_id = _add_message(
            db, scope, content="隔天我依旧先查清情况，不把别人当成工具。", sender_id="u2", timestamp=1_000.0 + 86_400,
        )
        challenge_id = _add_message(
            db, scope, content="这次我没有核实就下了结论，后来发现自己错了。", sender_id="u3", timestamp=1_000.0 + 172_800,
        )
        for memory_id in (first_id, second_id, challenge_id):
            _tag(db, scope, memory_id)

        content = "我会先核实事实再设定边界"
        llm = _BeliefLLM([
            {
                "content": content,
                "type": "self_identity",
                "evidence_memory_ids": [first_id],
                "challenge_memory_ids": [],
                "match_id": None,
                "relation": "new",
                "challenges": [],
                "anchor_sentence": "我会先核实事实",
            },
            {
                "content": content,
                "type": "self_identity",
                "evidence_memory_ids": [second_id],
                "challenge_memory_ids": [],
                "match_id": None,
                "relation": "new",
                "challenges": [],
                "anchor_sentence": "先查清情况",
            },
            {
                "content": content,
                "type": "self_identity",
                "evidence_memory_ids": [second_id],
                "challenge_memory_ids": [],
                "match_id": None,
                "relation": "new",
                "challenges": [],
                "anchor_sentence": "先查清情况",
            },
            {
                "content": "我有时会因未核实而误判",
                "type": "self_identity",
                "evidence_memory_ids": [challenge_id],
                "challenge_memory_ids": [challenge_id],
                "match_id": None,
                "relation": "challenge",
                "challenges": [1],
                "anchor_sentence": "没有核实就下了结论",
            },
        ])
        engine = BeliefEngine(db, llm, bot_id=scope.bot_id)
        summary = "群聊中反复讨论如何在回应他人前先核实事实与边界。"

        created = asyncio.run(engine.extract_from_summary(summary, scope, source_memory_ids=[first_id]))
        assert len(created) == 1
        belief = db.list_scoped_beliefs(scope)[0]
        assert belief["status"] == "pending"
        assert belief["provenance"]["confidence_policy_version"] == POLICY_VERSION
        assert belief["provenance"]["confidence_evidence"]["support_windows"] == 1
        assert belief["strength"] < ACTIVATION_MIN_CONFIDENCE
        with __import__("pytest").raises(ValueError, match="belief_evidence_incomplete"):
            BeliefLifecycleService(db.scoped_knowledge).transition(scope, belief["id"], "approve")

        asyncio.run(engine.extract_from_summary(summary, scope, source_memory_ids=[second_id]))
        belief = db.list_scoped_beliefs(scope)[0]
        observations = db.list_scoped_belief_observations(scope, belief_id=belief["id"])
        assert len(observations) == 2
        assert belief["provenance"]["confidence_evidence"]["support_windows"] == 2
        assert belief["provenance"]["activation_eligible"]
        assert belief["strength"] >= ACTIVATION_MIN_CONFIDENCE

        # 同一 consolidation window 重试覆写观察而不重复加分。
        stable_strength = belief["strength"]
        asyncio.run(engine.extract_from_summary(summary, scope, source_memory_ids=[second_id]))
        belief = db.list_scoped_beliefs(scope)[0]
        assert len(db.list_scoped_belief_observations(scope, belief_id=belief["id"])) == 2
        assert belief["strength"] == stable_strength

        target_id = belief["id"]
        approved = BeliefLifecycleService(db.scoped_knowledge).transition(scope, target_id, "approve")
        assert approved["status"] == "active"
        assert content in engine.get_injection(scope)

        asyncio.run(engine.extract_from_summary(summary, scope, source_memory_ids=[challenge_id]))
        target_after = db.get_scoped_belief(scope, target_id)
        assert target_after["status"] == "active"
        assert target_after["provenance"]["confidence_evidence"]["challenge_windows"] == 0
        candidates = [
            row for row in db.list_scoped_beliefs(scope)
            if isinstance(row.get("provenance"), dict)
            and isinstance(row["provenance"].get("candidate"), dict)
            and row["provenance"]["candidate"].get("relation") == "challenge"
        ]
        assert len(candidates) == 1
        assert candidates[0]["status"] == "pending"
        assert candidates[0]["provenance"]["gating"]["reason_code"] == "relationship_unknown"
    finally:
        db.close()
