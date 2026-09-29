"""/context/prepare 的 game 字段：不传时行为与之前完全一致；传了才由 game_knowledge 通道选库注入，并记下当前在玩的游戏。"""

from __future__ import annotations

import pytest
from quart import Quart

from domain.bot_profile import BotProfile
from engine.db.connection import ConnectionManager
from engine.db.game_knowledge_repo import GameKnowledgeRepo
from services.bot_registry import BotRegistry
from services.config.channel_config import build_channel_config_from_plugin_config
from services.game_knowledge import GameKnowledgeService
from services.injection.channel_base import InjectionResult
from services.injection.channels.game_knowledge import GameKnowledgeChannel
from services.injection.runtime_prepare import RuntimeContextPreparer
from webui.blueprints.runtime import runtime_bp
from webui.container import get_container

HEADERS = {"Authorization": "Bearer yushu-dev-token"}
SCOPE = {"bot_id": "yushu", "visibility": "group", "session": {"id": "bilibili:group:20000001", "platform_id": "bilibili", "kind": "group", "conversation_id": "20000001"}}


class _Memory:
    name = "memory"

    async def build(self, ctx):
        return InjectionResult.hit("memory", f"相关记忆：{ctx.message}", items=[{"id": 1}])


class _SpyPreparer(RuntimeContextPreparer):
    """记录 prepare 收到的参数，确认不传 game 时调用与之前一致。"""

    calls: list[dict]

    async def prepare(self, **kwargs):
        self.calls.append(dict(kwargs))
        return await super().prepare(**kwargs)


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "gk.sqlite"))
    service = GameKnowledgeService(GameKnowledgeRepo(cm))
    service.create_game({"id": "minecraft", "name": "我的世界"})
    service.create_item("card", "minecraft", {"key": "creeper", "title": "苦力怕", "points": ["听到嘶嘶声马上后退"], "keywords": ["苦力怕"], "status": "active"})
    service.create_item("card", "minecraft", {"key": "night", "title": "夜里在外面", "points": ["夜里别乱跑"], "keywords": ["夜晚"], "conditions": ["time_of_day == night"], "status": "active"})
    container = get_container()
    saved = (container.bot_registry, container.runtime_context_preparer)
    container.bot_registry = BotRegistry.from_profiles([BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "qq_id": "10001"})])

    def install(channels):
        preparer = _SpyPreparer(
            channels_provider=lambda: channels,
            config_resolver=lambda scope: build_channel_config_from_plugin_config({}, scope=scope),
            context_config_builder=lambda **kwargs: {},
            query_options_factory=lambda cfg: None,
            game_context_sink=service.note_active_game,
        )
        preparer.calls = []
        container.runtime_context_preparer = preparer
        return preparer

    app = Quart(__name__)
    app.register_blueprint(runtime_bp)
    try:
        yield app.test_client(), service, install
    finally:
        container.bot_registry, container.runtime_context_preparer = saved
        cm.close()


def _stable(data: dict) -> dict:
    """去掉耗时与 trace 编号这类每次都不同的字段。"""
    stable = {key: value for key, value in data.items() if key not in {"elapsed_ms", "trace_id"}}
    stable["channels"] = {
        name: {k: v for k, v in stats.items() if k != "latency_ms"} for name, stats in (data.get("channels") or {}).items()
    }
    return stable


@pytest.mark.asyncio
async def test_without_game_behaviour_is_unchanged(env):
    client, service, install = env
    body = {"text": "门口有苦力怕", "tier": "light", "scope": SCOPE}
    install([_Memory()])
    before = await (await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=body)).get_json()
    preparer = install([_Memory(), GameKnowledgeChannel(service=service)])
    after = await (await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=body)).get_json()
    assert before["ok"] and after["ok"]
    assert _stable(after) == _stable(before), "不带 game：注入块、通道统计与之前完全一致"
    assert "game_knowledge" not in after["channels"]
    assert "game" not in preparer.calls[0], "不带 game 时连参数都不传"
    assert service.active_game(type("S", (), {"bot_id": "yushu", "session": None})()) is None


@pytest.mark.asyncio
async def test_with_game_the_channel_injects_and_records_active_game(env):
    client, service, install = env
    preparer = install([_Memory(), GameKnowledgeChannel(service=service)])
    body = {
        "text": "门口有苦力怕",
        "tier": "light",
        "scope": SCOPE,
        "game": {"id": "minecraft", "state": {"time_of_day": "night", "health": 6}, "keywords": ["夜晚", "河边"]},
    }
    data = await (await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=body)).get_json()
    assert data["ok"] and data["channels"]["game_knowledge"]["status"] == "hit"
    assert data["channels"]["game_knowledge"]["items"] == 2
    assert "[游戏攻略参考 · 我的世界" in data["block"] and "【苦力怕" in data["block"] and "【夜里在外面" in data["block"]
    assert data["block"].index("相关记忆") < data["block"].index("游戏攻略参考"), "按通道优先级排在记忆之后"
    game = preparer.calls[0]["game"]
    assert game.id == "minecraft" and game.keywords == ("夜晚", "河边") and game.state["health"] == 6

    from domain.scope import RuntimeScope, SessionRef

    scope = RuntimeScope("yushu", "group", SessionRef("bilibili:group:20000001", "bilibili", "group", "20000001"))
    assert service.active_game(scope) == ("minecraft", "session")
    other = RuntimeScope("yushu", "group", SessionRef("qq:group:20000002", "qq", "group", "20000002"))
    assert service.active_game(other) == ("minecraft", "bot"), "同一个 Bot 在别处被问到时也知道她在玩什么"


@pytest.mark.asyncio
async def test_minimal_tier_skips_channel_but_dry_run_does_not_record(env):
    client, service, install = env
    install([_Memory(), GameKnowledgeChannel(service=service)])
    minimal = {"text": "有苦力怕", "tier": "minimal", "scope": SCOPE, "game": {"id": "minecraft"}}
    data = await (await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=minimal)).get_json()
    assert "game_knowledge" not in data["channels"]
    dry = {"text": "有苦力怕", "tier": "light", "scope": SCOPE, "game": {"id": "minecraft"}, "dry_run": True}
    service._active_sessions.clear()
    service._active_bots.clear()
    await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=dry)
    assert service._active_bots == {}, "dry_run 不记当前游戏"


@pytest.mark.asyncio
async def test_invalid_game_payload_does_not_break_injection(env):
    client, service, install = env
    install([_Memory(), GameKnowledgeChannel(service=service)])
    body = {"text": "有苦力怕", "tier": "light", "scope": SCOPE, "game": {"id": "Not Valid", "state": "oops"}}
    res = await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=body)
    data = await res.get_json()
    assert res.status_code == 200 and data["ok"]
    assert data["channels"]["memory"]["status"] == "hit"
    assert data["channels"]["game_knowledge"]["status"] == "skipped"
    unknown = {"text": "有苦力怕", "tier": "light", "scope": SCOPE, "game": {"id": "slay_the_spire"}}
    data = await (await client.post("/api/runtime/v1/context/prepare", headers=HEADERS, json=unknown)).get_json()
    assert data["channels"]["game_knowledge"]["status"] == "empty"
