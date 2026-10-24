"""定时任务（AstrBot CronMessageEvent）作用域解析、拒绝提示与数据库文件访问拦截。

背景：2026-10-23 21:00 的 active_agent 定时任务里记忆工具全部 memory_scope_required，
模型转而用 shell/python 直连线上 SQLite，导致 WAL 被删、库损坏。
"""

from __future__ import annotations

import asyncio
import ast
import copy
import os
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.bot_profile import BotProfile
from domain.scope import RuntimeScope, SessionRef
from services.scopes import BotIdentityBinding, ScopeResolutionError, ScopeResolver, bindings_from_profiles
from services.tool_guard import GUARD_REASON, blocked_path, find_db_marker, guard_tool_call
from tools import scope_boundary
from tools.scope_boundary import (
    NO_CHAT_CONTEXT_GUIDANCE,
    extract_memory_runtime_scope,
    require_memory_runtime_scope,
    scope_error_message,
    set_event_scope_resolver,
)


class _MessageType(Enum):
    GROUP = "GroupMessage"
    PRIVATE = "FriendMessage"
    OTHER = "OtherMessage"


class _CronEvent:
    """按 AstrBot 4.28.2 astrbot/core/cron/events.py 的 CronMessageEvent 字段语义模拟。

    platform_meta.name="cron"、id=会话平台实例；message_obj.self_id="astrbot"；
    sender.user_id=session_id；没有 group（get_group_id() 返回 ""）。
    """

    def __init__(self, umo: str) -> None:
        platform_id, message_type, session_id = umo.split(":", 2)
        self.unified_msg_origin = umo
        self._platform_id = platform_id
        self._message_type = _MessageType(message_type)
        self._session_id = session_id

    def get_platform_name(self):
        return "cron"

    def get_platform_id(self):
        return self._platform_id

    def get_message_type(self):
        return self._message_type

    def get_session_id(self):
        return self._session_id

    def get_group_id(self):
        return ""

    def get_self_id(self):
        return "astrbot"

    def get_sender_id(self):
        return self._session_id


def _yushu_profiles():
    # 与线上 bot_profiles 结构一致：主 qq_id 外另有一个 astrbot 绑定写明 platform。
    yushu = BotProfile.from_dict({
        "db_id": "yushu", "name": "alpha", "qq_id": "1000000004", "session_prefix": "alpha",
        "bindings": [{"host": "astrbot", "self_id": "1000000001", "platform": "alpha"}],
    })
    baizz = BotProfile.from_dict({
        "db_id": "baizz", "name": "beta", "qq_id": "1000000002", "session_prefix": "beta",
    })
    return [yushu, baizz]


@pytest.fixture
def resolver():
    return ScopeResolver(bindings_from_profiles(_yushu_profiles()))


@pytest.fixture(autouse=True)
def _clear_tool_resolver():
    set_event_scope_resolver(None)
    yield
    set_event_scope_resolver(None)


def _tool_ctx(event):
    return SimpleNamespace(context=SimpleNamespace(event=event))


# ---------------------------------------------------------------- A: resolver


def test_cron_private_session_resolves_to_private_scope(resolver):
    resolved = resolver.resolve_event(_CronEvent("alpha:FriendMessage:1000000003"))
    scope = resolved.scope
    assert scope.bot_id == "yushu"
    assert scope.visibility == "private"
    assert scope.session.kind == "private"
    assert scope.session.id == "alpha:private:1000000003"
    assert scope.subject_principal_id == "alpha:user:1000000003"
    # 优先选显式绑定该平台实例的账号
    assert resolved.bot_self_id == "1000000001"
    assert resolved.host_platform_id == "alpha"


def test_cron_group_session_resolves_to_group_without_subject(resolver):
    scope = resolver.resolve_event(_CronEvent("alpha:GroupMessage:2000000001")).scope
    assert scope.visibility == "group"
    assert scope.session.id == "alpha:group:2000000001"
    # 群聊定时任务没有对话对象，不伪造 subject
    assert scope.subject_principal_id is None


