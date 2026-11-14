"""身体服务：懒推进、扣离线、事件幂等与习惯化、边沿注入、睡眠唤醒。"""

from __future__ import annotations

import sys
import types

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
from engine.db.bot_body_repo import BotBodyRepository
from engine.db.connection import ConnectionManager
from services.bot_body import REUNION_GAP_SECONDS, BotBodyService

BOT = "yushu"
HOUR = 3600.0
T0 = 1_800_000_000.0  # 固定起点；hour_of 用固定 14 点，避免昼夜基线干扰断言


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def parts(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
    clock = _Clock()
    service = BotBodyService(db, clock=clock, hour_of=lambda _ts: 14.0)
    try:
        yield service, clock, db
    finally:
        cm.close()


def test_first_reading_starts_at_the_baseline(parts):
    service, _clock, _db = parts
    reading = service.reading(BOT)
    assert reading is not None
    assert abs(reading.hormones["dopamine"] - bp.baseline("dopamine", 14)) < 0.01
    assert reading.asleep is False


def test_events_change_the_body_and_persist(parts):
    service, clock, db = parts
    before = service.reading(BOT).hormones
    applied = service.note_event(BOT, "praised", principal_id="qq:user:u1", now=clock.now)
    assert applied and applied["dopamine"] > 0
    after = service.reading(BOT).hormones
    assert after["dopamine"] > before["dopamine"]
    assert db.bot_body.load_state(BOT)["hormones"]["dopamine"] == pytest.approx(after["dopamine"], abs=0.01)


def test_same_relationship_event_is_idempotent(parts):
    service, clock, _db = parts
    first = service.note_relationship_event(BOT, "bot_attacked", principal_id="qq:user:u1",
                                            relationship_event_id=42, now=clock.now)
    second = service.note_relationship_event(BOT, "bot_attacked", principal_id="qq:user:u1",
                                             relationship_event_id=42, now=clock.now)
    assert first and second is None
    assert service.stats["duplicate"] == 1


def test_repeated_praise_from_one_person_habituates(parts):
    service, clock, _db = parts
    gains = []
    for i in range(4):
        applied = service.note_event(BOT, "praised", principal_id="qq:user:u1", now=clock.now + i)
        gains.append(applied["oxytocin"])
    assert gains[0] > gains[1] > gains[2] > gains[3]
    other = service.note_event(BOT, "praised", principal_id="qq:user:u2", now=clock.now + 10)
    assert other["oxytocin"] > gains[3], "换一个人，习惯化不跟着走"


def test_leap_doubles_the_impact(parts):
    service, clock, _db = parts
    normal = service.note_relationship_event(BOT, "bot_attacked", principal_id="qq:user:u1",
                                             relationship_event_id=1, now=clock.now)
    leap = service.note_relationship_event(BOT, "bot_attacked", principal_id="qq:user:u2",
                                           relationship_event_id=2, is_leap=True, now=clock.now)
    assert leap["cortisol"] > normal["cortisol"]


def test_unmapped_relationship_events_are_ignored(parts):
    service, clock, _db = parts
    assert service.note_relationship_event(BOT, "direct_reply", principal_id="qq:user:u1", relationship_event_id=3,
                                           now=clock.now) is None


def test_time_passes_while_online_but_not_while_offline(parts):
    service, clock, db = parts
    service.note_event(BOT, "scolded", principal_id="qq:user:u1", now=clock.now)
    stressed = service.reading(BOT, now=clock.now).hormones["cortisol"]
    service.mark_offline(now=clock.now)
    clock.now += 40 * HOUR  # 停机 40 小时
    service.reading(BOT, now=clock.now)
    after_restart = db.bot_body.load_state(BOT)["hormones"]["cortisol"]
    # 离线这段不算：压力应基本保持，而不是白白消退了 40 小时
    assert after_restart > stressed - 3.0


def test_time_passes_normally_when_there_was_no_shutdown(parts):
    service, clock, _db = parts
    service.note_event(BOT, "scolded", principal_id="qq:user:u1", now=clock.now)
    stressed = service.reading(BOT, now=clock.now).hormones["cortisol"]
    clock.now += 6 * HOUR
    assert service.reading(BOT, now=clock.now).hormones["cortisol"] < stressed


def test_reunion_after_a_long_gap(parts):
    service, clock, _db = parts
    service.note_human_message(BOT, "qq:user:u1", now=clock.now)
    assert "event:reunion" not in service.stats
    clock.now += REUNION_GAP_SECONDS + 60
    service.note_human_message(BOT, "qq:user:u1", directed=True, now=clock.now)
    assert service.stats.get("event:reunion") == 1 and service.stats.get("event:addressed") == 1


def test_human_message_in_any_scene_is_one_person(parts):
    service, clock, db = parts
    service.note_human_message(BOT, "qq:user:u1", scene="g1", now=clock.now)
    assert db.bot_body.last_seen(BOT, "qq:user:u1") == clock.now
    clock.now += 60
    service.note_human_message(BOT, "qq:user:u1", scene="private:u1", now=clock.now)
    assert db.bot_body.last_seen(BOT, "qq:user:u1") == clock.now


def test_edge_line_is_said_once_and_then_quiet(parts):
    service, clock, _db = parts
    for i in range(3):
        service.note_event(BOT, "boundary", principal_id=f"qq:user:u{i}", now=clock.now + i)
    first = service.render_lines(BOT, now=clock.now + 5)
    assert first and sum(len(x) for x in first) <= bp.EDGE_MAX_CHARS
    assert service.render_lines(BOT, now=clock.now + 60) == []


def test_asleep_inside_the_configured_window(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        body = types.SimpleNamespace(sleep_start=23, sleep_end=6)
        profile = types.SimpleNamespace(body=body)
        night = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: T0, hour_of=lambda _t: 2.0)
        day = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: T0, hour_of=lambda _t: 14.0)
        assert night.is_asleep(BOT) is True
        assert day.is_asleep(BOT) is False
    finally:
        cm.close()


def test_wake_ladder_and_staying_awake(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    try:
        db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
        profile = types.SimpleNamespace(body=types.SimpleNamespace(sleep_start=23, sleep_end=6))
        clock = _Clock()
        service = BotBodyService(db, profile_for=lambda _b: profile, clock=clock, hour_of=lambda _t: 2.0)
        assert service.is_asleep(BOT)
        results = []
        for i in range(6):
            clock.now = T0 + i * 20
            results.append(service.should_wake(BOT, "qq:user:u1", roll=0.9, now=clock.now))
        assert results[0] is False, "第一次叫不会醒"
        assert results[-1] is True, "第 6 次必醒"
        # 醒了之后 10 分钟内保持清醒
        assert service.is_asleep(BOT, now=clock.now + 60) is False
    finally:
        cm.close()


def test_snapshot_exposes_numbers_for_the_admin_only(parts):
    service, clock, _db = parts
    service.note_event(BOT, "praised", principal_id="qq:user:u1", now=clock.now)
    snap = service.snapshot(BOT, now=clock.now)
    assert snap["enabled"] and set(snap["hormones"]) == set(bp.HORMONES)
    assert snap["events"][0]["kind"] == "praised"
    assert "principal_id" not in snap["events"][0], "管理台曲线不带人的身份"
