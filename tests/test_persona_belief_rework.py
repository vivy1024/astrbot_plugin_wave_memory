import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


class _FakeLogger:
    def debug(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass


try:
    import astrbot.api  # type: ignore
except Exception:
    import sys
    import types
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = _FakeLogger()
    sys.modules.setdefault("astrbot", astrbot_mod)
    sys.modules["astrbot.api"] = api_mod


class _Completion:
    def __init__(self, text):
        self.completion_text = text


class _FakeLLM:
    def __init__(self):
        self.prompts = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        self.prompts.append({"prompt": prompt or "", "system_prompt": system_prompt or ""})
        return _Completion(
            "内心：先保持边界\n"
            "行动：回复\n"
            "语气：正常\n"
            "好感度：0\n"
            "印象：正常交流\n"
            "情绪：0"
        )


class _FakeBeliefEngine:
    def __init__(self):
        self.calls = []
        self.bot_id = "baizhenzhen"

    def get_injection(self, scope, sender_id=None, keywords=None):
        self.calls.append({"scope": scope, "sender_id": sender_id, "keywords": keywords})
        return "<beliefs>\n- 觉得：白真真不喜欢被当成攻击工具\n</beliefs>"


class _FakeQueryEngine:
    def __init__(self):
        self.calls = []

    async def query(self, **kwargs):
        self.calls.append(kwargs)
        return []

    def format_injection(self, memories):
        return "\n".join(f"[记忆#{m['id']}] {m['content']}" for m in memories)


class _ScopedBeliefDB:
    def __init__(self):
        self.calls = []

    def list_scoped_beliefs(self, scope, *, status=None, limit=50):
        self.calls.append({"scope": scope, "status": status, "limit": limit})
        evidence_v1 = {
            "confidence_policy_version": "evidence-v1",
            "tag_chain_status": "complete",
            "source_tags": [{"memory_id": 1, "tag_id": 1}],
            "evidence": {"memory_ids": [1, 2], "support_memory_ids": [1, 2], "challenge_memory_ids": []},
            "confidence_components": {"confidence": 0.8},
            "confidence_evidence": {"support_windows": 2},
            "activation_eligible": True,
        }
        return [
            {"id": 1, "content": "我会先核实事实再设定边界", "belief_type": "self_identity", "strength": 0.8, "status": "active", "provenance": evidence_v1},
            {"id": 2, "content": "u1 在边界问题上值得认真回应", "belief_type": "person_judgment", "strength": 0.6, "status": "active", "provenance": evidence_v1},
            {"id": 3, "content": "legacy pending 不得注入", "belief_type": "world_view", "strength": 1.0, "status": "pending_legacy"},
        ]


class PersonaBeliefReworkTest(unittest.TestCase):
    def _connect(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "wave_memory.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(tmp.cleanup)
        self.addCleanup(conn.close)
        return conn

    def test_persona_composer_builds_layered_safe_context(self):
        from services.persona_composer import PersonaComposer

        conn = self._connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS book_experience_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            user_id TEXT,
            content TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            source_candidate_id INTEGER,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        conn.commit()
        now = time.time()
        conn.executemany(
            """INSERT INTO book_experience_episodes
               (bot_id, group_id, user_id, content, evidence_json, source_candidate_id,
                idempotency_key, created_at, updated_at)
               VALUES (?, ?, NULL, ?, '{}', NULL, ?, ?, ?)""",
            [
                ("baizhenzhen", "arc01_炼气高中期", "第一次在群里认真解释剑阵，不靠嘴臭压人。", "k1", now, now),
                ("baizhenzhen", "arc02_筑基大学期", "（猫耳朵一抖）这种身份污染经历不能注入。", "k2", now, now),
                ("baizhenzhen", "arc03_金丹大学期", "后来学会先判断事实，再给出克制回应。", "k3", now, now),
                ("otherbot", "arc01_炼气高中期", "别人的经历不能泄漏。", "k4", now, now),
            ],
        )
        conn.commit()

        composer = PersonaComposer(
            db=SimpleNamespace(conn=conn),
            belief_engine=_FakeBeliefEngine(),
            query_engine=_FakeQueryEngine(),
            few_shot_service=SimpleNamespace(get_injection=lambda bot_id="", max_items=None: "<style_examples>\n- 先看事实，再冷淡回应。\n</style_examples>"),
            bot_profiles={
                "1336495069": SimpleNamespace(
                    qq_id="1336495069",
                    name="白真真",
                    db_id="baizhenzhen",
                    aliases=["真真"],
                    exclude_sources=[],
                )
            },
        )

        from domain.scope import RuntimeScope, SessionRef

        scope = RuntimeScope(
            bot_id="baizhenzhen",
            visibility="group",
            session=SessionRef(
                id="qq:group:g1",
                platform_id="qq",
                kind="group",
                conversation_id="g1",
            ),
            subject_principal_id="qq:user:u1",
        )
        result = asyncio.run(composer.build_self_persona(
            bot_id="1336495069",
            group_id="g1",
            sender_id="u1",
            sender_name="芒果",
            message="你怎么看刚才那件事",
            recent_context=["芒果: 刚才争论有点乱"],
            scope=scope,
        ))

        self.assertIn("persona_block", result)
        self.assertIn("belief_block", result)
        self.assertIn("experience_block", result)
        self.assertIn("style_block", result)
        self.assertTrue(result["persona_block"].startswith("<self_persona>"))
        self.assertTrue(result["belief_block"].startswith("<beliefs>"))
        self.assertIn("白真真", result["persona_block"])
        self.assertIn("不喜欢被当成攻击工具", result["belief_block"])
        self.assertIn("认真解释剑阵", result["experience_block"])
        self.assertIn("先判断事实", result["experience_block"])
        self.assertNotIn("猫耳朵", result["experience_block"])
        self.assertNotIn("别人的经历", result["experience_block"])
        self.assertEqual(result["style_block"], "")
        self.assertEqual(set(result["debug"]["experience_ids"]), {1, 3})

        other_group = RuntimeScope(
            bot_id="baizhenzhen",
            visibility="group",
            session=SessionRef(
                id="qq:group:g9",
                platform_id="qq",
                kind="group",
                conversation_id="g9",
            ),
            subject_principal_id="qq:user:u9",
        )
        elsewhere = asyncio.run(composer.build_self_persona(
            bot_id="1336495069",
            group_id="g9",
            sender_id="u9",
            sender_name="路人",
            message="随便聊聊",
            recent_context=[],
            scope=other_group,
        ))
        self.assertIn("认真解释剑阵", elsewhere["experience_block"])

    def test_belief_engine_requires_group_scope_and_reads_only_scoped_active_beliefs(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.belief_engine import BeliefEngine

        db = _ScopedBeliefDB()
        engine = BeliefEngine(db=db, llm_client=None, bot_id="legacy_bot")
        group_scope = RuntimeScope(
            bot_id="baizhenzhen",
            visibility="group",
            session=SessionRef(
                id="qq:group:g1",
                platform_id="qq",
                kind="group",
                conversation_id="g1",
            ),
            subject_principal_id="qq:user:u1",
        )

        text = engine.get_injection(group_scope, sender_id="u1", keywords=["边界"])

        self.assertIn("核实事实", text)
        self.assertIn("值得认真回应", text)
        self.assertNotIn("legacy pending", text)
        self.assertEqual(db.calls, [{"scope": group_scope, "status": "active", "limit": 50}])
        self.assertEqual(engine.get_injection(None), "")
        private_scope = RuntimeScope(
            bot_id="baizhenzhen",
            visibility="private",
            session=SessionRef(
                id="qq:private:u1",
                platform_id="qq",
                kind="private",
                conversation_id="u1",
            ),
            subject_principal_id="qq:user:u1",
        )
        self.assertEqual(engine.get_injection(private_scope), "")
        self.assertEqual(len(db.calls), 1)

    def test_metathinking_extreme_attack_uses_boundary_not_attack_back(self):
        from services.meta_thinking import MetaThinking

        meta = MetaThinking(
            db=SimpleNamespace(conn=self._connect()),
            context=None,
            bot_qq_id="1336495069",
            bot_qq_ids=["1336495069"],
            bot_names={"1336495069": "白真真"},
            bot_db_ids={"1336495069": "baizhenzhen"},
            config={"enabled": True},
        )
        meta.llm = _FakeLLM()

        result = asyncio.run(meta.should_respond(
            sender_id="u1",
            group_id="g1",
            nickname="芒果",
            message="@白真真 nmsl",
            is_at_bot=True,
            context_messages=[],
            bot_id="1336495069",
            system_prompt="你是白真真。保持克制。",
        ))

        self.assertNotEqual(result.get("action"), "attack_back")
        self.assertIn(result.get("action"), {"reply", "short_reply", "ignore"})
        prompt_text = "\n".join(p["prompt"] for p in meta.llm.prompts)
        self.assertNotIn("怼回去", prompt_text)
        self.assertNotIn("不要客气", prompt_text)

    def test_metathinking_without_system_prompt_uses_neutral_prompt_not_hardcoded_persona(self):
        from services.meta_thinking import MetaThinking

        meta = MetaThinking(
            db=SimpleNamespace(conn=self._connect()),
            context=None,
            bot_qq_id="1336495069",
            bot_qq_ids=["1336495069"],
            bot_names={"1336495069": "白真真"},
            bot_db_ids={"1336495069": "baizhenzhen"},
            config={"enabled": True},
        )
        meta.llm = _FakeLLM()

        asyncio.run(meta.should_respond(
            sender_id="u1",
            group_id="g1",
            nickname="芒果",
            message="你在吗",
            is_at_bot=True,
            context_messages=[],
            bot_id="1336495069",
            system_prompt=None,
        ))

        prompt_text = "\n".join(p["prompt"] for p in meta.llm.prompts)
        self.assertNotIn("你是一个有感情的人。你有喜好、有脾气、有记忆", prompt_text)
        self.assertNotIn("怼回去", prompt_text)

    def test_few_shot_filters_aggressive_or_identity_contaminated_examples(self):
        from services.few_shot.service import FewShotService

        from domain.scope import RuntimeScope, SessionRef

        class _Repo:
            def list_approved(self, *, scope, limit):
                return [
                    {"id": 1, "content": "先看事实，再简短回应。"},
                    {"id": 2, "content": "这种人就该狠狠怼回去，别客气。"},
                    {"id": 3, "content": "（猫耳朵一抖）本真君才不是猫娘喵。"},
                ][:limit]

        conn = self._connect()
        service = FewShotService(
            db=SimpleNamespace(conn=conn),
            enabled=True,
            config={"max_inject": 3},
            repository=_Repo(),
        )
        scope = RuntimeScope(
            bot_id="bot-profile-main",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
        )

        injection = service.get_injection(scope=scope, max_items=3)

        self.assertIn("先看事实", injection)
        self.assertNotIn("怼回去", injection)
        self.assertNotIn("猫耳朵", injection)
        self.assertEqual(service._last_injected_ids, [1])


if __name__ == "__main__":
    unittest.main()
