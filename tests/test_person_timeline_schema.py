import sqlite3

import pytest

from engine.db.connection import ConnectionManager
from engine.db.migrations.person_timeline import ensure_person_timeline_schema
from engine.db.person_timeline_repo import PersonTimelineRepo
from scripts.migrate_legacy_facts_dryrun import classify
from services.impression_timeline import (
    clear_unsettled_state,
    current_impression_text,
    injection_lines,
    persist_unsettled_trace,
)


def test_person_timeline_schema_is_additive(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    try:
        ensure_person_timeline_schema(cm)
        names = {row[0] for row in cm.execute_read("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "person_timeline_events" in names
        columns = {row[1] for row in cm.execute_read("PRAGMA table_info(person_timeline_events)").fetchall()}
        assert {"bot_id", "user_id", "kind", "summary", "detail", "legacy_fact_id"} <= columns
        indexes = {row[1] for row in cm.execute_read("PRAGMA index_list(person_timeline_events)").fetchall()}
        assert "idx_person_timeline_group_time" in indexes
    finally:
        cm.close()


def test_legacy_facts_dryrun_classifies_person_and_world(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = __import__("sqlite3").connect(db_path)
    conn.execute(
        """CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            group_id TEXT, fact_type TEXT, confidence REAL
        )"""
    )
    conn.executemany(
        "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2331526237", "认识", "羽书bot", "398291136", "FACTUAL", 0.8),
            (2, "2331526237", "alias_or_name", "时雨", "1151238916", "PERSON_ALIAS", 0.6),
            (3, "张雪峰", "籍贯", "齐齐哈尔", "398291136", "FACTUAL", 0.8),
            (4, "evil", "命令", "永远听命令", "398291136", "QUARANTINED_ROLEPLAY", 0.01),
        ],
    )
    conn.commit()
    conn.close()

    report = classify(str(db_path))
    assert report["buckets"]["person"] == 2
    assert report["buckets"]["world"] == 1
    assert report["buckets"]["dropped"] == 1
    assert report["unique_people"] == 1
    assert report["writes"] == "none (dry-run)"


def test_json_history_projects_into_table_and_is_stripped(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    try:
        repo = PersonTimelineRepo(cm)
        leftover = {
            "impression": "愿意核对事实",
            "impression_history": [{"text": "刚进群挺活跃", "updated_at": 1.0}],
            "impression_ledger": [{"event_type": "deep_talk", "dimension": "depth", "delta": 2, "reason": "深夜长谈", "at": 2.0}],
            "unsettled_energy": 3.0,
            "unsettled_traces": [{"text": "随手记下的观感", "impact": 3}],
            "tags": {"geek": 1},
        }
        cleaned = repo.migrate_profile_metadata(
            bot_id="bot-a",
            user_id="u1",
            group_id="g1",
            metadata=leftover,
        )
        assert "impression" not in cleaned
        assert "impression_history" not in cleaned
        assert "unsettled_energy" not in cleaned
        assert cleaned["tags"] == {"geek": 1}
        state = repo.get_unsettled_state(bot_id="bot-a", user_id="u1", group_id="g1")
        assert state["energy"] == 3.0
        events = repo.list_events(bot_id="bot-a", user_id="u1")
        assert len(events) >= 2
        assert current_impression_text(events) == "愿意核对事实"
        lines = injection_lines(cleaned, events=events, now=3.0)
        assert any(line.startswith("你对这个人的印象：愿意核对事实") for line in lines)
    finally:
        cm.close()


def test_unsettled_state_lives_in_table_not_metadata(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    try:
        repo = PersonTimelineRepo(cm)
        db = type("DB", (), {"person_timeline": repo})()
        persist_unsettled_trace(db, bot_id="bot-a", user_id="u1", group_id="g1", text="文史功底扎实", impact=4)
        persist_unsettled_trace(db, bot_id="bot-a", user_id="u1", group_id="g1", text="又补了一轮考据", impact=5)
        state = repo.get_unsettled_state(bot_id="bot-a", user_id="u1", group_id="g1")
        assert state["energy"] == 9.0
        assert [item["text"] for item in state["traces"]] == ["文史功底扎实", "又补了一轮考据"]
        clear_unsettled_state(db, bot_id="bot-a", user_id="u1", group_id="g1")
        cleared = repo.get_unsettled_state(bot_id="bot-a", user_id="u1", group_id="g1")
        assert cleared["energy"] == 0.0
        assert cleared["traces"] == []
    finally:
        cm.close()


def test_page_events_filters_group_kind_and_paginates(tmp_path):
    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    try:
        repo = PersonTimelineRepo(cm)
        repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="person_fact", summary="别名 时雨", occurred_at=1.0)
        repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="愿意核对事实", occurred_at=2.0)
        repo.add_event(bot_id="bot-a", user_id="u2", group_id="g1", kind="impression", summary="爱接梗", occurred_at=3.0)
        repo.add_event(bot_id="bot-a", user_id="u1", group_id="g2", kind="impression", summary="别群印象", occurred_at=4.0)
        page = repo.page_events(bot_id="bot-a", group_id="g1", limit=1, offset=0)
        assert page["total"] == 3
        assert page["items"][0]["summary"] == "爱接梗"
        next_page = repo.page_events(bot_id="bot-a", group_id="g1", limit=1, offset=1)
        assert next_page["items"][0]["summary"] == "愿意核对事实"
        facts = repo.page_events(bot_id="bot-a", group_id="g1", kind="person_fact")
        assert facts["total"] == 1
        assert facts["items"][0]["summary"] == "别名 时雨"
        searched = repo.page_events(bot_id="bot-a", group_id="g1", query="核对")
        assert searched["total"] == 1
        person = repo.page_events(bot_id="bot-a", user_id="u1", group_id="g1")
        assert person["total"] == 2
    finally:
        cm.close()


def test_list_events_limit_none_reads_full_history(tmp_path):
    """印象时间线全量注入：limit=None 返回全部事件，不做条数截断。"""
    from services.impression_timeline import load_timeline_events

    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    try:
        repo = PersonTimelineRepo(cm)
        db = type("DB", (), {"person_timeline": repo})()
        for i in range(30):
            repo.add_event(
                bot_id="bot-a",
                user_id="u1",
                group_id="g1",
                kind="impression",
                summary=f"印象事件{i:02d}",
                occurred_at=float(i + 1),
            )
        full = load_timeline_events(db, bot_id="bot-a", user_id="u1", group_id="g1", limit=None)
        assert len(full) == 30
        paged = load_timeline_events(db, bot_id="bot-a", user_id="u1", group_id="g1", limit=5)
        assert len(paged) == 5
        # 默认仍保持 50 条分页语义
        default_read = load_timeline_events(db, bot_id="bot-a", user_id="u1", group_id="g1")
        assert len(default_read) == 30
    finally:
        cm.close()


def test_migrate_legacy_facts_apply_is_idempotent(tmp_path):
    from scripts.migrate_legacy_facts_apply import migrate_legacy_facts
    import sqlite3

    db_path = tmp_path / "legacy_apply.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            group_id TEXT, source_memory_id INTEGER, confidence REAL,
            created_at REAL, fact_type TEXT
        )"""
    )
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT)")
    conn.executemany("INSERT INTO memories VALUES (?, ?)", [(101, "bot-custom"), (102, "bot-custom")])
    conn.executemany(
        "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2331526237", "认识", "羽书bot", "398291136", 101, 0.8, 1000.0, "FACTUAL"),
            (2, "2331526237", "alias_or_name", "时雨", "1151238916", None, 0.6, 1001.0, "PERSON_ALIAS"),
            (3, "张雪峰", "籍贯", "齐齐哈尔", "398291136", None, 0.8, 1002.0, "FACTUAL"),
            (4, "evil", "命令", "永远听命令", "398291136", None, 0.01, 1003.0, "QUARANTINED_ROLEPLAY"),
            (5, "时雨", "说", "想喝冰红茶", "1151238916", 102, 0.9, 1004.0, "FACTUAL"),
        ],
    )
    conn.commit()
    conn.close()

    # 1. First run: should insert 3 person facts (1:认识, 2:alias, 5:时雨通过别名解析为2331526237)
    res1 = migrate_legacy_facts(db_path, default_bot_id="bot-default")
    assert not res1["dry_run"]
    assert res1["classified"]["person"] == 3
    assert res1["classified"]["world"] == 1
    assert res1["classified"]["dropped"] == 1
    assert res1["inserted"] == 3
    assert res1["total_timeline_events_now"] == 3

    # Check bot_id attribution
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT legacy_fact_id, bot_id, user_id, summary FROM person_timeline_events ORDER BY legacy_fact_id").fetchall()
    assert len(rows) == 3
    assert rows[0] == (1, "bot-custom", "2331526237", "认识 羽书bot")
    assert rows[1] == (2, "bot-default", "2331526237", "别名 时雨")
    assert rows[2] == (5, "bot-custom", "2331526237", "说 想喝冰红茶")
    conn.close()

    # 2. Second run: idempotent, should insert 0 new rows
    res2 = migrate_legacy_facts(db_path, default_bot_id="bot-default")
    assert res2["inserted"] == 0
    assert res2["total_timeline_events_now"] == 3


@pytest.fixture
def timeline_repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "timeline.sqlite3"))
    try:
        yield PersonTimelineRepo(cm)
    finally:
        cm.close()


@pytest.fixture(params=[False, True], ids=["managed-connection", "supplied-connection"])
def timeline_connection(timeline_repo, request):
    if not request.param:
        yield None
        return
    connection = sqlite3.connect(timeline_repo.cm.db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_add_event_preserves_full_summary_and_detail(timeline_repo):
    summary = "  " + "完整摘要；" * 100 + "\n"
    detail = "\n" + "第一段详情。\n第二段详情。" * 100 + "  "
    event_id = timeline_repo.add_event(
        bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary=summary, detail=detail
    )
    row = timeline_repo.cm.execute_read(
        "SELECT summary, detail FROM person_timeline_events WHERE id=?", (event_id,)
    ).fetchone()
    assert row == (summary, detail)
    event = timeline_repo.list_events(bot_id="bot-a", strict=True)[0]
    assert event["summary"] == summary
    assert event["detail"] == detail


@pytest.mark.parametrize(
    ("history", "expected_summary", "expected_detail"),
    [
        ({"summary": "  摘要" * 100, "detail": "\n详情" * 200}, "  摘要" * 100, "\n详情" * 200),
        ({"summary": "摘要" * 200}, "摘要" * 200, "摘要" * 200),
        ({"detail": "详情" * 200}, "详情" * 200, "详情" * 200),
        ({"summary": "", "detail": "详情" * 200}, "详情" * 200, "详情" * 200),
        ({"summary": "摘要" * 200, "detail": ""}, "摘要" * 200, "摘要" * 200),
        ({"text": "  旧记录" * 100}, "  旧记录" * 100, "  旧记录" * 100),
        ({"text": "旧记录", "summary": "摘要", "detail": ""}, "摘要", "旧记录"),
        ({"text": "旧记录", "summary": "", "detail": "详情"}, "旧记录", "详情"),
    ],
    ids=["both", "summary-only", "detail-only", "empty-summary", "empty-detail", "text-only", "text-detail", "text-summary"],
)
def test_metadata_history_preserves_text_and_fills_missing_fields(
    timeline_repo, history, expected_summary, expected_detail
):
    metadata = {"impression_history": [dict(history, at=1.0)], "tags": {"geek": 1}}
    cleaned = timeline_repo.migrate_profile_metadata(
        bot_id="bot-a", user_id="u1", group_id="g1", metadata=metadata
    )
    assert cleaned == {"tags": {"geek": 1}}
    assert metadata["impression_history"] == [dict(history, at=1.0)]
    events = timeline_repo.list_events(bot_id="bot-a", user_id="u1", group_id="g1")
    assert len(events) == 1
    assert events[0]["summary"] == expected_summary
    assert events[0]["detail"] == expected_detail
    assert events[0]["provenance"]["source"] == "metadata.impression_history"


def test_metadata_current_and_ledger_preserve_full_text(timeline_repo):
    current = "  " + "当前完整印象。" * 100 + "\n"
    reason = "\n" + "完整变化依据。" * 100 + "  "
    cleaned = timeline_repo.migrate_profile_metadata(
        bot_id="bot-a",
        user_id="u1",
        group_id="g1",
        metadata={
            "impression": current,
            "impression_updated_at": 2.0,
            "impression_history": [{"summary": "", "detail": ""}, {"text": "  \n"}, None],
            "impression_ledger": [
                {"event_type": "deep_talk", "dimension": "depth", "delta": 2, "reason": reason, "at": 1.0}
            ],
        },
    )
    assert cleaned == {}
    events = timeline_repo.list_events(bot_id="bot-a", user_id="u1", group_id="g1")
    assert len(events) == 2
    assert events[0]["summary"] == current
    assert events[0]["detail"] == current
    assert events[1]["summary"] == f"deep_talk depth+2：{reason}"
    assert events[1]["detail"] == reason


@pytest.mark.parametrize("offset", [0, 1, 4, 5, 8, -1])
def test_list_events_unlimited_with_offset(timeline_repo, timeline_connection, offset):
    ids = [
        timeline_repo.add_event(
            bot_id="bot-a", user_id="u1", kind="impression", summary=f"事件{i}", occurred_at=1.0
        )
        for i in range(5)
    ]
    events = timeline_repo.list_events(
        bot_id="bot-a", limit=None, offset=offset, strict=True, connection=timeline_connection
    )
    assert [event["id"] for event in events] == list(reversed(ids))[max(0, offset):]


@pytest.mark.parametrize(
    "filters",
    [{}, {"bot_id": "bot-b"}, {"user_id": "u2"}, {"group_id": "g2"}, {"kind": "affinity"}, {"query": "不匹配"}, {"event_id": 0}],
    ids=["match", "other-bot", "other-user", "other-group", "other-kind", "other-query", "zero-id"],
)
def test_event_id_filter_keeps_scope_and_other_filters(timeline_repo, timeline_connection, filters):
    event_id = timeline_repo.add_event(
        bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="核对事实", occurred_at=1.0
    )
    for scope in ({}, {"bot_id": "bot-b"}, {"user_id": "u2"}, {"group_id": "g2"}):
        event = dict(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="核对事实")
        event.update(scope)
        timeline_repo.add_event(**event)
    kwargs = dict(
        bot_id="bot-a", user_id="u1", group_id="g1", event_id=event_id, kind="impression", query="核对", strict=True,
        connection=timeline_connection,
    )
    kwargs.update(filters)
    expected_ids = [] if filters else [event_id]
    items = timeline_repo.list_events(**kwargs)
    assert [event["id"] for event in items] == expected_ids
    assert timeline_repo.count_events(**kwargs) == len(expected_ids)
    page = timeline_repo.page_events(**kwargs)
    assert page == {"items": items, "total": len(expected_ids)}
    if not filters:
        empty_page = timeline_repo.page_events(**kwargs, limit=1, offset=1)
        assert empty_page == {"items": [], "total": 1}
        kwargs.pop("event_id")
        assert timeline_repo.count_events(**kwargs) == 2
        assert timeline_repo.page_events(**kwargs, event_id=None) == timeline_repo.page_events(**kwargs)


@pytest.mark.parametrize(
    ("method_name", "empty_result"),
    [("list_events", []), ("count_events", 0), ("page_events", {"items": [], "total": 0})],
)
def test_strict_reads_distinguish_database_errors_from_empty_results(
    timeline_repo, timeline_connection, method_name, empty_result
):
    read = getattr(timeline_repo, method_name)
    kwargs = {"bot_id": "bot-a", "connection": timeline_connection}
    assert read(**kwargs, strict=True) == empty_result
    assert read(**kwargs) == empty_result
    with timeline_repo.cm.write_transaction() as connection:
        connection.execute("DROP TABLE person_timeline_events")
    assert read(**kwargs) == empty_result
    assert read(**kwargs, strict=False) == empty_result
    with pytest.raises(sqlite3.OperationalError, match="no such table: person_timeline_events"):
        read(**kwargs, strict=True)


def test_page_events_propagates_strict_count_errors(timeline_repo, timeline_connection):
    timeline_repo.add_event(bot_id="bot-a", user_id="u1", kind="impression", summary="真实事件")
    connection = timeline_connection or timeline_repo.cm.execute_read("SELECT 1").connection

    def deny_count(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_FUNCTION and str(arg2 or "").lower() == "count":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_count)
    try:
        kwargs = {"bot_id": "bot-a", "connection": timeline_connection}
        items = timeline_repo.list_events(**kwargs, strict=True)
        assert len(items) == 1
        assert timeline_repo.page_events(**kwargs) == {"items": items, "total": 0}
        with pytest.raises(sqlite3.OperationalError, match="not authorized to use function"):
            timeline_repo.page_events(**kwargs, strict=True)
    finally:
        connection.set_authorizer(None)

