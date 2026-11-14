"""覆盖立体心智状态、人物 Bot 自动发现与防互刷、以及多特征读空气打分引擎的单测。"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from domain.bot_profile import BotProfile, ProactiveSettings
from domain.mood_policy import MoodSettings, circadian_phase, compute_mind_state
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.social_repo import SocialRepo
from services.bot_discovery import BotDiscoveryService
from services.bot_mood import BotMoodService
from services.reply_decision import (
    GroupProactiveArbiter,
    ReplyDecisionService,
    ReplySignals,
    detect_message_air_flags,
    score_topic_division,
)


def _group(group_id: str = "398291136", user: str | None = "u1", bot: str = "yushu") -> RuntimeScope:
    return RuntimeScope(
        bot,
        "group",
        SessionRef(f"qq:group:{group_id}", "qq", "group", group_id),
        subject_principal_id=f"qq:user:{user}" if user else None,
    )


def _private(user: str = "u1", bot: str = "yushu") -> RuntimeScope:
    return RuntimeScope(
        bot,
        "private",
        SessionRef(f"qq:private:{user}", "qq", "private", user),
        subject_principal_id=f"qq:user:{user}",
    )


class TestMindStateAndSoulContext:
    def test_circadian_and_mind_state_compute(self):
        night = circadian_phase(3, MoodSettings())
        day = circadian_phase(14, MoodSettings())
        assert night["phase"] == "late_night"
        assert night["night_quiet"] is True
        assert day["phase"] == "afternoon"
        assert day["night_quiet"] is False

        mind_day = compute_mind_state(
            settings=MoodSettings(),
            valence=0.45,
            arousal=0.7,
            fatigue=0.1,
            hour=14,
            recent_replies=2,
            ignored_streak=0,
        )
        mind_night = compute_mind_state(
            settings=MoodSettings(),
            valence=0.0,
            arousal=0.3,
            fatigue=0.6,
            hour=3,
            recent_replies=25,
            ignored_streak=2,
        )
        assert 0.0 <= mind_day.energy <= 1.0
        assert 0.0 <= mind_day.sleepiness <= 1.0
        assert 0.0 <= mind_day.composure <= 1.0
        assert 0.0 <= mind_day.social_drive <= 1.0
        assert mind_day.energy > mind_night.energy
        assert mind_night.sleepiness > mind_day.sleepiness
        assert mind_day.stance in {"eager", "engaged", "open", "observing", "withdrawn"}

    def test_mood_service_soul_context_provider_and_group_focus(self, tmp_path):
        db = WaveMemoryDB(str(tmp_path / "mind.db"))
        clock = [1_800_000_000.0]
        profile = BotProfile(db_id="yushu", name="羽书", qq_id="2715127743", mood=MoodSettings(enabled=True))
        profiles = {"yushu": profile}
        svc = BotMoodService(db, profile_for=lambda b: profiles.get(b), clock=lambda: clock[0])

        g_scope = _group("g1", "u1", "yushu")
        svc.observe_message(g_scope, content="缺氧的液氧冷凝器又过热了", sender_name="阿明", directed=False)
        svc.observe_message(g_scope, content="感觉管道布置有问题", sender_name="阿明", directed=False)
        svc.note_bot_reply("yushu", session_id="qq:group:g1")

        ctx = svc.get_soul_context(scope=g_scope)
        assert ctx["status"] == "available"
        assert ctx["timezone"]
        assert isinstance(ctx["energy"], float)
        assert isinstance(ctx["sleepiness"], float)
        assert ctx["circadian"]["phase"]
        assert ctx["mind_state"]["group_focus"]["last_speaker"] == "阿明"
        assert ctx["mind_state"]["group_focus"]["engaged_active"] is True
        assert ctx["mind_state"]["group_focus"]["topic"]

        # 群友发消息之后无人回复 → 若记一次冷场(ignored)，ignored_streak 上升且冷却激活
        svc.note_silence_or_ignored("yushu", "qq:group:g1", reason="ignored")
        ctx_after = svc.get_soul_context(scope=g_scope)
        assert ctx_after["mind_state"]["group_focus"]["ignored_streak"] == 1
        assert ctx_after["mind_state"]["group_focus"]["cooling_active"] is True

        # 私聊触发的情绪，在群视角 mind_state 不泄露私聊具体原因
        p_scope = _private("u_secret", "yushu")
        svc.observe_message(p_scope, content="你真好，太喜欢你了", sender_name="神秘人", directed=True)
        mind_group = svc.mind_state("yushu", viewer_scope=g_scope)
        assert "narrative" in mind_group and mind_group["narrative"]
        db.close()


class TestBotDiscoveryService:
    def test_schema_migration_and_four_tier_tags(self, tmp_path):
        db_path = str(tmp_path / "disc.db")
        # 模拟旧库无 entity_kind 列
        raw = sqlite3.connect(db_path)
        raw.execute(
            "CREATE TABLE person_registry (qq_id TEXT PRIMARY KEY, display_name TEXT, aliases TEXT, first_seen REAL, last_seen REAL, message_count INTEGER DEFAULT 0, groups TEXT, metadata TEXT)"
        )
        raw.execute(
            "INSERT INTO person_registry (qq_id, display_name) VALUES (?, ?)",
            ("1336495069", "白真真"),
        )
        raw.commit()
        raw.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        SocialRepo(conn)  # 应自动幂等补齐 entity_kind 列

        clock = [1_800_000_000.0]
        svc = BotDiscoveryService(
            conn,
            system_bot_ids={"1336495069"},
            clock=lambda: clock[0],
        )
        # 1. 同部署已知己方 Bot 自动识别为 confirmed_bot
        tag_peer = svc.get_tag("1336495069", display_name="白真真")
        assert tag_peer["is_bot"] is True
        assert tag_peer["entity_kind"] == "confirmed_bot"

        # 2. 没有按昵称的自动判定：像 Bot 的昵称在羽书标记之前仍是 human
        assert svc.get_tag("999001", display_name="小冰Bot")["entity_kind"] == "human"

        # 3. 羽书在对话里标成疑似 Bot（source=bot_check）
        tag_sus = svc.set_tag("999001", entity_kind="suspected_bot", source="bot_check", reason="三次回得一字不差")
        assert tag_sus["entity_kind"] == "suspected_bot" and tag_sus["is_bot"] is True
        assert svc.get_tag("999001")["reason"] == "三次回得一字不差"

        # 管理员标为 verified_human 后，羽书之后的判断不能改写
        tag_human = svc.set_tag("999001", entity_kind="verified_human", source="admin", reason="真人朋友玩梗昵称")
        assert tag_human["entity_kind"] == "verified_human" and tag_human["is_bot"] is False
        tag_recheck = svc.set_tag("999001", entity_kind="suspected_bot", source="bot_check", reason="又觉得像")
        assert tag_recheck["entity_kind"] == "verified_human"

        # 4. 防互刷协同上限：2 轮后熔断
        ok1, c1 = svc.check_and_record_collab_turn(bot_id="yushu", session_id="qq:group:g1", sender_id="1336495069", max_turns=2, record=True)
        ok2, c2 = svc.check_and_record_collab_turn(bot_id="yushu", session_id="qq:group:g1", sender_id="1336495069", max_turns=2, record=True)
        ok3, c3 = svc.check_and_record_collab_turn(bot_id="yushu", session_id="qq:group:g1", sender_id="1336495069", max_turns=2, record=True)
        assert (ok1, c1) == (True, 1)
        assert (ok2, c2) == (True, 2)
        assert (ok3, c3) == (False, 2)
        conn.close()


class TestBotTagIsJudgedByTheModel:
    """谁是 Bot 由羽书在对话里自己判断：系统里没有任何按昵称、报错词、复读或节奏的规则。"""

    def test_no_rule_based_detection_is_left(self):
        import services.bot_discovery as mod

        assert not hasattr(mod, "_BOT_NAME_RE")
        assert not hasattr(mod, "_BOT_ERROR_LEAK_RE")
        assert not hasattr(mod, "_CROWD_BOT_CALLOUT_RE")
        assert not hasattr(mod.BotDiscoveryService, "observe_group_message")

    def test_names_that_look_like_bots_stay_human_until_the_model_decides(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "names.db"))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE person_registry (qq_id TEXT PRIMARY KEY, display_name TEXT)")
        conn.execute("INSERT INTO person_registry (qq_id, display_name) VALUES ('1', '小冰Bot'), ('2', 'Abbot'), ('3', '[Bot]客服')")
        conn.commit()
        svc = BotDiscoveryService(conn, system_bot_ids=set())

        for uid, name in ("1", "小冰Bot"), ("2", "Abbot"), ("3", "[Bot]客服"):
            tag = svc.get_tag(uid, display_name=name)
            assert tag["entity_kind"] == "human" and tag["is_bot"] is False, name
        conn.close()
class TestMultiFeatureAirReadingAndArbiter:
    def test_proactive_settings_round_trip(self):
        p = BotProfile(
            db_id="baizz",
            name="白真真",
            qq_id="1336495069",
            proactive=ProactiveSettings(
                enabled=True,
                mode="enforce",
                style_preset="quiet",
                preferred_topics=("安监", "天剑门", "功法"),
                avoid_topics=("缺氧",),
                score_threshold=65,
            ),
        )
        data = p.to_dict()
        restored = BotProfile.from_dict(data)
        assert restored.proactive.enabled is True
        assert restored.proactive.mode == "enforce"
        assert restored.proactive.style_preset == "quiet"
        assert list(restored.proactive.preferred_topics) == ["安监", "天剑门", "功法"]
        assert list(restored.proactive.avoid_topics) == ["缺氧"]
        assert restored.proactive.score_threshold == 65

    def test_air_flags_and_topic_division(self):
        flags = detect_message_air_flags(
            "阿明你觉得呢",
            bot_terms=("羽书",),
            bot_qq_ids=("2715127743",),
            at_qq_ids=("123456",),
        )
        assert flags["addressed_other"] is True

        flags_cmd = detect_message_air_flags("/help", bot_terms=("羽书",))
        assert flags_cmd["is_command"] is True

        match, avoid_hit, hits = score_topic_division(
            "今天安监局抓了个不按规范修仙的炼丹家伙",
            preferred_topics=("安监", "天剑门"),
            interest_keywords=("炼丹",),
            avoid_topics=("缺氧",),
        )
        assert match >= 0.70
        assert avoid_hit is False
        assert "安监" in hits and "炼丹" in hits

    def test_proactive_wake_hard_filters_and_scoring(self):
        clock = [1_800_000_000.0]
        arbiter = GroupProactiveArbiter(clock=lambda: clock[0])
        svc = ReplyDecisionService(
            {
                "bots": {
                    "yushu": {"mode": "enforce", "score_threshold": 55},
                    "baizz": {"mode": "enforce", "score_threshold": 65},
                }
            },
            clock=lambda: clock[0],
            arbiter=arbiter,
        )

        # 1. 发消息者是 Bot 且未直接 @ → 硬过滤拦截，防互刷
        d_bot = svc.decide(ReplySignals(
            bot_id="yushu", session_id="qq:group:g1", visibility="group",
            message="今天天气真好", direct=False, proactive_enabled=True, sender_is_bot=True,
        ))
        assert d_bot["allow"] is False
        assert "sender_is_bot_no_proactive" in d_bot["reasons"]

        # 2. 对话中命中避让词 → 压制
        d_avoid = svc.decide(ReplySignals(
            bot_id="baizz", session_id="qq:group:g1", visibility="group",
            message="缺氧的液氧管道好难修", direct=False, proactive_enabled=True,
            avoid_topic_hit=True, topic_match=0.0,
        ))
        assert d_avoid["allow"] is False
        assert "avoid_topic_hit" in d_avoid["reasons"]

        # 3. 命中擅长话题 + 接话窗口 + 心智状态高驱动力 → 读空气主动唤醒
        d_wake = svc.decide(ReplySignals(
            bot_id="yushu", session_id="qq:group:g1", visibility="group",
            message="羽书之前不是也卡过缺氧电解吗", direct=False, proactive_enabled=True,
            topic_match=1.0, followup_eligible=True, mind_drive=0.85, mind_energy=0.8,
            mind_stance="eager", mood_influence=1.0, silence_messages=3,
        ))
        assert d_wake["allow"] is True
        assert d_wake["proactive_wake"] is True
        assert d_wake["score"] >= 55

        # 4. 群级双 Bot 仲裁：同一个群同条消息只能有一个 Bot 获胜，另一个落败让路
        assert arbiter.try_claim_message(bot_id="yushu", session_id="qq:group:g1", message_key="m1", score=80.0) is True
        assert arbiter.try_claim_message(bot_id="baizz", session_id="qq:group:g1", message_key="m1", score=60.0) is False
        # 羽书主动发言落库后，共享冷却生效
        svc.note_reply("yushu", "qq:group:g1", "group", proactive=True)
        assert arbiter.shared_cooldown_remaining(bot_id="baizz", session_id="qq:group:g1", cooldown_seconds=90.0) > 0
        # 90 秒后共享冷却解除
        clock[0] += 95.0
        assert arbiter.shared_cooldown_remaining(bot_id="baizz", session_id="qq:group:g1", cooldown_seconds=90.0) == 0.0
