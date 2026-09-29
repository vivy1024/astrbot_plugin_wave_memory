"""wave_memory_game_knowledge：查攻略卡与词条、总长上限、省略游戏时用当前在玩的游戏、草稿查不到。"""

from __future__ import annotations

import asyncio

import pytest

from domain.game_knowledge import parse_game_context
from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.game_knowledge_repo import GameKnowledgeRepo
from services.game_knowledge import GameKnowledgeService
from services.game_knowledge.service import TOOL_FOOTER, TOOL_MAX_CHARS
from services.tool_registry import ToolRegistry, ToolSpec, runtime_tool_context
from tools.game_knowledge import WaveMemoryGameKnowledgeTool

LIVE = RuntimeScope("bot-test", "group", SessionRef("bilibili:group:20000001", "bilibili", "group", "20000001"))
QQ = RuntimeScope("bot-test", "group", SessionRef("qq:group:20000002", "qq", "group", "20000002"))
OTHER_BOT = RuntimeScope("bot-other", "group", SessionRef("qq:group:20000002", "qq", "group", "20000002"))


@pytest.fixture
def service(tmp_path):
    cm = ConnectionManager(str(tmp_path / "gk.sqlite"))
    svc = GameKnowledgeService(GameKnowledgeRepo(cm))
    svc.create_game({"id": "minecraft", "name": "我的世界", "aliases": ["MC"]})
    svc.create_game({"id": "oni", "name": "缺氧", "aliases": ["ONI"]})
    svc.create_item("card", "minecraft", {
        "key": "creeper", "title": "苦力怕", "stage": "前期", "points": ["听到嘶嘶声马上后退", "用弓箭远程打"],
        "pitfalls": ["爆炸会炸坏方块"], "keywords": ["苦力怕", "爬行者"], "source": "测试来源 A", "status": "active",
    })
    svc.create_item("card", "minecraft", {"key": "night", "title": "第一夜怎么过", "points": ["天黑前封好房子"], "keywords": ["第一夜"], "status": "active"})
    svc.create_item("card", "minecraft", {"key": "draft", "title": "末影人（草稿）", "points": ["别直视末影人"], "keywords": ["末影人"]})
    svc.create_item("term", "minecraft", {"term": "苦力怕", "aliases": ["Creeper"], "explanation": "会爆炸的绿色怪物", "status": "active"})
    svc.create_item("term", "minecraft", {"term": "末影人", "explanation": "草稿词条", "status": "draft"})
    svc.create_item("card", "oni", {"key": "oni_ep08", "title": "EP08 制氧模块", "points": ["电解水制氧，上方收集氢气"], "keywords": ["制氧"], "status": "active"})
    try:
        yield svc
    finally:
        cm.close()


def _call(tool, scope=None, **kwargs):
    return asyncio.run(tool.call(runtime_tool_context(scope) if scope is not None else None, **kwargs))


def test_query_returns_cards_and_terms_marked_as_reference(service):
    text = _call(WaveMemoryGameKnowledgeTool(service=service), LIVE, game="minecraft", query="苦力怕")
    assert text.startswith("[游戏攻略参考 · 我的世界｜查「苦力怕」]")
    assert "【苦力怕 · 前期】" in text and "要点：听到嘶嘶声马上后退" in text and "出处：测试来源 A" in text
    assert "词条：\n- 苦力怕（Creeper）：会爆炸的绿色怪物" in text
    assert text.endswith(TOOL_FOOTER) and "wave_memory_search" in text
    by_name = _call(WaveMemoryGameKnowledgeTool(service=service), LIVE, game="我的世界", query="第一夜")
    assert "【第一夜怎么过】" in by_name, "游戏可以写显示名或别名"


def test_drafts_are_not_returned(service):
    text = _call(WaveMemoryGameKnowledgeTool(service=service), LIVE, game="mc", query="末影人")
    assert "没有找到" in text and "不要自行编造" in text and "别直视" not in text


