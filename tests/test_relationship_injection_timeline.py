"""关系注入：印象时间线摘要（半衰期挑选、最多 8 条、按时间排序）+ 最近一条印象，之后附一行次要参考的好感分数。

好感分数（五维与综合值）是给群友互动用的数字；Bot 以印象时间线为主自己判断关系，分数只在印象之后
以一行紧凑数值出现，并标明仅供参考。注入里不得出现态度/档位字样、每行的权重数值、来源群号或长小数。
"""

from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services import impression_timeline
from services.impression_timeline import (
    configure_impression_prompting,
    impression_prompting,
    impression_timeline_lines,
    select_timeline_events,
)
from services.injection.channels.relationship import RelationshipChannel
from services.relationship_narrative import SCORE_LINE_PREFIX, affinity_score_line, shared_experience_lines

NOW = time.time()
DAY = 86400.0
USER = "10000001"
QQ = f"qq:user:{USER}"
GROUP = "900000001"

# 注入里不得出现的字样：英文维度键/态度/档位/权重数值/来源群号/被撤掉的「当下感受」与档位句
# （好感分数只以「好感分数（…仅供参考…）：综合 X；熟悉 A、…」一行出现）
FORBIDDEN = (
    "综合值", "态度=", "好感从", "好感 ", "familiarity", "trust", "fun=", "depth", "hostility",
    "权重=", "来源群", GROUP, "【你和对方】", "【当下感受】", "走势",
    "陌生人", "面熟", "常聊", "合得来", "信得过", "亲近", "有点烦", "反感", "敌对",
    "neutral", "friendly", "intimate",
)


@pytest.fixture(autouse=True)
def _reset_prompting():
    saved = impression_prompting()
    yield
    impression_timeline._PROMPTING.update(saved)


def _group(group: str = GROUP, subject: str | None = QQ) -> RuntimeScope:
    return RuntimeScope("yushu", "group", SessionRef(f"qq:group:{group}", "qq", "group", group), subject_principal_id=subject)


def _ctx(scope, *, channel=None, config=None):
    merged = {"channels": {"affinity": {"enabled": True, **(channel or {})}}, **(config or {})}
    return SimpleNamespace(
        mode="full", config=merged, scope=scope, sender_id=USER, group_id=GROUP,
        sender_name="阿木", now=NOW,
    )


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "relationship.sqlite"), dimension=3)
    try:
        yield database
    finally:
        database.close()


def _seed(db) -> dict[int, float]:
    """一个群友：五维分数、15 条印象时间线、Bot 群日记与一条与他相关的人情备忘。返回 事件编号→时间。"""
    soul = db.soul_repository
    scope = _group()
    soul.upsert_relationship(scope, subject_principal_id=QQ, affinity=20, state="neutral",
                             dimensions={"familiarity": 63.3, "trust": 3.0, "fun": 10.5})
    soul.add_timeline_event(_group(subject=None), event_summary="【日记】今日全群畅聊 1333 条，106 位群友参与。",
                            event_type="episode", timestamp=NOW - 3600)
    soul.add_timeline_event(scope, event_summary="人情备忘（user_helped_bot）：借账号帮我救急",
                            event_type="social_anchor", timestamp=NOW - 2 * DAY)
    when: dict[int, float] = {}
    for i in range(12):
        at = NOW - (30 - 2 * i) * DAY
        event_id = db.person_timeline.add_event(
            bot_id="yushu", user_id=USER, group_id=GROUP, kind="impression",
            summary=f"第{i:02d}次印象：半夜陪聊游戏攻略的阿木", occurred_at=at,
        )
        when[int(event_id)] = at
    at = NOW - 3 * DAY
    event_id = db.person_timeline.add_event(
        bot_id="yushu", user_id=USER, group_id=GROUP, kind="affinity",
        summary="日常直接互动（帮忙核对了版本号：细心的阿木），好感从 15 升至 16",
        occurred_at=at, provenance={"before_affinity": 15, "after_affinity": 16},
    )
    when[int(event_id)] = at
    at = NOW - 1 * DAY
    event_id = db.person_timeline.add_event(
        bot_id="yushu", user_id=USER, group_id=GROUP, kind="affinity",
        summary="joke fun+1：接住了冷笑话", occurred_at=at,
    )
    when[int(event_id)] = at
    at = NOW - 0.5 * DAY
    event_id = db.person_timeline.add_event(
        bot_id="yushu", user_id=USER, group_id=GROUP, kind="impression",
        summary="会接冷笑话、半夜陪聊的阿木", occurred_at=at,
    )
    when[int(event_id)] = at
    return when


