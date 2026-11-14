from __future__ import annotations

import asyncio
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from engine.db.connection import ConnectionManager


def _install_astrbot_tool_stub(monkeypatch) -> None:
    """The focused tool test only needs FunctionTool's generic base type."""
    if "astrbot.core.agent.tool" in sys.modules:
        return

    class FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    astrbot = types.ModuleType("astrbot")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool = types.ModuleType("astrbot.core.agent.tool")
    run_context = types.ModuleType("astrbot.core.agent.run_context")
    astr_context = types.ModuleType("astrbot.core.astr_agent_context")
    tool.FunctionTool = FunctionTool
    run_context.ContextWrapper = object
    astr_context.AstrAgentContext = object
    for name, module in {
        "astrbot": astrbot,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.tool": tool,
        "astrbot.core.agent.run_context": run_context,
        "astrbot.core.astr_agent_context": astr_context,
    }.items():
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, module)


from domain.scope import RuntimeScope, SessionRef

# Import-only shim: do not leave fake AstrBot modules in other tests' registry.
with pytest.MonkeyPatch.context() as _import_patch:
    _install_astrbot_tool_stub(_import_patch)
    from tools.affinity_update import WaveMemoryAffinityTool


class _Repository:
    def __init__(self):
        self.list_scopes = []
        self.state_scopes = []
        self.audit_scopes = []

    def list_relationships(self, scope):
        self.list_scopes.append(scope)
        return [
            {
                "subject_principal_id": "qq:user:u1",
                "affinity": 42,
                "state": "friendly",
                "values": {"trust": {"effective_value": 50}},
            },
            {
                "subject_principal_id": "qq:user:u2",
                "affinity": -8,
                "state": "cold",
                "values": {"hostility": {"effective_value": 20}},
            },
        ]

    def get_state(self, scope, **_kwargs):
        self.state_scopes.append(scope)
        return {
            "relationship": {
                "affinity": 42,
                "state": "friendly",
                "dimensions": {"familiarity": 30, "trust": 50, "fun": 4, "hostility": 0, "depth": 18},
                "values": {},
            },
        }

    def list_legacy_relationship_audit_summary(self, scope, **_kwargs):
        self.audit_scopes.append(scope)
        return {
            "available": True,
            "total": 12,
            "by_type": [
                {"event_type": "direct_reply", "count": 10},
                {"event_type": "bot_attacked", "count": 2},
            ],
            "recent": [
                {
                    "event_type": "direct_reply",
                    "dimension": "familiarity",
                    "delta": 0.5,
                    "reason": "看见一条群友消息",
                    "occurred_at": 1.0,
                    "legacy_event_id": "10",
                }
            ],
        }


def _context() -> SimpleNamespace:
    scope = RuntimeScope(
        "bot-a",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )
    return SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope)))


@pytest.fixture
def connection(tmp_path):
    manager = ConnectionManager(str(tmp_path / "affinity.db"))
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def tool(connection):
    connection.conn.executescript(
        """
        CREATE TABLE user_profiles(user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT, interaction_count INTEGER, last_seen REAL);
        CREATE TABLE person_registry(qq_id TEXT, display_name TEXT, aliases TEXT, message_count INTEGER);
        INSERT INTO user_profiles VALUES ('u1', 'g1', 'bot-a', '甲昵称', 5, 1);
        INSERT INTO user_profiles VALUES ('u2', 'g1', 'bot-a', '乙昵称', 23, 1);
        INSERT INTO user_profiles VALUES ('u3', 'g2', 'bot-a', '跨群用户', 999, 1);
        INSERT INTO user_profiles VALUES ('u4', 'g1', 'bot-b', '跨Bot用户', 9999, 1);
        INSERT INTO person_registry VALUES ('u1', '甲', '["小甲"]', 5);
        INSERT INTO person_registry VALUES ('u2', '乙', '[]', 23);
        INSERT INTO person_registry VALUES ('u3', '跨群登记名', '["跨群别名"]', 999);
        INSERT INTO person_registry VALUES ('u4', '跨Bot登记名', '["跨Bot别名"]', 9999);
        """
    )
    return WaveMemoryAffinityTool(db=SimpleNamespace(conn=connection.conn, soul_repository=_Repository()))


