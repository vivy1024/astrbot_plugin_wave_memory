from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.lifecycle import AffinityEngine, classify_social_event


def test_praise_beats_at_and_passby():
    classified = classify_social_event(content="你真厉害", is_at_bot=True, last_seen=1.0, now=2.0)
    assert classified["kind"] == "praise"
    assert classified["event_type"] == "bot_praised"
    assert classified["ledger"] is True
    assert [item["dimension"] for item in classified["entries"]] == ["trust"]


def test_passby_does_not_enter_ledger():
    classified = classify_social_event(content="今天天气不错", last_seen=1.0, now=2.0)
    assert classified["kind"] == "passby"
    assert classified["event_type"] == "message_seen"
    assert classified["ledger"] is False
    assert classified["entries"][0]["dimension"] == "familiarity"


def test_insult_emits_hostility_and_trust_rows():
    classified = classify_social_event(content="你这个废物", is_reply_to_bot=True)
    assert classified["kind"] == "insult"
    assert [(item["dimension"], item["delta"]) for item in classified["entries"]] == [
        ("hostility", 8.0),
        ("trust", -3.0),
    ]


def test_gift_maps_to_fun_only():
    classified = classify_social_event(content="给你红包", is_at_bot=True)
    assert classified["event_type"] == "gift_or_feed"
    assert classified["entries"] == [
        {"dimension": "fun", "delta": 2.0, "reason": "投喂或送礼", "event_type": "gift_or_feed"}
    ]


def test_jargon_signals_trigger_joke_and_protect_from_passby():
    # 模拟群友发了一句抽象反串/黑话：“你说得对，但是”
    signals = {"has_global_irony": True, "matched_terms": ["你说得对，但是"]}
    classified = classify_social_event(content="你说得对，但是我们原神", jargon_signals=signals)
    assert classified["kind"] == "joke"
    assert classified["event_type"] == "joke"
    assert classified["ledger"] is True
    assert classified["entries"][0]["dimension"] == "fun"
    assert "你说得对，但是" in classified["entries"][0]["reason"]


def test_failed_formal_write_does_not_append_ledger():
    calls = []

    class _Service:
        def record_event(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("formal write failed")

    db = SimpleNamespace(conn=SimpleNamespace(execute=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no db write"))))
    engine = AffinityEngine(db=db, bot_qq_id="bot-qq", bot_db_id="bot-alpha", relationship_service=_Service())
    scope = RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )
    engine._record_relationship_events(
        user_id="u1",
        group_id="g1",
        before={},
        after={"trust": 3},
        reasons={"trust": ["正面评价 bot"]},
        scope=scope,
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

    db = SimpleNamespace(conn=conn)
    engine = AffinityEngine(db=db, bot_qq_id="bot-qq", bot_db_id="bot-alpha", relationship_service=_Service())
    scope = RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )
    engine._record_relationship_events(
        user_id="u1",
        group_id="g1",
        before={},
        after={"trust": 3},
        reasons={"trust": ["正面评价 bot"]},
        scope=scope,
        classified={
            "kind": "praise",
            "event_type": "bot_praised",
            "ledger": True,
            "entries": [{"dimension": "trust", "delta": 3.0, "reason": "正面评价 bot", "event_type": "bot_praised"}],
        },
    )

    row = conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()
    meta = json.loads(row[0])
    assert meta["impression"].startswith("获得正面赞赏（正面评价 bot），好感从 10 升至 18")
    assert len(meta["impression_history"]) == 1
    assert meta["impression_history"][0]["text"].startswith("获得正面赞赏")
    assert meta["impression_history"][0]["actor"] == "affinity_milestone"
    assert len(meta["impression_ledger"]) == 1
    assert meta["impression_ledger"][0]["event_type"] == "bot_praised"