def _timeline_rows(text: str) -> list[tuple[int, str]]:
    return [(int(match.group(1)), match.group(0)) for match in re.finditer(r"^- 事件#(\d+) .*$", text, re.M)]


def _score_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith(SCORE_LINE_PREFIX)]
    assert len(lines) == 1, "恰好一行好感分数"
    return lines[0]


LONG_DECIMAL = re.compile(r"\d\.\d{2,}")


# ─── 注入通道 ───


def test_channel_injects_timeline_summary_then_secondary_score_line(db):
    when = _seed(db)
    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=True)
    result = asyncio.run(RelationshipChannel(repository=db.soul_repository, db=db).build(_ctx(_group())))
    text = result.text
    assert result.status == "hit"
    assert text.startswith("印象时间线（挑选最相关的 8 条，另有 7 条较早或次要；时间权重每 21 天减半）")
    assert "wave_memory_person_search(query_type=timeline, person=当前人物, event_id=编号)" in text

    rows = _timeline_rows(text)
    assert len(rows) == 8, "最多注入 8 条"
    stamps = [when[event_id] for event_id, _line in rows]
    assert stamps == sorted(stamps), "按时间先后排列"
    assert all(re.search(r"\d{4}-\d{2}-\d{2}（", line) for _id, line in rows), "每条带日期"
    assert rows[-1][0] == max(when, key=when.get), "最近的一条一定入选"

    assert "你对这个人的印象：会接冷笑话、半夜陪聊的阿木（最近一条，" in text
    assert "帮忙核对了版本号" in text, "结算里程碑保留文字，只去掉好感数值"
    assert "joke：接住了冷笑话" in text, "旧账本条目去掉维度与增量"
    assert "共同交往锚点：人情备忘（对方帮过你）：借账号帮我救急" in text
    assert "1333" not in text, "Bot 自己的群日记与此人无关，不塞进关系注入"

    score = _score_line(text)
    assert "仅供参考" in score and "理解对方以上面的印象为准" in score, "标明分数只是次要参考"
    # 数值取当前场合可见的汇总：综合值由汇总后的维度推导（与管理台跨群汇总同口径）
    assert score.endswith("：综合 18；熟悉 63.3、信任 3、好玩 10.5"), score
    assert text.index(score) > text.index(rows[-1][1]), "分数在印象时间线之后"
    assert text.index(score) > text.index("你对这个人的印象："), "分数在最近一条印象之后"
    assert text.index(score) > text.index("共同交往锚点："), "分数在共同交往锚点之后"
    assert not LONG_DECIMAL.search(text), "整数或一位小数，不出现长小数"
    for word in FORBIDDEN:
        assert word not in text, word
    assert "<<impression" not in text and "未结算能量" not in text, "默认不要求每轮写观感标签"
    assert result.preserve_full_text is True


def test_channel_uses_configured_half_life(db):
    _seed(db)
    result = asyncio.run(RelationshipChannel(repository=db.soul_repository, db=db).build(
        _ctx(_group(), config={"Inject_Settings": {"timeline_decay_half_life_days": 7}})))
    assert "时间权重每 7 天减半" in result.text


def test_known_person_without_impression_injects_only_score_line(db):
    soul = db.soul_repository
    soul.upsert_relationship(_group(), subject_principal_id=QQ, affinity=40, state="friendly",
                             dimensions={"familiarity": 90.0, "trust": 40.0, "fun": 30.0})
    result = asyncio.run(RelationshipChannel(repository=soul, db=db).build(_ctx(_group())))
    assert result.status == "hit"
    assert result.text == _score_line(result.text), "只有分数没有印象：只注入这一行分数"
    assert "仅供参考" in result.text and "你还没有记下对 ta 的印象" in result.text
    for word in FORBIDDEN:
        assert word not in result.text, word


