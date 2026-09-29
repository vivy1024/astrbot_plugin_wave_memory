"""心情收尾回归：仅使用 tmp_path 下的 SQLite，不调用模型或 embedding。"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from domain.bot_profile import BotProfile
from domain.mood_policy import (
    MOOD_RESIDUAL_EPSILON,
    MoodEventView,
    MoodSettings,
    compose_mood,
    mood_event_horizon,
)
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.bot_mood_repo import MoodReadBudgetExceeded
from services import bot_mood
from services.bot_mood import BotMoodService
from services.relationship_narrative import shared_experience_lines

NOW = 1_800_000_000.0
DAY = 86400.0
BOT = "bot-a"
SUBJECT = "qq:user:u1"


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "mood-review.sqlite"), dimension=3)
    try:
        yield database
    finally:
        database.close()


def _scope(kind="group", conversation="g1", *, bot=BOT, subject=SUBJECT):
    return RuntimeScope(
        bot, kind, SessionRef(f"qq:{kind}:{conversation}", "qq", kind, conversation),
        subject_principal_id=subject,
    )


def _service(db, settings=None):
    profile = SimpleNamespace(mood=settings or MoodSettings(fatigue_enabled=False))
    return BotMoodService(db, profile_for=lambda _: profile, clock=lambda: NOW, hour_of=lambda _: 12)


def _add(db, **overrides):
    values = dict(source="directed_message", valence=0.2, arousal=0.1, intensity=0.5,
                  subject_principal_id=SUBJECT, session_id="qq:group:g1", created_at=NOW)
    values.update(overrides)
    return db.bot_mood.add_event(BOT, **values)


def _seed_events(db, count, *, created_at=NOW, valence=0.0001):
    with db.bot_mood.cm.write_transaction() as tx:
        tx.executemany(
            """INSERT INTO bot_mood_events(bot_id, source, valence, arousal, intensity, created_at)
               VALUES (?, 'regression', ?, 0, 0, ?)""",
            [(BOT, valence, created_at)] * count,
        )


def _relationship(db, *, scope=None, dimension="trust", memory_id=42, operation_id=None,
                  event_type="bot_praised", episode_id=None):
    db.soul_repository.record_relationship_event(
        scope or _scope(), event_type=event_type, dimension=dimension, delta=2,
        reason="一起核对版本", source_memory_id=memory_id, source_episode_id=episode_id,
        created_at=NOW - 60,
    )
    row_id = db.bot_mood.max_relationship_event_id((scope or _scope()).bot_id)
    if operation_id is not None:
        with db.bot_mood.cm.write_transaction() as tx:
            tx.execute("UPDATE scoped_soul_relationship_events SET operation_id=? WHERE id=?",
                       (operation_id, row_id))
    return row_id


@pytest.mark.parametrize("origin", ["memory", "operation"])
def test_relationship_dimensions_share_one_source_across_syncs_and_restart(db, origin):
    kwargs = {"memory_id": 42} if origin == "memory" else {"memory_id": None, "operation_id": "op-42"}
    _relationship(db, **kwargs)
    _relationship(db, dimension="fun", **kwargs)
    service = _service(db)
    assert service.sync_relationship_events(BOT, force=True) == 1
    before = service.current(BOT)
    last_id = _relationship(db, dimension="depth", **kwargs)
    restarted = _service(db)
    assert restarted.sync_relationship_events(BOT, force=True) == 0
    after = restarted.current(BOT)
    assert after["valence"] == before["valence"]
    assert after["active_events"] == 1
    assert db.bot_mood.get_state(BOT)["relationship_cursor"] == last_id


@pytest.mark.parametrize("origin", ["memory", "operation"])
def test_legacy_relationship_row_reference_blocks_new_dimension_import(db, origin):
    kwargs = {"memory_id": 42} if origin == "memory" else {"memory_id": None, "operation_id": "legacy-op"}
    first_id = _relationship(db, **kwargs)
    _relationship(db, dimension="fun", **kwargs)
    legacy_id = _add(db, source="relationship_event", source_ref=f"relationship_event:{first_id}")
    assert _service(db).sync_relationship_events(BOT, force=True) == 0
    assert [event.id for event in db.bot_mood.list_events(BOT, since=0)] == [legacy_id]


def test_relationship_source_key_keeps_scope_subject_type_and_bot_boundaries(db):
    for scope in (_scope(), _scope(conversation="g2"), _scope("private", "u1"),
                  _scope(subject="qq:user:u2"), _scope(bot="bot-b")):
        _relationship(db, scope=scope)
    _relationship(db, event_type="deep_talk", dimension="depth")
    service = _service(db)
    assert service.sync_relationship_events(BOT, force=True) == 5
    assert service.sync_relationship_events("bot-b", force=True) == 1
    assert service.sync_relationship_events(BOT, force=True) == 0


def test_episode_alone_does_not_deduplicate_distinct_relationship_interactions(db):
    _relationship(db, memory_id=None, episode_id=9)
    _relationship(db, memory_id=None, episode_id=9)
    assert _service(db).sync_relationship_events(BOT, force=True) == 2


def test_increasing_messages_replace_one_event_with_strongest_whole_impulse(db):
    service = _service(db)
    ids = [service.observe_message(_scope(), content=content, directed=True, now=NOW + offset)
           for content, offset in (("可爱", 0), ("可爱 聪明", 10), ("可爱 聪明 谢谢", 20))]
    assert ids[0] is not None and ids == [ids[0]] * 3
    assert service.observe_message(_scope(), content="可爱", directed=True, now=NOW + 30) is None
    events = db.bot_mood.list_events(BOT, since=0)
    assert len(events) == 1
    event = events[0]
    assert (event.valence, event.arousal, event.intensity, event.created_at) == (0.6, 0.18, 1.0, NOW + 20)
    assert "谢谢" in event.cause
    reading = service.current(BOT, now=NOW + 30)
    expected = compose_mood(events, service.settings_for(BOT), now=NOW + 30)
    assert (reading["valence"], reading["arousal"], reading["active_events"]) == (expected.valence, expected.arousal, 1)
    assert service.observe_message(_scope(), content="可爱", directed=True, now=NOW + 66) != ids[0]


@pytest.mark.parametrize("private_first", [True, False])
def test_same_window_merge_preserves_private_cause_and_public_cause_separately(db, private_first):
    service = _service(db)
    scopes = [_scope("private", "u1"), _scope()]
    if not private_first:
        scopes.reverse()
    for offset, scope in enumerate(scopes):
        private = scope.visibility == "private"
        service.observe_message(scope, content="可爱", sender_name="私密昵称" if private else "群昵称",
                                directed=True, now=NOW + offset)
        service.observe_message(scope, content="可爱 聪明 谢谢 私密原话" if private else "可爱 聪明",
                                sender_name="私密昵称" if private else "群昵称", directed=True, now=NOW + offset + 5)
    assert len(db.bot_mood.list_events(BOT, since=0)) == 2
    public = service.current(BOT, viewer_scope=_scope(), now=NOW + 10)
    text = json.dumps(public, ensure_ascii=False)
    assert "私密昵称" not in text and "私密原话" not in text and "qq:private:u1" not in text
    assert "群昵称" in text
    assert sum(cause["redacted"] for cause in public["causes"]) == 1
    private = service.current(BOT, viewer_scope=_scope("private", "u1"), now=NOW + 10)
    assert any("私密原话" in cause["text"] for cause in private["causes"])


def test_source_reference_is_idempotent_even_when_merge_is_requested(db):
    keyed_id = _add(db, source_ref="message:1", cause="first", merge_seconds=45)
    assert _add(db, source_ref="message:1", valence=0.6, cause="replay", merge_seconds=45) is None
    events = db.bot_mood.list_events(BOT, since=0)
    assert [(event.id, event.valence, event.cause) for event in events] == [(keyed_id, 0.2, "first")]


def test_unkeyed_merge_does_not_erase_existing_source_reference(db):
    keyed_id = _add(db, source_ref="message:1", merge_seconds=45)
    merged_id = _add(db, valence=0.4, merge_seconds=45, created_at=NOW + 1)
    assert merged_id != keyed_id
    assert _add(db, valence=0.6, merge_seconds=45, created_at=NOW + 2) == merged_id
    assert _add(db, source_ref="message:1", created_at=NOW + 100, merge_seconds=45) is None
    assert len(db.bot_mood.list_events(BOT, since=0)) == 2


def test_paged_mood_keeps_events_beyond_400_and_seven_days(db):
    settings = MoodSettings(fatigue_enabled=False, half_life_minutes=7 * 24 * 60, strong_event_boost=0)
    old_id = _add(db, valence=-0.5, intensity=1.0, created_at=NOW - 8 * DAY)
    _seed_events(db, 450)
    _add(db, valence=1.0, created_at=NOW + 1)  # 未来事件不得提前影响心情。
    service = _service(db, settings)
    actual = service.current(BOT, max_causes=100)
    events = list(db.bot_mood.iter_events(BOT, since=NOW - mood_event_horizon(settings), until=NOW))
    expected = compose_mood(events, settings, now=NOW)
    assert len(events) == actual["active_events"] == 451
    assert old_id in {event.id for event in events}
    assert actual["valence"] == expected.valence < settings.baseline_valence
    assert actual["arousal"] == expected.arousal
    assert len(actual["causes"]) <= bot_mood.MAX_CAUSES


def test_keyset_paging_handles_equal_timestamps_and_excludes_later_inserts(db):
    _seed_events(db, 7)
    expected = [event.id for event in db.bot_mood.list_events(BOT, since=0)]
    events = db.bot_mood.iter_events(BOT, since=NOW, until=NOW, page_size=2, max_pages=4)
    first = next(events)
    new_id = _add(db)  # 已开始的分页读取固定 ID 上界。
    ids = [first.id, *(event.id for event in events)]
    assert ids == sorted(expected, reverse=True) and new_id not in ids


def test_read_budget_detects_one_extra_row_and_accepts_exact_budget(db):
    _seed_events(db, 4)
    assert len(list(db.bot_mood.iter_events(BOT, since=0, until=NOW, page_size=2, max_pages=2))) == 4
    _add(db)
    with pytest.raises(MoodReadBudgetExceeded, match="read_budget"):
        list(db.bot_mood.iter_events(BOT, since=0, until=NOW, page_size=2, max_pages=2))


def test_repository_page_budget_cannot_be_expanded_past_hard_limit(db):
    _seed_events(db, 129)
    seen = []
    with pytest.raises(MoodReadBudgetExceeded):
        for event in db.bot_mood.iter_events(BOT, since=0, until=NOW, page_size=1, max_pages=10 ** 6):
            seen.append(event.id)
    assert len(seen) == len(set(seen)) == 128


def test_service_caps_causes_but_composes_every_event(db):
    _seed_events(db, 30, valence=0.1)
    mood = _service(db).current(BOT, max_causes=10 ** 6)
    assert mood["active_events"] == 30
    assert len(mood["causes"]) == bot_mood.MAX_CAUSES == 20


@pytest.mark.parametrize("existing_snapshot", [False, True])
def test_exhausted_read_budget_never_persists_fabricated_snapshot(db, monkeypatch, existing_snapshot):
    service = _service(db)
    if existing_snapshot:
        _add(db, valence=-0.4)
        service.snapshot(BOT, relationship_cursor=7)
    before = db.bot_mood.get_state(BOT)
    _seed_events(db, 3)
    monkeypatch.setattr(bot_mood, "EVENT_PAGE_SIZE", 2)
    monkeypatch.setattr(bot_mood, "EVENT_MAX_PAGES", 1)
    assert service.current(BOT) is None
    assert service.render_lines(BOT, _scope()) == []
    service.snapshot(BOT, now=NOW + 10, relationship_cursor=99)
    assert db.bot_mood.get_state(BOT) == before, "不可用不能改写已有状态，也不能创建全零快照"
    assert service.stats["read_budget_exceeded"] == 3


def test_relationship_sync_retries_idempotently_after_snapshot_budget_recovers(db, monkeypatch):
    _seed_events(db, 2)
    _relationship(db)
    last_id = _relationship(db, dimension="fun")
    service = _service(db)
    with monkeypatch.context() as limited:
        limited.setattr(bot_mood, "EVENT_PAGE_SIZE", 2)
        limited.setattr(bot_mood, "EVENT_MAX_PAGES", 1)
        assert service.sync_relationship_events(BOT, force=True) == 1
        assert db.bot_mood.get_state(BOT) is None
        assert service.sync_relationship_events(BOT, force=True) == 0
        assert db.bot_mood.get_state(BOT) is None
    assert service.sync_relationship_events(BOT, force=True) == 0
    state = db.bot_mood.get_state(BOT)
    assert state["relationship_cursor"] == last_id and state["word"]
    assert len(db.bot_mood.list_events(BOT, since=0)) == 3


def test_long_half_life_cleanup_keeps_active_events_and_bounds_each_delete(db):
    settings = MoodSettings(fatigue_enabled=False, half_life_minutes=7 * 24 * 60, strong_event_boost=5)
    horizon = mood_event_horizon(settings)
    assert math.isfinite(horizon) and horizon < 840 * DAY
    active_id = _add(db, valence=-0.6, intensity=1.0, created_at=NOW - 20 * DAY)
    _seed_events(db, 1100, created_at=NOW - horizon - 1)
    service = _service(db, settings)
    before = service.current(BOT)
    service.tick([BOT])
    after = service.current(BOT)
    assert before["valence"] == after["valence"] < settings.baseline_valence
    assert after["active_events"] == 1
    assert service.stats["pruned"] == 512
    remaining = db.bot_mood.list_events(BOT, since=0, limit=2000)
    assert len(remaining) == 589 and active_id in {event.id for event in remaining}


def test_compose_bounds_causes_without_truncating_sum_or_equal_weight_events():
    settings = MoodSettings()
    events = [MoodEventView(i, "test", (-1 if i % 2 else 1) * 0.001, 0.001, 0.0, NOW)
              for i in range(600)]
    full = compose_mood(events, settings, now=NOW)
    limited = compose_mood(iter(events), settings, now=NOW, max_contributions=3)
    assert limited.active_events == 600
    assert (limited.valence, limited.arousal) == (full.valence, full.arousal)
    assert limited.contributions == full.contributions[:3]
    assert compose_mood(iter(events), settings, now=NOW, max_contributions=0).contributions == []


def test_residual_horizon_reaches_baseline_continuously():
    settings = MoodSettings(half_life_minutes=60, strong_event_boost=0)
    horizon = mood_event_horizon(settings)
    event = MoodEventView(1, "test", 1.0, 0.0, 1.0, NOW)
    before = compose_mood([event], settings, now=NOW + horizon - 1)
    after = compose_mood([event], settings, now=NOW + horizon + 1)
    assert 0 < before.contributions[0].valence < MOOD_RESIDUAL_EPSILON
    assert after.active_events == 0 and after.valence == settings.baseline_valence
    assert abs(before.valence - after.valence) <= 0.0001


def _seed_replies(db, stamps):
    with db.bot_mood.cm.write_transaction() as tx:
        tx.executemany(
            """INSERT INTO memories(group_id, sender_id, content, timestamp, bot_id)
               VALUES ('g1', 'bot', 'local test reply', ?, ?)""", [(stamp, BOT) for stamp in stamps],
        )


def test_fatigue_max_threshold_warms_latest_replies_without_timestamp_dedup(db):
    _seed_replies(db, [NOW - DAY - 1] * 10 + [NOW - 7200] * 100 + [NOW - 3600] * 20000 + [NOW + 1] * 10)
    settings = MoodSettings(fatigue_window_minutes=1440, fatigue_reply_threshold=10000, fatigue_strength=1.0)
    service = _service(db, settings)
    actual = service.current(BOT)
    assert actual["recent_replies"] == 20000
    assert actual["fatigue"] == 1.0
    assert len(service._replies[BOT]) == bot_mood.MAX_REPLY_RECORDS == 20000
    assert set(service._replies[BOT]) == {NOW - 3600}
    service.note_bot_reply(BOT, now=NOW - 7200)  # 满队列的旧回调不能挤掉较新回复。
    assert set(service._replies[BOT]) == {NOW - 3600}
    assert service.current(BOT, now=NOW + DAY)["recent_replies"] == 0


def test_fatigue_warmup_retains_maximum_window_for_later_profile_expansion(db):
    _seed_replies(db, [NOW - 7200, NOW - 60])
    settings = MoodSettings(fatigue_window_minutes=10, fatigue_reply_threshold=1, fatigue_strength=1.0)
    service = _service(db, settings)
    assert service.current(BOT)["recent_replies"] == 1
    settings.fatigue_window_minutes = 1440
    expanded = service.current(BOT)
    assert expanded["recent_replies"] == 2 and expanded["fatigue"] == 1.0


def test_fatigue_live_replies_survive_window_expansion_and_out_of_order_callbacks(db):
    settings = MoodSettings(fatigue_window_minutes=10, fatigue_reply_threshold=10000, fatigue_strength=1.0)
    service = _service(db, settings)
    for _ in range(10001):
        service.note_bot_reply(BOT, now=NOW - 3600)
    service.note_bot_reply(BOT, now=NOW - 7200)
    service.note_bot_reply(BOT, now=NOW)
    assert service.current(BOT)["recent_replies"] == 1
    settings.fatigue_window_minutes = 1440
    mood = service.current(BOT)
    assert mood["recent_replies"] == 10003 and mood["fatigue"] > 0
    assert list(service._replies[BOT]) == sorted(service._replies[BOT])


@pytest.mark.parametrize("invalid", [float("inf"), float("-inf"), float("nan"), "1e309", 10 ** 1000],
                         ids=["inf", "negative-inf", "nan", "overflow-string", "huge-int"])
def test_nonfinite_profile_numeric_fields_fall_back_without_overflow(invalid):
    keys = ("baseline_valence", "baseline_arousal", "sensitivity", "half_life_minutes", "strong_event_boost",
            "atmosphere_weight", "fatigue_window_minutes", "fatigue_reply_threshold", "night_start_hour",
            "night_end_hour", "fatigue_strength")
    profile = BotProfile.from_dict({"db_id": BOT, "name": "测试", "mood": dict.fromkeys(keys, invalid)})
    assert profile.mood == MoodSettings()
    assert BotProfile.from_dict(profile.to_dict()).mood == MoodSettings()


@pytest.mark.parametrize("invalid", [float("inf"), float("nan"), 10 ** 1000], ids=["inf", "nan", "huge-int"])
def test_nonfinite_default_half_life_does_not_make_unbounded_horizon(db, invalid):
    service = BotMoodService(db, default_half_life_minutes=invalid)
    assert service.default_half_life_minutes == 120.0
    assert math.isfinite(mood_event_horizon(service.settings_for(BOT), service.default_half_life_minutes))


@pytest.mark.parametrize("participants", [SUBJECT, SUBJECT + "0", [SUBJECT + "0"], {SUBJECT: True}, None])
def test_shared_experiences_reject_nickname_and_nonexact_or_malformed_participants(participants):
    item = {"event_summary": "小雨和小雨点都只是天气现象", "participant_principal_ids": participants}
    assert shared_experience_lines([item], subject_principal_id=SUBJECT, display_names=["小雨"]) == []


@pytest.mark.parametrize("evidence", [{"participant_principal_ids": [SUBJECT]},
                                    [{"participant_principal_ids": [SUBJECT]}]])
def test_shared_experiences_accept_structured_evidence_without_display_name(evidence):
    item = {"event_summary": "一起核对了版本信息", "evidence": evidence}
    assert shared_experience_lines([item], subject_principal_id=SUBJECT) == ["一起核对了版本信息"]
    assert shared_experience_lines([item], subject_principal_id="") == []