def test_cron_private_matches_real_private_event_session(resolver):
    """cron 私聊与真实私聊消息落在同一条会话线上。"""
    real = SimpleNamespace(
        get_self_id=lambda: "1000000001",
        get_sender_id=lambda: "1000000003",
        get_platform_id=lambda: "alpha",
        get_message_type=lambda: _MessageType.PRIVATE,
        get_group_id=lambda: "",
        get_session_id=lambda: "1000000003",
        get_platform_name=lambda: "aiocqhttp",
    )
    assert resolver.resolve_event(real).scope == resolver.resolve_event(_CronEvent("alpha:FriendMessage:1000000003")).scope


@pytest.mark.parametrize(
    ("umo", "reason"),
    [
        ("cron:OtherMessage:job-1", "cron_session_unbound"),
        ("未知平台:FriendMessage:1", "unknown_bot_platform"),
        ("alpha:OtherMessage:1", "chat_kind_unresolved"),
        ("alpha:GroupMessage:1000000003_2000000001", "cron_session_ambiguous"),
    ],
)
def test_cron_event_fail_closed(resolver, umo, reason):
    with pytest.raises(ScopeResolutionError) as exc:
        resolver.resolve_event(_CronEvent(umo))
    assert exc.value.reason_code == reason


def test_cron_event_never_matches_by_display_name():
    # 没写 session_prefix / platform 时，不能靠显示名“alpha”认 Bot。
    profile = BotProfile.from_dict({"db_id": "yushu", "name": "alpha", "qq_id": "1000000001"})
    resolver = ScopeResolver(bindings_from_profiles([profile]))
    with pytest.raises(ScopeResolutionError) as exc:
        resolver.resolve_event(_CronEvent("alpha:FriendMessage:1"))
    assert exc.value.reason_code == "unknown_bot_platform"


def test_cron_event_rejects_platform_shared_by_two_bots():
    resolver = ScopeResolver([
        BotIdentityBinding(self_id="1", db_id="a", host_platforms=("qq",)),
        BotIdentityBinding(self_id="2", db_id="b", host_platforms=("qq",)),
    ])
    with pytest.raises(ScopeResolutionError) as exc:
        resolver.resolve_event(_CronEvent("qq:FriendMessage:1"))
    assert exc.value.reason_code == "ambiguous_bot_platform"


def test_real_event_with_astrbot_self_id_still_rejected(resolver):
    """非 cron 事件仍只按真实 self_id 认 Bot。"""
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        get_self_id=lambda: "astrbot",
        get_sender_id=lambda: "1",
        get_platform_id=lambda: "alpha",
        get_message_type=lambda: _MessageType.PRIVATE,
        get_session_id=lambda: "1",
    )
    with pytest.raises(ScopeResolutionError) as exc:
        resolver.resolve_event(event)
    assert exc.value.reason_code == "unknown_bot_self_id"


# ---------------------------------------------------------------- A: 工具回退


def test_tool_falls_back_to_resolver_and_caches_scope(resolver):
    event = _CronEvent("alpha:FriendMessage:1000000003")
    ctx = _tool_ctx(event)
    assert extract_memory_runtime_scope(ctx) is None  # 未注册 resolver：fail closed

    set_event_scope_resolver(resolver)
    scope, error = require_memory_runtime_scope(ctx, "person.search")
    assert error is None
    assert scope.session.id == "alpha:private:1000000003"
    assert event._wave_memory_runtime_scope is scope  # 缓存


def test_tool_fallback_private_cron_cannot_reach_group_only_command(resolver):
    set_event_scope_resolver(resolver)
    ctx = _tool_ctx(_CronEvent("alpha:FriendMessage:1000000003"))
    scope, error = require_memory_runtime_scope(ctx, "affinity.update")  # 仅群聊
    assert scope is None and error == "scope_visibility_not_allowed"


