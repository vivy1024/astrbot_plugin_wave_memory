"""记账与学习：自动审核、关切主动跟进、事后记账、每日日记、未结算能量合并。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.migrations.scoped_fact_review import ensure_scoped_fact_review_schema
from engine.db.outbox_repo import OutboxRepository
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.auto_review import AutoReviewer
from services.concern_followup import ConcernFollowupService
from services.daily_diary import DailyDiaryService
from services.impression_timeline import UNSETTLED_ENERGY_CAP, effective_unsettled_state
from services.post_reply_bookkeeping import PostReplyBookkeeper, _json_object


def _scope(group: str = "g1") -> RuntimeScope:
    return RuntimeScope("yushu", "group", SessionRef(f"qq:group:{group}", "qq", "group", group))


# ─── 未结算能量 ───


class _Timeline:
    def __init__(self, rows):
        self.rows = rows

    def get_unsettled_state(self, *, bot_id, user_id, group_id, connection=None):
        return self.rows.get(group_id, {"energy": 0.0, "traces": []})


def test_effective_unsettled_state_merges_rows_decays_and_caps():
    now = 2_000_000_000.0
    week = 7 * 86400
    db = SimpleNamespace(person_timeline=_Timeline({
        "": {"energy": 6.0, "traces": [{"impact": 3, "ts": now}, {"impact": 3, "ts": now - week}]},
        "g1": {"energy": 4.0, "traces": [{"impact": 4, "ts": now}]},
    }))
    state = effective_unsettled_state(db, bot_id="yushu", user_id="u1", scene="g1", now=now)
    # 跨群行：3 + 3×0.5 = 4.5 / 6 → 4.5；本群行 4 → 合计 8.5
    assert state["energy"] == pytest.approx(8.5)
    assert len(state["traces"]) == 3

    db.person_timeline.rows[""]["energy"] = 60.0
    assert effective_unsettled_state(db, bot_id="yushu", user_id="u1", scene="g1", now=now)["energy"] == UNSETTLED_ENERGY_CAP


def test_effective_unsettled_state_ignores_placeholder_timestamps():
    db = SimpleNamespace(person_timeline=_Timeline({"": {"energy": 5.0, "traces": [{"impact": 5, "ts": 5.0}]}}))
    assert effective_unsettled_state(db, bot_id="yushu", user_id="u1", now=2_000_000_000.0)["energy"] == 5.0


# ─── 自动审核 ───


class _Coordinator:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._consumer_names = ("projection",)

    async def transaction(self, callback, *, actor=None):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = callback(self.connection)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise


@pytest.fixture
def knowledge(tmp_path):
    manager = ConnectionManager(str(tmp_path / "learning.sqlite3"))
    manager._write_conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           resolution_state TEXT, quarantine INTEGER DEFAULT 0)"""
    )
    manager._write_conn.execute(
        "INSERT INTO memories VALUES (42, 'yushu', 'qq:group:g1', 'group', 'resolved', 0)"
    )
    manager._write_conn.commit()
    ensure_scoped_derived_knowledge_schema(manager)
    ensure_scoped_fact_review_schema(manager)
    connection = manager._write_conn
    OutboxRepository.migrate(connection)
    connection.commit()
    gateway = SimpleNamespace(coordinator=_Coordinator(connection))
    repo = ScopedKnowledgeRepo(manager)
    try:
        yield SimpleNamespace(scoped_knowledge=repo, conn=connection), gateway, repo
    finally:
        manager.close()


def _pending_fact(repo, *, quote="我在上海上班", memory_id=42, needs_jargon_review=False):
    return repo.upsert_scoped_fact(
        _scope(), subject="小明", predicate="住在", object="上海", confidence=0.85, status="pending",
        source_memory_id=memory_id,
        provenance={"source_quote": quote, "needs_jargon_review": needs_jargon_review},
    )


def _status(repo, fact_id):
    return next(f for f in repo.list_scoped_facts(_scope(), limit=50) if f["id"] == fact_id)["status"]


def test_auto_review_disabled_keeps_fact_pending(knowledge):
    db, gateway, repo = knowledge
    fact_id = _pending_fact(repo)
    reviewer = AutoReviewer(db, gateway, enabled=False)
    assert asyncio.run(reviewer.review_fact(_scope(), fact_id)) is None
    assert _status(repo, fact_id) == "pending"


