"""生理闭环单测：自然褪黑素入睡、日间开口耗能与歇口气回血、热聊余温不断电、可被吵醒起床气、独立服务静默与时区对齐。"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

if "astrbot.api" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass

    _api.logger = _Logger()
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

import pytest

from domain import body_policy as bp
from domain.bot_profile import BodySettings
from domain.mood_policy import MoodSettings
from engine.db.bot_body_repo import BotBodyRepository
from engine.db.connection import ConnectionManager
from services.bot_body import BotBodyService
from services.bot_mood import BotMoodService

BOT = "yushu"
T0 = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_natural_melatonin_curve_allows_falling_asleep_without_fixed_schedule(tmp_path):
    """不填 sleep_start/sleep_end 时，凌晨 2 点昼夜基线褪黑素 >= 80 可自然入睡，白天 14 点保持清醒。"""
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        profile = types.SimpleNamespace(body=BodySettings(enabled=True, sleep_start=None, sleep_end=None))
        night_svc = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: T0, hour_of=lambda _t: 2.0)
        day_svc = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: T0, hour_of=lambda _t: 14.0)
        assert bp.baseline("melatonin", 2.0) >= 80.0
        assert night_svc.is_asleep("bot_night") is True
        assert day_svc.is_asleep("bot_day") is False
    finally:
        cm.close()


def test_daytime_heavy_chat_drains_energy_into_resting_and_recovers_when_quiet(tmp_path):
    """白天连续高密开口耗尽精力后进入歇口气态（不睡死但压低主动意愿并给出边沿提示），安静休息后回血解除。"""
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        clock = _Clock()
        profile = types.SimpleNamespace(body=BodySettings(enabled=True))
        svc = BotBodyService(db, profile_for=lambda _b: profile, clock=clock, hour_of=lambda _t: 15.0)

        assert svc.reading(BOT).energy == pytest.approx(bp.ENERGY_DEFAULT, abs=0.5)
        assert svc.is_resting(BOT) is False

        # 连发 16 条较长回复，精力掉进 <= 28 的歇口气区间
        for _ in range(16):
            clock.now += 8.0
            svc.note_bot_reply(BOT, chars=120, now=clock.now)

        tired = svc.reading(BOT, now=clock.now)
        assert tired.energy <= bp.RESTING_ENERGY_ENTER
        assert tired.resting is True
        assert tired.asleep is False
        assert tired.proactive_drive <= 0.02
        lines = svc.render_lines(BOT, now=clock.now)
        assert any("歇口气" in ln for ln in lines)

        # 安静休息 50 分钟后，精力回升越过解除线 42，恢复正常在线
        clock.now += 50 * 60.0
        recovered = svc.reading(BOT, now=clock.now)
        assert recovered.energy >= bp.RESTING_ENERGY_EXIT
        assert recovered.resting is False
    finally:
        cm.close()


def test_late_night_reply_triggers_late_night_call_and_extra_drain(tmp_path):
    """深夜 23:30 开口说话消耗更多精力，并自动触发 late_night_call 推高褪黑素。"""
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        clock = _Clock()
        profile = types.SimpleNamespace(body=BodySettings(enabled=True))
        svc = BotBodyService(db, profile_for=lambda _b: profile, clock=clock, hour_of=lambda _t: 23.5)

        before_mel = svc.reading(BOT, now=clock.now).hormones["melatonin"]
        res = svc.note_bot_reply(BOT, chars=90, scene="qq:group:g1", now=clock.now + 5.0)
        assert res["spent"] > 4.0
        assert svc.stats.get("event:late_night_call") == 1
        after_mel = svc.reading(BOT, now=clock.now + 5.0).hormones["melatonin"]
        assert after_mel > before_mel
    finally:
        cm.close()


def test_wakable_mode_allows_first_call_wake_with_groggy_edge_and_woken_event(tmp_path):
    """默认 wakable 模式不设死第 1 声：浅睡或熟人第 1 次呼叫就有概率吵醒，醒来带迷糊边沿实感与 woken_up 事件。"""
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        clock = _Clock()
        body_cfg = BodySettings(enabled=True, sleep_start=0, sleep_end=7, sleep_behavior="wakable", sleep_jitter_minutes=0)
        profile = types.SimpleNamespace(body=body_cfg)
        svc = BotBodyService(
            db,
            profile_for=lambda _b: profile,
            closeness_for=lambda _b, _p: 85.0,
            clock=clock,
            hour_of=lambda _t: 0.2,  # 刚入睡浅睡期
        )
        assert svc.is_asleep(BOT) is True
        # 第 1 声呼叫，roll=0.25 即可直接吵醒（旧 strict 模式第 1 声恒为 False）
        assert svc.should_wake(BOT, "qq:user:friend", roll=0.25, now=clock.now) is True
        assert svc.is_asleep(BOT, now=clock.now + 10.0) is False
        snap = svc.snapshot(BOT, now=clock.now + 10.0)
        assert snap["woken_drowsy"] is True
        assert any(e["kind"] == "woken_up" for e in snap["events"])
        lines = svc.render_lines(BOT, principal_id="qq:user:friend", now=clock.now + 10.0)
        assert any("刚在睡梦中被叫醒" in ln for ln in lines)
    finally:
        cm.close()


def test_drowsy_only_and_active_chat_grace_never_hard_block_replies(tmp_path):
    """drowsy_only 模式始终放行；wakable 模式下若刚在缓冲期内回过话（热聊余温），跨入睡点也不断电。"""
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        clock = _Clock()

        # 1) drowsy_only：即便 roll=0.999 每次都放行
        drowsy_cfg = BodySettings(enabled=True, sleep_start=0, sleep_end=7, sleep_behavior="drowsy_only", sleep_jitter_minutes=0)
        svc_drowsy = BotBodyService(
            db, profile_for=lambda _b: types.SimpleNamespace(body=drowsy_cfg), clock=clock, hour_of=lambda _t: 3.0,
        )
        assert svc_drowsy.should_wake("bot_drowsy", "qq:user:u1", roll=0.999, now=clock.now) is True

        # 2) 热聊余温缓冲：刚在 60 秒前发过回复，跨入 0:05 入睡窗口时不秒睡断电，并给出收尾晚安边沿提示
        grace_cfg = BodySettings(
            enabled=True, sleep_start=0, sleep_end=7, sleep_behavior="wakable",
            sleep_jitter_minutes=0, active_chat_grace_seconds=180.0,
        )
        hour_box = {"h": 23.98}
        svc_grace = BotBodyService(
            db, profile_for=lambda _b: types.SimpleNamespace(body=grace_cfg), clock=clock, hour_of=lambda _t: hour_box["h"],
        )
        svc_grace.note_bot_reply(BOT, chars=30, now=clock.now)
        clock.now += 60.0
        hour_box["h"] = 0.08  # 已跨过 0 点进入作息区间
        assert svc_grace.is_asleep(BOT, now=clock.now) is False
        assert svc_grace.should_wake(BOT, "qq:user:u1", roll=0.999, now=clock.now) is True
        lines = svc_grace.render_lines(BOT, now=clock.now)
        assert any("晚安去睡" in ln for ln in lines)

        # 安静超过 180s 后正式睡着
        clock.now += 240.0
        assert svc_grace.is_asleep(BOT, now=clock.now) is True
    finally:
        cm.close()


def test_service_quiet_remains_independent_and_timezone_uses_shanghai(tmp_path):
    """service_quiet 独立可开且不受身体睡眠模式影响；默认时区固定按 Asia/Shanghai 墙上钟点换算；心情深夜窗自动跟随作息。"""
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        # UTC 2026-05-01 18:30 == 北京时间 02:30
        utc_ts = datetime(2026, 5, 1, 18, 30, tzinfo=timezone.utc).timestamp()
        body_cfg = BodySettings(
            enabled=True,
            sleep_start=23,
            sleep_end=7,
            sleep_behavior="drowsy_only",
            service_quiet_start=0,
            service_quiet_end=6,
        )
        profile = types.SimpleNamespace(body=body_cfg, mood=MoodSettings())
        svc = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: utc_ts, timezone="Asia/Shanghai")
        assert svc.hour_of(utc_ts) == pytest.approx(2.5, abs=0.01)
        assert svc.in_service_quiet(BOT, now=utc_ts) is True

        mood_svc = BotMoodService(db, profile_for=lambda _b: profile, clock=lambda: utc_ts)
        aligned = mood_svc.settings_for(BOT)
        assert (aligned.night_start_hour, aligned.night_end_hour) == (23, 7)
    finally:
        cm.close()
