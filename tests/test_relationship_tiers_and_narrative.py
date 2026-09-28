"""关系档位（读时从五维推导）、自然语言关系注入、当下感受与观感标签开关。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from domain.relationship_policy import (
    DEFAULT_RELATIONSHIP_TIERS,
    classify_relationship_tier,
    configure_relationship_tiers,
    parse_relationship_tiers,
    relationship_closeness,
    relationship_stance,
)
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services import impression_timeline
from services.impression_timeline import configure_impression_prompting, impression_prompting
from services.injection.channels.relationship import RelationshipChannel
from services.relationship_narrative import (
    compress_impressions,
    current_feeling_line,
    merge_epithets,
    relationship_sentence,
    shared_experience_lines,
)

NOW = time.time()
QQ = "qq:user:10001"


@pytest.fixture(autouse=True)
def _reset_module_state():
    saved = impression_prompting()
    configure_relationship_tiers(None)
    yield
    impression_timeline._PROMPTING.update(saved)
    configure_relationship_tiers(None)


def _tier(**dims) -> str:
    return classify_relationship_tier(dims)["tier"]


# ─── 档位 ───


def test_default_tiers_follow_dimension_combinations():
    assert _tier() == "stranger"
    assert _tier(familiarity=12) == "familiar"
    assert _tier(familiarity=63) == "regular", "光是常见面只算常聊，不因熟悉度涨满就变亲近"
    assert _tier(familiarity=100, fun=25.5, depth=3.5, trust=4) == "congenial"
    assert _tier(familiarity=100, trust=21.2, fun=6, depth=10) == "trusted"
    assert _tier(familiarity=90, trust=45, fun=20, depth=15) == "close"
    assert _tier(familiarity=100, trust=-13, fun=7.5) == "annoyed"
    assert _tier(familiarity=100, trust=30, hostility=9) == "trusted", "老朋友的一次小摩擦不直接变「有点烦」"
    assert _tier(familiarity=50, hostility=25) == "averse"
    assert _tier(familiarity=50, hostility=45, trust=60) == "hostile"
    labels = {tier["key"]: tier["label"] for tier in DEFAULT_RELATIONSHIP_TIERS}
    assert labels["close"] == "亲近" and labels["annoyed"] == "有点烦"


def test_closeness_weight_is_bounded_and_ordered():
    stranger = relationship_closeness({})
    regular = relationship_closeness({"familiarity": 100})
    trusted = relationship_closeness({"familiarity": 100, "trust": 21.2, "fun": 6, "depth": 10})
    close = relationship_closeness({"familiarity": 100, "trust": 80, "fun": 40, "depth": 40})
    annoyed = relationship_closeness({"familiarity": 100, "trust": -13})
    hostile = relationship_closeness({"hostility": 80, "trust": -50})
    assert stranger == 0.0
    assert hostile == -1.0 and close == 1.0
    assert hostile < annoyed < stranger < regular < trusted < close
    stance = relationship_stance({"familiarity": 100, "trust": 21.2, "depth": 10})
    assert stance["tier"] == "trusted" and stance["polarity"] == "positive" and -1 <= stance["closeness"] <= 1


def test_tier_config_patch_and_replace_and_reject_invalid():
    tiers = configure_relationship_tiers('{"close": {"label": "挚友"}, "regular": {"when": [{"familiarity": [80, null]}]}}')
    assert [tier["label"] for tier in tiers if tier["key"] == "close"] == ["挚友"]
    assert _tier(familiarity=63) == "familiar", "覆盖后的阈值生效"
    assert _tier(familiarity=90, trust=45, rapport=0, fun=20, depth=15) == "close"
    assert classify_relationship_tier({"familiarity": 90, "trust": 45, "fun": 20, "depth": 15})["tier_label"] == "挚友"
    configure_relationship_tiers([
        {"key": "enemy", "label": "仇人", "polarity": "negative", "when": [{"hostility": [10, None]}]},
        {"key": "anyone", "label": "路人", "when": [{}]},
    ])
    assert _tier(hostility=12) == "enemy" and _tier(familiarity=99) == "anyone"
    for bad in ('{"nope": {"label": "x"}}', '[{"key": "a", "label": "b", "when": [{"charm": [1, 2]}]}]', "not json", "[{\"key\": \"a\"}]"):
        with pytest.raises(Exception):
            parse_relationship_tiers(bad)


# ─── 仓储：tier 字段与耦合接口 ───


def _group(group: str = "g1", subject: str | None = QQ) -> RuntimeScope:
    return RuntimeScope("yushu", "group", SessionRef(f"qq:group:{group}", "qq", "group", group), subject_principal_id=subject)


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "tiers.sqlite"), dimension=3)
    try:
        yield database
    finally:
        database.close()


def test_repository_exposes_tier_and_cross_group_stance(db):
    soul = db.soul_repository
    soul.upsert_relationship(_group("g1"), subject_principal_id=QQ, affinity=20, state="neutral",
                             dimensions={"familiarity": 30.0, "trust": 12.0})
    soul.upsert_relationship(_group("g2"), subject_principal_id=QQ, affinity=20, state="neutral",
                             dimensions={"familiarity": 30.0, "trust": 12.0, "depth": 4.0})
    listed = soul.list_relationships(_group("g1", subject=None))
    assert listed[0]["tier"] == "familiar" and listed[0]["tier_label"] == "面熟"
    assert listed[0]["state"] == "neutral", "旧 attitude_level 字段保持兼容"
    state = soul.get_state(_group("g1"), subject_principal_id=QQ)
    assert state["relationship"]["tier"] == "familiar"
    merged = soul.summarize_cross_group_relationship(_group("g1"), subject_principal_id=QQ)
    assert merged["merged_tier"] == "trusted", "两群累加：熟悉 60、信任 24 → 信得过"
    stance = soul.relationship_stance_for("yushu", QQ)
    assert stance["available"] is True and stance["tier"] == "trusted" and stance["tier_label"] == "信得过"
    assert 0 < stance["closeness"] <= 1
    unknown = soul.relationship_stance_for("yushu", "qq:user:404")
    assert unknown == {"available": False, "tier": "stranger", "tier_label": "陌生人", "polarity": "neutral",
                       "closeness": 0.0, "dimensions": {}}
    assert soul.relationship_stance_for("other-bot", QQ)["available"] is False, "谁是谁是每个 Bot 自己的认识"


# ─── 渲染 ───


def _event(i: int, kind: str, summary: str, *, days_ago: float, **provenance):
    return {"id": i, "kind": kind, "summary": summary, "group_id": "900000001",
            "occurred_at": NOW - days_ago * 86400, "provenance": provenance}


def test_compress_impressions_merges_epithets_and_drops_numbers():
    events = [
        _event(1, "impression", "过零点准时打卡互换名片赞的守约老搭子RO", days_ago=20),
        _event(2, "impression", "跟风挑刺发图敷衍的嘴贫老搭子RO", days_ago=10),
        _event(3, "impression", "半夜陪聊游戏攻略的老搭子RO", days_ago=5),
        _event(4, "affinity", "日常直接互动（帮忙核对了版本号：细心的老搭子RO），好感从 15 升至 16",
               days_ago=9, before_affinity=15, after_affinity=16),
        _event(5, "affinity", "共同玩梗接梗（接住了冷笑话：会接梗的RO），好感从 16 升至 18",
               days_ago=2, before_affinity=16, after_affinity=18),
        _event(6, "affinity", "joke fun+1：消息带来趣味感", days_ago=1),
        _event(7, "person_fact", "别名 RO", days_ago=1),
        _event(8, "impression", "跟风管理挑刺发图敷衍的嘴贫老搭子RO", days_ago=0.5),
    ]
    compressed = compress_impressions(events, now=NOW)
    assert compressed["current"] == "跟风管理挑刺发图敷衍的嘴贫老搭子RO"
    assert ("老搭子RO", 5) in compressed["epithets"]
    texts = [item["text"] for item in compressed["events"]]
    assert 1 <= len(texts) <= 4
    assert not any("挑刺发图敷衍" in text for text in texts), "与当前看法近似的旧印象不重复罗列"
    assert not any("趣味感" in text or "别名" in text for text in texts), "噪声事件与人物事实不算印象"
    assert "帮忙核对了版本号" in "".join(texts) and "好感从" not in "".join(texts)
    assert compressed["trend"] == "好感 15→18，明显变好（2 次变动）"
    assert merge_epithets(["诸葛匹夫", "老油条诸葛匹夫", "匹夫哥"]) == [("诸葛匹夫", 2), ("匹夫哥", 1)]


def test_relationship_sentence_uses_words_not_scores():
    text = relationship_sentence(
        {"familiarity": 63.3, "trust": 3.0, "fun": 10.5},
        first_seen=NOW - 80 * 86400, interaction_count=1365, now=NOW,
    )
    assert text.startswith("【你和对方】算「常聊」")
    assert "认识约 3 个月" in text and "打过 1 千多次交道" in text
    assert "很熟" in text and "信任一般" in text and "挺合得来" in text
    assert "63" not in text and "10.5" not in text


def test_shared_experience_only_keeps_subject_related_items():
    items = [
        {"subject_principal_id": None, "event_summary": "【日记】今日全群畅聊 1333 条，106 位群友参与。"},
        {"subject_principal_id": "qq:user:other", "event_summary": "别人的人情账"},
        {"subject_principal_id": QQ, "event_summary": "人情备忘（user_helped_bot）：借账号帮我救急"},
        {"subject_principal_id": None, "event_summary": "【日记】今天小岸生图被挑刺，老搭子RO 在旁边笑得最大声，后来大家散了。"},
    ]
    lines = shared_experience_lines(items, subject_principal_id=QQ, display_names=["老搭子RO"])
    assert lines[0] == "人情备忘（对方帮过你）：借账号帮我救急"
    assert any("老搭子RO" in line for line in lines)
    assert not any("1333" in line or "别人的人情账" in line for line in lines)
    assert shared_experience_lines(items[:2], subject_principal_id=QQ) == []


def test_current_feeling_decays_and_skips_without_recent_interaction():
    recent = [
        {"event_type": "bot_attacked", "dimension": "trust", "delta": -2, "reason": "当众骂器灵笨蛋", "timestamp": NOW - 1800},
    ]
    line = current_feeling_line(history=recent, now=NOW)
    assert line.startswith("【当下感受】刚才被对方怼了")
    assert "当众骂器灵笨蛋" in line and ("有点受伤" in line or "有点戒备" in line)
    praised = [{"event_type": "bot_praised", "dimension": "trust", "delta": 2, "reason": "夸我画得好", "timestamp": NOW - 3600 * 3}]
    assert "被对方夸了" in current_feeling_line(history=praised, now=NOW)
    old = [{"event_type": "bot_attacked", "dimension": "trust", "delta": -2, "reason": "上周的事", "timestamp": NOW - 5 * 86400}]
    assert current_feeling_line(history=old, now=NOW) == ""
    faded = current_feeling_line(history=[dict(recent[0], timestamp=NOW - 40 * 3600)], now=NOW)
    assert "昨天" in faded and "受伤" not in faded, "一天多以前的冲突衰减后只剩轻微感受"
    traces = [{"text": "日常跟器灵拌嘴拆台的时雨", "impact": 1.0, "ts": NOW - 7200}]
    assert "日常跟器灵拌嘴拆台" in current_feeling_line(traces=traces, now=NOW)


# ─── 注入通道 ───


def _ctx(scope, *, sender_id="10001", group_id="g1", channel=None, sender_name="老搭子RO"):
    return SimpleNamespace(
        mode="full", config={"channels": {"affinity": {"enabled": True, **(channel or {})}}},
        scope=scope, sender_id=sender_id, group_id=group_id, sender_name=sender_name, now=NOW,
    )


def _seed(db):
    soul = db.soul_repository
    scope = _group("g1")
    soul.upsert_relationship(scope, subject_principal_id=QQ, affinity=20, state="neutral",
                             dimensions={"familiarity": 63.3, "trust": 3.0, "fun": 10.5})
    soul.add_timeline_event(_group("g1", subject=None), event_summary="【日记】今日全群畅聊 1333 条，106 位群友参与。",
                            event_type="episode", timestamp=NOW - 3600)
    for i in range(12):
        db.person_timeline.add_event(bot_id="yushu", user_id="10001", group_id="g1", kind="impression",
                                     summary=f"第{i}次跟风挑刺发图敷衍的嘴贫老搭子RO", occurred_at=NOW - (20 - i) * 86400)
    db.person_timeline.set_unsettled_state(
        bot_id="yushu", user_id="10001", group_id="", energy=12.0, interaction_count=6,
        traces=[{"text": "半夜拉着器灵复盘版本更新的老搭子RO", "impact": 2.0, "ts": NOW - 3600}],
    )
    return soul, scope


def test_channel_renders_natural_language_without_numbers_or_group_diary(db):
    soul, scope = _seed(db)
    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=True)
    result = asyncio.run(RelationshipChannel(repository=soul, db=db).build(_ctx(scope)))
    text = result.text
    assert result.status == "hit"
    assert text.startswith("【你和对方】算「常聊」")
    assert "老搭子RO×" in text, "重复的称呼合并计数"
    assert "【当下感受】" in text and "复盘版本更新" in text
    for noise in ("权重=", "来源群", "900000001", "综合值", "familiarity=", "事件#", "1333", "<<impression", "未结算能量"):
        assert noise not in text
    assert "【该结算了】" not in text, "群聊里事后记账已启动：结算交给记账回看"
    assert len(text) < 700


def test_channel_settle_hint_follows_prompting_switch(db):
    soul, scope = _seed(db)
    configure_impression_prompting(per_turn_tag=True, bookkeeping_active=True)
    legacy = asyncio.run(RelationshipChannel(repository=soul, db=db).build(_ctx(scope))).text
    assert "【感知】未结算能量" in legacy and "<<impression:当下观感 | impact:1-5>>" in legacy
    assert legacy.rstrip().endswith("不要覆盖这句结算。"), "开关打开：与旧行为一致，结算提示在末尾"

    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=False)
    no_bookkeeping = asyncio.run(RelationshipChannel(repository=soul, db=db).build(_ctx(scope))).text
    assert "【该结算了】" in no_bookkeeping and "wave_memory_record_social_impression" in no_bookkeeping
    assert "<<impression" not in no_bookkeeping

    # 通道级覆盖优先于全局开关
    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=True)
    override = asyncio.run(RelationshipChannel(repository=soul, db=db).build(
        _ctx(scope, channel={"per_turn_impression_tag": True}))).text
    assert "<<impression" in override


def test_configure_prompting_none_keeps_current_values():
    configure_impression_prompting(per_turn_tag=True, bookkeeping_active=True)
    configure_impression_prompting(per_turn_tag=None, bookkeeping_active=None)
    assert impression_prompting() == {"per_turn_tag": True, "bookkeeping_active": True}


def test_bookkeeping_settle_block_does_not_depend_on_injection_hint(db):
    """事后记账自己按未结算能量列出「需要结算的人」，与注入里是否出现结算提示无关。"""
    from services.post_reply_bookkeeping import PostReplyBookkeeper

    _seed(db)
    configure_impression_prompting(per_turn_tag=False, bookkeeping_active=True)
    keeper = PostReplyBookkeeper(db, tool_registry=object(), llm=object())
    block = keeper._settle_block(_group("g1", subject=None), [(1, "10001", "老搭子RO", "在吗")])
    assert "【需要结算的人】" in block and "老搭子RO" in block
