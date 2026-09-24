"""工具注册表：内置工具登记、能力开关、Runtime 调用、Bot 工具开关与外部扩展。"""

from __future__ import annotations

import asyncio
import sys
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

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    if not hasattr(api, "logger"):
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
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.tool": tool,
        "astrbot.core.agent.run_context": run_context,
        "astrbot.core.astr_agent_context": astr_context,
    })
    del astrbot


_install_astrbot_tool_stub()

from domain.bot_profile import BotProfile  # noqa: E402
from domain.scope import RuntimeScope, SessionRef  # noqa: E402
from services.tool_registry import ToolRegistry, ToolRegistryError, ToolSpec  # noqa: E402
from tools.scope_boundary import extract_event_runtime_scope  # noqa: E402


class _EchoTool:
    name = "echo"
    description = "回显作用域"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def call(self, context, **kwargs):
        scope = extract_event_runtime_scope(context)
        return f"{scope.bot_id}|{scope.session.id}|{kwargs.get('text', '')}"


def _scope() -> RuntimeScope:
    return RuntimeScope("yushu", "group", SessionRef("bilibili:group:1", "bilibili", "group", "1"))


def _registry(**spec_kwargs) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec("echo", lambda deps: _EchoTool(), **spec_kwargs))
    registry.build(None, capability_enabled=lambda capability, default: default)
    return registry


def test_runtime_invoke_passes_scope_the_same_way_as_astrbot():
    registry = _registry()
    result = asyncio.run(registry.invoke("echo", scope=_scope(), arguments={"text": "hi"}))
    assert result == "yushu|bilibili:group:1|hi"
    assert registry.get("echo").calls == 1


def test_capability_gate_bot_deny_and_disable():
    assert _registry(capability="agent_feedback_tools", capability_default=False).records() == []

    registry = _registry()
    profile = BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "tools_deny": ["echo"]})
    with pytest.raises(ToolRegistryError) as denied:
        asyncio.run(registry.invoke("echo", scope=_scope(), profile=profile))
    assert denied.value.code == "tool_denied"
    assert registry.describe(profile=profile)[0]["enabled"] is False

    registry.set_enabled("echo", False)
    with pytest.raises(ToolRegistryError) as disabled:
        asyncio.run(registry.invoke("echo", scope=_scope()))
    assert disabled.value.code == "tool_disabled"


def test_filter_request_tools_removes_denied_tools_from_toolset():
    registry = _registry()
    removed: list[str] = []
    toolset = SimpleNamespace(remove_tool=removed.append)
    profile = BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "tools_allow": ["something_else"]})
    assert registry.filter_request_tools(toolset, profile) == ["echo"]
    assert removed == ["echo"]


def test_duplicate_and_failing_factories_are_reported():
    registry = ToolRegistry()
    registry.register(ToolSpec("ok", lambda d: _EchoTool()))
    registry.register(ToolSpec("boom", lambda d: 1 / 0))
    with pytest.raises(ToolRegistryError):
        registry.register(ToolSpec("ok", lambda d: None))
    built = registry.build(None, capability_enabled=lambda c, d: True)
    assert len(built) == 1
    assert "boom" in registry.status()["build_errors"]


def test_builtin_specs_build_all_tools_with_unique_names():
    from tools.builtin_registry import builtin_tool_specs

    deps = SimpleNamespace(
        db=SimpleNamespace(soul_repository=None),
        query_engine=None,
        writer=None,
        write_gateway=None,
        relationship_service=None,
        concern_tracker=None,
        jargon_service=None,
        _bot_registry={},
        book_lore_index=None,
        embedding_service=None,
        lore_db_path="",
        book_lore_catalog_scope=None,
    )
    registry = ToolRegistry()
    registry.register_many(builtin_tool_specs())
    built = registry.build(deps, capability_enabled=lambda capability, default: True)
    names = [record.name for record in registry.records()]
    assert registry.status()["build_errors"] == {}
    assert len(built) == len(names) == len(set(names)) == 18
    assert "wave_memory_record_social_impression" in names
    assert "wave_memory_search" in names


def test_extensions_register_tools(tmp_path):
    ext = tmp_path / "extensions"
    ext.mkdir()
    (ext / "hello.py").write_text(
        "from services.tool_registry import ToolSpec\n"
        "class T:\n"
        "    name = 'hello_tool'\n"
        "    description = 'hi'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def call(self, context, **kw):\n"
        "        return 'hello'\n"
        "def register(tools, **registries):\n"
        "    tools.register(ToolSpec('hello_tool', lambda d: T()))\n",
        encoding="utf-8",
    )
    (ext / "broken.py").write_text("raise RuntimeError('x')\n", encoding="utf-8")
    registry = ToolRegistry()
    assert registry.load_extensions(ext) == ["hello.py"]
    assert "broken.py" in registry.status()["extension_errors"]
    registry.build(None, capability_enabled=lambda c, d: True)
    assert asyncio.run(registry.invoke("hello_tool", scope=_scope())) == "hello"