def _private(conversation: str = USER) -> RuntimeScope:
    return RuntimeScope("yushu", "private", SessionRef(f"qq:private:{conversation}", "qq", "private", conversation),
                        subject_principal_id=QQ)


def test_score_line_counts_private_rows_only_in_the_same_private_chat(db):
    soul = db.soul_repository
    db.person_timeline.add_event(bot_id="yushu", user_id=USER, group_id=GROUP, kind="impression",
                                 summary="会接冷笑话的阿木", occurred_at=NOW - DAY)
    soul.upsert_relationship(_group(), subject_principal_id=QQ, affinity=10, state="neutral",
                             dimensions={"familiarity": 30.0, "trust": 5.0})
    soul.upsert_relationship(_private(), subject_principal_id=QQ, affinity=10, state="neutral",
                             dimensions={"familiarity": 20.0, "trust": 10.0, "depth": 12.5})
    channel = RelationshipChannel(repository=soul, db=db)

    in_group = _score_line(asyncio.run(channel.build(_ctx(_group()))).text)
    assert in_group.endswith("熟悉 30、信任 5"), "群里不计入私聊的关系行"

    ctx = _ctx(_private())
    ctx.group_id = USER
    in_private = _score_line(asyncio.run(channel.build(ctx)).text)
    assert in_private.endswith("熟悉 50、信任 15、深度 12.5"), "同一私聊里计入本私聊的行"


def test_score_line_format():
    line = affinity_score_line(12.3456789, {"familiarity": 63.333333, "trust": 3.0, "hostility": -0.04}, has_impression=True)
    assert line == f"{SCORE_LINE_PREFIX}理解对方以上面的印象为准）：综合 12.3；熟悉 63.3、信任 3、敌意 0"
    assert affinity_score_line(None, {}, has_impression=True) == ""
    assert affinity_score_line(True, {"trust": float("nan")}, has_impression=False) == ""


def test_channel_settle_hint_follows_prompting_switch(db):
    _seed(db)
    db.person_timeline.set_unsettled_state(
        bot_id="yushu", user_id=USER, group_id="", energy=12.0, interaction_count=6,
        traces=[{"text": "半夜拉着我复盘版本更新的阿木", "impact": 2.0, "ts": NOW - 3600}],
    )
    channel = RelationshipChannel(repository=db.soul_repository, db=db)

    configure_impression_prompting(per_turn_tag=True, bookkeeping_active=True)
    legacy = asyncio.run(channel.build(_ctx(_group()))).text
    assert "【感知】未结算能量" in legacy and "<<impression:当下观感 | impact:1-5>>" in legacy
    assert legacy.rstrip().endswith("不要覆盖这句结算。"), "开关打开：与旧行为一致，结算提示在末尾"

    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=False)
    no_bookkeeping = asyncio.run(channel.build(_ctx(_group()))).text
    assert "【该结算了】" in no_bookkeeping and "wave_memory_record_social_impression" in no_bookkeeping
    assert "<<impression" not in no_bookkeeping

    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=True)
    grouped = asyncio.run(channel.build(_ctx(_group()))).text
    assert "【该结算了】" not in grouped, "群聊里事后记账已启动：结算交给记账回看"

    # 通道级覆盖优先于全局开关
    override = asyncio.run(channel.build(_ctx(_group(), channel={"per_turn_impression_tag": True}))).text
    assert "<<impression" in override


def test_configure_prompting_none_keeps_current_values():
    configure_impression_prompting(per_turn_tag=True, bookkeeping_active=True)
    configure_impression_prompting(per_turn_tag=None, bookkeeping_active=None)
    assert impression_prompting() == {"per_turn_tag": True, "bookkeeping_active": True}


def test_bookkeeping_settle_block_does_not_depend_on_injection_hint(db):
    """事后记账自己按未结算能量列出「需要结算的人」，与注入里是否出现结算提示无关。"""
    from services.post_reply_bookkeeping import PostReplyBookkeeper

    _seed(db)
    db.person_timeline.set_unsettled_state(bot_id="yushu", user_id=USER, group_id="", energy=12.0,
                                           interaction_count=6, traces=[])
    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=True)
    keeper = PostReplyBookkeeper(db, tool_registry=object(), llm=object())
    block = keeper._settle_block(_group(subject=None), [(1, USER, "阿木", "在吗")])
    assert "【需要结算的人】" in block and "阿木" in block


