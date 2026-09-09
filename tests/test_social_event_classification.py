from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services import lifecycle
from services.lifecycle import AffinityEngine


def _group_scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )


def test_keyword_classifier_removed_from_formal_path():
    assert not hasattr(lifecycle, "classify_social_event")


def test_uncategorized_delta_does_not_write_message_seen():
    class _Service:
        def record_event(self, **kwargs):
            raise AssertionError(f"noise must not be recorded: {kwargs}")

    db = SimpleNamespace(conn=SimpleNamespace(execute=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no db write"))))
    engine = AffinityEngine(db=db, bot_qq_id="bot-qq", bot_db_id="bot-alpha", relationship_service=_Service())
    engine._record_relationship_events(
        user_id="u1",
        group_id="g1",
        before={"familiarity": 0},
        after={"familiarity": 0.05},
        reasons={"familiarity": ["看见一条群友消息"]},
        scope=_group_scope(),
    )
    engine._record_relationship_events(
        user_id="u1",
        group_id="g1",
        before={},
        after={"fun": 1},
        reasons={"fun": ["消息带来趣味感"]},
        scope=_group_scope(),
        classified={
            "ledger": True,
            "entries": [{"dimension": "fun", "delta": 1.0, "reason": "消息带来趣味感", "event_type": "joke"}],
        },
    )


def test_process_message_only_marks_touch_and_skips_keyword_deltas():
    class _Conn:
        def execute(self, *args, **kwargs):
            raise AssertionError("process_message must not query last_seen or classify events")

    engine = AffinityEngine(
        db=SimpleNamespace(conn=_Conn()),
        bot_qq_id="bot-qq",
        bot_db_id="bot-alpha",
    )
    assert engine.process_message(content="你真厉害", is_at_bot=True, scope=_group_scope()) is True
    buf = engine._buffer[("u1", "g1")]
    assert buf["_touched"] == 1.0
    assert "trust" not in buf
    assert "hostility" not in buf
    assert "familiarity" not in buf
    assert "fun" not in buf


def test_failed_formal_write_does_not_append_ledger():
    calls = []

    class _Service:
        def record_event(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("formal write failed")

    db = SimpleNamespace(conn=SimpleNamespace(execute=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no db write"))))
    engine = AffinityEngine(db=db, bot_qq_id="bot-qq", bot_db_id="bot-alpha", relationship_service=_Service())
    engine._record_relationship_events(
        user_id="u1",
        group_id="g1",
        before={},
        after={"trust": 3},
        reasons={"trust": ["正面评价 bot"]},
        scope=_group_scope(),
        classified={
            "kind": "praise",
            "event_type": "bot_praised",
            "ledger": True,
            "entries": [{"dimension": "trust", "delta": 3.0, "reason": "正面评价 bot", "event_type": "bot_praised"}],
        },
    )
    assert calls and calls[0]["event_type"] == "bot_praised"


def test_real_affinity_shift_records_milestone_phrase(tmp_path):
    import json
    import sqlite3

    conn = sqlite3.connect(tmp_path / "social.db")
    conn.execute(
        """CREATE TABLE user_profiles (
               user_id TEXT, group_id TEXT, bot_id TEXT, metadata TEXT,
               PRIMARY KEY (user_id, group_id, bot_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE relationship_events (
               bot_id TEXT, group_id TEXT, user_id TEXT, event_type TEXT,
               dimension TEXT, delta REAL, reason TEXT, created_at REAL
           )"""
    )
    conn.execute("INSERT INTO user_profiles VALUES ('u1', 'g1', 'bot-alpha', '{}')")
    conn.commit()

    class _FakeStored:
        event_id = 42
        before_affinity = 10
        after_affinity = 18

    class _Service:
        def record_event(self, **kwargs):
            return _FakeStored()

    from engine.db.migrations.person_timeline import ensure_person_timeline_schema
    from engine.db.person_timeline_repo import PersonTimelineRepo

    class _CM:
        def execute_write(self, sql, params=None):
            return conn.execute(sql, params or ())
        def execute_read(self, sql, params=None):
            return conn.execute(sql, params or ())
        def migration_transaction(self):
            from contextlib import contextmanager
            @contextmanager
            def _tx():
                yield conn
                conn.commit()
            return _tx()

    cm = _CM()
    ensure_person_timeline_schema(cm)
    db = SimpleNamespace(conn=conn, person_timeline=PersonTimelineRepo(cm))
    engine = AffinityEngine(db=db, bot_qq_id="bot-qq", bot_db_id="bot-alpha", relationship_service=_Service())
    engine._record_relationship_events(
        user_id="u1",
        group_id="g1",
        before={},
        after={"trust": 3},
        reasons={"trust": ["正面评价 bot"]},
        scope=_group_scope(),
        classified={
            "kind": "praise",
            "event_type": "bot_praised",
            "ledger": True,
            "entries": [{"dimension": "trust", "delta": 3.0, "reason": "正面评价 bot", "event_type": "bot_praised"}],
        },
    )

    events = db.person_timeline.list_events(bot_id="bot-alpha", user_id="u1")
    assert events
    assert events[0]["summary"].startswith("获得正面赞赏（正面评价 bot），好感从 10 升至 18")
    assert events[0]["kind"] == "affinity"