def test_formal_affinity_tool_restores_group_scoped_rankings_only(tool):
    ctx = _context()

    ranking = asyncio.run(tool.call(ctx, mode="ranking", limit=10))
    blacklist = asyncio.run(tool.call(ctx, mode="blacklist", limit=10))
    active = asyncio.run(tool.call(ctx, mode="active", limit=10))

    assert "甲（u1）" in ranking
    assert "乙（u2）" not in ranking
    assert "乙（u2）" in blacklist
    assert "乙（u2）" in active
    assert "跨群用户" not in active
    assert "跨Bot用户" not in active
    assert "（u3）" not in active
    assert "（u4）" not in active
    # 排行只放宽 subject 过滤；Bot + canonical group session 仍不可改变。
    queried_scopes = tool.db.soul_repository.list_scopes
    assert len(queried_scopes) == 3
    assert all(item.subject_principal_id is None for item in queried_scopes)
    assert all(item.bot_id == "bot-a" and item.session == ctx.context.event._wave_memory_runtime_scope.session for item in queried_scopes)


def test_formal_affinity_tool_keeps_single_query_in_current_scope(tool):

    result = asyncio.run(tool.call(_context(), mode="single", target_user="u1"))

    assert "关系对象：甲（u1）" in result
    assert "hostility: 0" in result
    assert "历史事件审计：12 条" in result
    assert "direct_reply×10" in result
    assert "不改变好感度" in result


def test_single_does_not_enumerate_group_profiles(tool, monkeypatch):
    from tools import affinity_update

    calls = []
    original = affinity_update.display_name_for_user

    def counted(db, user_id, scope):
        calls.append(user_id)
        return original(db, user_id, scope)

    monkeypatch.setattr(affinity_update, "display_name_for_user", counted)

    def fail_group_scan(*_args, **_kwargs):
        pytest.fail("single must not enumerate every group profile")

    monkeypatch.setattr(WaveMemoryAffinityTool, "_people_by_id", fail_group_scan)
    result = asyncio.run(tool.call(_context(), mode="single", target_user="u1"))
    assert "关系对象：甲（u1）" in result
    assert calls == ["u1"]


@pytest.mark.parametrize("mode, expected_user", [("ranking", "u1"), ("blacklist", "u2"), ("active", "u2")])
def test_rankings_resolve_only_selected_display_names(tool, monkeypatch, mode, expected_user):
    from tools import affinity_update

    calls = []
    original = affinity_update.display_name_for_user

    def counted(db, user_id, scope):
        calls.append(user_id)
        return original(db, user_id, scope)

    monkeypatch.setattr(affinity_update, "display_name_for_user", counted)
    result = asyncio.run(tool.call(_context(), mode=mode, limit=1))
    assert f"（{expected_user}）" in result
    assert calls == [expected_user]


@pytest.mark.parametrize("mode", ["single", "ranking", "blacklist", "active"])
def test_slow_sync_query_keeps_event_loop_running(tool, monkeypatch, mode):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    released_during_query = []
    method = "get_state" if mode == "single" else "list_relationships"
    original = getattr(tool.db.soul_repository, method)

    def slow_query(*args, **kwargs):
        entered.set()
        # Finite wait also lets the broken, synchronous implementation fail safely.
        released_during_query.append(release.wait(timeout=1))
        return original(*args, **kwargs)

    monkeypatch.setattr(tool.db.soul_repository, method, slow_query)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while not finished.is_set():
                await asyncio.sleep(0)
                if entered.is_set() and not release.is_set():
                    ticks += 1
                    if ticks == 3:
                        release.set()

        beat = asyncio.create_task(ticker())
        query = asyncio.create_task(tool.call(_context(), mode=mode))
        try:
            result = await asyncio.wait_for(query, timeout=5)
        finally:
            release.set()
            finished.set()
            await beat
        assert "关系对象" in result if mode == "single" else "TOP" in result
        assert released_during_query == [True], "query blocked the asyncio ticker"
        assert ticks >= 3

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [{}, {"target_user": "甲昵称"}, {"target_user": "小甲"}, {"user_id": "小甲"}])
def test_single_preserves_current_speaker_nickname_and_argument_alias(tool, arguments):
    result = asyncio.run(tool.call(_context(), **arguments))
    assert "关系对象：甲（u1）" in result
    repo = tool.db.soul_repository
    expected = _context().context.event._wave_memory_runtime_scope
    assert repo.state_scopes == [expected]
    assert repo.audit_scopes == [expected]


