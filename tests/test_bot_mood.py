"""心情 v2：一个 Bot 一份心情，事件驱动、带惯性，向画像基线回落。

锁定的行为：
- 画像（Bot Profile 的 mood 段）缺省为中性默认值，越界值被夹到合法区间，旧 Profile 往返不丢字段；
- 冲 Bot 来的话按情绪词评估，关系好的人的损话更像玩笑、敌意高的人更刺痛；否定词反向；
- 事件按各自半衰期指数回落，强事件更久；叠加后软饱和，单条事件冲击有上限；
- 心情跨群/私聊共享，私聊来源的原因在别处只说「私下有件事」；
- 关系事件增量导入且幂等；事后记账的心情字段解析失败安全忽略；
- 疲劳（回复太多、深夜）可配可关；群氛围只作小权重；
- 迁移幂等，只新增表，不动旧 scoped_soul_mood。
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from domain.bot_profile import BotProfile
from domain.mood_policy import (
    MoodEventView,
    MoodSettings,
    compose_mood,
    decay_factor,
    fatigue_level,
    mood_word,
    parse_bookkeeping_mood,
    relationship_event_impulse,
    score_directed_message,
    speaking_hint,
)
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.connection import ConnectionManager
from engine.db.migrations.bot_mood import ensure_bot_mood_schema
from services.bot_mood import BotMoodService
from services.injection.channels.soul_state import SoulStateChannel

NOW = 1_800_000_000.0
MIN = 60.0
HOUR = 3600.0


def _group(group_id: str = "g1", user: str | None = "u1", bot: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(
        bot, "group", SessionRef(f"qq:group:{group_id}", "qq", "group", group_id),
        subject_principal_id=f"qq:user:{user}" if user else None,
    )


def _private(user: str = "u1", bot: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(
        bot, "private", SessionRef(f"qq:private:{user}", "qq", "private", user),
        subject_principal_id=f"qq:user:{user}",
    )


def _event(valence: float, *, age: float = 0.0, intensity: float = 0.3, arousal: float = 0.0, **kw) -> MoodEventView:
    return MoodEventView(id=1, source=kw.get("source", "test"), valence=valence, arousal=arousal,
                         intensity=intensity, created_at=NOW - age, cause=kw.get("cause", ""),
                         session_id=kw.get("session_id", "qq:group:g1"), visibility=kw.get("visibility", "group"))


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "mood.sqlite"), dimension=3)
    try:
        yield database
    finally:
        database.close()


def _service(db, *, settings: MoodSettings | None = None, hour: int = 12, now: float = NOW) -> BotMoodService:
    clock = {"now": now}
    profile = SimpleNamespace(mood=settings or MoodSettings())
    service = BotMoodService(db, profile_for=lambda _bot: profile, clock=lambda: clock["now"], hour_of=lambda _ts: hour)
    service._clock = clock  # 测试里推进时间用
    return service


# ─── 画像 ───


def test_settings_defaults_clamp_and_profile_round_trip():
    defaults = MoodSettings.from_dict(None)
    assert defaults == MoodSettings()
    assert defaults.enabled is True and defaults.fatigue_enabled is True
    weird = MoodSettings.from_dict({
        "enabled": None, "fatigue_enabled": "off", "baseline_valence": 9, "baseline_arousal": "-1",
        "sensitivity": "abc", "atmosphere_weight": 3, "night_start_hour": 30,
        "speaking_hints": "开心=话多一点\n烦躁：句子变短\n无效行", "positive_words": "绝绝子，yyds",
    })
    assert weird.enabled is True, "None 按默认开启（旧 Profile 缺字段）"
    assert weird.fatigue_enabled is False
    assert (weird.baseline_valence, weird.baseline_arousal) == (1.0, 0.0)
    assert weird.sensitivity == 1.0 and weird.atmosphere_weight == 1.0 and weird.night_start_hour == 23
    assert weird.speaking_hints == {"开心": "话多一点", "烦躁": "句子变短"}
    assert weird.positive_words == ["绝绝子", "yyds"]

    legacy = BotProfile.from_dict({"db_id": "bot-a", "name": "阿尔法"})
    assert legacy.mood == MoodSettings()
    custom = BotProfile.from_dict({"db_id": "bot-a", "name": "阿尔法", "mood": {"sensitivity": 1.5, "half_life_minutes": 30}})
    again = BotProfile.from_dict(custom.to_dict())
    assert again.mood.sensitivity == 1.5 and again.mood.half_life_seconds() == 1800.0
    assert MoodSettings().half_life_seconds(default_minutes=90) == 5400.0, "0 = 用全局 mood_duration_hours"


# ─── 冲她来的话 ───


def test_directed_message_scoring_with_relationship_and_negation():
    praise = score_directed_message("你真可爱，好厉害")
    insult = score_directed_message("你个人机，闭嘴")
    assert praise.kind == "praise" and praise.valence > 0.3
    assert insult.kind == "insult" and insult.valence < -0.3
    assert score_directed_message("今天天气怎么样").is_empty

    friend = score_directed_message("你个人机，闭嘴", closeness=1.0)
    enemy = score_directed_message("你个人机，闭嘴", hostility=1.0)
    assert abs(friend.valence) < abs(insult.valence) < abs(enemy.valence), "关系好的损话更像玩笑，敌意高更刺痛"
    banter = score_directed_message("哈哈你个笨蛋", closeness=0.9)
    assert banter.kind == "banter" and banter.valence > 0, "熟人的笑骂是斗嘴"
    assert score_directed_message("你真可爱", hostility=1.0).valence < praise.valence

    assert score_directed_message("你一点都不可爱").valence < 0, "被否定的夸奖算负面"
    assert score_directed_message("我不讨厌你").valence > 0
    extreme = score_directed_message("nmsl")
    assert extreme.kind == "extreme" and extreme.valence <= -0.5 and extreme.intensity == 1.0

    numb = score_directed_message("你真可爱", MoodSettings(sensitivity=0.0))
    assert numb.valence == 0.0, "敏感度 0 不为外事所动"
    custom = score_directed_message("yyds", MoodSettings(positive_words=["yyds"]))
    assert custom.valence > 0


def test_relationship_event_and_bookkeeping_parsing():
    praised = relationship_event_impulse("bot_praised", 3)
    attacked = relationship_event_impulse("bot_attacked", 5)
    assert praised[0] > 0 and attacked[0] < 0 and attacked[1] > 0
    assert relationship_event_impulse("direct_reply", 1) is None
    assert relationship_event_impulse("manual_adjustment", 5) is None

    up = parse_bookkeeping_mood({"direction": "up", "strength": 0.8, "reason": "大家陪我过生日"})
    assert up[0] > 0 and up[3] == "大家陪我过生日"
    down = parse_bookkeeping_mood({"direction": "变差", "strength": "0.5", "reason": "被说是人机"})
    assert down[0] < 0
    for bad in (None, "up", {"direction": "none", "strength": 1, "reason": "x"},
                {"direction": "up", "strength": "很多", "reason": "x"},
                {"direction": "up", "strength": 0.9, "reason": ""},
                {"direction": "up", "strength": float("nan"), "reason": "x"}):
        assert parse_bookkeeping_mood(bad) is None


# ─── 合成、衰减、情绪词 ───


def test_decay_returns_to_baseline_and_strong_events_last_longer():
    settings = MoodSettings(half_life_minutes=60, baseline_valence=0.1, strong_event_boost=1.0)
    assert decay_factor(3600, 3600) == pytest.approx(0.5)
    fresh = compose_mood([_event(-0.5, age=0)], settings, now=NOW)
    later = compose_mood([_event(-0.5, age=4 * HOUR)], settings, now=NOW)
    much_later = compose_mood([_event(-0.5, age=48 * HOUR)], settings, now=NOW)
    assert fresh.valence < later.valence < much_later.valence
    assert much_later.valence == pytest.approx(0.1, abs=1e-3), "向基线回落"

    weak = compose_mood([_event(-0.4, age=2 * HOUR, intensity=0.0)], settings, now=NOW)
    strong = compose_mood([_event(-0.4, age=2 * HOUR, intensity=1.0)], settings, now=NOW)
    assert strong.valence < weak.valence, "强事件影响更久"

    pile = compose_mood([_event(-1.0) for _ in range(30)], settings, now=NOW)
    assert -1.0 <= pile.valence < -0.9, "软饱和不越界"
    assert 0.0 <= pile.arousal <= 1.0


def test_mood_words_and_fatigue():
    assert mood_word(0.6, 0.8)[0] == "兴奋"
    assert mood_word(0.5, 0.3)[0] == "开心"
    assert mood_word(0.0, 0.4) == ("平静", "平静")
    assert mood_word(0.0, 0.1)[0] == "无聊"
    assert mood_word(-0.2, 0.7) == ("有点烦躁", "负面")
    assert mood_word(-0.6, 0.45)[0] == "委屈"
    assert mood_word(-0.6, 0.1)[0] == "低落"
    assert mood_word(-0.8, 0.8)[0] == "很生气"
    assert mood_word(0.0, 0.3, fatigue=0.7) == ("疲惫", "疲惫")
    assert mood_word(0.5, 0.3, fatigue=0.35)[0] == "开心，有点累"

    settings = MoodSettings(fatigue_reply_threshold=10, fatigue_strength=1.0, night_start_hour=23, night_end_hour=6)
    assert fatigue_level(settings, recent_replies=5, hour=12) == 0.0
    assert fatigue_level(settings, recent_replies=15, hour=12) == 0.5
    assert fatigue_level(settings, recent_replies=0, hour=2) == 1.0, "跨午夜的深夜时段"
    assert fatigue_level(MoodSettings(fatigue_enabled=False), recent_replies=999, hour=2) == 0.0
    assert fatigue_level(MoodSettings(night_start_hour=3, night_end_hour=3, fatigue_strength=1.0), recent_replies=0, hour=3) == 0.0

    hints = MoodSettings(speaking_hints={"烦躁": "句子变短", "正面": "多开玩笑"})
    assert speaking_hint(hints, "有点烦躁", "负面") == "句子变短"
    assert speaking_hint(hints, "开心", "正面") == "多开玩笑"
    assert speaking_hint(MoodSettings(), "开心", "正面") == ""


# ─── 服务：一份心情、跨场合、脱敏 ───


def test_one_mood_shared_across_groups_and_private_causes_redacted(db):
    service = _service(db)
    assert service.observe_message(_group("g1", "u1"), content="你个人机，闭嘴", sender_name="小明",
                                   directed=True, now=NOW - 10 * MIN)
    assert service.observe_message(_group("g1", "u2"), content="人机", sender_name="小红",
                                   directed=False, now=NOW) is None, "不是冲她来的不评估"
    in_other_group = service.current("bot-a", viewer_scope=_group("g2", None))
    assert in_other_group["category"] == "负面" and in_other_group["valence"] < 0
    assert in_other_group["causes"][0]["text"].startswith("10 分钟前在别的群里，小明说你「")
    same_group = service.current("bot-a", viewer_scope=_group("g1", None))
    assert same_group["causes"][0]["text"].startswith("10 分钟前，小明说你")

    service.observe_message(_private("u3"), content="你真好，谢谢你，爱你", sender_name="阿青", directed=True, now=NOW - MIN)
    in_group = service.current("bot-a", viewer_scope=_group("g2", None))
    assert in_group["valence"] > in_other_group["valence"], "私聊里的事也影响同一份心情"
    private_causes = [c for c in in_group["causes"] if c["private"]]
    assert private_causes and private_causes[0]["redacted"] is True
    assert private_causes[0]["text"] == "刚才私下有件事让你开心"
    assert private_causes[0]["subject_name"] == "" and private_causes[0]["session_id"] == ""
    in_that_private = service.current("bot-a", viewer_scope=_private("u3"))
    assert any("阿青夸你" in c["text"] for c in in_that_private["causes"])
    assert all("阿青" not in c["text"] for c in service.current("bot-a", viewer_scope=_private("u4"))["causes"])
    assert service.current("bot-b")["causes"] == [], "别的 Bot 不共享"


def test_message_spam_is_merged_and_single_event_is_capped(db):
    service = _service(db, settings=MoodSettings(sensitivity=3.0))
    first = service.observe_message(_group(), content="nmsl 傻逼 废物 垃圾", sender_name="x", directed=True, now=NOW - 20)
    again = service.observe_message(_group(), content="傻逼", sender_name="x", directed=True, now=NOW - 10)
    assert first is not None and again is None
    assert service.stats["merged"] == 1
    events = db.bot_mood.list_events("bot-a", since=0)
    assert len(events) == 1 and events[0].valence == -0.6, "单条事件冲击有上限"


def test_relationship_closeness_uses_stance_when_repository_offers_it(db):
    class Stance:
        def __init__(self, closeness):
            self.closeness = closeness

        def relationship_stance_for(self, bot_id, subject):
            return {"available": True, "closeness": self.closeness, "dimensions": {"hostility": 0}}

    service = _service(db)
    service.db = SimpleNamespace(bot_mood=db.bot_mood, soul_repository=Stance(1.0))
    assert service._relationship(_group(), "qq:user:u1") == (1.0, 0.0)
    service.db = SimpleNamespace(bot_mood=db.bot_mood, soul_repository=Stance(-0.5))
    assert service._relationship(_group(), "qq:user:u1") == (0.0, 0.5)
    # 没有 relationship_stance_for 时按跨群五维汇总
    scope = _group("g1", "u1")
    db.soul_repository.record_relationship_event(scope, event_type="bot_attacked", dimension="hostility",
                                                 delta=8, reason="骂人", created_at=NOW - HOUR)
    service.db = db
    closeness, hostility = service._relationship(scope, "qq:user:u1")
    assert closeness == 0.0 and hostility == pytest.approx(0.2)


def test_relationship_events_are_imported_once_with_cursor(db):
    scope = _group("g1", "u1")
    repo = db.soul_repository
    repo.record_relationship_event(scope, event_type="joke", dimension="fun", delta=2, reason="很久以前的玩笑",
                                   created_at=NOW - 30 * HOUR)
    repo.record_relationship_event(scope, event_type="bot_praised", dimension="trust", delta=3,
                                   reason="夸她画得好", created_at=NOW - HOUR)
    repo.record_relationship_event(scope, event_type="direct_reply", dimension="trust", delta=1,
                                   reason="回复", created_at=NOW - HOUR)
    service = _service(db)
    assert service.sync_relationship_events("bot-a", now=NOW, force=True) == 1, "首次只回看 12 小时，direct_reply 不影响心情"
    assert service.sync_relationship_events("bot-a", now=NOW, force=True) == 0, "幂等"
    state = db.bot_mood.get_state("bot-a")
    assert state["relationship_cursor"] == db.bot_mood.max_relationship_event_id("bot-a")
    repo.record_relationship_event(_private("u2"), event_type="bot_attacked", dimension="hostility", delta=5,
                                   reason="私下吵架", created_at=NOW - MIN)
    assert service.sync_relationship_events("bot-a", now=NOW, force=True) == 1
    causes = service.current("bot-a", viewer_scope=_group("g1", None))["causes"]
    assert any(c["redacted"] and "私下有件事" in c["text"] for c in causes)
    assert all("私下吵架" not in c["text"] for c in causes)
    assert any("夸了你（夸她画得好）" in c["text"] for c in causes)


def test_bookkeeping_mood_and_atmosphere(db):
    service = _service(db, settings=MoodSettings(atmosphere_weight=0.2))
    assert service.record_bookkeeping(_group(), {"direction": "up", "strength": 0.6, "reason": "大家一起通关了"},
                                      ref="42", now=NOW) is not None
    assert service.record_bookkeeping(_group(), {"direction": "up", "strength": 0.6, "reason": "重复"},
                                      ref="42", now=NOW) is None, "同一次记账只记一次"
    assert service.record_bookkeeping(_group(), {"direction": "???"}, now=NOW) is None
    assert service.record_bookkeeping(_group(), None, now=NOW) is None
    assert service.stats["bookkeeping:ignored"] == 1 and service.stats["bookkeeping:absent"] == 1

    assert service.observe_atmosphere(_group("g1", None), valence=0.05, arousal=0.5, now=NOW) is None, "平稳不算"
    assert service.observe_atmosphere(_group("g1", None), valence=0.8, arousal=0.6, now=NOW) is not None
    assert service.observe_atmosphere(_group("g1", None), valence=0.8, arousal=0.6, now=NOW + 60) is None, "限频"
    atmosphere = [e for e in db.bot_mood.list_events("bot-a", since=0) if e.source == "atmosphere"][0]
    assert 0 < atmosphere.valence <= 0.1, "群氛围只作小权重"
    off = _service(db, settings=MoodSettings(atmosphere_weight=0.0))
    assert off.observe_atmosphere(_group("g9", None), valence=0.9, arousal=0.9, now=NOW) is None


def test_fatigue_from_replies_and_disabled_profile(db):
    settings = MoodSettings(fatigue_reply_threshold=5, fatigue_strength=1.0, fatigue_window_minutes=60)
    service = _service(db, settings=settings)
    for i in range(12):
        service.note_bot_reply("bot-a", now=NOW - i * MIN)
    mood = service.current("bot-a")
    assert mood["fatigue"] == 1.0 and mood["category"] == "疲惫"
    assert mood["fatigue_reasons"] == ["这 1 小时回了很多消息"]
    lines = service.render_lines("bot-a", _group())
    assert lines[0] == "心情：疲惫" and "回了很多消息" in lines[1]

    night = _service(db, settings=MoodSettings(fatigue_strength=0.5), hour=3)
    assert "夜深了" in night.current("bot-b")["fatigue_reasons"]

    disabled = _service(db, settings=MoodSettings(enabled=False))
    assert disabled.observe_message(_group(), content="你真可爱", directed=True, now=NOW) is None
    assert disabled.render_lines("bot-a", _group()) == []


def test_render_lines_are_natural_language_with_hint(db):
    service = _service(db, settings=MoodSettings(speaking_hints={"负面": "回话更短，带点刺"}))
    assert service.render_lines("bot-a", _group()) == [], "没事发生、心情在基线：不注入"
    service.observe_message(_group("g1", "u1"), content="你是废物吧，闭嘴", sender_name="小明", directed=True, now=NOW - 30 * MIN)
    lines = service.render_lines("bot-a", _group("g2", None))
    assert lines[0].startswith("心情：") and any(w in lines[0] for w in ("烦躁", "郁闷", "委屈", "低落", "生气"))
    assert lines[1].startswith("缘由：30 分钟前在别的群里，小明说你「")
    assert lines[2] == "说话倾向：回话更短，带点刺"
    text = "\n".join(lines)
    assert "valence" not in text and "0." not in text, "不输出裸数字"

    ctx = SimpleNamespace(mode="full", config={"channels": {"soul_state": {"enabled": True}}}, scope=_group("g2", None))
    injected = asyncio.run(SoulStateChannel(repository=db.soul_repository, mood_service=service).build(ctx))
    assert injected.status == "hit" and "- 心情：" in injected.text and "- 缘由：" in injected.text


def test_prune_and_tick(db):
    service = _service(db)
    service.observe_message(_group(), content="你真棒", directed=True, now=NOW - 20 * 86400)
    service.observe_message(_group(), content="你真棒", directed=True, now=NOW - MIN)
    service.tick(["bot-a"], now=NOW)
    assert len(db.bot_mood.list_events("bot-a", since=0)) == 1
    assert service.stats["pruned"] == 1
    assert db.bot_mood.get_state("bot-a")["word"]


# ─── 迁移 ───


def test_migration_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE scoped_soul_mood (bot_id TEXT, session_id TEXT, visibility TEXT, valence REAL)")
    conn.execute("INSERT INTO scoped_soul_mood VALUES ('bot-a', 'qq:group:g1', 'group', 0.3)")
    conn.commit()
    conn.close()
    cm = ConnectionManager(str(path))
    try:
        ensure_bot_mood_schema(cm)
        ensure_bot_mood_schema(cm)
        tables = {row[0] for row in cm.execute_read("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"bot_mood_events", "bot_mood_state", "scoped_soul_mood"} <= tables
        assert cm.execute_read("SELECT valence FROM scoped_soul_mood").fetchall() == [(0.3,)], "旧数据不动"
    finally:
        cm.close()
