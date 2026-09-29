"""Runtime API 的 game_event：Bot 自己的游戏经历按她的经历入库，不走群友处理。"""

from __future__ import annotations

import ast
import asyncio
import time
from dataclasses import dataclass

import pytest

from domain.bot_profile import BotProfile
from domain.scope import RuntimeScope, SessionRef
from plugin_source import helper_nodes, plugin_method
from services.bot_registry import BotRegistry
from services.game_events import GameEventError, game_event_payload

LIVE = SessionRef("bilibili:group:30000001", "bilibili", "group", "30000001")


def _live_scope(subject: str | None = None) -> RuntimeScope:
    return RuntimeScope("yushu", "group", LIVE, subject_principal_id=subject)


def _event(**overrides):
    base = {
        "event_id": "cortico:101",
        "kind": "game_event",
        "game": "minecraft",
        "event_type": "death",
        "content": "[我的世界] 今天第 2 次死亡：被打死（yushu was slain by Zombie）",
        "timestamp": 1790000000.5,
    }
    base.update(overrides)
    return base


class TestGameEventPayload:
    def test_bot_experience_payload(self):
        item = game_event_payload(_event(), _live_scope("bilibili:user:10000001"), bot_name="羽书")
        assert item["scope"].subject_principal_id is None
        assert item["scope"].session == LIVE and item["scope"].visibility == "group"
        assert item["group_id"] == "30000001"
        assert item["sender_id"] == "bot" and item["sender_name"] == "羽书"
        assert item["source"] == "game_event"
        assert item["event_id"] == "game_event:cortico:101"
        assert item["timestamp"] == 1790000000.5
        assert item["content"].startswith("[我的世界] 今天第 2 次死亡")
        assert item["metadata"] == {"origin_kind": "runtime:game_event:minecraft:death"}

    def test_prefix_added_when_missing_and_whitespace_collapsed(self):
        item = game_event_payload(_event(game="sts", event_type="milestone", content="  打进了第 2 幕\n生命 50/80 "), _live_scope(), bot_name="羽书")
        assert item["content"] == "[杀戮尖塔] 打进了第 2 幕 生命 50/80"
        unknown = game_event_payload(_event(game="terraria", content="打败了克苏鲁之眼"), _live_scope(), bot_name="羽书")
        assert unknown["content"] == "[terraria] 打败了克苏鲁之眼"

    def test_bad_timestamp_falls_back_to_now(self):
        for bad in (None, "", "abc", float("nan"), -1, True):
            item = game_event_payload(_event(timestamp=bad), _live_scope(), bot_name="羽书", now=123.0)
            assert item["timestamp"] == 123.0

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"event_id": ""}, "event_id_required"),
            ({"game": ""}, "invalid_game"),
            ({"game": "MineCraft"}, "invalid_game"),
            ({"game": "我的世界"}, "invalid_game"),
            ({"event_type": ""}, "invalid_event_type"),
            ({"event_type": "death;drop table"}, "invalid_event_type"),
            ({"content": "   "}, "empty_content"),
        ],
    )
    def test_illegal_values_rejected(self, overrides, code):
        with pytest.raises(GameEventError) as err:
            game_event_payload(_event(**overrides), _live_scope(), bot_name="羽书")
        assert err.value.code == code

    def test_private_scope_rejected(self):
        private = RuntimeScope("yushu", "private", SessionRef("terminal:private:operator", "terminal", "private", "operator"),
                               subject_principal_id="terminal:user:operator")
        with pytest.raises(GameEventError) as err:
            game_event_payload(_event(), private, bot_name="羽书")
        assert err.value.code == "game_event_requires_group_scope"


def _load(*names):
    obs_class = helper_nodes()["_ObservationEvent"]
    nodes = [obs_class]
    for name in names:
        method = plugin_method(name)
        for arg in (*method.args.args, *method.args.kwonlyargs):
            arg.annotation = None
        method.returns = None
        nodes.append(method)
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    namespace = {
        "dataclass": dataclass,
        "time": time,
        "RuntimeScope": RuntimeScope,
        "GameEventError": GameEventError,
        "game_event_payload": game_event_payload,
    }
    exec(compile(module, "main.py", "exec"), namespace)
    return namespace


class _Writer:
    def __init__(self):
        self.items = []

    async def enqueue(self, item):
        self.items.append(item)


class _NoPipeline:
    async def process_message(self, **kwargs):  # pragma: no cover - 走到这里就是错
        raise AssertionError("game_event 不应走群友消息的处理")


