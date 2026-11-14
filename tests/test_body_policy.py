"""激素身体的纯策略：昼夜基线、时间推进（扣离线）、习惯化、亲密缩放、边沿注入、睡眠唤醒。"""

from __future__ import annotations

import math

import pytest

from domain import body_policy as bp

DAY = 86400.0
HOUR = 3600.0


def _clock(ts: float) -> float:
    """测试用：把时间戳直接当作「当天小时」，由各用例自行构造。"""
    return (ts / HOUR) % 24.0


def test_waves_peak_at_their_hour():
    assert bp.wave(14, 14) == pytest.approx(1.0)
    assert bp.wave(2, 14) == pytest.approx(0.0, abs=1e-9)
    assert bp.baseline("cortisol", 8) > bp.baseline("cortisol", 20)
    assert bp.baseline("melatonin", 2) > bp.baseline("melatonin", 14)


def test_oxytocin_baseline_rises_with_closeness():
    assert bp.baseline("oxytocin", 14, closeness=90) > bp.baseline("oxytocin", 14, closeness=20)


def test_state_relaxes_toward_the_circadian_baseline():
    start = {name: 100.0 for name in bp.HORMONES}
    later = bp.advance(start, elapsed_seconds=24 * HOUR, hour_at=_clock, start_ts=0.0)
    # 一整天后 100 的多巴胺应该回落到基线附近，而不是停在 100
    assert later["dopamine"] < 70.0
    assert later["cortisol"] < 60.0


def test_elapsed_time_is_capped_at_48_hours():
    start = {name: 100.0 for name in bp.HORMONES}
    capped = bp.advance(start, elapsed_seconds=48 * HOUR, hour_at=_clock, start_ts=0.0)
    far = bp.advance(start, elapsed_seconds=400 * HOUR, hour_at=_clock, start_ts=0.0)
    assert capped == far


def test_zero_elapsed_changes_nothing_but_clamps():
    state = {name: 50.0 for name in bp.HORMONES}
    assert bp.advance(state, elapsed_seconds=0.0, hour_at=_clock, start_ts=0.0) == state


def test_offline_time_is_the_callers_to_subtract():
    """停机 7 小时再醒来：调用方只推进「实际在线」的那一小段，身体几乎不变。"""
    state = {name: 80.0 for name in bp.HORMONES}
    online_only = bp.advance(state, elapsed_seconds=10 * 60.0, hour_at=_clock, start_ts=0.0)
    naive = bp.advance(state, elapsed_seconds=7 * HOUR, hour_at=_clock, start_ts=0.0)
    assert abs(online_only["dopamine"] - 80.0) < abs(naive["dopamine"] - 80.0)


def test_boredom_waits_for_grace_then_climbs():
    start = bp.default_state(14)
    start["boredom"] = 10.0
    short = bp.advance(start, elapsed_seconds=10 * 60.0, hour_at=_clock, start_ts=14 * HOUR, quiet_seconds=0.0)
    long = bp.advance(start, elapsed_seconds=12 * HOUR, hour_at=_clock, start_ts=14 * HOUR, quiet_seconds=0.0)
    assert short["boredom"] < 30.0, "宽限期内不累积"
    assert long["boredom"] > 60.0, "安静很久之后明显无聊（半衰期 5 小时，是慢变量）"


def test_cortisol_decays_faster_when_dopamine_is_high():
    base = {**bp.default_state(14), "cortisol": 80.0, "dopamine": 10.0}
    happy = {**base, "dopamine": 90.0}
    flat = bp.advance(base, elapsed_seconds=2 * HOUR, hour_at=lambda _t: 14.0, start_ts=0.0)
    cheerful = bp.advance(happy, elapsed_seconds=2 * HOUR, hour_at=lambda _t: 14.0, start_ts=0.0)
    assert cheerful["cortisol"] < flat["cortisol"]


def test_high_cortisol_drags_serotonin_down():
    start = {**bp.default_state(14), "cortisol": 95.0, "serotonin": 60.0}
    out = bp.advance(start, elapsed_seconds=2 * HOUR, hour_at=lambda _t: 14.0, start_ts=0.0)
    calm = bp.advance({**start, "cortisol": 20.0}, elapsed_seconds=2 * HOUR, hour_at=lambda _t: 14.0, start_ts=0.0)
    assert out["serotonin"] < calm["serotonin"]


def test_habituation_is_geometric():
    assert bp.habituation_factor(0) == 1.0
    assert bp.habituation_factor(2) == pytest.approx(0.64)
    state = bp.default_state(14)
    first, d1 = bp.apply_event(state, "praised", closeness=50, prior_count=0)
    again, d2 = bp.apply_event(state, "praised", closeness=50, prior_count=3)
    assert d2["dopamine"] < d1["dopamine"]


def test_unhabituated_events_ignore_prior_count():
    state = bp.default_state(14)
    _, d0 = bp.apply_event(state, "addressed", prior_count=0)
    _, d9 = bp.apply_event(state, "addressed", prior_count=9)
    assert d0 == d9


