"""主动开口无人接话的指数退避：每次主动只记一次冷落，真人说话即清零，上限 24 小时。"""

from __future__ import annotations

from services.reply_decision import (
    UNANSWERED_BACKOFF_CAP_SECONDS,
    UNANSWERED_WINDOW_SECONDS,
    ReplyDecisionService,
)

BOT, SESSION, VIS = "yushu", "qq:group:1", "group"


class _Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _backoff(service: ReplyDecisionService, gap: float = 300.0) -> float:
    return service.proactive_backoff_seconds(bot_id=BOT, session_id=SESSION, visibility=VIS, base_gap_seconds=gap)


def _speak_and_wait(service: ReplyDecisionService, clock: _Clock, wait: float) -> None:
    service.note_reply(BOT, SESSION, VIS, proactive=True)
    clock.now += wait


def test_no_backoff_before_any_unanswered_proactive():
    clock = _Clock()
    service = ReplyDecisionService(clock=clock)
    assert _backoff(service) == 0.0
    service.note_reply(BOT, SESSION, VIS, proactive=True)
    clock.now += 60
    # 还在等待窗口内，不算冷落
    assert _backoff(service) == 0.0


def test_backoff_doubles_per_unanswered_proactive():
    clock = _Clock()
    service = ReplyDecisionService(clock=clock)
    for _ in range(3):
        _speak_and_wait(service, clock, UNANSWERED_WINDOW_SECONDS + 1)
    # 第三次之后再结算一次：连续 3 次无人接话 → 间隔 300 * 2^3
    remaining = _backoff(service)
    expected_gap = 300.0 * 8
    assert abs(remaining - (expected_gap - (UNANSWERED_WINDOW_SECONDS + 1))) < 1e-6


def test_single_proactive_counts_once_even_if_silence_lasts():
    clock = _Clock()
    service = ReplyDecisionService(clock=clock)
    _speak_and_wait(service, clock, UNANSWERED_WINDOW_SECONDS + 1)
    first = _backoff(service, gap=3600.0)
    clock.now += 3 * UNANSWERED_WINDOW_SECONDS
    # 沉默更久也只记 1 次（不会因为反复查询而叠加）
    state = service._state((BOT, VIS, SESSION))
    _backoff(service, gap=3600.0)
    assert state.unanswered_proactive == 1
    assert first > 0


def test_human_message_resets_backoff():
    clock = _Clock()
    service = ReplyDecisionService(clock=clock)
    for _ in range(2):
        _speak_and_wait(service, clock, UNANSWERED_WINDOW_SECONDS + 1)
    assert _backoff(service, gap=3600.0) > 0
    service.note_human_activity(SESSION)
    assert _backoff(service, gap=3600.0) == 0.0


def test_reply_within_window_is_not_a_snub():
    clock = _Clock()
    service = ReplyDecisionService(clock=clock)
    service.note_reply(BOT, SESSION, VIS, proactive=True)
    clock.now += 120
    service.note_human_activity(SESSION)
    clock.now += UNANSWERED_WINDOW_SECONDS * 2
    assert _backoff(service) == 0.0
    assert service._state((BOT, VIS, SESSION)).unanswered_proactive == 0


def test_backoff_is_capped_at_24_hours():
    clock = _Clock()
    service = ReplyDecisionService(clock=clock)
    for _ in range(12):
        _speak_and_wait(service, clock, UNANSWERED_WINDOW_SECONDS + 1)
    remaining = _backoff(service, gap=3600.0)
    assert remaining <= UNANSWERED_BACKOFF_CAP_SECONDS
