from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import types
from types import SimpleNamespace

import pytest


def _install_astrbot_tool_stub() -> None:
    if "astrbot.core.agent.tool" in sys.modules:
        return

    class FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None, debug=lambda *a, **k: None)
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool = types.ModuleType("astrbot.core.agent.tool")
    run_context = types.ModuleType("astrbot.core.agent.run_context")
    astr_context = types.ModuleType("astrbot.core.astr_agent_context")
    tool.FunctionTool = FunctionTool
    run_context.ContextWrapper = object
    astr_context.AstrAgentContext = object
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.tool": tool,
        "astrbot.core.agent.run_context": run_context,
        "astrbot.core.astr_agent_context": astr_context,
    })


_install_astrbot_tool_stub()

from domain.scope import RuntimeScope, SessionRef
from tools.affinity_update import WaveMemoryAffinityTool
from tools.person_identity import display_name_for_user, resolve_user_id
from tools.person_search import WaveMemoryPersonSearchTool


def _scope() -> RuntimeScope:
    return RuntimeScope(
        "yushu",
        "group",
        SessionRef("qq:group:398291136", "qq", "group", "398291136"),
        subject_principal_id="qq:user:111",
    )


def _db() -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE user_profiles(
            user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT,
            interaction_count INTEGER, last_seen REAL
        );
        CREATE TABLE person_registry(
            qq_id TEXT, display_name TEXT, aliases TEXT, message_count INTEGER
        );
        CREATE TABLE memories(
            id INTEGER PRIMARY KEY,
            group_id TEXT, bot_id TEXT, session_id TEXT, visibility TEXT,
            sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL,
            quarantine INTEGER DEFAULT 0, source TEXT DEFAULT 'chat',
            importance REAL DEFAULT 1.0, memory_type TEXT DEFAULT 'message'
        );
        INSERT INTO user_profiles VALUES ('2696534623', '398291136', 'yushu', '', 382, 1);
        INSERT INTO user_profiles VALUES ('2769029004', '398291136', 'yushu', '', 253, 1);
        INSERT INTO user_profiles VALUES ('999', '150727649', 'yushu', '诸葛匹夫', 10, 1);
        INSERT INTO person_registry VALUES ('2696534623', '诸葛匹夫', '["匹夫"]', 100);
        INSERT INTO person_registry VALUES ('2769029004', '', '[]', 10);
        INSERT INTO memories VALUES
            (1, '398291136', 'yushu', 'qq:group:398291136', 'group', '2696534623', '诸葛匹夫', '主篇战力太膨胀了', 100.0, 0, 'chat', 1.0, 'message'),
            (2, '398291136', 'yushu', 'qq:group:398291136', 'group', '111', '提问者', '@诸葛匹夫 还有这种', 101.0, 0, 'chat', 1.0, 'message'),
            (3, '398291136', 'yushu', 'qq:group:398291136', 'group', '2769029004', '飞花轻似梦', '这是胖猫主题曲', 102.0, 0, 'chat', 1.0, 'message'),
            (4, '150727649', 'yushu', 'qq:group:150727649', 'group', '2696534623', '诸葛匹夫', '别群发言', 103.0, 0, 'chat', 1.0, 'message');
        """
    )

    class _Soul:
        def get_state(self, scope, **_kwargs):
            user = (scope.subject_principal_id or "").split(":")[-1]
            if user == "2696534623":
                return {
                    "relationship": {
                        "affinity": 12,
                        "state": "neutral",
                        "dimensions": {"familiarity": 50, "trust": 10, "fun": 4, "hostility": 0, "depth": 8},
                        "values": {},
                    }
                }
            return {"relationship": None}

        def list_relationships(self, _scope):
            return [{
                "subject_principal_id": "qq:user:2696534623",
                "affinity": 12,
                "state": "neutral",
            }]

    return SimpleNamespace(conn=conn, soul_repository=_Soul(), closed=False)


def _ctx(scope: RuntimeScope) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope)))


def test_resolve_user_id_prefers_qq_and_current_group_chat_names():
    db = _db()
    scope = _scope()

    assert resolve_user_id(db, "2696534623", scope) == "2696534623"
    assert resolve_user_id(db, "诸葛匹夫", scope) == "2696534623"
    assert resolve_user_id(db, "飞花轻似梦", scope) == "2769029004"
    # Cross-group nickname alone must not hijack current-group resolution.
    assert resolve_user_id(db, "不应存在", scope) == ""


def test_display_name_falls_back_to_chat_sender_name_when_profile_empty():
    db = _db()
    scope = _scope()
    assert display_name_for_user(db, "2769029004", scope) == "飞花轻似梦"
    assert display_name_for_user(db, "2696534623", scope) == "诸葛匹夫"


def test_person_search_recent_is_current_group_only():
    tool = WaveMemoryPersonSearchTool(db=_db())
    result = asyncio.run(tool.call(_ctx(_scope()), person="诸葛匹夫", query_type="recent", limit=5))
    assert "当前群最近发言" in result
    assert "主篇战力太膨胀了" in result
    assert "别群发言" not in result


def test_person_search_all_groups_includes_other_group_and_tags_group_id():
    tool = WaveMemoryPersonSearchTool(db=_db())
    result = asyncio.run(
        tool.call(
            _ctx(_scope()),
            person="诸葛匹夫",
            query_type="recent",
            scope="all_groups",
            limit=5,
        )
    )
    assert "跨群最近发言" in result
    assert "主篇战力太膨胀了" in result
    assert "别群发言" in result
    assert "[群 150727649]" in result


def test_person_search_cross_group_bool_alias():
    tool = WaveMemoryPersonSearchTool(db=_db())
    result = asyncio.run(
        tool.call(
            _ctx(_scope()),
            person="2696534623",
            query_type="recent",
            cross_group=True,
            limit=5,
        )
    )
    assert "别群发言" in result


def test_person_search_profile_includes_formal_relationship():
    tool = WaveMemoryPersonSearchTool(db=_db())
    result = asyncio.run(tool.call(_ctx(_scope()), person="2696534623", query_type="profile"))
    assert "QQ: 2696534623" in result
    assert "正式关系: affinity=12" in result


def test_person_search_works_in_private_chat():
    """私聊下检索人物画像与发言，必须正常返回，绝不能报 scope_required。"""
    db = _db()
    private_scope = RuntimeScope(
        "yushu",
        "private",
        SessionRef("qq:private:111", "qq", "private", "111"),
        subject_principal_id="qq:user:111",
    )
    tool = WaveMemoryPersonSearchTool(db=db)
    result = asyncio.run(tool.call(_ctx(private_scope), person="诸葛匹夫", query_type="profile"))
    assert "QQ: 2696534623" in result
    assert "正式关系: affinity=12" in result

    recent_result = asyncio.run(tool.call(_ctx(private_scope), person="诸葛匹夫", query_type="recent", limit=5))
    assert "主篇战力太膨胀了" in recent_result
    assert "别群发言" in recent_result


def test_affinity_query_works_in_private_chat():
    """私聊下查询好感度，必须正常返回，绝不能报 scope_required。"""
    db = _db()
    private_scope = RuntimeScope(
        "yushu",
        "private",
        SessionRef("qq:private:111", "qq", "private", "111"),
        subject_principal_id="qq:user:111",
    )
    tool = WaveMemoryAffinityTool(db=db)
    result = asyncio.run(tool.call(_ctx(private_scope), mode="single", target_user="诸葛匹夫"))
    assert "关系对象：诸葛匹夫（2696534623）" in result
    assert "好感度：12" in result



def test_person_search_accepts_display_platform_session_ids():
    """Production rows use prefixes like 羽书:group:<id>, not only qq:group:<id>."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE user_profiles(user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT, interaction_count INTEGER, last_seen REAL);
        CREATE TABLE person_registry(qq_id TEXT, display_name TEXT, aliases TEXT, message_count INTEGER);
        CREATE TABLE memories(
            id INTEGER PRIMARY KEY, group_id TEXT, bot_id TEXT, session_id TEXT, visibility TEXT,
            sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL,
            quarantine INTEGER DEFAULT 0, source TEXT DEFAULT 'chat', importance REAL DEFAULT 1.0,
            memory_type TEXT DEFAULT 'message'
        );
        INSERT INTO user_profiles VALUES ('2696534623', '398291136', 'yushu', '诸葛匹夫', 10, 1);
        INSERT INTO person_registry VALUES ('2696534623', '诸葛匹夫', '[]', 10);
        INSERT INTO memories VALUES
            (1, '398291136', 'yushu', '羽书:group:398291136', 'group', '2696534623', '诸葛匹夫', '本群发言A', 100.0, 0, 'chat', 1.0, 'message');
        """
    )

    class _Soul:
        def get_state(self, scope, **_kwargs):
            return {
                "relationship": {
                    "affinity": 12,
                    "state": "neutral",
                    "dimensions": {"familiarity": 50, "trust": 0, "fun": 0, "hostility": 0, "depth": 0},
                    "values": {},
                }
            }

        def list_relationships(self, _scope):
            return []

    db = SimpleNamespace(conn=conn, soul_repository=_Soul(), closed=False)
    scope = RuntimeScope(
        "yushu",
        "group",
        SessionRef("羽书:group:398291136", "羽书", "group", "398291136"),
        subject_principal_id="羽书:user:111",
    )
    tool = WaveMemoryPersonSearchTool(db=db)
    result = asyncio.run(tool.call(_ctx(scope), person="诸葛匹夫", query_type="recent", limit=3))
    assert "本群发言A" in result
    assert resolve_user_id(db, "诸葛匹夫", scope) == "2696534623"


def test_person_search_timeline_reads_full_detail_and_pages(tmp_path):
    from engine.db.connection import ConnectionManager
    from engine.db.person_timeline_repo import PersonTimelineRepo

    cm = ConnectionManager(str(tmp_path / "people.sqlite3"))
    try:
        repo = PersonTimelineRepo(cm)
        cm.executescript(
            """
            CREATE TABLE user_profiles(user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT, interaction_count INTEGER, last_seen REAL);
            CREATE TABLE person_registry(qq_id TEXT, display_name TEXT, aliases TEXT, message_count INTEGER);
            INSERT INTO user_profiles VALUES ('2696534623', '398291136', 'yushu', '诸葛匹夫', 10, 1);
            INSERT INTO person_registry VALUES ('2696534623', '诸葛匹夫', '[]', 10);
            """
        )
        summary = "完整摘要；" * 20
        detail = "完整详情第一段。" * 40
        event_id = repo.add_event(
            bot_id="yushu", user_id="2696534623", group_id="398291136",
            kind="impression", summary=summary, detail=detail, occurred_at=100.0,
        )
        other_id = repo.add_event(
            bot_id="yushu", user_id="2696534623", group_id="398291136",
            kind="person_fact", summary="第二页旧事", detail="第二页详情", occurred_at=50.0,
        )
        repo.add_event(
            bot_id="yushu", user_id="2696534623", group_id="150727649",
            kind="impression", summary="别群印象", detail="别群详情", occurred_at=80.0,
        )
        repo.add_event(
            bot_id="other-bot", user_id="2696534623", group_id="398291136",
            kind="impression", summary="别的Bot", detail="别的详情", occurred_at=90.0,
        )
        db = SimpleNamespace(conn=cm, person_timeline=repo, closed=False)
        tool = WaveMemoryPersonSearchTool(db=db)
        before = repo.count_events(bot_id="yushu", user_id="2696534623", group_id="398291136")
        page = asyncio.run(tool.call(
            _ctx(_scope()), person="诸葛匹夫", query_type="timeline", limit=1,
        ))
        assert summary in page
        assert detail in page
        assert f"id: {event_id}" in page
        assert "别群印象" not in page
        assert "别的Bot" not in page
        assert "next_offset: 1" in page
        next_page = asyncio.run(tool.call(
            _ctx(_scope()), person="诸葛匹夫", query_type="timeline", limit=1, offset=1,
        ))
        assert "第二页旧事" in next_page
        by_id = asyncio.run(tool.call(
            _ctx(_scope()), person="诸葛匹夫", query_type="timeline", event_id=other_id,
        ))
        assert "第二页详情" in by_id
        leaked = asyncio.run(tool.call(
            _ctx(_scope()), person="诸葛匹夫", query_type="timeline", event_id=999999,
        ))
        assert "未找到" in leaked
    finally:
        cm.close()


def test_facts_and_deep_search_tools_work_in_private_chat():
    from tools.extra_tools import WaveMemoryFactsTool
    from tools.deep_search import WaveMemoryDeepSearchTool

    class _ScopedKnowledge:
        def list_scoped_facts(self, scope, limit=50):
            return [{
                "subject": "张羽",
                "predicate": "是",
                "object": "散修",
                "confidence": 0.9,
                "status": "active",
            }]

    db = SimpleNamespace(
        conn=sqlite3.connect(":memory:"),
        scoped_knowledge=_ScopedKnowledge(),
        closed=False,
    )
    db.conn.executescript(
        """
        CREATE TABLE memories(
            id INTEGER PRIMARY KEY, group_id TEXT, bot_id TEXT, session_id TEXT, visibility TEXT,
            sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL,
            quarantine INTEGER DEFAULT 0, source TEXT DEFAULT 'chat', importance REAL DEFAULT 1.0,
            memory_type TEXT DEFAULT 'message'
        );
        CREATE VIRTUAL TABLE fts_memories USING fts5(content, content='memories', content_rowid='id');
        INSERT INTO memories VALUES (1, '111', 'yushu', 'qq:private:111', 'private', '111', '用户', '我们在聊 极情剑道', 100.0, 0, 'chat', 1.0, 'message');
        INSERT INTO fts_memories(rowid, content) VALUES (1, '我们在聊 极情剑道');
        """
    )
    private_scope = RuntimeScope(
        "yushu",
        "private",
        SessionRef("qq:private:111", "qq", "private", "111"),
        subject_principal_id="qq:user:111",
    )

    facts_tool = WaveMemoryFactsTool(db=db)
    fact_res = asyncio.run(facts_tool.call(_ctx(private_scope), query="张羽"))
    assert "张羽" in fact_res and "散修" in fact_res
    assert "scope_required" not in fact_res

    deep_tool = WaveMemoryDeepSearchTool(db=db)
    deep_res = asyncio.run(deep_tool.call(_ctx(private_scope), keywords="极情剑道"))
    assert "极情剑道" in deep_res
    assert "scope_required" not in deep_res