def test_strangers_praise_counts_less_and_close_ones_hurt_more():
    state = bp.default_state(14)
    _, stranger = bp.apply_event(state, "praised", closeness=0)
    _, friend = bp.apply_event(state, "praised", closeness=100)
    assert stranger["dopamine"] < friend["dopamine"]
    _, cold = bp.apply_event(state, "scolded", closeness=0)
    _, close = bp.apply_event(state, "scolded", closeness=100)
    assert close["cortisol"] > cold["cortisol"]


def test_high_serotonin_blunts_stress_gain():
    calm = {**bp.default_state(14), "serotonin": 85.0}
    low = {**bp.default_state(14), "serotonin": 30.0}
    _, a = bp.apply_event(calm, "scolded", closeness=50)
    _, b = bp.apply_event(low, "scolded", closeness=50)
    assert a["cortisol"] < b["cortisol"]


def test_single_event_never_pins_a_hormone_to_the_ceiling():
    """社区的教训：一两轮就顶到上限叫「吵」。单次事件的增量必须是小量。"""
    state = bp.default_state(14)
    for kind in bp.EVENT_TABLE:
        _, applied = bp.apply_event(state, kind, closeness=100)
        # boredom 是被打断时的「清零」，不是情绪冲击；其余激素单次增量必须是小量
        shocks = {name: v for name, v in applied.items() if name != "boredom"}
        assert all(abs(v) <= 15.0 for v in shocks.values()), kind


def test_relationship_events_map_to_body_events():
    assert bp.RELATIONSHIP_EVENT_MAP["bot_attacked"] == "scolded"
    assert all(target in bp.EVENT_TABLE for target in bp.RELATIONSHIP_EVENT_MAP.values())


def test_mood_bias_is_bounded():
    wild = {**{name: 100.0 for name in bp.HORMONES}, "cortisol": 0.0}
    reading = bp.read(wild, hour=14)
    assert -0.2 <= reading.mood_bias <= 0.2 and -0.2 <= reading.arousal_bias <= 0.2
    awful = {**{name: 0.0 for name in bp.HORMONES}, "cortisol": 100.0}
    assert bp.read(awful, hour=14).mood_bias < 0


def test_sleep_window_and_drowsiness_mark_asleep():
    awake = bp.read(bp.default_state(14), hour=14, sleep_start=23, sleep_end=6)
    night = bp.read(bp.default_state(1), hour=1, sleep_start=23, sleep_end=6)
    drowsy = bp.read({**bp.default_state(14), "melatonin": 90.0}, hour=14)
    assert awake.asleep is False and night.asleep is True and drowsy.asleep is True


def test_sleep_window_wraps_midnight_and_handles_none():
    assert bp._in_sleep_window(23.5, 23, 6) and bp._in_sleep_window(5.9, 23, 6)
    assert not bp._in_sleep_window(6.0, 23, 6) and not bp._in_sleep_window(12, 23, 6)
    assert not bp._in_sleep_window(3, None, None)


def test_edge_notes_announce_once_then_stay_quiet():
    flags = {"stressed": True}
    lines, memo = bp.edge_notes(flags, {}, now=1000.0)
    assert len(lines) == 1 and "绷" in lines[0]
    again, memo = bp.edge_notes(flags, memo, now=1000.0 + 60.0)
    assert again == [], "状态持续时不重复"
    later, memo = bp.edge_notes(flags, memo, now=1000.0 + 41 * 60.0)
    assert len(later) == 1, "40 分钟后才重提"


def test_edge_notes_reset_when_state_clears():
    _, memo = bp.edge_notes({"stressed": True}, {}, now=0.0)
    _, memo = bp.edge_notes({"stressed": False}, memo, now=60.0)
    assert memo == {}
    lines, _ = bp.edge_notes({"stressed": True}, memo, now=120.0)
    assert len(lines) == 1, "变回假之后再越过，可以重新说"


def test_edge_notes_respect_the_80_char_budget():
    flags = {key: True for key in ("stressed", "wired", "low", "affectionate", "lonely", "bored", "drowsy")}
    lines, memo = bp.edge_notes(flags, {}, now=0.0)
    assert sum(len(line) for line in lines) <= bp.EDGE_MAX_CHARS
    # 没说出口的不记为已播报
    assert len(memo) == len(lines)


def test_wake_probability_ladder():
    assert bp.wake_probability(1, sleepiness=0.5) == 0.0
    assert bp.wake_probability(6, sleepiness=0.99) == 1.0
    assert 0.0 < bp.wake_probability(3, sleepiness=0.5) < 1.0
    assert bp.wake_probability(3, sleepiness=0.9) < bp.wake_probability(3, sleepiness=0.5)
    ladder = [bp.wake_probability(n, sleepiness=0.5) for n in range(1, 7)]
    assert ladder == sorted(ladder)


def test_no_nan_or_inf_anywhere():
    state = bp.default_state(3)
    out = bp.advance(state, elapsed_seconds=30 * HOUR, hour_at=_clock, start_ts=3 * HOUR)
    assert all(math.isfinite(v) and 0.0 <= v <= 100.0 for v in out.values())