def test_auto_review_approves_fact_with_quote_and_source(knowledge):
    db, gateway, repo = knowledge
    fact_id = _pending_fact(repo)
    reviewer = AutoReviewer(db, gateway, enabled=True)
    assert asyncio.run(reviewer.review_fact(_scope(), fact_id)) == "active"
    assert _status(repo, fact_id) == "active"


def test_auto_review_skips_fact_without_evidence_or_with_unknown_jargon(knowledge):
    db, gateway, repo = knowledge
    reviewer = AutoReviewer(db, gateway, enabled=True)
    no_source = _pending_fact(repo, memory_id=None)
    assert asyncio.run(reviewer.review_fact(_scope(), no_source)) is None
    assert reviewer.stats["fact_skipped_no_evidence"] == 1


def test_reproposing_approved_fact_keeps_it_approved(knowledge):
    db, gateway, repo = knowledge
    fact_id = _pending_fact(repo)
    asyncio.run(AutoReviewer(db, gateway, enabled=True).review_fact(_scope(), fact_id))
    again = _pending_fact(repo)
    assert again == fact_id
    assert _status(repo, fact_id) == "active"


# ─── 关切主动跟进 ───


def _concern_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE scoped_soul_concerns (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT, evidence TEXT, created_at REAL, last_triggered REAL,
           last_progress_at REAL, expected_resolution_at REAL)"""
    )
    for row in rows:
        conn.execute(
            "INSERT INTO scoped_soul_concerns (id, bot_id, session_id, visibility, topic, intensity, status, evidence, created_at)"
            " VALUES (?, 'yushu', 'qq:group:g1', 'group', ?, 0.8, ?, ?, 1.9e9)",
            row,
        )
    return SimpleNamespace(conn=conn)


NOON = datetime(2026, 9, 28, 12, 0).timestamp()


def test_followup_disabled_by_default():
    db = _concern_db([(1, "面试结果", "active", json.dumps([{"kind": "subject", "user_id": "u1"}]))])
    service = ConcernFollowupService(db)
    assert service.enabled is False
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON) is None
    # 关闭时仍能查到（本来就要回复此人时附提示）
    assert [c["id"] for c in service.open_concerns_for(_scope(), "u1")] == [1]


def test_followup_matches_subject_and_respects_cooldown_and_gap():
    db = _concern_db([
        (1, "面试结果", "active", json.dumps([{"kind": "subject", "user_id": "u1"}])),
        (2, "小红：搬家", "progressing", "[]"),
        (3, "已结束的事", "resolved", json.dumps([{"kind": "subject", "user_id": "u1"}])),
    ])
    service = ConcernFollowupService(db, enabled=True, group_min_gap_seconds=600, group_max_per_hour=3)
    assert service.claim_followup(_scope(), "u9", "路人", "大家好", now=NOON) is None
    chosen = service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)
    assert chosen["id"] == 1
    # 同一条关切冷却中
    assert service.claim_followup(_scope(), "u1", "小明", "在吗", now=NOON + 700) is None
    # 旧关切按「昵称：」前缀匹配；但距上次主动不足 10 分钟
    assert service.claim_followup(_scope(), "u2", "小红", "搬完了", now=NOON + 60) is None
    assert service.claim_followup(_scope(), "u2", "小红", "搬完了", now=NOON + 700)["id"] == 2
    assert "concern:2" in service.hint({"id": 2, "topic": "小红：搬家"}, "小红", proactive=True)


def test_followup_quiet_hours():
    db = _concern_db([(1, "面试结果", "active", json.dumps([{"kind": "subject", "user_id": "u1"}]))])
    service = ConcernFollowupService(db, enabled=True, quiet_hours=(1, 7))
    night = datetime(2026, 9, 28, 3, 0).timestamp()
    assert service.claim_followup(_scope(), "u1", "小明", "睡不着", now=night) is None
    assert service.stats["skip_quiet_hours"] == 1


# ─── 事后记账 ───


def test_json_object_tolerates_comments_and_prose():
    text = '好的：\n{"facts": [ // 注释\n {"subject": "小明"}]}\n以上'
    assert _json_object(text) == {"facts": [{"subject": "小明"}]}
    assert _json_object("没有") == {}


class _Tool:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    async def call(self, ctx, **kwargs):
        self.calls.append((self.name, kwargs))
        return "已记录"


class _Registry:
    def __init__(self):
        self.calls = []

    def get(self, name):
        return SimpleNamespace(enabled=True, instance=_Tool(name, self.calls))


class _LLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    async def text_chat(self, *, prompt, system_prompt=None, contexts=None):
        self.prompts.append(prompt)
        return SimpleNamespace(completion_text=json.dumps(self.payload, ensure_ascii=False))


def _memory_db(lines):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL, quarantine INTEGER DEFAULT 0)"""
    )
    conn.execute(
        """CREATE TABLE scoped_soul_concerns (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT)"""
    )
    for sender_id, name, content in lines:
        conn.execute(
            "INSERT INTO memories (bot_id, session_id, visibility, sender_id, sender_name, content, timestamp)"
            " VALUES ('yushu', 'qq:group:g1', 'group', ?, ?, ?, ?)",
            (sender_id, name, content, time.time()),
        )
    return SimpleNamespace(conn=conn, person_timeline=_Timeline({}))


