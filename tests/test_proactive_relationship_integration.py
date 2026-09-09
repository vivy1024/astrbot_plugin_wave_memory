import asyncio
import sqlite3
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.meta_thinking import MetaThinking
from services.proactive_policy import evaluate_proactive_policy


class Completion:
    def __init__(self, text):
        self.completion_text = text


class FakeLLM:
    def __init__(self, text="内心：可以补充\n行动：主动插话\n兴趣更新：不变"):
        self.text = text
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        self.calls.append({"prompt": prompt or "", "system_prompt": system_prompt or ""})
        return Completion(self.text)


def make_scope(subject="qq:user:u1"):
    return RuntimeScope(
        bot_id="yushu",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id=subject,
    )


def make_meta():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    meta = MetaThinking(
        db=SimpleNamespace(conn=conn),
        context=None,
        bot_qq_id="bot-qq",
        bot_qq_ids=["bot-qq"],
        bot_names={"bot-qq": "测试 Bot"},
        bot_db_ids={"bot-qq": "yushu"},
        config={"enabled": True, "proactive_enabled": True, "silent_hours_start": 23, "silent_hours_end": 23},
    )
    return meta


def test_metathinking_never_calls_llm_for_relationship_pending():
    meta = make_meta()
    llm = FakeLLM()
    meta.llm = llm
    blocked = {
        "policy_version": "relationship-proactive-v1",
        "decision": "pending",
        "reason_code": "relationship_trust_pending",
    }
    result = asyncio.run(
        meta.should_proactive(
            "g1",
            ["user: hello"],
            relationship_policy=blocked,
            scope_key="yushu:group:qq:group:g1",
        )
    )
    assert result["action"] == "不说"
    assert result["reason_code"] == "relationship_trust_pending"
    assert llm.calls == []


def test_metathinking_passes_relationship_guidance_only_after_allow_gate():
    meta = make_meta()
    llm = FakeLLM()
    meta.llm = llm
    policy = {
        "policy_version": "relationship-proactive-v1",
        "decision": "allow_llm",
        "reason_code": "high_depth_interest",
        "behavior_type": "ambient_interjection",
        "trigger_weight": 0.65,
        "effective_trust": 75,
        "effective_hostility": 5,
        "effective_depth": 60,
        "concern_score": 0.0,
        "is_interesting": True,
        "subjects": ["qq:user:u1"],
        "relationship_revisions": {"qq:user:u1": 3},
    }
    result = asyncio.run(
        meta.should_proactive(
            "g1",
            ["user: topic"],
            relationship_policy=policy,
            scope_key="yushu:group:qq:group:g1",
            proactive_interval_seconds=0,
            proactive_max_per_hour=3,
        )
    )
    assert result["action"] == "主动插话"
    assert result["relationship_revisions"] == {"qq:user:u1": 3}
    assert llm.calls
    assert "relationship-proactive-v1" in llm.calls[0]["prompt"]
    assert "只参与安全公开话题" not in llm.calls[0]["prompt"]
    assert '"mode": "trusted"' not in llm.calls[0]["prompt"]


def test_formal_repository_timeline_audit_uses_group_scope_and_evidence(tmp_path):
    manager = ConnectionManager(str(tmp_path / "proactive-audit.db"))
    try:
        ensure_scoped_soul_schema(manager)
        repo = ScopedSoulRepository(manager)
        scope = make_scope()
        policy = evaluate_proactive_policy(
            [
                __import__("services.belief_gating", fromlist=["RelationshipSnapshot"]).RelationshipSnapshot(
                    "qq:user:u1",
                    {"familiarity": 40, "trust": 70, "fun": 40, "hostility": 5, "depth": 55},
                    revision=7,
                )
            ],
            concern_score=0.4,
            is_interesting=True,
        )
        with manager.write_transaction() as connection:
            event_id = repo.add_timeline_event(
                RuntimeScope(
                    bot_id=scope.bot_id,
                    visibility=scope.visibility,
                    session=scope.session,
                    subject_principal_id=None,
                ),
                event_summary="主动补充一个公开话题",
                emotional_weight=policy["trigger_weight"],
                event_type="proactive.interjection",
                evidence=[
                    {"kind": "proactive_policy", "policy_version": policy["policy_version"]},
                    {"kind": "relationship_snapshot", "subject_principal_id": "qq:user:u1", "revision": 7},
                    {"kind": "source_memory", "memory_id": 12, "timestamp": 100.0, "role": "primary"},
                ],
                connection=connection,
            )
        assert event_id > 0
        row = manager.execute_read(
            "SELECT subject_principal_id, event_type, emotional_weight, evidence FROM scoped_soul_timeline WHERE id=?",
            (event_id,),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "proactive.interjection"
        assert row[2] == policy["trigger_weight"]
        assert '"policy_version":"relationship-proactive-v1"' in row[3]
        assert '"revision":7' in row[3]
    finally:
        manager.close()
