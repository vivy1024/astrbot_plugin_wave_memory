"""信念 v2 注入：不再每轮注入，只在话题相关时浮现 1–2 条已形成/稳固的看法（不调用模型、不做向量检索）。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.bot_belief_repo import BeliefWrite
from services.belief_engine import BeliefEngine, describe_experiences, relevance
from services.injection.channels.belief import BeliefChannel

NOW = datetime(2026, 9, 29, 20, 0).timestamp()
BOT = "bot-demo"


def group(conversation: str = "g1", bot: str = BOT) -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(f"qq:group:{conversation}", "qq", "group", conversation))


def private(user: str = "10000001") -> RuntimeScope:
    return RuntimeScope(BOT, "private", SessionRef(f"qq:private:{user}", "qq", "private", user),
                        subject_principal_id=f"qq:user:{user}")


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "inject.sqlite"), dimension=3)
    rows = [
        ("我讲冷笑话群里大多不接，他们更吃玩梗", "group", "formed", ["冷笑话", "玩梗"], ["2026-09-21", "2026-09-24", "2026-09-27"], ["qq:group:g1"]),
        ("直播时我更喜欢在 MC 里盖房子而不是打怪", "self", "stable", ["盖房子", "打怪"], ["2026-09-02", "2026-09-15"], ["qq:group:g2"]),
        ("这个群晚上话多，白天大家都在潜水", "group", "formed", ["晚上", "潜水"], ["2026-09-22", "2026-09-26"], ["qq:group:g1"]),
        ("萌芽的看法：冷笑话其实挺好笑", "self", "sprout", ["冷笑话"], ["2026-09-28"], ["qq:group:g1"]),
        ("动摇的看法：冷笑话大家都爱听", "self", "shaken", ["冷笑话"], ["2026-09-10", "2026-09-11"], ["qq:group:g1"]),
        ("淡出的看法：冷笑话没人在意", "self", "faded", ["冷笑话"], ["2026-08-10"], ["qq:group:g1"]),
        ("否决的看法：冷笑话是最好的笑话", "self", "vetoed", ["冷笑话"], ["2026-09-01", "2026-09-02"], ["qq:group:g1"]),
    ]
    database.bot_beliefs.apply(BOT, [
        BeliefWrite(None, {"content": c, "category": cat, "status": st, "triggers": tr, "support_dates": dates,
                           "session_ids": sessions, "confirm_runs": 3, "last_confirmed_at": NOW - 86400})
        for c, cat, st, tr, dates, sessions in rows
    ], now=NOW)
    yield database
    database.close()


def engine(db) -> BeliefEngine:
    return BeliefEngine(db, clock=lambda: NOW)


def test_only_topic_relevant_formed_or_stable_views_surface(db):
    details = engine(db).get_injection_details(group("g1"), message="我再讲个冷笑话你们接不接")
    assert details["text"].splitlines() == [
        "<beliefs>",
        "你这段时间的体会（是你自己从经历里慢慢形成的看法，可以参考，不必复述）：",
        "- 我讲冷笑话群里大多不接，他们更吃玩梗（来自 9 月下旬的几次经历）",
        "</beliefs>",
    ]
    assert details["matches"][0]["triggers_hit"] == ["冷笑话"]
    for hidden in ("萌芽的看法", "动摇的看法", "淡出的看法", "否决的看法"):
        assert hidden not in details["text"], "只含已形成/稳固"


def test_unrelated_topic_injects_nothing(db):
    for message in ("晚饭吃什么好呢", "嗯", "", "今天天气真不错"):
        details = engine(db).get_injection_details(group("g1"), message=message)
        assert details["text"] == "" and details["belief_ids"] == []


def test_at_most_two_views_and_english_word_overlap(db):
    details = engine(db).get_injection_details(group("g1"), message="晚上一起玩 MC 盖房子吗，顺便讲冷笑话")
    assert len(details["belief_ids"]) == 2
    mc = engine(db).get_injection_details(private(), message="今晚玩mc吗")
    assert "盖房子" in mc["text"]
    assert "来自 9 月的两次经历" in mc["text"], "跨旬只说月份"


def test_views_about_a_group_only_surface_in_that_group(db):
    assert "晚上话多" in engine(db).get_injection_details(group("g1"), message="晚上大家都在吗")["text"]
    assert "晚上话多" not in engine(db).get_injection_details(group("g2"), message="晚上大家都在吗")["text"]
    assert "晚上话多" not in engine(db).get_injection_details(private(), message="晚上大家都在吗")["text"]
    # 关于自己的看法跨群、私聊都可以
    assert "盖房子" in engine(db).get_injection_details(group("g3"), message="要不要去盖房子")["text"]


def test_views_belong_to_their_own_bot(db):
    other = BeliefEngine(db, clock=lambda: NOW).get_injection_details(group("g1", bot="bot-other"), message="讲个冷笑话")
    assert other["text"] == ""


def test_relevance_is_cheap_and_ignores_function_words():
    belief = {"content": "我讲冷笑话群里大多不接，他们更吃玩梗", "triggers": []}
    assert relevance("你们觉得这个怎么样", belief)[0] == 0
    score, hits = relevance("来个冷笑话", belief)
    assert score >= 2 and hits == []
    assert relevance("[CQ:at,qq=10000001] 冷", belief)[0] == 0


def test_experience_phrase():
    assert describe_experiences(["2026-09-21", "2026-09-28"], now=NOW) == "来自 9 月下旬的两次经历"
    assert describe_experiences(["2026-09-05"], now=NOW) == "来自 9 月上旬的一次经历"
    assert describe_experiences(["2026-08-30", "2026-09-02", "2026-09-03"], now=NOW) == "来自 8 月到 9 月的几次经历"
    assert describe_experiences(["2025-12-30", "2026-01-02"], now=NOW) == "来自 2025 年 12 月到 2026 年 1 月的两次经历"
    assert describe_experiences([], now=NOW) == "来自最近的经历"


def test_channel_end_to_end_without_model_or_embedding(db):
    ctx = SimpleNamespace(mode="full", config={"channels": {"belief": {"enabled": True}}}, scope=group("g1"),
                          message="冷笑话时间到", sender_id="10000001", bot_profile_id=BOT, trace_id="t1")
    result = asyncio.run(BeliefChannel(belief_engine=BeliefEngine(db, llm_client=None, clock=lambda: NOW)).build(ctx))
    assert result.status == "hit" and "我讲冷笑话群里大多不接" in result.text
    quiet = SimpleNamespace(**{**vars(ctx), "message": "晚饭吃什么"})
    assert asyncio.run(BeliefChannel(belief_engine=BeliefEngine(db, clock=lambda: NOW)).build(quiet)).status == "empty"


def test_legacy_archived_rows_are_never_injected(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    db = WaveMemoryDB(path, dimension=3)
    db.scoped_knowledge.upsert_scoped_belief(group("g1"), belief_key="legacy", content="测试群友甲偏爱射击游戏",
                                             belief_type="person_judgment", status="active")
    db.close()
    db = WaveMemoryDB(path, dimension=3)
    try:
        assert engine(db).get_injection_details(group("g1"), message="射击游戏")["text"] == ""
    finally:
        db.close()


# ─── 双向词法匹配 + 稳固看法常驻兜底 ───


class _Rng:
    """可控随机：random() 按队列给值，choices 取第一条。"""

    def __init__(self, values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if self.values else 1.0

    def choices(self, population, weights=None, k=1):
        return [population[0]]


def test_partial_word_hits_trigger_in_both_directions():
    belief = {"content": "我讲冷笑话群里大多不接", "triggers": ["冷笑话"]}
    score, hits = relevance("来讲个笑话听听", belief)
    assert score > 0 and hits == ["冷笑话"], "消息里的「笑话」是触发词「冷笑话」的一部分"
    score, hits = relevance("我昨天盖房子盖到半夜", {"content": "直播时我更喜欢盖房子", "triggers": ["房子"]})
    assert hits == ["房子"]


def test_whole_word_in_belief_text_counts_without_trigger():
    belief = {"content": "直播时我更喜欢在 MC 里盖房子而不是打怪", "triggers": []}
    score, hits = relevance("今天打怪好累", belief)
    assert score > 0 and hits == []


def test_filler_words_do_not_match():
    belief = {"content": "我讲冷笑话群里大多不接", "triggers": ["冷笑话"]}
    for message in ("我们知道了", "真的吗", "然后呢"):
        assert relevance(message, belief) == (0.0, [])


def test_ambient_stable_view_surfaces_with_probability_and_cooldown(db):
    clock = {"now": NOW}
    eng = BeliefEngine(db, clock=lambda: clock["now"], ambient_probability=0.5,
                       ambient_cooldown_seconds=3600, rng=_Rng([0.9, 0.1, 0.1]))
    miss = eng.get_injection_details(group("g1"), message="晚饭吃什么好呢")
    assert miss["text"] == "" and miss["reason"] == "no_relevant_belief", "概率没抽中"

    hit = eng.get_injection_details(group("g1"), message="晚饭吃什么好呢")
    assert hit["reason"] == "ambient"
    assert "平时就有的一个态度" in hit["text"]
    assert "盖房子" in hit["text"], "只从稳固看法里挑"
    assert hit["matches"][0]["ambient"] is True

    again = eng.get_injection_details(group("g1"), message="晚饭吃什么好呢")
    assert again["text"] == "", "冷却期内不再流露"
    other_place = eng.get_injection_details(group("g2"), message="晚饭吃什么好呢")
    assert other_place["reason"] == "ambient", "冷却按场合分开"


def test_ambient_dry_run_does_not_consume_cooldown(db):
    eng = BeliefEngine(db, clock=lambda: NOW, ambient_probability=1.0, rng=_Rng([0.0, 0.0]))
    preview = eng.get_injection_details(group("g1"), message="晚饭吃什么", dry_run=True)
    assert preview["reason"] == "ambient"
    real = eng.get_injection_details(group("g1"), message="晚饭吃什么")
    assert real["reason"] == "ambient", "预览不占冷却"


def test_ambient_off_by_default_and_relevant_views_take_priority(db):
    assert engine(db).get_injection_details(group("g1"), message="晚饭吃什么")["text"] == ""
    eng = BeliefEngine(db, clock=lambda: NOW, ambient_probability=1.0, rng=_Rng([0.0]))
    details = eng.get_injection_details(group("g1"), message="我再讲个冷笑话")
    assert details.get("reason") != "ambient" and "冷笑话" in details["text"]