# ─── 渲染与挑选 ───


def _event(i: int, kind: str, summary: str, *, days_ago: float) -> dict:
    return {"id": i, "kind": kind, "summary": summary, "group_id": GROUP, "occurred_at": NOW - days_ago * DAY}


def test_timeline_lines_strip_score_fragments_but_keep_ids_and_dates():
    events = [
        _event(1, "affinity", "【关系跃迁】发生言语冲突（当众拆台），好感从 -3 跃迁降至 -8", days_ago=9),
        _event(2, "affinity", "joke fun+1：接住了冷笑话", days_ago=5),
        _event(3, "affinity", "deep_talk depth+3.5", days_ago=4),
        _event(4, "impression", "愿意核对事实 | impact: 4", days_ago=2),
    ]
    lines = impression_timeline_lines({}, events=events, now=NOW, half_life_days=21)
    body = "\n".join(lines)
    assert lines[0].startswith("印象时间线（早→近；时间权重每 21 天减半）")
    assert [line.split(" ", 2)[1] for line in lines[1:]] == ["事件#1", "事件#2", "事件#3", "事件#4"]
    assert "【关系跃迁】发生言语冲突（当众拆台）" in body
    assert "joke：接住了冷笑话" in body and "- 事件#3" in body
    assert "愿意核对事实" in body
    for word in ("好感从", "fun+1", "depth+3.5", "impact", "权重=", "来源群", GROUP):
        assert word not in body, word


def test_timeline_cue_prompt_strips_affinity_numbers():
    from services.impression_timeline import timeline_cue_prompt

    prompt = timeline_cue_prompt({"summary": "日常直接互动（帮忙核对了版本号），好感从 15 升至 16", "occurred_at": NOW - DAY})
    assert "帮忙核对了版本号" in prompt and "好感从" not in prompt


def test_select_timeline_events_is_the_single_selection_point():
    events = [_event(i, "impression", f"印象{i:02d}", days_ago=30 - i) for i in range(12)]
    events.append(_event(99, "affinity", "很早但分量重的一次结算", days_ago=6.5))
    selected, omitted = select_timeline_events(events, now=NOW, half_life_days=21, max_events=8)
    assert omitted == 5 and len(selected) == 8
    assert [row["occurred_at"] for row in selected] == sorted(row["occurred_at"] for row in selected)
    assert any(row["id"] == 99 for row in selected), "时间衰减 × 类别分量：好感结算的分量更高"
    assert select_timeline_events(events[:3], now=NOW, max_events=8) == (sorted(events[:3], key=lambda r: r["occurred_at"]), 0)


def test_long_summary_is_truncated_to_90_chars():
    lines = impression_timeline_lines({}, events=[_event(1, "impression", "长" * 200, days_ago=1)], now=NOW)
    assert lines[1].endswith("长" * 90 + "…")


# ─── 共同交往锚点 ───


def test_shared_experience_only_keeps_subject_related_items():
    items = [
        {"subject_principal_id": None, "event_summary": "【日记】今日全群畅聊 1333 条，106 位群友参与。"},
        {"subject_principal_id": "qq:user:10000002", "event_summary": "别人的人情账"},
        {"subject_principal_id": QQ, "event_summary": "人情备忘（user_helped_bot）：借账号帮我救急"},
        {"subject_principal_id": None, "participant_principal_ids": [QQ], "event_summary": "【日记】今天小林生图被挑刺，阿木在旁边笑得最大声，后来大家散了。"},
    ]
    lines = shared_experience_lines(items, subject_principal_id=QQ, display_names=["阿木"])
    assert lines[0] == "人情备忘（对方帮过你）：借账号帮我救急"
    assert any("阿木" in line for line in lines)
    assert not any("1333" in line or "别人的人情账" in line for line in lines)
    assert shared_experience_lines(items[:2], subject_principal_id=QQ) == []
    assert shared_experience_lines([
        {"event_summary": "【日记】今天下了小雨，我独自读完一本书。"},
    ], subject_principal_id=QQ, display_names=["小雨"]) == []
