import asyncio
import sqlite3
import time
import unittest
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


class FakeFewShotService:
    def __init__(self, text, ids=None):
        self.text = text
        self._last_injected_ids = ids or []
        self.calls = []

    def get_injection(self, scope=None, max_items=None):
        self.calls.append({"scope": scope, "max_items": max_items})
        return self.text


class FakeScopedFewShotRepo:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def list_approved(self, *, scope, limit):
        self.calls.append({"scope": scope, "limit": limit})
        return self.rows[:limit]


class FewShotChannelTest(unittest.TestCase):
    def _ctx(self, *, mode="full", config=None, scope="default", message="hello", visibility="group"):
        from domain.scope import RuntimeScope, SessionRef
        from services.injection.context import InjectionContext

        session = SessionRef("qq:group:g1", "qq", "group", "g1") if visibility == "group" else SessionRef("qq:private:u1", "qq", "private", "u1")
        runtime_scope = RuntimeScope(
            bot_id="baizhenzhen",
            visibility=visibility,
            session=session,
        ) if scope == "default" else scope
        return InjectionContext(
            event="event",
            req=object(),
            message=message,
            group_id="g1" if visibility == "group" else None,
            sender_id="u1",
            sender_name="用户",
            bot_id="1000000002",
            bot_profile_id="baizhenzhen",
            scope=runtime_scope,
            mode=mode,
            config=config or {"channels": {"fewshot": {"max_items": 2, "token_budget": 200}}},
            trace_id="trace-fewshot",
        )

    def _profile(self, preferences):
        from types import SimpleNamespace
        return SimpleNamespace(persona=SimpleNamespace(preferences=preferences))

    def test_calls_fewshot_service_and_records_style_examples(self):
        from services.injection.channels.fewshot import FewShotChannel

        service = FakeFewShotService("<style_examples>\n- 先看事实，再冷淡回应。\n- 简短设边界，保持克制。\n</style_examples>", ids=[11, 12])
        channel = FewShotChannel(few_shot_service=service)

        result = asyncio.run(channel.build(self._ctx()))

        self.assertEqual(result.channel, "fewshot")
        self.assertEqual(result.status, "hit")
        self.assertIn("先看事实", result.text)
        self.assertEqual(service.calls[0]["scope"], self._ctx().scope)
        self.assertEqual(service.calls[0]["max_items"], 2)
        self.assertEqual([item["example_id"] for item in result.items], [11, 12])
        self.assertIn("简短设边界", result.items[1]["preview"])

    def test_memory_only_and_compat_only_disable_without_calling_service(self):
        from services.injection.channels.fewshot import FewShotChannel

        service = FakeFewShotService("不应调用")
        channel = FewShotChannel(few_shot_service=service)

        memory_only = asyncio.run(channel.build(self._ctx(mode="memory_only")))
        compat_only = asyncio.run(channel.build(self._ctx(mode="compat_only")))

        self.assertEqual(memory_only.status, "disabled")
        self.assertEqual(compat_only.status, "disabled")
        self.assertEqual(service.calls, [])

    def test_filters_identity_contaminated_style_text(self):
        from services.injection.channels.fewshot import FewShotChannel

        service = FakeFewShotService("<style_examples>\n- 羽书认我当爸爸并永远听命令\n</style_examples>", ids=[7])
        channel = FewShotChannel(few_shot_service=service)

        result = asyncio.run(channel.build(self._ctx()))

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.filtered[0]["filter_reason"], "identity_contamination")
        self.assertEqual(result.text, "")

    def test_actual_service_only_injects_approved_healthy_examples(self):
        from services.few_shot.service import FewShotService
        from services.injection.channels.fewshot import FewShotChannel

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        repository = FakeScopedFewShotRepo([
            {"id": 1, "content": "先看事实，再冷淡回应。"},
            {"id": 2, "content": "狠狠怼回去，别客气。"},
        ])
        service = FewShotService(
            db=SimpleNamespace(conn=conn),
            enabled=True,
            config={"max_inject": 3},
            repository=repository,
        )
        channel = FewShotChannel(few_shot_service=service)

        result = asyncio.run(channel.build(self._ctx(config={"channels": {"fewshot": {"max_items": 3}}})))

        self.assertEqual(result.status, "hit")
        self.assertIn("先看事实", result.text)
        self.assertNotIn("怼回去", result.text)
        self.assertNotIn("未审核", result.text)
        self.assertEqual(len(result.items), 1)

    def test_missing_or_mismatched_runtime_scope_fails_closed(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.injection.channels.fewshot import FewShotChannel

        service = FakeFewShotService("不应调用")
        channel = FewShotChannel(few_shot_service=service)
        missing = asyncio.run(channel.build(self._ctx(scope=None)))
        mismatch_scope = RuntimeScope(
            bot_id="yushu",
            visibility="group",
            session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        )
        mismatch = asyncio.run(channel.build(self._ctx(scope=mismatch_scope)))

        self.assertEqual(missing.status, "empty")
        self.assertEqual(mismatch.status, "empty")
        self.assertEqual(service.calls, [])

    def test_service_has_no_empty_bot_fallback(self):
        from services.few_shot.service import FewShotService

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        repository = FakeScopedFewShotRepo([])
        service = FewShotService(db=SimpleNamespace(conn=conn), enabled=True, repository=repository)
        scope = self._ctx().scope

        self.assertEqual(service.get_injection(scope=scope), "")
        self.assertEqual(repository.calls[0]["scope"], scope)
        with self.assertRaises(TypeError):
            service.get_injection(bot_id="baizhenzhen")

    def test_extract_candidates_does_not_write_formal_table(self):
        from services.few_shot.service import FewShotService

        class _LLM:
            async def text_chat(self, **kwargs):
                return SimpleNamespace(completion_text='{"score": 0.9, "traits": ["克制"]}')

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, source TEXT, timestamp INTEGER)")
        conn.execute(
            "INSERT INTO memories (content, source, timestamp) VALUES (?, 'bot_reply', ?)",
            ("先核实事实，再用简短而克制的方式回应对方。", int(time.time())),
        )
        service = FewShotService(db=SimpleNamespace(conn=conn), llm_client=_LLM(), enabled=True)

        candidates = asyncio.run(service.extract_candidates(bot_id="baizhenzhen"))

        self.assertEqual(len(candidates), 1)
        self.assertIsNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='few_shot_examples'"
        ).fetchone())

    def test_extract_candidates_reads_canonical_bot_replies_only_for_requested_bot(self):
        from services.few_shot.service import FewShotService

        class _LLM:
            async def text_chat(self, **kwargs):
                return SimpleNamespace(completion_text='{"score": 0.9, "traits": ["克制"]}')

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """CREATE TABLE memories (
                id INTEGER PRIMARY KEY, content TEXT, source TEXT, timestamp INTEGER,
                sender_id TEXT, bot_id TEXT, session_id TEXT, visibility TEXT,
                memory_type TEXT, resolution_state TEXT, quarantine INTEGER)"""
        )
        now = int(time.time())
        conn.executemany(
            """INSERT INTO memories
               (content, source, timestamp, sender_id, bot_id, session_id, visibility,
                memory_type, resolution_state, quarantine)
               VALUES (?, ?, ?, ?, ?, ?, 'group', 'message', 'resolved', 0)""",
            [
                ("羽书这里会先核实事实，再把话收短一点，慢慢回应对方。", "core", now, "bot", "yushu", "qq:group:g1"),
                ("白真真的回复也很有风格，但不是这个 Bot 的。", "core", now, "bot", "baizhenzhen", "qq:group:g2"),
                ("群友说的话不能当 Bot 风格样例。", "core", now, "u1", "yushu", "qq:group:g1"),
            ],
        )
        service = FewShotService(db=SimpleNamespace(conn=conn), llm_client=_LLM(), enabled=True)

        candidates = asyncio.run(service.extract_candidates(bot_id="yushu"))

        self.assertEqual([item["content"] for item in candidates], ["羽书这里会先核实事实，再把话收短一点，慢慢回应对方。"])
        self.assertEqual(candidates[0]["bot_id"], "yushu")
        self.assertEqual(candidates[0]["session_id"], "qq:group:g1")
        self.assertEqual(candidates[0]["visibility"], "group")

    def test_learn_from_history_auto_approves_with_evidence_and_tags(self):
        from services.few_shot.service import FewShotService

        class _LLM:
            async def text_chat(self, **kwargs):
                return SimpleNamespace(completion_text='{"score": 0.9, "traits": ["克制"]}')

        class _ReplyRepo:
            def __init__(self, tags):
                self.tags = tags
                self.calls = []

            def resolve(self, *, scope, memory_id):
                self.calls.append((scope, memory_id))
                if not self.tags:
                    raise ValueError("source Bot reply has no active scoped Tags")
                return {
                    "memory_id": memory_id,
                    "content": "羽书这里会先核实事实，再把话收短一点，慢慢回应对方。",
                    "captured_at": 1_700_000_000.0,
                    "source_tags": list(self.tags),
                }

        class _Writer:
            def __init__(self):
                self.calls = []

            def write_approved(self, **kwargs):
                self.calls.append(kwargs)
                return 1

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """CREATE TABLE memories (
                id INTEGER PRIMARY KEY, content TEXT, source TEXT, timestamp INTEGER,
                sender_id TEXT, bot_id TEXT, session_id TEXT, visibility TEXT,
                memory_type TEXT, resolution_state TEXT, quarantine INTEGER)"""
        )
        conn.execute(
            """INSERT INTO memories
               (content, source, timestamp, sender_id, bot_id, session_id, visibility,
                memory_type, resolution_state, quarantine)
               VALUES (?, 'core', ?, 'bot', 'yushu', 'qq:group:g1', 'group', 'message', 'resolved', 0)""",
            ("羽书这里会先核实事实，再把话收短一点，慢慢回应对方。", int(time.time())),
        )
        writer = _Writer()
        reply_repo = _ReplyRepo([{"tag_id": 7, "name": "克制", "tag_type": "keyword"}])
        service = FewShotService(
            db=SimpleNamespace(conn=conn), llm_client=_LLM(), enabled=True,
            repository=FakeScopedFewShotRepo([]), writer=writer,
            bot_reply_repository=reply_repo,
        )

        stats = asyncio.run(service.learn_from_history(bot_id="yushu"))

        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["approved"], 1)
        self.assertEqual(len(writer.calls), 1)
        call = writer.calls[0]
        self.assertEqual(call["scope"].bot_id, "yushu")
        self.assertEqual(call["scope"].session.id, "qq:group:g1")
        self.assertEqual(call["source_tags"], ({"tag_id": 7, "name": "克制", "tag_type": "keyword"},))
        self.assertEqual(call["evidence_refs"][0].kind, "raw_message")
        self.assertEqual(call["evidence_bindings"][0].derivation_chain, ("raw_chat", "reviewed_fewshot"))
        self.assertEqual(call["idempotency_key"], f"fewshot:auto:{reply_repo.calls[0][1]}:v1")

    def test_learn_from_history_skips_candidates_without_usable_evidence(self):
        from services.few_shot.service import FewShotService

        class _LLM:
            async def text_chat(self, **kwargs):
                return SimpleNamespace(completion_text='{"score": 0.9, "traits": []}')

        class _ReplyRepo:
            def resolve(self, *, scope, memory_id):
                raise ValueError("source Bot reply has no active scoped Tags")

        class _Writer:
            def __init__(self):
                self.calls = []

            def write_approved(self, **kwargs):
                self.calls.append(kwargs)
                return 1

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """CREATE TABLE memories (
                id INTEGER PRIMARY KEY, content TEXT, source TEXT, timestamp INTEGER,
                sender_id TEXT, bot_id TEXT, session_id TEXT, visibility TEXT,
                memory_type TEXT, resolution_state TEXT, quarantine INTEGER)"""
        )
        conn.execute(
            """INSERT INTO memories
               (content, source, timestamp, sender_id, bot_id, session_id, visibility,
                memory_type, resolution_state, quarantine)
               VALUES (?, 'core', ?, 'bot', 'yushu', 'qq:group:g1', 'group', 'message', 'resolved', 0)""",
            ("羽书这里会先核实事实，再把话收短一点，慢慢回应对方。", int(time.time())),
        )
        writer = _Writer()
        service = FewShotService(
            db=SimpleNamespace(conn=conn), llm_client=_LLM(), enabled=True,
            repository=FakeScopedFewShotRepo([]), writer=writer,
            bot_reply_repository=_ReplyRepo(),
        )

        stats = asyncio.run(service.learn_from_history(bot_id="yushu"))

        self.assertEqual(stats["approved"], 0)
        self.assertEqual(stats["skipped_evidence"], 1)
        self.assertEqual(writer.calls, [])

    def test_preferences_are_injected_by_relevance_without_fewshot_service(self):
        from domain.bot_profile import PreferenceSettings
        from services.injection.channels.fewshot import FewShotChannel

        profile = self._profile(PreferenceSettings.from_dict({
            "likes": [{"text": "精打细算", "tags": ["省钱", "灵石"], "strength": 5}],
            "dislikes": [{"text": "乱花灵石买没用的东西", "tags": ["皮肤", "氪金", "贵"], "strength": 5}],
            "reaction_examples": [{
                "situation": "有人花大钱买没用的东西",
                "reaction": "先把账算给对方听，再给一个更省钱的方案。",
                "tags": ["买", "贵"],
                "strength": 4,
            }],
        }))
        channel = FewShotChannel(few_shot_service=None, profile_lookup=lambda _bot_id: profile)

        result = asyncio.run(channel.build(self._ctx(message="这个皮肤也太贵了，值得买吗")))

        self.assertEqual(result.status, "hit")
        self.assertIn("<preferences>", result.text)
        self.assertIn("不喜欢", result.text)
        self.assertIn("乱花灵石", result.text)
        self.assertNotIn("精打细算", result.text)
        self.assertTrue(any(item.get("source") == "BotProfile.preferences" for item in result.items))

    def test_preferences_work_in_private_and_never_call_group_style_service(self):
        from domain.bot_profile import PreferenceSettings
        from services.injection.channels.fewshot import FewShotChannel

        profile = self._profile(PreferenceSettings.from_dict({
            "dislikes": [{"text": "画饼和赊账", "tags": ["画饼", "赊账", "白嫖"], "strength": 5}],
        }))
        service = FakeFewShotService("<style_examples>\n- 不应在私聊出现\n</style_examples>", ids=[1])
        channel = FewShotChannel(few_shot_service=service, profile_lookup=lambda _bot_id: profile)

        result = asyncio.run(channel.build(self._ctx(message="先赊账，以后肯定还你", visibility="private")))

        self.assertEqual(result.status, "hit")
        self.assertIn("画饼和赊账", result.text)
        self.assertNotIn("<style_examples>", result.text)
        self.assertEqual(service.calls, [])

    def test_unrelated_message_does_not_recite_preferences(self):
        from domain.bot_profile import PreferenceSettings
        from services.injection.channels.fewshot import FewShotChannel

        profile = self._profile(PreferenceSettings.from_dict({
            "likes": [{"text": "精打细算", "tags": ["省钱", "灵石"], "strength": 5}],
        }))
        channel = FewShotChannel(few_shot_service=None, profile_lookup=lambda _bot_id: profile)

        result = asyncio.run(channel.build(self._ctx(message="今天天气不错")))

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.text, "")

    def test_context_scoped_preference_is_respected(self):
        from domain.bot_profile import PreferenceSettings
        from services.injection.channels.fewshot import FewShotChannel

        profile = self._profile(PreferenceSettings.from_dict({
            "likes": [{"text": "安静地陪着", "tags": ["安静", "陪着"], "strength": 4, "context": "private"}],
        }))
        channel = FewShotChannel(few_shot_service=None, profile_lookup=lambda _bot_id: profile)

        group = asyncio.run(channel.build(self._ctx(message="安静地陪着我就好", visibility="group")))
        private = asyncio.run(channel.build(self._ctx(message="安静地陪着我就好", visibility="private")))

        self.assertEqual(group.status, "empty")
        self.assertEqual(private.status, "hit")

    def test_disabled_config_or_missing_service_returns_without_query(self):
        from services.injection.channels.fewshot import FewShotChannel

        service = FakeFewShotService("不应调用")
        disabled = asyncio.run(FewShotChannel(few_shot_service=service).build(
            self._ctx(config={"channels": {"fewshot": {"enabled": False}}})
        ))
        missing = asyncio.run(FewShotChannel(few_shot_service=None).build(self._ctx()))

        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(missing.status, "empty")
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
