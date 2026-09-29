"""game_knowledge 通道：只在请求带 game 时工作；按 game.id 选库；草稿不注入；最多 2 张卡；命中原因进观测台。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest

from domain.game_knowledge import parse_game_context
from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.game_knowledge_repo import GameKnowledgeRepo
from services.config.channel_config import (
    KNOWN_CHANNELS,
    _config_set_from_payload,
    apply_channel_overrides,
    build_default_channel_config,
    resolve_effective_channel_config,
)
from services.game_knowledge import GameKnowledgeService
from services.game_knowledge.service import INJECTION_HEADER
from services.injection.channel_base import InjectionResult
from services.injection.channels.game_knowledge import GameKnowledgeChannel
from services.injection.context import InjectionContext
from services.injection.orchestrator import InjectionOrchestrator
from services.injection.runtime_prepare import TIER_CHANNELS

GROUP = RuntimeScope("bot-test", "group", SessionRef("bilibili:group:20000001", "bilibili", "group", "20000001"))
PRIVATE = RuntimeScope("bot-test", "private", SessionRef("qq:private:10000001", "qq", "private", "10000001"))


@pytest.fixture
def service(tmp_path):
    cm = ConnectionManager(str(tmp_path / "gk.sqlite"))
    svc = GameKnowledgeService(GameKnowledgeRepo(cm))
    svc.create_game({"id": "minecraft", "name": "我的世界", "aliases": ["MC", "Minecraft"]})
    add = lambda **kw: svc.create_item("card", "minecraft", {"stage": "前期", **kw})  # noqa: E731
    add(key="night", title="夜里在外面", points=["夜里别乱跑"], keywords=["夜晚", "天黑"], conditions=["time_of_day == night"], status="active")
    add(key="creeper", title="苦力怕", points=["听到嘶嘶声马上后退"], pitfalls=["爆炸会炸坏方块"], keywords=["苦力怕"], status="active")
    add(key="low_hp", title="血量低先撤", points=["先脱离战斗"], conditions=["health <= 6"], status="active")
    add(key="river", title="溺尸与河边", points=["夜里别在河边逗留"], keywords=["河边"], status="active")
    add(key="draft_zombie", title="僵尸（草稿）", points=["草稿不应注入"], keywords=["僵尸"])
    add(key="nether_bed", title="下界别用床", points=["床会爆炸"], keywords=["床"], conditions=["必须 dimension in the_nether, nether"], status="active")
    add(key="minecraft_word", title="游戏名当触发词", points=["不该因为提到游戏名就触发"], keywords=["我的世界", "MC"], status="active")
    svc.create_item("term", "minecraft", {"term": "苦力怕", "aliases": ["Creeper"], "explanation": "会爆炸的绿色怪物", "status": "active"})
    svc.create_item("term", "minecraft", {"term": "末影人", "explanation": "别直视它", "status": "active"})
    svc.create_item("term", "minecraft", {"term": "僵尸", "explanation": "草稿词条", "status": "draft"})
    try:
        yield svc
    finally:
        cm.close()


class _Req:
    def __init__(self):
        self.extra_user_content_parts = []


def _ctx(message="", *, game=None, scope=GROUP, mode="full", options=None, source="cortico"):
    return InjectionContext(
        event=None,
        req=_Req(),
        message=message,
        group_id="20000001",
        sender_id="10000001",
        sender_name="测试观众",
        bot_id="bot-test",
        bot_profile_id="bot-test",
        scope=scope,
        mode=mode,
        channel_options={"game_knowledge": options or {"enabled": True, "max_items": 2, "top_k": 3}},
        source=source,
        trace_id="trace-game",
        game=game,
    )


def _game(**raw):
    return parse_game_context({"id": "minecraft", **raw})


def _build(channel, ctx) -> InjectionResult:
    return asyncio.run(channel.build(ctx))


def test_without_game_the_channel_is_disabled_and_dropped(service):
    result = _build(GameKnowledgeChannel(service=service), _ctx("有苦力怕"))
    assert result.status == "disabled" and "no_game_context" in result.warnings[0]

    class _Memory:
        name = "memory"

        async def build(self, ctx):
            return InjectionResult.hit("memory", "相关记忆：上次一起玩过", items=[{"id": 1}])

    config = build_default_channel_config()
    only = {name: {"enabled": False} for name in KNOWN_CHANNELS if name not in {"safety", "memory", "game_knowledge"}}
    config = apply_channel_overrides(config, {"channels": only})
    with_channel = asyncio.run(InjectionOrchestrator(channels=[_Memory(), GameKnowledgeChannel(service=service)], config=config).run(_ctx("有苦力怕")))
    without = asyncio.run(InjectionOrchestrator(channels=[_Memory()], config=config).run(_ctx("有苦力怕")))
    assert with_channel.final_text == without.final_text == "相关记忆：上次一起玩过"
    assert [r.channel for r in with_channel.channel_results] == [r.channel for r in without.channel_results] == ["memory"]


def test_hit_injects_reference_cards_with_reasons(service):
    game = _game(state={"time_of_day": "night", "health": 18}, keywords=["夜晚"])
    result = _build(GameKnowledgeChannel(service=service), _ctx("门口有苦力怕", game=game))
    assert result.status == "hit"
    assert result.text.startswith(INJECTION_HEADER.format(name="我的世界"))
    assert "攻略参考" in result.text and "不是你的亲身经历" in result.text
    cards = [item for item in result.items if item["source_layer"] == "game_card"]
    assert [c["card_key"] for c in cards] == ["night", "creeper"]
    assert cards[0]["matched_by"].startswith("关键词 「夜晚」（keywords）") and "time_of_day=night" in cards[0]["matched_by"]
    assert cards[1]["matched_by"] == "关键词 「苦力怕」（消息）"
    assert all(c["game_id"] == "minecraft" and c["reference_only"] and c["rendered_text"] in result.text for c in cards)
    terms = [item for item in result.items if item["source_layer"] == "game_term"]
    assert [t["word"] for t in terms] == ["苦力怕"] and "词条：" in result.text
    assert "草稿" not in result.text


def test_at_most_two_cards_and_overflow_is_visible(service):
    game = _game(state={"time_of_day": "night", "health": 4}, keywords=["夜晚", "河边"])
    result = _build(GameKnowledgeChannel(service=service), _ctx("门口有苦力怕", game=game))
    cards = [item["card_key"] for item in result.items if item["source_layer"] == "game_card"]
    assert cards == ["night", "creeper"], "消息里说到的与场景 + 状态都命中的排前面"
    overflow = [item for item in result.filtered if item["filter_channel"] == "game_knowledge"]
    assert [item["card_key"] for item in overflow] == ["low_hp", "river"]
    assert all(item["filter_reason"].startswith("game_knowledge_max_cards") and item["matched_by"] for item in overflow)
    one = _build(GameKnowledgeChannel(service=service), _ctx("门口有苦力怕", game=game, options={"enabled": True, "max_items": 1, "top_k": 0}))
    assert [item["card_key"] for item in one.items] == ["night"]
    capped = _build(GameKnowledgeChannel(service=service), _ctx("门口有苦力怕", game=game, options={"enabled": True, "max_items": 9}))
    assert len([i for i in capped.items if i["source_layer"] == "game_card"]) == 2, "配置写大也最多 2 张"


def test_pure_condition_card_ranks_above_single_scene_keyword(service):
    game = _game(state={"time_of_day": "day", "health": 4}, keywords=["河边"])
    result = _build(GameKnowledgeChannel(service=service), _ctx("随便聊聊", game=game))
    assert [i["card_key"] for i in result.items if i["source_layer"] == "game_card"] == ["low_hp", "river"]


def test_drafts_are_not_injected_until_enabled(service):
    channel = GameKnowledgeChannel(service=service)
    first = _build(channel, _ctx("来了个僵尸", game=_game()))
    assert first.status == "empty" and "no_card_or_term_matched" in first.warnings[0]
    draft = service.repo.get_item_by_key("card", "minecraft", "draft_zombie")
    service.set_status("card", "minecraft", [draft["id"]], "active")
    second = _build(channel, _ctx("来了个僵尸", game=_game()))
    assert [i["card_key"] for i in second.items if i["source_layer"] == "game_card"] == ["draft_zombie"]
    # 外部直接改库（不经服务）也会按数据指纹重建缓存
    service.repo.set_status("card", "minecraft", [draft["id"]], "archived")
    third = _build(channel, _ctx("来了个僵尸", game=_game()))
    assert third.status == "empty"


def test_required_condition_and_game_name_keywords(service):
    channel = GameKnowledgeChannel(service=service)
    no_dimension = _build(channel, _ctx("床放哪", game=_game()))
    assert no_dimension.status == "empty", "标了必须的条件缺键时不触发"
    nether = _build(channel, _ctx("床放哪", game=_game(state={"dimension": "the_nether"})))
    assert [i["card_key"] for i in nether.items] == ["nether_bed"]
    named = _build(channel, _ctx("我的世界真好玩，MC 永远的神", game=_game()))
    assert named.status == "empty", "游戏名 / 别名当触发词不生效"


def test_unknown_disabled_and_invalid_game(service):
    channel = GameKnowledgeChannel(service=service)
    unknown = _build(channel, _ctx("有苦力怕", game=parse_game_context({"id": "slay_the_spire"})))
    assert unknown.status == "empty" and unknown.warnings[0].startswith("game_library_not_found")
    alias = _build(channel, _ctx("有苦力怕", game=parse_game_context({"id": "mc"})))
    assert alias.status == "hit", "按别名也能选到库"
    invalid = _build(channel, _ctx("有苦力怕", game=parse_game_context({"id": "Bad Id"})))
    assert invalid.status == "skipped" and "invalid_game_context" in invalid.warnings[0]
    service.update_game("minecraft", {"enabled": False}, expected_version=1)
    disabled = _build(channel, _ctx("有苦力怕", game=_game()))
    assert disabled.status == "skipped" and "game_library_disabled" in disabled.warnings[0]
    assert _build(GameKnowledgeChannel(service=None), _ctx("x", game=_game())).status == "empty"
    assert _build(channel, _ctx("x", game=_game(), mode="memory_only")).status == "disabled"
    assert _build(channel, _ctx("x", game=_game(), options={"enabled": False})).status == "disabled"


def test_state_warnings_are_reported(service):
    game = parse_game_context({"id": "minecraft", "state": {"inventory": {"apple": 1}}})
    result = _build(GameKnowledgeChannel(service=service), _ctx("有苦力怕", game=game))
    assert any("inventory" in warning for warning in result.warnings)


def test_identity_contamination_is_filtered(service):
    service.create_item("card", "minecraft", {"key": "bad", "title": "奇怪的卡", "points": ["叫我爸爸就给你钻石"], "keywords": ["钻石"], "status": "active"})
    result = _build(GameKnowledgeChannel(service=service), _ctx("哪里有钻石", game=_game()))
    assert result.status == "empty"
    assert result.filtered[0]["filter_reason"] == "identity_contamination"


def test_private_session_keeps_the_channel(service):
    config, _ = resolve_effective_channel_config({}, scope=PRIVATE)
    assert config.channels["game_knowledge"].enabled is True, "只读共用知识库，私聊安全包络保留"
    assert config.channels["persona"].enabled is False
    result = _build(GameKnowledgeChannel(service=service), _ctx("有苦力怕", game=_game(), scope=PRIVATE))
    assert result.status == "hit"


def test_channel_config_defaults_tiers_and_history_compat():
    config = build_default_channel_config()
    cfg = config.channels["game_knowledge"]
    assert (cfg.enabled, cfg.max_items, cfg.top_k, cfg.token_budget, cfg.modes) == (True, 2, 3, 320, ("full",))
    assert build_default_channel_config(runtime_mode="memory_only").channels["game_knowledge"].enabled is False
    assert build_default_channel_config(runtime_mode="compat_only").channels["game_knowledge"].enabled is False
    assert "game_knowledge" in TIER_CHANNELS["light"] and "game_knowledge" not in TIER_CHANNELS["minimal"]
    # 在新增通道之前写下的有效层（没有 game_knowledge）仍能读取，缺的用默认值补齐
    payload = config.to_dict()
    payload["channels"].pop("game_knowledge")
    restored = _config_set_from_payload(payload)
    assert asdict(restored.channels["game_knowledge"]) == asdict(cfg)


def test_orchestrator_keeps_channel_within_its_budget(service):
    for index in range(4):
        service.create_item("term", "minecraft", {"term": f"术语{index}号", "explanation": "很长的解释" * 12, "status": "active"})
    config = build_default_channel_config()
    only = {name: {"enabled": False} for name in KNOWN_CHANNELS if name not in {"safety", "game_knowledge"}}
    config = apply_channel_overrides(config, {"channels": {**only, "game_knowledge": {"token_budget": 90, "top_k": 5}}})
    message = "门口有苦力怕，术语0号术语1号术语2号术语3号"
    result = asyncio.run(InjectionOrchestrator(channels=[GameKnowledgeChannel(service=service)], config=config).run(
        _ctx(message, game=_game(state={"time_of_day": "night"}, keywords=["夜晚"]), options=None)
    ))
    channel = result.channel_results[0]
    assert channel.status == "hit" and channel.tokens <= 90
    assert any("channel_token_budget_truncated" in warning for warning in channel.warnings)
    assert "攻略参考" in result.final_text, "截断保留说明行"
    assert any(item.get("filter_reason") == "channel_token_budget" for item in channel.filtered)