@pytest.mark.parametrize("mode", ["single", "ranking", "blacklist", "active"])
def test_profile_nickname_is_used_without_registry_display_name(tool, mode):
    tool.db.conn.execute("UPDATE person_registry SET display_name='' WHERE qq_id IN ('u1', 'u2')")
    tool.db.conn.commit()
    result = asyncio.run(tool.call(_context(), mode=mode))
    expected = "甲昵称（u1）" if mode in {"single", "ranking"} else "乙昵称（u2）"
    assert expected in result


def test_single_unknown_target_does_not_query_relationship_state(tool):
    result = asyncio.run(tool.call(_context(), target_user="完全不存在的人"))
    assert "未找到目标用户" in result
    assert tool.db.soul_repository.state_scopes == []


def test_single_missing_formal_relationship_stays_unknown(tool, monkeypatch):
    monkeypatch.setattr(tool.db.soul_repository, "get_state", lambda *_args, **_kwargs: {"relationship": {"affinity": None}})
    result = asyncio.run(tool.call(_context()))
    assert "尚无" in result
    assert "状态未知" in result
    assert "好感度：" not in result


@pytest.mark.parametrize("mode", ["ranking", "blacklist", "active"])
def test_empty_rankings_are_reported_without_display_resolution(tool, monkeypatch, mode):
    from tools import affinity_update

    def unexpected_display(*_args, **_kwargs):
        pytest.fail("empty ranking must not resolve display names")

    monkeypatch.setattr(affinity_update, "display_name_for_user", unexpected_display)
    monkeypatch.setattr(tool.db.soul_repository, "list_relationships", lambda _scope: [])
    tool.db.conn.execute("DELETE FROM user_profiles")
    tool.db.conn.commit()
    result = asyncio.run(tool.call(_context(), mode=mode))
    assert "当前 Scope 没有" in result


@pytest.mark.parametrize("invalid", ["scope", "mode", "limit"])
def test_invalid_scope_or_arguments_are_rejected_before_thread_dispatch(tool, monkeypatch, invalid):
    from tools import affinity_update

    def forbidden_dispatch(*_args, **_kwargs):
        pytest.fail("invalid request must be rejected before asyncio.to_thread")

    monkeypatch.setattr(affinity_update.asyncio, "to_thread", forbidden_dispatch)
    ctx = _context()
    arguments = {}
    if invalid == "scope":
        ctx.context.event._wave_memory_runtime_scope = None
    elif invalid == "mode":
        arguments["mode"] = "invalid"
    else:
        arguments["limit"] = "invalid"
    result = asyncio.run(tool.call(ctx, **arguments))
    assert ("scope_required" if invalid == "scope" else invalid) in result
    assert tool.db.soul_repository.state_scopes == []
    assert tool.db.soul_repository.list_scopes == []


def test_real_scoped_repository_keeps_bot_group_and_subject_isolated(tool, connection):
    from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
    from engine.db.scoped_soul_repo import ScopedSoulRepository

    ensure_scoped_soul_schema(connection)
    repository = ScopedSoulRepository(connection)
    tool.db.soul_repository = repository
    for bot_id, group_id, user_id, affinity in [
        ("bot-a", "g1", "u1", 42),
        ("bot-a", "g1", "u2", -8),
        ("bot-a", "g2", "u1", 99),
        ("bot-b", "g1", "u1", 88),
        ("bot-a", "g2", "u3", 100),
        ("bot-b", "g1", "u4", 100),
    ]:
        scope = RuntimeScope(
            bot_id, "group", SessionRef(f"qq:group:{group_id}", "qq", "group", group_id),
            subject_principal_id=f"qq:user:{user_id}",
        )
        repository.upsert_relationship(scope, subject_principal_id=scope.subject_principal_id, affinity=affinity, state="friendly")

    async def scenario():
        single = await tool.call(_context(), target_user="小甲")
        ranking = await tool.call(_context(), mode="ranking")
        blacklist = await tool.call(_context(), mode="blacklist", limit=1)
        active = await tool.call(_context(), mode="active")
        return single, ranking, blacklist, active

    single, ranking, blacklist, active = asyncio.run(scenario())
    # 一个生命对一个人只有一份好感：u1 在 g1(42) 与 g2(99) 的积累合并（钳制到 100）；
    # 其他 Bot（bot-b 的 88）与没在本群出现的人（u3/u4）仍然隔离。
    assert "好感度：100" in single
    assert "甲（u1） · +100" in ranking
    assert "乙（u2） · -8" in blacklist
    assert "乙（u2） · 互动 23" in active
    for result in (single, ranking, blacklist, active):
        assert "（u3）" not in result
        assert "（u4）" not in result
        assert "+88" not in result
