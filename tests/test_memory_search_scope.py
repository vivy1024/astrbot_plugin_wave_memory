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
                quarantine INTEGER, group_id TEXT, memory_type TEXT, source TEXT,
                sender_id TEXT, importance REAL DEFAULT 1.0
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
            "INSERT INTO memories VALUES (?, '用户', ?, 1, ?, ?, 'group', ?, ?, ?, ?, ?, 'u1', 1.0)",
            (memory_id, content, bot, session, state, quarantine, group_id, memory_type, source),
        )
        self.conn.execute("INSERT INTO fts_memories(rowid, content) VALUES (?, ?)", (memory_id, content))
        self.conn.commit()


def _context(scope):
    event = SimpleNamespace(_wave_memory_runtime_scope=scope)
    return SimpleNamespace(context=SimpleNamespace(event=event))


class MemorySearchScopeTest(unittest.TestCase):
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

    def test_unified_memory_search_tool_with_context_window(self):
        from tools.memory_search import WaveMemorySearchTool

        tool = WaveMemorySearchTool(db=self.db, query_engine=None)
        result = asyncio.run(tool.call(_context(self._scope()), query="咖啡", include_context=True))

        self.assertIn("对话切片", result)
        self.assertIn("咖啡", result)
        self.assertIn("同一会话上下文", result)
        # 上下文只取本 Bot、同会话的邻居
        self.assertNotIn("另一个 Bot", result)
        self.assertNotIn("跨会话", result)

    def test_search_fallback_is_bot_scoped(self):
        from tools.memory_search import WaveMemorySearchTool

        self.db.add(20, "奶茶 只有白真真见过", bot="bzz")
        tool = WaveMemorySearchTool(db=self.db, query_engine=None)
        result = asyncio.run(tool.call(_context(self._scope()), query="奶茶", include_context=True))
        self.assertIn("没有找到", result)

    def test_search_fallback_uses_cjk_index_when_ready(self):
        from engine.db import fts_cjk
        from tools.memory_search import WaveMemorySearchTool

        # 旧索引里「张羽」和后文连成一个词，只有中文索引能按词命中
        self.db.add(30, "昨天张羽师兄来过")
        fts_cjk.ensure_schema(self.db.conn)
        fts_cjk.sync_memory(self.db.conn, 30)
        self.db.add(31, "张羽今天没进中文索引")  # 只在旧索引里
        tool = WaveMemorySearchTool(db=self.db, query_engine=None)
        before = asyncio.run(tool.call(_context(self._scope()), query="张羽", include_context=False))
        self.assertIn("张羽师兄", before)  # 未就绪：旧索引 + 作用域内 LIKE 兜底
        self.assertIn("没进中文索引", before)
        fts_cjk.mark_ready(self.db.conn)
        after = asyncio.run(tool.call(_context(self._scope()), query="张羽", include_context=False))
        self.assertIn("张羽师兄", after)
        self.assertNotIn("没进中文索引", after)  # 就绪后只查中文索引，不再全表 LIKE

    def test_context_window_skips_interleaved_other_conversations(self):
        from tools.memory_search import WaveMemorySearchTool

        # 同一会话的相邻消息被别的群、别的 Bot 的写入隔开很远
        self.db.add(100, "前一句 同一会话")
        for i in range(101, 140):
            self.db.add(i, f"别的群 {i}", session="qq:group:g9")
        self.db.add(140, "豆浆 命中")
        for i in range(141, 160):
            self.db.add(i, f"白真真副本 {i}", bot="bzz")
        self.db.add(160, "后一句 同一会话")
        tool = WaveMemorySearchTool(db=self.db, query_engine=None)
        result = asyncio.run(tool.call(_context(self._scope()), query="豆浆", include_context=True))
        self.assertIn("前一句", result)
        self.assertIn("后一句", result)
        self.assertNotIn("别的群", result)
        self.assertNotIn("白真真副本", result)



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
