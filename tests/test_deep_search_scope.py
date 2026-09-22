import asyncio
import sqlite3
import sys
import types
import unittest
from types import SimpleNamespace


if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

if "astrbot.core.agent.tool" not in sys.modules:
    tool = types.ModuleType("astrbot.core.agent.tool")
    run_context = types.ModuleType("astrbot.core.agent.run_context")
    agent_context = types.ModuleType("astrbot.core.astr_agent_context")

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    class _ContextWrapper:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    tool.FunctionTool = _FunctionTool
    run_context.ContextWrapper = _ContextWrapper
    agent_context.AstrAgentContext = type("AstrAgentContext", (), {})
    sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    sys.modules.setdefault("astrbot.core.agent", types.ModuleType("astrbot.core.agent"))
    sys.modules["astrbot.core.agent.tool"] = tool
    sys.modules["astrbot.core.agent.run_context"] = run_context
    sys.modules["astrbot.core.astr_agent_context"] = agent_context


class _Db:
    closed = False

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """CREATE TABLE memories (
                id INTEGER PRIMARY KEY, sender_name TEXT, content TEXT, timestamp REAL,
                bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT,
                quarantine INTEGER, group_id TEXT, memory_type TEXT, source TEXT
            )"""
        )
        self.conn.execute("CREATE VIRTUAL TABLE fts_memories USING fts5(content)")

    def add(
        self,
        memory_id,
        content,
        *,
        bot="yushu",
        session="qq:group:g1",
        quarantine=0,
        state="resolved",
        group=None,
        memory_type="message",
        source="live",
    ):
        group_id = group if group is not None else session.rsplit(":", 1)[-1]
        self.conn.execute(
            "INSERT INTO memories VALUES (?, '用户', ?, 1, ?, ?, 'group', ?, ?, ?, ?, ?)",
            (memory_id, content, bot, session, state, quarantine, group_id, memory_type, source),
        )
        self.conn.execute("INSERT INTO fts_memories(rowid, content) VALUES (?, ?)", (memory_id, content))
        self.conn.commit()


def _context(scope):
    event = SimpleNamespace(_wave_memory_runtime_scope=scope)
    return SimpleNamespace(context=SimpleNamespace(event=event))


class DeepSearchScopeTest(unittest.TestCase):
    def setUp(self):
        self.db = _Db()
        self.addCleanup(self.db.conn.close)
        self.db.add(10, "咖啡 命中消息")
        self.db.add(9, "同一会话上下文")
        self.db.add(11, "另一个 Bot 的相邻泄露", bot="bzz")
        self.db.add(12, "跨会话相邻泄露", session="qq:group:g2")
        self.db.add(13, "隔离的咖啡", quarantine=1)
        self.db.add(14, "legacy 咖啡", state="unresolved_legacy")

    @staticmethod
    def _scope():
        from domain.scope import RuntimeScope, SessionRef

        return RuntimeScope("yushu", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))

    def test_hits_and_context_window_are_scope_filtered(self):
        """Group-open read: same group any bot/session; other groups excluded."""
        from tools.deep_search import WaveMemoryDeepSearchTool

        # Historical display-name session must still hit in the same group.
        self.db.add(
            15,
            "历史编码 咖啡",
            bot="yushu",
            session="羽书:group:g1",
            group="g1",
            state="",
        )

        tool = WaveMemoryDeepSearchTool(db=self.db)
        result = asyncio.run(tool.call(_context(self._scope()), keywords="咖啡", window_size=3))

        self.assertIn("命中消息", result)
        self.assertIn("同一会话上下文", result)
        self.assertIn("另一个 Bot", result)  # same group
        self.assertIn("历史编码", result)
        self.assertIn("legacy", result)  # same group, unresolved still searchable
        self.assertNotIn("跨会话", result)
        self.assertNotIn("隔离", result)

    def test_missing_scope_fails_closed_before_querying(self):
        from tools.deep_search import WaveMemoryDeepSearchTool

        tool = WaveMemoryDeepSearchTool(db=self.db)
        result = asyncio.run(tool.call(_context(None), keywords="咖啡"))
        self.assertIn("已拒绝", result)

    def test_unified_memory_search_tool_with_context_window(self):
        from tools.memory_search import WaveMemorySearchTool

        tool = WaveMemorySearchTool(db=self.db, query_engine=None)
        result = asyncio.run(tool.call(_context(self._scope()), query="咖啡", include_context=True))

        self.assertIn("对话切片", result)
        self.assertIn("咖啡", result)
        self.assertIn("同一会话上下文", result)



class _ScopedFactsRepository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list_scoped_facts(self, scope, *, limit):
        self.calls.append((scope, limit))
        return list(self.rows)


class FactsToolScopeTest(unittest.TestCase):
    @staticmethod
    def _scope():
        from domain.scope import RuntimeScope, SessionRef

        return RuntimeScope("yushu", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))

    def test_facts_tool_reads_only_from_scoped_repository(self):
        from tools.extra_tools import WaveMemoryFactsTool

        repository = _ScopedFactsRepository([
            {"subject": "Alice", "predicate": "喜欢", "object": "咖啡", "confidence": 0.8},
            {"subject": "Bob", "predicate": "喜欢", "object": "茶", "confidence": 0.9},
        ])
        db = SimpleNamespace(closed=False, scoped_knowledge=repository)
        scope = self._scope()

        result = asyncio.run(WaveMemoryFactsTool(db=db).call(_context(scope), query="咖啡"))

        self.assertIn("Alice", result)
        self.assertNotIn("Bob", result)
        self.assertEqual(repository.calls, [(scope, 50)])

    def test_facts_tool_rejects_missing_scope_before_repository_read(self):
        from tools.extra_tools import WaveMemoryFactsTool

        repository = _ScopedFactsRepository([])
        db = SimpleNamespace(closed=False, scoped_knowledge=repository)

        result = asyncio.run(WaveMemoryFactsTool(db=db).call(_context(None), query="咖啡"))

        self.assertIn("已拒绝", result)
        self.assertEqual(repository.calls, [])


class ToolScopeBoundaryTest(unittest.TestCase):
    def test_read_tools_fail_closed_even_when_called_directly(self):
        from tools.extra_tools import WaveMemoryAffinityTool, WaveMemoryTagGraphTool
        from tools.person_search import WaveMemoryPersonSearchTool

        affinity = asyncio.run(WaveMemoryAffinityTool().call(None, mode="ranking", scope="global"))
        tag_graph = asyncio.run(WaveMemoryTagGraphTool().call(None, tag_name="跨群标签"))
        person = asyncio.run(WaveMemoryPersonSearchTool().call(None, person="跨群用户"))

        # Legacy social projections stay migration-gated. Person search remains a
        # group-only feature, and validates that boundary before it opens a DB.
        for result in (affinity, tag_graph):
            self.assertIn("scope_migration_required", result)
        self.assertIn("scope_required", person)

    def test_book_lore_tool_requires_explicit_catalog_scope(self):
        from tools.book_lore_search import BookLoreGraphTool, BookLoreSearchTool

        search = asyncio.run(BookLoreSearchTool().call(None, query="设定"))
        graph = asyncio.run(BookLoreGraphTool().call(None, entity_name="角色"))

        self.assertIn("catalog_scope_required", search)
        self.assertIn("catalog_scope_required", graph)


if __name__ == "__main__":
    unittest.main()
