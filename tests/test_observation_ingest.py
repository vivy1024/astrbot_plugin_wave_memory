"""Runtime 观察写入：与 AstrBot 消息钩子共用 InboundMessagePipeline 与 Bot 回复处理。"""

from __future__ import annotations
from plugin_source import helper_nodes, plugin_class_node, plugin_method, plugin_source_text

import ast
import asyncio
import time
import pytest
from dataclasses import dataclass
from pathlib import Path

from domain.bot_profile import BotProfile
from domain.scope import RuntimeScope, SessionRef
from services.bot_registry import BotRegistry
from services.inbound_message_handler import InboundMessagePipeline

ROOT = Path(__file__).resolve().parents[1]


def _load_ingest():
    obs_class = helper_nodes()["_ObservationEvent"]
    method = plugin_method("ingest_observation")
    for arg in (*method.args.args, *method.args.kwonlyargs):
        arg.annotation = None
    method.returns = None
    module = ast.fix_missing_locations(ast.Module(body=[obs_class, method], type_ignores=[]))
    namespace = {"dataclass": dataclass, "time": time, "RuntimeScope": RuntimeScope}
    exec(compile(module, "main.py", "exec"), namespace)
    return namespace["ingest_observation"]


class _Writer:
    def __init__(self):
        self.items = []

    async def enqueue(self, item):
        self.items.append(item)


class _Plugin:
    def __init__(self):
        profile = BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "qq_id": "10001", "session_prefix": "羽书"})
        self.bot_registry = BotRegistry.from_profiles([profile])
        self.writer = _Writer()
        self.jargon_service = None
        self.self_reflect = None
        self.lifecycle = None
        self.desire_engine = None
        self._bot_qq_ids = ["10001"]
        self.group_names = {}
        self.bot_replies = []
        self.inbound_pipeline = InboundMessagePipeline(self)

    def _remember_group_name(self, bot_id, group_id, name):
        self.group_names[(bot_id, group_id)] = name

    async def _process_bot_reply(self, **kwargs):
        self.bot_replies.append(kwargs)


def _scope(principal="羽书:user:u1"):
    return RuntimeScope("yushu", "group", SessionRef("羽书:group:100", "羽书", "group", "100"), subject_principal_id=principal)


def test_message_goes_through_shared_pipeline_with_platform_message_id():
    ingest = _load_ingest()
    plugin = _Plugin()
    receipt = asyncio.run(ingest(plugin, {"event_id": 777, "content": "今天一起打尖塔", "sender_name": "阿一", "group_name": "粉丝群"}, _scope()))
    assert receipt == {"status": "accepted", "kind": "message"}
    item = plugin.writer.items[0]
    assert item["event_id"] == 777 and item["sender_id"] == "u1" and item["scope"].session.id == "羽书:group:100"
    assert plugin.group_names[("yushu", "100")] == "粉丝群"


def test_remember_prefix_behaves_like_astrbot():
    ingest = _load_ingest()
    plugin = _Plugin()
    asyncio.run(ingest(plugin, {"event_id": 1, "content": "记住 我周五要考试", "is_at_bot": True}, _scope()))
    assert plugin.writer.items[0]["content"] == "[用户要求记住] 我周五要考试"
    assert plugin.writer.items[0]["source"] == "explicit"


def test_prefix_words_in_ordinary_group_chat_are_just_messages():
    """没对 Bot 说时，「记住了」「忘记带伞了」只是普通聊天：照常入库，不当指令。"""
    ingest = _load_ingest()
    plugin = _Plugin()
    asyncio.run(ingest(plugin, {"event_id": 11, "content": "记住了记住了下次一定"}, _scope()))
    asyncio.run(ingest(plugin, {"event_id": 12, "content": "忘记带伞了淋成狗"}, _scope()))
    assert [item["content"] for item in plugin.writer.items] == ["记住了记住了下次一定", "忘记带伞了淋成狗"]
    assert all(item.get("source") != "explicit" for item in plugin.writer.items)


def test_directed_forget_prefix_skips_this_message():
    ingest = _load_ingest()
    plugin = _Plugin()
    asyncio.run(ingest(plugin, {"event_id": 13, "content": "别记 我刚才说的地址", "is_at_bot": True}, _scope()))
    assert plugin.writer.items == []


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("content", ["忘记带伞了", "记住了，下次一定", "forgettable evening", "remembering yesterday", "记住："])
def test_directed_ordinary_prefixes_are_still_stored(private, content):
    plugin = _Plugin()
    scope = _scope()
    if private:
        scope = RuntimeScope("yushu", "private", SessionRef("羽书:private:u1", "羽书", "private", "u1"),
                             subject_principal_id="羽书:user:u1")
    asyncio.run(_load_ingest()(plugin, {"event_id": 14, "content": content, "is_at_bot": True}, scope))
    assert plugin.writer.items[0]["content"] == content
    assert plugin.writer.items[0].get("source") != "explicit"


@pytest.mark.parametrize("content", ["记住：阿木", "/remember 阿木", "REMEMBER 阿木"])
def test_explicit_short_memory_is_not_silently_dropped(content):
    plugin = _Plugin()
    asyncio.run(_load_ingest()(plugin, {"event_id": 15, "content": content, "is_at_bot": True}, _scope()))
    assert plugin.writer.items[0]["content"] == "[用户要求记住] 阿木"
    assert plugin.writer.items[0]["source"] == "explicit"


def test_self_kind_uses_bot_reply_path_and_rejects_bad_input():
    ingest = _load_ingest()
    plugin = _Plugin()
    receipt = asyncio.run(ingest(plugin, {"event_id": "m9", "kind": "self", "content": "我也想玩"}, _scope()))
    assert receipt["kind"] == "self"
    assert plugin.bot_replies[0]["message_id"] == "m9" and plugin.bot_replies[0]["bot_self_id"] == "10001"
    assert asyncio.run(ingest(plugin, {"event_id": 2, "content": ""}, _scope()))["error"] == "empty_content"
    assert asyncio.run(ingest(plugin, {"event_id": 3, "content": "hi there", "kind": "weird"}, _scope()))["status"] == "rejected"
    no_sender = asyncio.run(ingest(plugin, {"event_id": 4, "content": "no sender here"}, _scope(principal=None)))
    assert no_sender["error"] == "sender_id_required"