def test_output_is_capped(service):
    for index in range(12):
        service.create_item("card", "minecraft", {
            "key": f"long{index}", "title": f"很长的苦力怕攻略{index}", "points": ["苦力怕相关的很长一段说明" * 6] * 5,
            "pitfalls": ["注意苦力怕" * 10] * 3, "keywords": ["苦力怕"], "status": "active",
        })
    text = _call(WaveMemoryGameKnowledgeTool(service=service), LIVE, game="minecraft", query="苦力怕")
    assert len(text) <= TOOL_MAX_CHARS
    assert text.startswith("[游戏攻略参考") and text.endswith(TOOL_FOOTER)


def test_omitted_game_uses_current_game_or_asks(service):
    tool = WaveMemoryGameKnowledgeTool(service=service)
    asked = _call(tool, LIVE, query="苦力怕")
    assert "请在参数 game 里写上游戏" in asked and "我的世界（minecraft）" in asked and "缺氧（oni）" in asked
    service.note_active_game(LIVE, parse_game_context({"id": "minecraft"}))
    same_session = _call(tool, LIVE, query="苦力怕")
    assert "当前在玩（这个会话）" in same_session and "【苦力怕" in same_session
    elsewhere = _call(tool, QQ, query="苦力怕")
    assert "当前在玩（最近的直播）" in elsewhere, "同一个 Bot 在 QQ 群里被问到时也知道她在直播玩什么"
    assert "请在参数 game 里写上游戏" in _call(tool, OTHER_BOT, query="苦力怕"), "别的 Bot 不串"
    explicit = _call(tool, LIVE, game="oni", query="制氧")
    assert "【EP08 制氧模块】" in explicit, "显式写了游戏就用写的"
    # 超过 30 分钟不算「正在玩」
    assert service.active_game(LIVE, now=10**12) is None


def test_unknown_or_disabled_game_and_missing_query(service):
    tool = WaveMemoryGameKnowledgeTool(service=service)
    assert "请提供要查的内容" in _call(tool, LIVE, game="minecraft", query=" ")
    unknown = _call(tool, LIVE, game="slay_the_spire", query="能量")
    assert "没有「slay_the_spire」的游戏库" in unknown and "不要自行编造" in unknown
    service.update_game("oni", {"enabled": False}, expected_version=1)
    assert "已停用" in _call(tool, LIVE, game="oni", query="制氧")
    assert "暂时不可用" in _call(WaveMemoryGameKnowledgeTool(service=None), LIVE, game="oni", query="制氧")
    # AstrBot 事件没有解析出作用域时仍可查（知识库所有 Bot 共用），只是不能省略游戏
    assert "【苦力怕" in _call(tool, None, game="minecraft", query="苦力怕")


def test_tool_is_callable_through_runtime_registry(service):
    registry = ToolRegistry()
    registry.register(ToolSpec("wave_memory_game_knowledge", lambda d: WaveMemoryGameKnowledgeTool(service=d)))
    registry.build(service, capability_enabled=lambda capability, default: True)
    described = registry.describe(runtime_only=True)[0]
    assert described["name"] == "wave_memory_game_knowledge"
    assert "记忆搜索" in described["description"] and described["parameters"]["required"] == ["query"]
    result = asyncio.run(registry.invoke("wave_memory_game_knowledge", scope=LIVE, arguments={"game": "minecraft", "query": "苦力怕"}))
    assert "【苦力怕" in result


def test_capability_follows_runtime_mode():
    from services.runtime_mode import runtime_capability_enabled
    from tools.builtin_registry import _game_knowledge_tools

    spec = next(spec for spec in _game_knowledge_tools() if spec.key == "wave_memory_game_knowledge")
    assert spec.capability == "game_knowledge" and spec.group == "game_knowledge"
    assert runtime_capability_enabled("full", spec.capability, spec.capability_default) is True
    assert runtime_capability_enabled("memory_only", spec.capability, spec.capability_default) is False
    assert runtime_capability_enabled("compat_only", spec.capability, spec.capability_default) is False
