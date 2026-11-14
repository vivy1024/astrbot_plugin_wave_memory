"""合并工具：薄路由。逻辑不重复，schema 是并集，必填校验在转发前，mode 按 Bot 权限动态裁剪。"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any

if "astrbot.api" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass

    _api.logger = _Logger()
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

import pytest

from domain.bot_profile import BotProfile
from tools import names as N
from tools.merged import MergedTool, ModeSpec, forward_arguments, missing_required, union_schema
from tools.merged_families import FAMILIES, build_merged_tool


@dataclass
class _Child:
    name: str
    parameters: dict
    calls: list = field(default_factory=list)

    async def call(self, ctx: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"{self.name}:{sorted(kwargs)}"


def _children() -> dict[str, _Child]:
    return {
        N.SEARCH: _Child(N.SEARCH, {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}),
        N.PERSON_SEARCH: _Child(N.PERSON_SEARCH, {"type": "object", "properties": {
            "person": {"type": "string"}, "query": {"type": "string"},
            "query_type": {"type": "string", "enum": ["recent", "profile"]}}, "required": ["person"]}),
        N.AFFINITY: _Child(N.AFFINITY, {"type": "object", "properties": {
            "target_user": {"type": "string"},
            "mode": {"type": "string", "enum": ["single", "ranking"]}, "limit": {"type": "integer"}}, "required": []}),
    }


def _recall(children, is_allowed=None) -> MergedTool:
    _, modes, _ = FAMILIES[N.RECALL]
    return MergedTool(name=N.RECALL, intro="查记忆", modes=modes, resolve_child=children.get, is_allowed=is_allowed)


def _run(coro):
    return asyncio.run(coro)


def test_schema_is_the_union_and_keeps_types_and_enums():
    schema = _recall(_children()).parameters
    props = schema["properties"]
    assert schema["required"] == ["mode"]
    assert props["mode"]["enum"] == ["search", "person", "relation"], "只列实际存在的子工具"
    assert {"query", "limit", "person", "query_type", "target_user", "view"} <= set(props)
    assert props["query_type"]["enum"] == ["recent", "profile"]
    assert props["view"]["enum"] == ["single", "ranking"], "affinity 自己的 mode 在合并工具里叫 view"
    assert "mode" in props and props["mode"]["type"] == "string"


def test_dispatch_forwards_only_the_arguments_that_child_declares():
    children = _children()
    out = _run(_recall(children).dispatch(None, mode="search", query="羽书", limit=3, person="别人", view="ranking"))
    assert children[N.SEARCH].calls == [{"query": "羽书", "limit": 3}]
    assert "person" not in children[N.SEARCH].calls[0] and out.startswith(N.SEARCH)


def test_relation_view_is_renamed_to_the_childs_mode():
    children = _children()
    _run(_recall(children).dispatch(None, mode="relation", target_user="阿木", view="ranking", limit=5))
    assert children[N.AFFINITY].calls == [{"target_user": "阿木", "mode": "ranking", "limit": 5}]


def test_missing_required_is_reported_before_calling_the_child():
    children = _children()
    reply = _run(_recall(children).dispatch(None, mode="search"))
    assert "缺少必填参数" in reply and "query" in reply
    assert children[N.SEARCH].calls == []


def test_unknown_mode_lists_the_valid_ones():
    reply = _run(_recall(_children()).dispatch(None, mode="nope"))
    assert "search" in reply and "person" in reply and "relation" in reply


def test_empty_values_are_not_forwarded():
    children = _children()
    _run(_recall(children).dispatch(None, mode="relation", target_user="", view=None))
    assert children[N.AFFINITY].calls == [{}]


def test_disallowed_mode_is_refused_and_hidden_modes_stay_out_of_the_schema():
    children = _children()
    allowed = lambda ctx, tool, mode: mode != "person"
    router = _recall(children, is_allowed=allowed)
    reply = _run(router.dispatch(None, mode="person", person="阿木"))
    assert "不可用" in reply and children[N.PERSON_SEARCH].calls == []


def test_missing_child_removes_that_mode():
    children = _children()
    children.pop(N.PERSON_SEARCH)
    assert _recall(children).parameters["properties"]["mode"]["enum"] == ["search", "relation"]


def test_description_lists_every_available_mode_once():
    text = _recall(_children()).description
    assert text.count("mode=search") == 1 and text.count("mode=relation") == 1 and "mode=facts" not in text


def test_build_returns_none_when_no_child_exists():
    assert build_merged_tool(N.RECALL, lambda _name: None) is None


def test_diary_mode_is_not_exposed_to_the_model():
    child = _Child(N.NOTE_EPISODE, {"type": "object", "properties": {"outcome": {"type": "string"}}, "required": []})
    diary = _Child(N.RECORD_DIARY_EPISODE, {"type": "object", "properties": {"diary_title": {"type": "string"}}, "required": []})
    tool = build_merged_tool(N.EPISODE, {N.NOTE_EPISODE: child, N.RECORD_DIARY_EPISODE: diary}.get)
    assert tool.parameters["properties"]["mode"]["enum"] == ["group"]


def test_union_helpers_are_pure():
    spec = ModeSpec("x", "t", "s", fixed={"mode": "single"})
    schema = union_schema([(spec, {"properties": {"mode": {"type": "string"}, "q": {"type": "string"}}})], mode_description="d")
    assert "q" in schema["properties"] and schema["properties"]["mode"]["enum"] == ["x"]
    assert forward_arguments(spec, {"properties": {"mode": {}, "q": {}}}, {"q": "a"}) == {"q": "a", "mode": "single"}
    assert missing_required({"required": ["q"]}, {}) == ["q"]


# ── Bot 权限：旧名 / 新名 / 名:mode 都能解释 ──


def _profile(**kwargs) -> BotProfile:
    return BotProfile.from_dict({"db_id": "yushu", "name": "羽书", **kwargs})


def test_empty_lists_allow_everything():
    profile = _profile()
    assert profile.tool_enabled(N.RECALL) and profile.tool_enabled(N.RECALL, "person")
    assert profile.tool_enabled("echo")


def test_deny_a_single_mode_keeps_the_rest_of_the_tool():
    profile = _profile(tools_deny=["wave_memory_social:concern"])
    assert profile.tool_enabled(N.SOCIAL) is True
    assert profile.tool_enabled(N.SOCIAL, "concern") is False
    assert profile.tool_enabled(N.SOCIAL, "impression") is True


def test_legacy_name_in_deny_maps_to_the_right_mode():
    profile = _profile(tools_deny=[N.NOTE_CONCERN])
    assert profile.tool_enabled(N.SOCIAL, "concern") is False
    assert profile.tool_enabled(N.SOCIAL, "anchor") is True
    assert profile.tool_enabled(N.NOTE_CONCERN) is False, "旧名也按旧名查"


def test_deny_the_whole_merged_tool():
    profile = _profile(tools_deny=[N.RECALL])
    assert not profile.tool_enabled(N.RECALL) and not profile.tool_enabled(N.RECALL, "search")


def test_allow_listing_only_some_modes():
    profile = _profile(tools_allow=["wave_memory_recall:search", "wave_memory_recall:facts"])
    assert profile.tool_enabled(N.RECALL, "search") and profile.tool_enabled(N.RECALL, "facts")
    assert not profile.tool_enabled(N.RECALL, "person")
    assert not profile.tool_enabled(N.SOCIAL)
    assert profile.allowed_modes(N.RECALL, ["search", "facts", "person"]) == ["search", "facts"]


def test_allow_with_legacy_names_still_works():
    profile = _profile(tools_allow=[N.SEARCH, N.HOLD_PEACE])
    assert profile.tool_enabled(N.RECALL, "search") and profile.tool_enabled(N.HOLD_PEACE)
    assert not profile.tool_enabled(N.RECALL, "person")


def test_deny_wins_over_allow():
    profile = _profile(tools_allow=[N.RECALL], tools_deny=["wave_memory_recall:person"])
    assert profile.tool_enabled(N.RECALL, "search") and not profile.tool_enabled(N.RECALL, "person")