def test_tool_fallback_group_cron_write_needing_subject_fails_closed(resolver):
    set_event_scope_resolver(resolver)
    ctx = _tool_ctx(_CronEvent("alpha:GroupMessage:2000000001"))
    scope, error = require_memory_runtime_scope(ctx, "social_anchor.note")
    assert scope is None and error == "scope_subject_required"
    scope, error = require_memory_runtime_scope(ctx, "memory.message.read")
    assert error is None and scope.session.id == "alpha:group:2000000001"


def test_tool_fallback_failure_stays_fail_closed(resolver):
    set_event_scope_resolver(resolver)
    event = _CronEvent("cron:OtherMessage:job-1")
    scope, error = require_memory_runtime_scope(_tool_ctx(event), "memory.message.read")
    assert scope is None and error == "memory_scope_required"
    assert not hasattr(event, "_wave_memory_runtime_scope")


def test_attached_scope_is_never_re_resolved(resolver):
    class _Exploding:
        def resolve_event(self, _event):
            raise AssertionError("must not resolve when scope attached")

    set_event_scope_resolver(_Exploding())
    attached = RuntimeScope("yushu", "group", SessionRef("alpha:group:1", "alpha", "group", "1"))
    ctx = _tool_ctx(SimpleNamespace(_wave_memory_runtime_scope=attached))
    assert extract_memory_runtime_scope(ctx) is attached
    # 已附着但类型错误的脏值：不重解析，fail closed
    assert extract_memory_runtime_scope(_tool_ctx(SimpleNamespace(_wave_memory_runtime_scope="group:1"))) is None


def test_runtime_tool_context_without_scope_does_not_resolve(resolver):
    from services.tool_registry import runtime_tool_context

    set_event_scope_resolver(resolver)
    assert extract_memory_runtime_scope(runtime_tool_context(None)) is None


def test_rebuild_scope_resolver_registers_for_tools():
    source = (Path(__file__).resolve().parents[1] / "app" / "bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_rebuild_scope_resolver"
    )
    assert "set_event_scope_resolver(self.scope_resolver)" in ast.unparse(method)


# ---------------------------------------------------------------- B: 拒绝提示


def test_memory_scope_required_message_is_actionable():
    message = scope_error_message("人物检索", "memory_scope_required")
    assert "（memory_scope_required）" in message
    assert "记忆作用域" in message
    assert NO_CHAT_CONTEXT_GUIDANCE in message
    for word in ("定时任务", "不要用 shell", "wave_memory.db", "data_v4.db", "拿不到记忆"):
        assert word in message


def test_other_reason_codes_keep_short_message():
    message = scope_error_message("x", "scope_mismatch")
    assert message == "目标不属于当前作用域，已拒绝x（scope_mismatch）"


# ---------------------------------------------------------------- C: 工具拦截


def test_shell_sqlite_access_is_rewritten():
    args = {
        "command": "python3 -c \"import sqlite3; sqlite3.connect('/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db')\"",
        "env": {"A": "1"},
    }
    assert guard_tool_call("astrbot_execute_shell", args) is not None
    assert args["command"] == "echo '" + GUARD_REASON + "'"
    assert "connect(" not in args["command"] and "python3" not in args["command"]
    assert args["env"] == {}


@pytest.mark.parametrize("tool", ["astrbot_execute_python", "astrbot_execute_ipython"])
def test_python_db_access_is_rewritten(tool):
    args = {"code": "import sqlite3\nc = sqlite3.connect('/AstrBot/data/data_v4.db')", "timeout": 30}
    assert guard_tool_call(tool, args) is not None
    assert args["code"] == f"print({GUARD_REASON!r})"
    exec(compile(args["code"], "<guard>", "exec"), {"print": lambda *_: None})


@pytest.mark.parametrize("marker", ["wave_memory.db-wal", "x.db-shm", "DATA_V4.DB"])
def test_markers_case_insensitive(marker):
    assert find_db_marker({"command": f"cat {marker}"}) is not None