class _Plugin:
    def __init__(self, namespace):
        profile = BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "qq_id": "10001", "session_prefix": "羽书"})
        self.bot_registry = BotRegistry.from_profiles([profile])
        self.writer = _Writer()
        self.inbound_pipeline = _NoPipeline()
        self.bot_replies = []
        self._ns = namespace

    def _remember_group_name(self, *args):  # pragma: no cover
        raise AssertionError("game_event 不记群名")

    async def _process_bot_reply(self, **kwargs):
        self.bot_replies.append(kwargs)

    async def _process_game_event(self, observation, runtime_scope):
        return await self._ns["_process_game_event"](self, observation, runtime_scope)

    def _bot_display_name(self, db_id):
        return self._ns["_bot_display_name"](self, db_id)


class TestIngestObservation:
    def _setup(self):
        ns = _load("ingest_observation", "_process_game_event", "_bot_display_name")
        return ns["ingest_observation"], _Plugin(ns)

    def test_game_event_is_written_as_bot_experience(self):
        ingest, plugin = self._setup()
        receipt = asyncio.run(ingest(plugin, _event(), _live_scope("bilibili:user:10000001")))
        assert receipt == {"status": "accepted", "kind": "game_event"}
        assert plugin.bot_replies == []
        [item] = plugin.writer.items
        assert item["source"] == "game_event" and item["sender_id"] == "bot" and item["sender_name"] == "羽书"
        assert item["scope"].subject_principal_id is None
        assert item["event_id"] == "game_event:cortico:101"

    def test_replay_uses_the_same_idempotency_key(self):
        ingest, plugin = self._setup()
        asyncio.run(ingest(plugin, _event(), _live_scope()))
        asyncio.run(ingest(plugin, _event(), _live_scope()))
        first, second = plugin.writer.items
        assert first == second

    def test_invalid_game_event_rejected_without_writing(self):
        ingest, plugin = self._setup()
        assert asyncio.run(ingest(plugin, _event(game="Bad Game"), _live_scope())) == {"status": "rejected", "error": "invalid_game"}
        assert asyncio.run(ingest(plugin, _event(content=""), _live_scope()))["error"] == "empty_content"
        assert plugin.writer.items == []

    def test_self_passes_spoken_timestamp(self):
        ingest, plugin = self._setup()
        asyncio.run(ingest(plugin, {"event_id": "cortico:7", "kind": "self", "content": "各位晚上好", "timestamp": 1790000001.0}, _live_scope()))
        assert plugin.bot_replies[0]["timestamp"] == 1790000001.0
        assert plugin.bot_replies[0]["bot_text"] == "各位晚上好"


@pytest.mark.asyncio
async def test_runtime_batch_does_not_attach_subject_to_game_event():
    from quart import Quart

    from webui.blueprints.runtime import runtime_bp
    from webui.container import get_container

    container = get_container()
    saved = (getattr(container, "bot_registry", None), getattr(container, "observation_ingestor", None))
    profile = BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "qq_id": "10001"})
    container.bot_registry = BotRegistry.from_profiles([profile])
    seen = []

    async def ingest(event, scope):
        seen.append((event, scope))
        return {"status": "accepted", "kind": event.get("kind")}

    container.observation_ingestor = ingest
    app = Quart(__name__)
    app.register_blueprint(runtime_bp)
    scope = {"bot_id": "yushu", "visibility": "group", "session": {"id": "bilibili:group:30000001"}}
    try:
        res = await app.test_client().post(
            "/api/runtime/v1/observations/batch",
            headers={"Authorization": "Bearer yushu-dev-token"},
            json={"events": [
                {**_event(), "sender_id": "10000001", "scope": scope},
                {"event_id": "cortico:102", "kind": "danmaku", "content": "666", "sender_id": "10000001", "scope": scope},
            ]},
        )
        data = await res.get_json()
        assert data["accepted"] == 2
        assert seen[0][1].subject_principal_id is None
        assert seen[1][1].subject_principal_id == "bilibili:user:10000001"
    finally:
        container.bot_registry, container.observation_ingestor = saved


def test_game_events_are_recallable_durable_and_labelled():
    from engine.query_engine import QueryEngine
    from services.injection.channels.memory_recall import _DEFAULT_SOURCE_FILTER
    from services.storage_capacity_policy import StorageCapacityPolicy, decide_storage_admission

    assert "game_event" in _DEFAULT_SOURCE_FILTER
    decision = decide_storage_admission(
        source="game_event",
        active_count=100,
        policy=StorageCapacityPolicy(max_active_memories=100, cold_chat_when_over_capacity=True, enabled=True),
        has_vector=True,
    )
    assert decision.keep_vector is True
    tag = QueryEngine._attribution_tag(
        {"sender_id": "bot", "source": "game_event", "content": "[我的世界] 驯服了狼"},
        speaker_id="10000001",
        bot_id_set={"yushu"},
    )
    assert tag == "[你的游戏经历] "
