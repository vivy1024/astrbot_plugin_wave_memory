"""跃迁证据卷宗：观感带方向与原话，结算时整段交给模型，跃迁点记住它消化了哪些观感。"""

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

from engine.db.connection import ConnectionManager
from engine.db.person_timeline_repo import PersonTimelineRepo
from services.impression_timeline import (
    effective_unsettled_state,
    parse_impression_mark,
    parse_impression_polarity,
    persist_timeline_event,
    persist_unsettled_trace,
    render_unsettled_dossier,
    settle_unsettled_traces,
    synthesize_milestone_phrase,
    unsettled_energy_line,
)

BOT, USER = "bot-a", "u1"
NOW = 1_800_000_000.0


def _db(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    repo = PersonTimelineRepo(cm)
    return cm, type("DB", (), {"person_timeline": repo})()


def _add(db, scene, text, impact, polarity=0, quote="", at=NOW):
    persist_unsettled_trace(
        db, bot_id=BOT, user_id=USER, group_id=scene, text=text, impact=impact,
        polarity=polarity, quote=quote, source_memory_id=7 if quote else None, now=at,
    )


def test_polarity_parsing_keeps_magnitude_positive():
    assert parse_impression_mark("被冷落了有点难过 | impact:-3") == ("被冷落了有点难过", 3.0)
    assert parse_impression_polarity("被冷落了有点难过 | impact:-3") == -1
    assert parse_impression_polarity("帮了我大忙 | impact:+4") == 1
    assert parse_impression_polarity("普通观感一条 | impact:2") == 0
    assert parse_impression_polarity("没有标记的观感") == 0


def test_traces_carry_evidence_and_short_ids(tmp_path):
    cm, db = _db(tmp_path)
    try:
        _add(db, "g1", "替我解了围", 4, polarity=1, quote="没事我来处理")
        trace = effective_unsettled_state(db, bot_id=BOT, user_id=USER, now=NOW)["traces"][0]
        assert trace["tid"] and len(trace["tid"]) == 10
        assert trace["polarity"] == 1 and trace["quote"] == "没事我来处理" and trace["source_memory_id"] == 7
        assert trace["group_id"] == "g1"
    finally:
        cm.close()


def test_settlement_keeps_traces_as_evidence_and_zeroes_energy(tmp_path):
    cm, db = _db(tmp_path)
    try:
        _add(db, "g1", "替我解了围", 5, polarity=1, quote="没事我来处理")
        _add(db, "g2", "又帮我改了稿", 5, polarity=1, quote="我看看哪里不顺")
        assert effective_unsettled_state(db, bot_id=BOT, user_id=USER, now=NOW)["energy"] == 10.0
        settled = settle_unsettled_traces(db, bot_id=BOT, user_id=USER, now=NOW)
        assert len(settled) == 2 and all(t["tid"] for t in settled)
        after = effective_unsettled_state(db, bot_id=BOT, user_id=USER, now=NOW)
        assert after["energy"] == 0.0 and after["traces"] == [], "已结算观感不再计能量，也不再出现在待结算里"
        stored = db.person_timeline.get_unsettled_state(bot_id=BOT, user_id=USER, group_id="")
        assert [t["settled"] for t in stored["traces"]] == [True, True], "但作为证据仍保留在库里"
    finally:
        cm.close()


def test_milestone_event_remembers_which_traces_it_settled(tmp_path):
    cm, db = _db(tmp_path)
    try:
        _add(db, "g1", "替我解了围", 5, polarity=1, quote="没事我来处理")
        _add(db, "g1", "又帮我改了稿", 5, polarity=1)
        settled = settle_unsettled_traces(db, bot_id=BOT, user_id=USER, now=NOW)
        tids = [t["tid"] for t in settled]
        persist_timeline_event(
            db, bot_id=BOT, user_id=USER, group_id="g1", kind="affinity",
            summary=synthesize_milestone_phrase(
                event_type="bot_praised", reason="一直在帮我", before_affinity=10, after_affinity=15,
                dimension="trust", delta=5, is_leap=True, settled_count=len(settled),
            ),
            occurred_at=NOW, settled_trace_ids=tids, is_leap=True,
        )
        event = db.person_timeline.list_events(bot_id=BOT, user_id=USER, kind="affinity")[0]
        assert event["is_leap"] is True and event["settled_trace_ids"] == tids
        assert "源于 2 件事的积累" in event["summary"]
    finally:
        cm.close()


def test_dossier_lists_direction_quote_and_hides_other_private_chats(tmp_path):
    cm, db = _db(tmp_path)
    try:
        _add(db, "g1", "替我解了围", 5, polarity=1, quote="没事我来处理", at=NOW - 3600)
        _add(db, "private:u2", "私下说了很难过的事", 3, polarity=-1, quote="我最近很不好", at=NOW - 1800)
        traces = effective_unsettled_state(db, bot_id=BOT, user_id=USER, now=NOW)["traces"]
        text = render_unsettled_dossier(traces, viewer_scene="g1", now=NOW)
        assert "正向 1、负向 1" in text
        assert "＋" in text and "替我解了围" in text and "没事我来处理" in text
        assert "私下说了很难过的事" not in text and "我最近很不好" not in text
        assert "另有 1 条发生在私聊里" in text
        # 同一私聊里能看到自己的原文
        own = render_unsettled_dossier(traces, viewer_scene="private:u2", now=NOW)
        assert "私下说了很难过的事" in own and "－" in own
    finally:
        cm.close()


def test_energy_line_embeds_dossier_only_when_full(tmp_path):
    cm, db = _db(tmp_path)
    try:
        _add(db, "g1", "替我解了围", 5, polarity=1, quote="没事我来处理")
        low = effective_unsettled_state(db, bot_id=BOT, user_id=USER, now=NOW)
        assert "整体定性" not in unsettled_energy_line({}, energy=low["energy"], traces=low["traces"])
        _add(db, "g1", "又帮我改了稿", 5, polarity=1, quote="我看看哪里不顺")
        full = effective_unsettled_state(db, bot_id=BOT, user_id=USER, now=NOW)
        line = unsettled_energy_line({}, energy=full["energy"], traces=full["traces"])
        assert "整体定性" in line and "没事我来处理" in line and "我看看哪里不顺" in line
    finally:
        cm.close()