@pytest.mark.parametrize(
    ("tool", "args", "path_key"),
    [
        ("astrbot_file_read_tool", {"path": "/AstrBot/data/data_v4.db"}, "path"),
        ("astrbot_file_write_tool", {"path": "/AstrBot/data/x.db-wal", "content": "x"}, "path"),
        ("astrbot_file_edit_tool", {"path": "/tmp/a.py", "old": "a", "new": "sqlite3.connect('wave_memory.db')"}, "path"),
        ("astrbot_grep_tool", {"pattern": "x", "path": "/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db"}, "path"),
        ("astrbot_upload_file", {"local_path": "/AstrBot/data/data_v4.db"}, "local_path"),
        ("astrbot_download_file", {"remote_path": "wave_memory.db"}, "remote_path"),
    ],
)
def test_file_tools_are_redirected_to_blocked_path(tool, args, path_key):
    assert guard_tool_call(tool, args) is not None
    assert args[path_key] == blocked_path()
    for key in ("content", "old", "new"):
        if key in args:
            assert args[key] == ""
    # 被重定向的路径不可读也不可建目录：父级是普通文件
    assert os.path.isfile(os.path.dirname(blocked_path()))
    assert not os.path.exists(blocked_path())
    assert find_db_marker(args) is None


def test_shell_session_write_is_neutralized():
    args = {"action": "write_line", "session_id": "s1", "chars": "sqlite3 /AstrBot/data/data_v4.db"}
    assert guard_tool_call("astrbot_shell_session", args) is not None
    assert args["action"] == "list" and args["chars"] == ""


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("astrbot_execute_shell", {"command": "ls /AstrBot/data/plugin_data"}),
        ("astrbot_execute_python", {"code": "print(1 + 1)"}),
        ("astrbot_file_read_tool", {"path": "/AstrBot/data/temp/report.md"}),
        # 不在管辖范围的工具即使带关键字也不改（例如记忆工具自己的参数）
        ("wave_memory_search", {"query": "sqlite3 是什么"}),
    ],
)
def test_unrelated_calls_pass_through(tool, args):
    before = copy.deepcopy(args)
    assert guard_tool_call(tool, args) is None
    assert args == before


def test_non_dict_args_ignored():
    assert guard_tool_call("astrbot_execute_shell", None) is None


def _load_guard_handler():
    source = (Path(__file__).resolve().parents[1] / "app" / "injection.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = copy.deepcopy(next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_using_llm_tool"
    ))
    for argument in (*method.args.args, *method.args.kwonlyargs):
        argument.annotation = None

    class _AbsoluteImport(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.level == 2 and node.module == "services.tool_guard":
                node.level = 0
            return node

    method = _AbsoluteImport().visit(method)
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    warnings: list[str] = []
    errors: list[tuple] = []
    namespace = {
        "logger": SimpleNamespace(warning=warnings.append, debug=lambda *_: None),
        "_record_err": lambda *a: errors.append(a),
    }
    exec(compile(module, "injection.py", "exec"), namespace)
    return namespace["_handle_using_llm_tool"], warnings, errors


def test_plugin_hook_rewrites_args_in_place_and_logs():
    handler, warnings, errors = _load_guard_handler()
    tool = SimpleNamespace(name="astrbot_execute_shell")
    args = {"command": "sqlite3 /AstrBot/data/data_v4.db .tables"}
    event = SimpleNamespace(unified_msg_origin="alpha:FriendMessage:1000000003")
    asyncio.run(handler(None, event, tool, args))
    assert args["command"].startswith("echo '")
    assert warnings and "astrbot_execute_shell" in warnings[0]
    assert errors and errors[0][0] == "DbAccessGuard"

    clean = {"command": "ls"}
    asyncio.run(handler(None, event, tool, clean))
    assert clean == {"command": "ls"} and len(warnings) == 1


def test_main_registers_on_using_llm_tool_hook():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "guard_db_file_access"
    )
    assert any("on_using_llm_tool" in ast.unparse(dec) for dec in method.decorator_list)
    # AstrBot 以 handler(event, tool, tool_args) 调用
    assert [a.arg for a in method.args.args] == ["self", "event", "tool", "tool_args"]
