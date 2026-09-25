from plugin_source import plugin_source_text
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef


def _scope(bot_id="yushu", group_id="group-1", user_id="user-1"):
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(
            id=f"test:group:{group_id}",
            platform_id="test",
            kind="group",
            conversation_id=group_id,
        ),
        subject_principal_id=f"test:user:{user_id}",
    )


def _ctx(scope=None):
    return SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope)))


class FakeFacade:
    def __init__(self):
        self.search_calls = []
        self.add_calls = []

    async def search_memories(self, query, k=5, session_id=None, persona_id=None, scope=None):
        self.search_calls.append({
            "query": query,
            "k": k,
            "session_id": session_id,
            "persona_id": persona_id,
            "scope": scope,
        })
        return [{"id": "7", "content": "苹果派记忆", "score": 0.9, "importance": 1.2, "metadata": {"source": "core"}}]

    async def add_memory(self, content, session_id=None, persona_id=None, importance=0.7, metadata=None, scope=None):
        self.add_calls.append({
            "content": content,
            "session_id": session_id,
            "persona_id": persona_id,
            "importance": importance,
            "metadata": metadata,
            "scope": scope,
        })
        return "queued:abc123"


class LivingMemoryCompatToolsTest(unittest.TestCase):
    def test_build_alias_tools_respects_config_gate(self):
        from tools.livingmemory_compat_tools import build_livingmemory_compat_tools

        facade = FakeFacade()

        self.assertEqual(build_livingmemory_compat_tools(facade, enabled=False), [])
        tools = build_livingmemory_compat_tools(facade, enabled=True)

        self.assertEqual([tool.name for tool in tools], ["recall_long_term_memory", "memorize_long_term_memory"])

    def test_recall_alias_calls_facade_search_and_returns_json_results(self):
        from tools.livingmemory_compat_tools import RecallLongTermMemoryTool

        facade = FakeFacade()
        tool = RecallLongTermMemoryTool(memory_engine=facade)

        scope = _scope()
        payload = json.loads(asyncio.run(tool.call(
            _ctx(scope), query="苹果派", k=3, session_id="group-1", persona_id="yushu"
        )))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["results"][0]["id"], "7")
        self.assertEqual(len(facade.search_calls), 1)
        self.assertEqual(facade.search_calls[0]["query"], "苹果派")
        self.assertEqual(facade.search_calls[0]["session_id"], "group-1")
        self.assertEqual(facade.search_calls[0]["persona_id"], "yushu")
        self.assertIs(facade.search_calls[0]["scope"], scope)

    def test_memorize_alias_calls_facade_add_and_returns_queued_id(self):
        from tools.livingmemory_compat_tools import MemorizeLongTermMemoryTool

        facade = FakeFacade()
        tool = MemorizeLongTermMemoryTool(memory_engine=facade)

        scope = _scope()
        payload = json.loads(asyncio.run(tool.call(
            _ctx(scope),
            content="用户喜欢苹果派",
            session_id="group-1",
            persona_id="yushu",
            importance=0.8,
            metadata={"origin": "chatplus"},
        )))

        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["memory_id"], "queued:abc123")
        self.assertEqual(facade.add_calls[0]["metadata"]["origin"], "chatplus")
        self.assertEqual(facade.add_calls[0]["metadata"]["origin_kind"], "livingmemory_compat_tool")
        self.assertIs(facade.add_calls[0]["scope"], scope)

    def test_memorize_alias_rejects_missing_or_mismatched_scope(self):
        from tools.livingmemory_compat_tools import MemorizeLongTermMemoryTool

        facade = FakeFacade()
        tool = MemorizeLongTermMemoryTool(memory_engine=facade)
        missing = json.loads(asyncio.run(tool.call(
            None,
            content="不能写到伪群",
        )))
        self.assertEqual(missing["status"], "error")
        self.assertIn("scope_required", missing["message"])

        bad_session = json.loads(asyncio.run(tool.call(
            _ctx(_scope()),
            content="不能跨会话",
            session_id="another-group",
        )))
        self.assertEqual(bad_session["status"], "error")
        self.assertIn("session_id", bad_session["message"])

        bad_persona = json.loads(asyncio.run(tool.call(
            _ctx(_scope()),
            content="不能跨 Bot",
            persona_id="baizz",
        )))
        self.assertEqual(bad_persona["status"], "error")
        self.assertIn("persona_id", bad_persona["message"])
        self.assertEqual(facade.add_calls, [])

    def test_facade_requires_scope_and_never_uses_global_default_session(self):
        from services.compat.livingmemory_facade import WaveMemoryLivingMemoryFacade

        class Writer:
            def __init__(self):
                self.items = []

            async def enqueue(self, item):
                self.items.append(dict(item))

        writer = Writer()
        facade = WaveMemoryLivingMemoryFacade(writer=writer, now=lambda: 100.0)
        self.assertEqual(asyncio.run(facade.add_memory("无 Scope")), "")
        self.assertEqual(facade.last_error, "scope_required")

        scope = _scope()
        queued_id = asyncio.run(facade.add_memory(
            "作用域记忆",
            scope=scope,
            session_id="group-1",
            persona_id="yushu",
        ))
        self.assertTrue(queued_id.startswith("queued:"))
        self.assertEqual(len(writer.items), 1)
        item = writer.items[0]
        self.assertIs(item["scope"], scope)
        self.assertEqual(item["group_id"], "group-1")
        self.assertEqual(item["sender_id"], "user-1")
        self.assertEqual(item["metadata"]["canonical_session_id"], "test:group:group-1")
        self.assertEqual(asyncio.run(facade.add_memory(
            "跨会话", scope=scope, session_id="another-group",
        )), "")
        self.assertEqual(facade.last_error, "scope_session_mismatch")

    def test_alias_tools_return_friendly_error_when_facade_missing(self):
        from tools.livingmemory_compat_tools import RecallLongTermMemoryTool, MemorizeLongTermMemoryTool

        recall = RecallLongTermMemoryTool(memory_engine=None)
        memorize = MemorizeLongTermMemoryTool(memory_engine=None)

        self.assertIn("未初始化", asyncio.run(recall.call(None, query="苹果派")))
        self.assertIn("未初始化", asyncio.run(memorize.call(None, content="用户喜欢苹果派")))

    def test_main_source_registers_alias_tools_without_default_spoofing(self):
        source = plugin_source_text()

        self.assertIn("build_livingmemory_compat_tools", source)
        self.assertIn("self.livingmemory_alias_tools_registered", source)
        self.assertIn("*livingmemory_alias_tools", source)


if __name__ == "__main__":
    unittest.main()
