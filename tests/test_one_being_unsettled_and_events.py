"""一个生命对一个人只有一份未结算观感；私聊也会记账；负向变化有真实的事件类型。"""

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

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.person_timeline_repo import PersonTimelineRepo
from services.impression_timeline import (
    clear_unsettled_state,
    effective_unsettled_state,
    event_type_for_shift,
    persist_unsettled_trace,
)

BOT, USER = "bot-a", "u1"


def _db(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    repo = PersonTimelineRepo(cm)
    return cm, type("DB", (), {"person_timeline": repo})()


def test_impressions_from_every_scene_fill_one_pool(tmp_path):
    cm, db = _db(tmp_path)
    try:
        persist_unsettled_trace(db, bot_id=BOT, user_id=USER, group_id="g1", text="群一里很靠谱", impact=4)
        persist_unsettled_trace(db, bot_id=BOT, user_id=USER, group_id="g2", text="群二里也帮人", impact=3)
        persist_unsettled_trace(db, bot_id=BOT, user_id=USER, group_id="private:u1", text="私下聊了很多", impact=4)
        # 无论在哪个场合读，能量都是 11
        for scene in ("g1", "g2", "private:u1", "g9"):
            assert effective_unsettled_state(db, bot_id=BOT, user_id=USER, scene=scene, now=0)["energy"] == 11.0
        traces = effective_unsettled_state(db, bot_id=BOT, user_id=USER, scene="g1", now=0)["traces"]
        assert {t["group_id"] for t in traces} == {"g1", "g2", "private:u1"}
    finally:
        cm.close()


def test_legacy_per_group_rows_are_merged_and_cleared(tmp_path):
    cm, db = _db(tmp_path)
    try:
        # 旧版本按群号写下的行
        db.person_timeline.set_unsettled_state(
            bot_id=BOT, user_id=USER, group_id="g1", energy=6.0, interaction_count=2,
            traces=[{"text": "旧观感", "impact": 6.0, "ts": 0}],
        )
        persist_unsettled_trace(db, bot_id=BOT, user_id=USER, group_id="g2", text="新观感", impact=4)
        state = effective_unsettled_state(db, bot_id=BOT, user_id=USER, scene="g2", now=0)
        assert state["energy"] == 10.0
        legacy = [t for t in state["traces"] if t["text"] == "旧观感"][0]
        assert legacy["group_id"] == "g1", "旧行的观感补上所在场合，供可见性判断"
        clear_unsettled_state(db, bot_id=BOT, user_id=USER)
        assert effective_unsettled_state(db, bot_id=BOT, user_id=USER, scene="g2", now=0)["energy"] == 0.0
    finally:
        cm.close()


def test_negative_shifts_get_real_event_types():
    assert event_type_for_shift("trust", -2) == "bot_attacked"
    assert event_type_for_shift("familiarity", -1) == "bot_attacked"
    assert event_type_for_shift("fun", -2) == "ignored_boundary"
    assert event_type_for_shift("trust", 2) == "bot_praised"
    assert event_type_for_shift("hostility", 3) == "ignored_boundary"
    assert event_type_for_shift("depth", 2) == "deep_talk"


def test_model_supplied_event_type_wins_when_valid():
    assert event_type_for_shift("trust", 2, "gift_or_feed") == "gift_or_feed"
    assert event_type_for_shift("trust", -2, "not_a_type") == "bot_attacked"


def test_bookkeeping_schedules_private_chats():
    import asyncio

    from services.post_reply_bookkeeping import PostReplyBookkeeper

    keeper = PostReplyBookkeeper.__new__(PostReplyBookkeeper)
    keeper.enabled = True
    keeper._timers = {}
    started = []

    async def fake_delayed(scope):
        started.append(scope.visibility)

    keeper._delayed = fake_delayed
    private = RuntimeScope(BOT, "private", SessionRef("qq:private:u1", "qq", "private", "u1"))

    async def scenario():
        keeper.schedule(private)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert started == ["private"]