def test_bookkeeper_dispatches_items_to_formal_tools():
    db = _memory_db([
        ("u1", "小明", "我下周三去面试"),
        ("bot", "羽书", "加油！"),
        ("u1", "小明", "我在上海上班，天天加班"),
    ])
    llm = _LLM({
        "impressions": [{"user": "小明", "impression": "很努力", "dimension": "trust", "delta": 1, "source_quote": "天天加班"}],
        "facts": [{"subject": "小明", "predicate": "工作在", "object": "上海", "source_quote": "我在上海上班"}],
        "facts_bad": [],
        "concerns": [{"topic": "小明下周三面试", "target_user": "小明"}],
        "jargon": [{"phrase": "", "meaning": "空的不执行"}],
    })
    registry = _Registry()
    keeper = PostReplyBookkeeper(db, registry, llm, bot_name_for=lambda _b: "羽书")
    result = asyncio.run(keeper.run(_scope()))
    names = [name for name, _ in registry.calls]
    assert names == ["wave_memory_record_social_impression", "wave_memory_propose_fact", "wave_memory_note_concern"]
    assert result == {"actions": 3, "messages": 3}
    assert "羽书：加油！" in llm.prompts[0]
    # 第二次只回看新消息：没有新消息就跳过
    assert asyncio.run(keeper.run(_scope())) == {}
    assert keeper.stats["skip_short"] == 1


def test_bookkeeper_disabled_without_llm_and_ignores_private():
    keeper = PostReplyBookkeeper(_memory_db([]), _Registry(), None)
    assert keeper.enabled is False
    keeper.schedule(_scope())
    assert keeper._timers == {}


# ─── 每日日记 ───


def test_daily_diary_writes_once_via_tool_and_memory():
    db = _memory_db([("u1", "小明", "今天好热"), ("bot", "羽书", "我也觉得，想吃冰淇淋")])
    db.conn.execute(
        """CREATE TABLE scoped_soul_timeline (bot_id TEXT, session_id TEXT, visibility TEXT, event_summary TEXT,
           occurred_at REAL)"""
    )
    db.conn.execute(
        "CREATE TABLE person_timeline_events (bot_id TEXT, group_id TEXT, kind TEXT, summary TEXT, created_at REAL)"
    )
    content = "今天群里好热闹，" * 10 + "\nTag: 天气, 小明"
    llm = _LLM({"title": "热天", "content": content, "summary": "和小明聊天气"})
    registry = _Registry()

    class _Writer:
        items = []

        async def enqueue(self, item):
            self.items.append(item)

    writer = _Writer()
    diary = DailyDiaryService(db, registry, llm, writer, hour=0)
    now = time.time()
    assert asyncio.run(diary.write_due(now=now)) == 1
    assert registry.calls[0][0] == "wave_memory_record_diary_episode"
    assert writer.items[0]["source"] == "experience" and "热天" in writer.items[0]["content"]

    # 经历时间线里已有当天日记：不再写
    db.conn.execute(
        "INSERT INTO scoped_soul_timeline VALUES ('yushu', 'qq:group:g1', 'group', '【日记】热天', ?)", (now,)
    )
    assert asyncio.run(diary.write_due(now=now)) == 0
