"""关系注入的两种格式：有关系小传（小传 + 最近原始记录 + 私聊场合的本私聊记录 + 旧事线索 + 查询提示）
与没有小传（沿用印象时间线规则摘要）；两种格式之后都只附一行次要参考的好感分数，不出现档位或态度。
另测 wave_memory_person_search 的时间线结果开头附上小传。测试数据只用虚构账号与昵称。"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import types
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services import relationship_portrait
from services.impression_timeline import portrait_injection_lines
from services.injection.channels.relationship import RelationshipChannel
from services.relationship_narrative import SCORE_LINE_PREFIX
from services.relationship_portrait import configure_portrait_injection, portrait_injection_enabled

NOW = time.time()
DAY = 86400.0
BOT = "yushu"
USER = "10000001"
QQ = f"qq:user:{USER}"
GROUP = "900000001"
PRIVATE = f"private:{USER}"
PORTRAIT = "阿木是群里的游戏攻略达人，你们从一起接冷笑话开始熟起来；你答应过帮他查新版本的掉率，还没查。"
TIERS = ("陌生人", "面熟", "常聊", "合得来", "信得过", "亲近", "有点烦", "反感", "敌对", "neutral", "friendly", "态度")


@pytest.fixture(autouse=True)
def _reset_injection_switch():
    saved = portrait_injection_enabled()
    yield
    configure_portrait_injection(enabled=saved)


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "portrait-injection.sqlite"), dimension=3)
    try:
        yield database
    finally:
        database.close()


def _group() -> RuntimeScope:
    return RuntimeScope(BOT, "group", SessionRef(f"qq:group:{GROUP}", "qq", "group", GROUP), subject_principal_id=QQ)


def _private(conversation: str = USER) -> RuntimeScope:
    return RuntimeScope(BOT, "private", SessionRef(f"qq:private:{conversation}", "qq", "private", conversation),
                        subject_principal_id=QQ)


def _ctx(scope, *, message="", event=None):
    return SimpleNamespace(
        mode="full", config={"channels": {"affinity": {"enabled": True}}}, scope=scope, sender_id=USER,
        group_id=scope.session.conversation_id, sender_name="阿木", now=NOW, message=message, event=event,
    )


def _seed(db) -> dict[str, int]:
    """群里 12 条印象/好感 + 2 条人物事实 + 本私聊 2 条 + 另一私聊 1 条；一行正式关系分数。"""
    ids: dict[str, int] = {}
    add = db.person_timeline.add_event
    for i in range(10):
        ids[f"imp{i}"] = add(bot_id=BOT, user_id=USER, group_id=GROUP, kind="impression",
                             summary=f"第{i:02d}次印象：半夜陪聊游戏攻略", occurred_at=NOW - (40 - 3 * i) * DAY)
    ids["old_trip"] = add(bot_id=BOT, user_id=USER, group_id=GROUP, kind="person_fact",
                          summary="阿木说过春节要去哈尔滨看冰雕", occurred_at=NOW - 60 * DAY)
    ids["aff"] = add(bot_id=BOT, user_id=USER, group_id=GROUP, kind="affinity",
                     summary="日常直接互动（帮忙核对了版本号），好感从 15 升至 16", occurred_at=NOW - 2 * DAY)
    ids["fact_new"] = add(bot_id=BOT, user_id=USER, group_id=GROUP, kind="person_fact",
                          summary="最近在学吉他", occurred_at=NOW - 1 * DAY)
    ids["latest"] = add(bot_id=BOT, user_id=USER, group_id=GROUP, kind="impression",
                        summary="会接冷笑话、半夜陪聊的阿木", occurred_at=NOW - 0.5 * DAY)
    ids["priv1"] = add(bot_id=BOT, user_id=USER, group_id=PRIVATE, kind="impression",
                       summary="私聊里说了最近工作压力大", occurred_at=NOW - 3 * DAY)
    ids["priv2"] = add(bot_id=BOT, user_id=USER, group_id=PRIVATE, kind="person_fact",
                       summary="私聊里提到在准备考研", occurred_at=NOW - 0.2 * DAY)
    ids["other_priv"] = add(bot_id=BOT, user_id=USER, group_id="private:10000009", kind="impression",
                            summary="别的私聊会话里的事", occurred_at=NOW - 0.1 * DAY)
    db.soul_repository.upsert_relationship(_group(), subject_principal_id=QQ, affinity=20, state="neutral",
                                           dimensions={"familiarity": 63.3, "trust": 3.0, "fun": 10.5})
    return ids


def _save_portrait(db, max_event_id: int, content: str = PORTRAIT) -> dict:
    return db.relationship_portraits.save_content(
        bot_id=BOT, user_id=USER, content=content, status="auto", expected_version=0,
        provider="fake-provider", source_max_event_id=max_event_id, source_event_count=12,
        now=NOW - 0.3 * DAY,
    )


def _build(db, ctx):
    return asyncio.run(RelationshipChannel(repository=db.soul_repository, db=db).build(ctx))


def _event_rows(block: str) -> list[int]:
    return [int(value) for value in re.findall(r"^- 事件#(\d+) ", block, re.M)]


def test_portrait_injection_format(db):
    ids = _seed(db)
    _save_portrait(db, ids["latest"])
    configure_portrait_injection(enabled=True)
    result = _build(db, _ctx(_group()))
    text = result.text
    lines = text.splitlines()
    assert result.status == "hit" and result.preserve_full_text is True

    assert re.fullmatch(r"关系小传（你自己写的，正文里的「你」就是你；\d{4}-\d{2}-\d{2} 更新，依据截至事件#\d+）：", lines[0])
    assert f"依据截至事件#{ids['latest']}" in lines[0]
    assert lines[1] == PORTRAIT
    assert lines[2] == "最近的印象记录（早→近）："
    recent_block = text.split("最近的印象记录（早→近）：", 1)[1].split("更早的交往", 1)[0]
    recent = _event_rows(recent_block)
    assert recent == [ids["imp7"], ids["imp8"], ids["imp9"], ids["aff"], ids["latest"]], "最近 5 条，只按时间、早→近"
    assert ids["fact_new"] not in recent, "只取印象/好感类，人物事实不在这里"
    assert "帮忙核对了版本号" in text and "好感从" not in text, "摘要去掉好感数值"
    assert "印象时间线（" not in text and "你对这个人的印象：" not in text, "有小传时不再用规则摘要"
    assert "私聊里" not in text and "这段私聊里的记录" not in text, "群里不出现任何私聊记录"
    assert "更早的交往用 wave_memory_person_search(query_type=timeline, person=当前人物" in text
    assert "最近关系线索：" not in text

    score = next(line for line in lines if line.startswith(SCORE_LINE_PREFIX))
    assert text.index(score) > text.index("更早的交往用"), "分数在小传与原始记录之后"
    assert "理解对方以上面的印象为准" in score and not re.search(r"\d\.\d{2,}", score)
    for word in TIERS:
        assert word not in text, word


def test_without_portrait_falls_back_to_rule_summary(db):
    _seed(db)
    configure_portrait_injection(enabled=True)
    text = _build(db, _ctx(_group())).text
    assert text.startswith("印象时间线（挑选最相关的 8 条")
    assert "关系小传" not in text and "你对这个人的印象：会接冷笑话、半夜陪聊的阿木" in text
    assert text.splitlines()[-1].startswith(SCORE_LINE_PREFIX)


def test_portrait_is_ignored_when_feature_is_off(db):
    ids = _seed(db)
    _save_portrait(db, ids["latest"])
    configure_portrait_injection(enabled=False)
    text = _build(db, _ctx(_group())).text
    assert text.startswith("印象时间线（") and PORTRAIT not in text, "关闭时完全沿用规则摘要"


def test_private_scene_appends_this_private_chat_only(db):
    ids = _seed(db)
    _save_portrait(db, ids["latest"])
    configure_portrait_injection(enabled=True)
    text = _build(db, _ctx(_private())).text
    assert PORTRAIT in text
    assert "这段私聊里的记录（只在这段私聊里可见，早→近）：" in text
    private_block = text.split("这段私聊里的记录（只在这段私聊里可见，早→近）：", 1)[1].split("更早的交往", 1)[0]
    assert _event_rows(private_block) == [ids["priv1"], ids["priv2"]]
    assert "私聊里提到在准备考研" in text and "私聊里说了最近工作压力大" in text
    assert "别的私聊会话里的事" not in text, "其他私聊的记录不外流"
    recent_block = text.split("最近的印象记录（早→近）：", 1)[1].split("这段私聊里的记录", 1)[0]
    assert ids["priv1"] not in _event_rows(recent_block), "公开的最近记录里不混入私聊"


def test_topic_cue_for_old_events_and_dedupe_with_ingress_cue(db):
    ids = _seed(db)
    _save_portrait(db, ids["latest"])
    configure_portrait_injection(enabled=True)
    text = _build(db, _ctx(_group(), message="今年冬天想去哈尔滨看冰雕")).text
    assert "【印象线索】" in text and "哈尔滨看冰雕" in text, "话题命中不在最近记录里的旧事"
    assert text.index("【印象线索】") < text.index("更早的交往用")

    already = SimpleNamespace(_wave_memory_timeline_cue={"summary": "阿木说过春节要去哈尔滨看冰雕"})
    deduped = _build(db, _ctx(_group(), message="今年冬天想去哈尔滨看冰雕", event=already)).text
    assert "【印象线索】" not in deduped, "消息钩子已注入过线索时不重复"


def test_portrait_injection_lines_without_portrait_is_empty():
    assert portrait_injection_lines(None, events=[], now=NOW) == []
    assert portrait_injection_lines({"content": "   "}, events=[], now=NOW) == []
    lines = portrait_injection_lines({"content": "只有小传"}, events=[], now=NOW)
    assert lines[0] == "关系小传（你自己写的，正文里的「你」就是你）：" and lines[1] == "只有小传"
    assert lines[-1].startswith("更早的交往用 wave_memory_person_search")


# ─── wave_memory_person_search ───


def _install_tool_stub() -> None:
    if "astrbot.core.agent.tool" in sys.modules:
        return

    class FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.tool",
        "astrbot.core.agent.run_context", "astrbot.core.astr_agent_context",
    )}
    modules["astrbot.api"].logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None,
                                                    debug=lambda *a, **k: None)
    modules["astrbot.core.agent.tool"].FunctionTool = FunctionTool
    modules["astrbot.core.agent.run_context"].ContextWrapper = object
    modules["astrbot.core.astr_agent_context"].AstrAgentContext = object
    sys.modules.update(modules)


def _tool_ctx(scope):
    return SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope)))


def test_person_search_timeline_starts_with_portrait(db):
    _install_tool_stub()
    from tools.person_search import WaveMemoryPersonSearchTool

    ids = _seed(db)
    db.conn.execute(
        "INSERT INTO user_profiles (user_id, group_id, bot_id, nickname, interaction_count, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
        (USER, GROUP, BOT, "阿木", 12, NOW),
    )
    db.conn.commit()
    tool = WaveMemoryPersonSearchTool(db=db)
    configure_portrait_injection(enabled=True)
    assert "关系小传" not in asyncio.run(tool.call(_tool_ctx(_group()), person=USER, query_type="timeline"))

    _save_portrait(db, ids["latest"])
    first = asyncio.run(tool.call(_tool_ctx(_group()), person=USER, query_type="timeline", limit=3))
    lines = first.splitlines()
    assert lines[1] == f"QQ: {USER}" and lines[2].startswith("关系小传（你自己写的") and lines[3] == PORTRAIT
    assert lines[4].startswith("total: ")
    later = asyncio.run(tool.call(_tool_ctx(_group()), person=USER, query_type="timeline", limit=3, offset=3))
    assert "关系小传" not in later, "只在第一页开头附上"
    exact = asyncio.run(tool.call(_tool_ctx(_group()), person=USER, query_type="timeline", event_id=ids["aff"]))
    assert "关系小传" not in exact, "按编号查单条时不附"
    configure_portrait_injection(enabled=False)
    assert "关系小传" not in asyncio.run(tool.call(_tool_ctx(_group()), person=USER, query_type="timeline"))


def test_injection_switch_is_off_by_default_and_none_keeps_it():
    assert relationship_portrait.PortraitSettings().enabled is False
    configure_portrait_injection(enabled=True)
    configure_portrait_injection(enabled=None)
    assert portrait_injection_enabled() is True
    configure_portrait_injection(enabled=False)
    assert portrait_injection_enabled() is False
