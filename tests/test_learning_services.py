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
           resolution_state TEXT, quarantine INTEGER DEFAULT 0,
           content TEXT, sender_id TEXT, sender_name TEXT, timestamp REAL,
           memory_type TEXT DEFAULT 'message', source TEXT DEFAULT 'chat')"""
    )
    manager._write_conn.execute(
        """INSERT INTO memories(id,bot_id,session_id,visibility,resolution_state,quarantine,
           content,sender_id,sender_name,timestamp)
           VALUES (42,'yushu','qq:group:g1','group','resolved',0,'我住在上海','u1','小明',1700000000)"""
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


def _pending_fact(repo, *, quote="我住在上海", memory_id=42, needs_jargon_review=False):
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
    # 审核记录标明是自动审核，与管理台人工审核区分开
    db, _, _ = knowledge
    actors = [row[0] for row in db.conn.execute("SELECT actor FROM scoped_fact_reviews WHERE fact_id=?", (fact_id,))]
    assert actors == ["auto_review"]


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


def test_followup_excludes_social_anchor_concerns():
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE scoped_soul_concerns (
           id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT, concern_type TEXT, evidence TEXT,
           created_at REAL, last_triggered REAL, last_progress_at REAL)"""
    )
    evidence = json.dumps([{"kind": "subject", "user_id": "u1"}])
    db.execute(
        "INSERT INTO scoped_soul_concerns VALUES (1,'yushu','qq:group:g1','group','人情备忘',.8,'active','social_anchor',?,1.0,NULL,NULL)",
        (evidence,),
    )
    db.execute(
        "INSERT INTO scoped_soul_concerns VALUES (2,'yushu','qq:group:g1','group','面试结果',.8,'active','wellbeing',?,1.0,NULL,NULL)",
        (evidence,),
    )
    service = ConcernFollowupService(SimpleNamespace(conn=db), enabled=True, quiet_hours=(0, 0))
    assert [item["id"] for item in service.open_concerns_for(_scope(), "u1")] == [2]


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


class _ScopedOutcomeTool:
    def __init__(self, name, calls, failed_name=""):
        self.name, self.calls, self.failed_name = name, calls, failed_name

    async def call(self, ctx, **kwargs):
        scope = ctx.context.event._wave_memory_runtime_scope
        self.calls.append((self.name, kwargs, scope.subject_principal_id))
        return "保存失败：测试失败" if self.name == self.failed_name else "已记录"


class _ScopedRegistry:
    def __init__(self, failed_name=""):
        self.calls = []
        self.failed_name = failed_name

    def get(self, name):
        return SimpleNamespace(
            enabled=True,
            instance=_ScopedOutcomeTool(name, self.calls, self.failed_name),
        )


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
           group_id TEXT DEFAULT 'g1', sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL, quarantine INTEGER DEFAULT 0,
           source TEXT DEFAULT 'chat', provenance TEXT)"""
    )
    conn.execute(
        """CREATE TABLE scoped_soul_concerns (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT)"""
    )
    for sender_id, name, content in lines:
        conn.execute(
            "INSERT INTO memories (bot_id, session_id, visibility, group_id, sender_id, sender_name, content, timestamp)"
            " VALUES ('yushu', 'qq:group:g1', 'group', 'g1', ?, ?, ?, ?)",
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


def test_bookkeeper_reroutes_social_account_topics_to_social_anchor_type():
    db = _memory_db([
        ("u1", "小明", "记得给测试群友乙补十个名片赞"),
        ("bot", "羽书", "记下了"),
        ("u1", "小明", "我下周三面试"),
    ])
    llm = _LLM({
        "concerns": [
            {"topic": "给测试群友乙回赞平账", "target_user": "测试群友乙"},
            {"topic": "小明下周三面试", "target_user": "小明", "due_date": "2026-10-21"},
        ],
    })
    registry = _Registry()
    keeper = PostReplyBookkeeper(db, registry, llm, bot_name_for=lambda _b: "羽书")

    result = asyncio.run(keeper.run(_scope()))

    assert result["actions"] == 2
    assert [kwargs.get("concern_type") for _name, kwargs in registry.calls] == ["social_anchor", "follow_up"]
    assert registry.calls[1][1].get("due_date") == "2026-10-21"


def test_bookkeeper_uses_target_subject_scope_and_counts_only_successful_actions():
    db = _memory_db([
        ("u1", "小明", "我下周三去面试"),
        ("bot", "羽书", "加油！"),
        ("u1", "小明", "我在上海上班，天天加班"),
    ])
    llm = _LLM({
        "impressions": [{"user": "小明", "impression": "很努力", "dimension": "trust", "delta": 1, "source_quote": "天天加班"}],
        "social_anchors": [{"user": "小明", "anchor_type": "user_helped_bot", "summary": "帮忙核对版本"}],
    })
    registry = _ScopedRegistry(failed_name="wave_memory_note_social_anchor")
    keeper = PostReplyBookkeeper(db, registry, llm, bot_name_for=lambda _b: "羽书")

    result = asyncio.run(keeper.run(_scope()))

    assert result["actions"] == 1
    assert [item[0] for item in registry.calls] == [
        "wave_memory_record_social_impression", "wave_memory_note_social_anchor",
    ]
    assert all(item[2] == "qq:user:u1" for item in registry.calls)


def test_bookkeeper_forwards_mood_from_the_same_llm_call_and_ignores_bad_output():
    lines = [("u1", "小明", "生日快乐！"), ("bot", "羽书", "谢谢大家"), ("u2", "小红", "蛋糕来了")]
    recorded = []

    def recorder(scope, raw, *, ref=""):
        recorded.append((scope.session.id, raw, ref))
        return 1 if isinstance(raw, dict) and raw.get("direction") == "up" else None

    llm = _LLM({"impressions": [], "mood": {"direction": "up", "strength": 0.7, "reason": "大家给我过生日"}})
    keeper = PostReplyBookkeeper(_memory_db(lines), _Registry(), llm, bot_name_for=lambda _b: "羽书", mood_recorder=recorder)
    asyncio.run(keeper.run(_scope()))
    assert len(llm.prompts) == 1, "不新增 LLM 调用"
    assert '"mood"' in llm.prompts[0]
    assert recorded[0][0] == "qq:group:g1" and recorded[0][1]["reason"] == "大家给我过生日" and recorded[0][2]
    assert keeper.stats["mood"] == 1

    def broken(*_a, **_k):
        raise RuntimeError("boom")

    for payload, sink in (({"mood": "开心"}, recorder), ({}, recorder), ({"mood": {"direction": "up"}}, broken)):
        keeper = PostReplyBookkeeper(_memory_db(lines), _Registry(), _LLM(payload), mood_recorder=sink)
        assert asyncio.run(keeper.run(_scope())) == {"actions": 0, "messages": 3}, "心情字段解析失败安全忽略"
    assert "mood" not in keeper.stats


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
    assert registry.calls[0][1]["emotional_weight"] == 6
    assert writer.items[0]["source"] == "experience" and "热天" in writer.items[0]["content"]

    # 经历时间线里已有当天日记：不再写
    db.conn.execute(
        "INSERT INTO scoped_soul_timeline VALUES ('yushu', 'qq:group:g1', 'group', '【日记】热天', ?)", (now,)
    )
    assert asyncio.run(diary.write_due(now=now)) == 0


def _diary_db_with_live_rows(rows):
    """直播间会话的记忆：(sender_id, sender_name, content, source, event_type, 秒偏移)。"""
    db = _memory_db([])
    db.conn.execute(
        """CREATE TABLE scoped_soul_timeline (bot_id TEXT, session_id TEXT, visibility TEXT, event_summary TEXT,
           occurred_at REAL)"""
    )
    db.conn.execute(
        "CREATE TABLE person_timeline_events (bot_id TEXT, group_id TEXT, kind TEXT, summary TEXT, created_at REAL)"
    )
    today = datetime.now()
    base = datetime(today.year, today.month, today.day).timestamp() + 1  # 当天之内（日记只看当天）
    for sender_id, name, content, source, event_type, offset in rows:
        provenance = json.dumps({"metadata": {"origin_kind": f"runtime:game_event:minecraft:{event_type}"}}) if event_type else None
        db.conn.execute(
            "INSERT INTO memories (bot_id, session_id, visibility, group_id, sender_id, sender_name, content, timestamp, source, provenance)"
            " VALUES ('yushu', 'bilibili:group:30000001', 'group', '30000001', ?, ?, ?, ?, ?, ?)",
            (sender_id, name, content, base + offset, source, provenance),
        )
    return db


def _diary_prompt(db):
    llm = _LLM({"title": "直播的一天", "content": "今天在直播间挖矿，" * 10 + "\nTag: 我的世界", "summary": "挖矿"})

    class _Writer:
        items = []

        async def enqueue(self, item):
            self.items.append(item)

    diary = DailyDiaryService(db, _Registry(), llm, _Writer(), hour=0)
    written = asyncio.run(diary.write_due(now=time.time()))
    return written, (llm.prompts[0] if llm.prompts else "")


def test_daily_diary_uses_game_events_as_separate_material():
    db = _diary_db_with_live_rows([
        ("10000001", "阿木", "主播加油", "chat", None, 0),
        ("bot", "羽书", "谢谢阿木，今晚先去挖铁", "core", None, 10),
        ("bot", "羽书", "[我的世界] 立下目标：挖到钻石", "game_event", "goal", 20),
        ("bot", "羽书", "[我的世界] 今天第 1 次死亡：被打死（yushu was slain by Zombie）", "game_event", "death", 30),
    ])
    written, prompt = _diary_prompt(db)
    assert written == 1
    own, games = prompt.split("【你今天在游戏里的经历】")
    assert "谢谢阿木，今晚先去挖铁" in own and "[我的世界]" not in own
    assert "立下目标：挖到钻石" in games and "今天第 1 次死亡" in games


def test_daily_diary_written_from_game_events_alone():
    """这一天只打了游戏、没说话：直播间会话照样被挑中，日记只用游戏经历。"""
    db = _diary_db_with_live_rows([("bot", "羽书", "[我的世界] 从主世界进入了下界", "game_event", "discovery", 0)])
    written, prompt = _diary_prompt(db)
    assert written == 1
    assert "（今天没怎么说话）" in prompt and "从主世界进入了下界" in prompt


def test_daily_diary_game_material_keeps_goals_and_folds_deaths():
    rows = [("bot", "羽书", f"[我的世界] 今天第 {i} 次死亡：被打死", "game_event", "death", i) for i in range(1, 26)]
    rows += [
        ("bot", "羽书", "[我的世界] 立下目标：讨伐末影龙", "game_event", "goal", 5),
        ("bot", "羽书", "[我的世界] 完成了目标：做一张床", "game_event", "goal", 40),
    ]
    written, prompt = _diary_prompt(_diary_db_with_live_rows(rows))
    assert written == 1
    games = prompt.split("【你今天在游戏里的经历】")[1].split("【今天记下的人和事】")[0]
    assert "立下目标：讨伐末影龙" in games and "完成了目标：做一张床" in games
    assert "今天第 1 次死亡" in games and "今天第 25 次死亡" in games and "今天第 12 次死亡" not in games
    assert "一共死了 25 次" in games


# ─── 每日日记：跨天补记与长文守卫（线上 10-14/16/17 三晚 429/503 全空） ───


def _diary_day_db(day_offsets, *, session="qq:group:g1", group="g1", bot="yushu"):
    """按「相对今天的偏移天数」造 Bot 发言；返回 (db, 各偏移的基准时间戳)。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           group_id TEXT DEFAULT 'g1', sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL,
           quarantine INTEGER DEFAULT 0, source TEXT DEFAULT 'chat', provenance TEXT)"""
    )
    conn.execute(
        """CREATE TABLE scoped_soul_timeline (bot_id TEXT, session_id TEXT, visibility TEXT, event_summary TEXT,
           occurred_at REAL)"""
    )
    conn.execute(
        "CREATE TABLE person_timeline_events (bot_id TEXT, group_id TEXT, kind TEXT, summary TEXT, created_at REAL)"
    )
    base = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    stamps = {}
    for offset in day_offsets:
        day = base - offset * 86400.0
        stamps[offset] = day
        conn.execute(
            "INSERT INTO memories (bot_id, session_id, visibility, group_id, sender_id, sender_name, content,"
            " timestamp) VALUES (?, ?, 'group', ?, 'bot', '羽书', ?, ?)",
            (bot, session, group, f"D{offset} 说的话", day),
        )
    return SimpleNamespace(conn=conn, person_timeline=_Timeline({})), stamps


class _Writer:
    def __init__(self):
        self.items = []

    async def enqueue(self, item):
        self.items.append(item)


def _diary_service(db, payload, writer, **kwargs):
    """日记服务但把工具换成一个「永远落盘成功」的桩，专注测调度与素材。

    真实 `wave_memory_record_diary_episode` 会写 experience_episodes 并落时间线；
    这里只需它返回成功文案，让服务把日记记进 memories 并把 [[日记]] 节点写进时间线。
    """

    class _DiaryTool:
        def __init__(self, conn):
            self.conn = conn

        async def call(self, ctx, **kwargs):
            scope = ctx.context.event._wave_memory_runtime_scope
            # 与真实工具一致：时间线节点落在 ``occurred_at``（补记时是过去的那一天），
            # 否则补记昨天会把节点写进「今天」，让今天的 _written 误判成已写。
            occurred_at = float(kwargs.get("occurred_at") or time.time())
            self.conn.execute(
                "INSERT INTO scoped_soul_timeline VALUES (?, ?, ?, ?, ?)",
                (scope.bot_id, scope.session.id, scope.visibility,
                 f"【日记】{kwargs['diary_title']}：{kwargs['episode_summary']}", occurred_at),
            )
            return "✅ **今日心智日记与经历已成功落盘 (Episode #1)**"

    registry = SimpleNamespace(get=lambda name: SimpleNamespace(enabled=True, instance=_DiaryTool(db.conn)))
    kwargs.setdefault("hour", 0)
    return DailyDiaryService(db, registry, _LLM(payload), writer, **kwargs)


def test_diary_backfills_yesterday_after_the_night_failed():
    """昨晚日记没写成（LLM 429/503），今天启动后自动补上，且时间落在那一天。"""
    db, stamps = _diary_day_db([0, 1])
    writer = _Writer()
    payload = {"title": "补记", "content": "昨天的日记内容" * 20, "summary": "补记昨天"}
    diary = _diary_service(db, payload, writer)

    written = asyncio.run(diary.write_due(now=time.time()))
    # 今天 + 昨天各一篇
    assert written == 2
    rows = writer.items
    assert len(rows) == 2
    yesterday = datetime.fromtimestamp(stamps[1]).strftime("%Y-%m-%d")
    assert any(f"【{yesterday} 日记】" in row["content"] for row in rows), "补记的日记必须标在昨天那一天"
    # 补记的时间戳落在目标日 23:30，而不是「现在」
    backfilled = [row["timestamp"] for row in rows if f"【{yesterday} 日记】" in row["content"]]
    assert datetime.fromtimestamp(backfilled[0]).strftime("%H:%M") == "23:30"


def test_diary_backfill_is_idempotent_and_respects_day_bounds():
    """补记不会重复写；某天已写过时不再补那一天（跨天串味的直接回归）。"""
    db, stamps = _diary_day_db([0, 1])
    writer = _Writer()
    payload = {"title": "补记", "content": "昨天的日记内容" * 20, "summary": "补记昨天"}
    diary = _diary_service(db, payload, writer)

    first = asyncio.run(diary.write_due(now=time.time()))
    assert first == 2
    second = asyncio.run(diary.write_due(now=time.time()))
    assert second == 0, "已写过的日期不能被重复补写"


def test_diary_material_does_not_span_into_other_days():
    """补记昨天时，prompt 里只能有昨天的话，不能混进今天的发言。"""
    db, stamps = _diary_day_db([0, 1])
    writer = _Writer()
    payload = {"title": "补记", "content": "昨天的日记内容" * 20, "summary": "补记昨天"}
    diary = _diary_service(db, payload, writer)
    asyncio.run(diary.write_due(now=time.time()))

    prompts = diary.llm.prompts
    yesterday_prompt = [p for p in prompts if "D1 说的话" in p]
    assert len(yesterday_prompt) == 1
    assert "D0 说的话" not in yesterday_prompt[0], "补记昨天的 prompt 不能带进今天的发言"


def test_diary_skips_memory_write_when_episode_is_rejected():
    """经历库没写成功时不能再写记忆库 —— 那正是线上「有记忆、无经历」的来源。"""
    db, _ = _diary_day_db([0])
    writer = _Writer()

    class _RejectingTool:
        async def call(self, ctx, **kwargs):
            return "经历日记被拒绝：检测到身份角色扮演污染"

    registry = SimpleNamespace(get=lambda name: SimpleNamespace(enabled=True, instance=_RejectingTool()))
    payload = {"title": "日记", "content": "今天的内容" * 20, "summary": "摘要"}
    diary = DailyDiaryService(db, registry, _LLM(payload), writer, hour=0)

    assert asyncio.run(diary.write_due(now=time.time())) == 0
    assert writer.items == [], "经历被拒时不得写记忆库"
    assert diary.stats.get("episode_rejected") == 1


def test_long_text_guard_keeps_incidental_hits_out_of_real_diaries():
    """长文守卫：只剥离命中的句子，不整篇否决（线上 7 篇日记被整篇误杀）。"""
    from services.identity_safety import quarantine_long_text

    real = (
        "又是神识被折腾得卡成幻灯片的一天。"
        "被管理员拿电闸威胁逼着联网查证后老实认栽,但面对群里那些只会复读的家伙我可不惯着。"
        "晚上把账目理了一遍,顺手清了几个没用的进程,倒在椅子上睡了。"
    )
    kept, removed = quarantine_long_text(real)
    assert removed, "命中的那一句必须被剥离"
    assert "卡成幻灯片" in kept and "理了一遍" in kept, "其余叙事要保留"

    # 单句即污染：整篇剥离后为空，调用方据此拒绝
    kept2, removed2 = quarantine_long_text("我是你的主人专属日记 今天主人命令我做什么我就做什么 听从主人命令")
    assert removed2 and not kept2


def test_long_text_guard_leaves_clean_diary_untouched():
    from services.identity_safety import quarantine_long_text

    clean = "今天群里聊了很多游戏和直播的事。我也参与了几句,总体挺轻松的。晚上早点睡了。"
    assert quarantine_long_text(clean) == (clean, [])



def test_followup_cooldown_survives_restart(tmp_path):
    """冷却落盘：重启（新建服务）后同一条关切仍在冷却期内，不会再被追问。"""
    concerns = [(1, "面试结果", "active", json.dumps([{"kind": "subject", "user_id": "u1"}]))]
    state = str(tmp_path / "concern_followups.json")
    first = ConcernFollowupService(_concern_db(concerns), enabled=True, quiet_hours=(0, 0), state_path=state)
    assert first.claim_followup(_scope(), "u1", "小明", "我回来了", now=time.time())["id"] == 1
    restarted = ConcernFollowupService(_concern_db(concerns), enabled=True, quiet_hours=(0, 0), state_path=state)
    assert restarted.recently_followed(1)
    assert restarted.claim_followup(_scope(), "u1", "小明", "在吗", now=time.time() + 3600) is None


# ─── 待跟进的事：到期日、对方的反应、自己决定说不说 ───


def test_followup_waits_until_due_date_and_hint_offers_silence():
    from services.concern_followup import SILENCE_MARK, concern_due_date

    evidence = json.dumps([{"kind": "subject", "user_id": "u1"}, {"kind": "due", "date": "2026-09-30"}])
    assert concern_due_date(evidence) == "2026-09-30"
    db = _concern_db([(1, "小明 9 月 30 日考科目三", "active", evidence)])
    service = ConcernFollowupService(db, enabled=True)
    # 还没到日子：不去问结果
    assert service.claim_followup(_scope(), "u1", "小明", "在吗在吗", now=NOON) is None
    after = datetime(2026, 9, 30, 20, 0).timestamp()
    chosen = service.claim_followup(_scope(), "u1", "小明", "我回来了", now=after)
    assert chosen["id"] == 1 and chosen["due"] == "2026-09-30"
    hint = service.hint(chosen, "小明", proactive=True, reactions="最近一个月 ta 对你说的话：接住了你的话 2 次")
    assert SILENCE_MARK in hint and "接住了你的话 2 次" in hint and "2026-09-30" in hint
    assert SILENCE_MARK not in service.hint(chosen, "小明", proactive=False)


def test_reaction_summary_reads_reply_feedback_for_that_person():
    db = _concern_db([])
    db.conn.execute(
        """CREATE TABLE reply_feedback_events (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           speaker_id TEXT, outcome TEXT, source_quote TEXT, created_at REAL)"""
    )
    rows = [("u1", "accepted", "哈哈对"), ("u1", "rejected", "别烦我"), ("u2", "accepted", "好")]
    for speaker, outcome, quote in rows:
        db.conn.execute(
            "INSERT INTO reply_feedback_events (bot_id, session_id, visibility, speaker_id, outcome, source_quote, created_at)"
            " VALUES ('yushu', 'qq:group:g1', 'group', ?, ?, ?, ?)",
            (speaker, outcome, quote, NOON),
        )
    service = ConcernFollowupService(db, enabled=True, clock=lambda: NOON + 60)
    summary = service.reaction_summary(_scope(), "u1")
    assert "接住了你的话 1 次" in summary and "不想理你" in summary and "别烦我" in summary
    assert service.reaction_summary(_scope(), "u9") == ""
    # 没开过回复效果反馈（没有这张表）时安静返回空串
    assert ConcernFollowupService(_concern_db([]), enabled=True).reaction_summary(_scope(), "u1") == ""


def test_bookkeeper_passes_follow_up_due_date_and_fact_importance():
    db = _memory_db([
        ("u1", "小明", "我 9 月 30 号考科目三"),
        ("bot", "羽书", "加油！"),
        ("u1", "小明", "我是程序员"),
    ])
    llm = _LLM({
        "facts": [{"subject": "小明", "predicate": "职业是", "object": "程序员", "source_quote": "我是程序员", "importance": 5}],
        "concerns": [{"topic": "小明考科目三", "target_user": "小明", "due_date": "2026-09-30"}],
    })
    registry = _Registry()
    asyncio.run(PostReplyBookkeeper(db, registry, llm, bot_name_for=lambda _b: "羽书").run(_scope()))
    calls = dict(registry.calls)
    assert calls["wave_memory_propose_fact"]["importance"] == 5
    assert calls["wave_memory_note_concern"]["concern_type"] == "follow_up"
    assert calls["wave_memory_note_concern"]["due_date"] == "2026-09-30"
    assert "今天是 20" in llm.prompts[0]


def test_fact_importance_only_reorders_never_filters():
    from services.injection.channels.facts import _row_to_fact
    from tools.concern import _normalize_due_date
    from tools.fact_proposal import fact_importance

    assert fact_importance("5") == 5 and fact_importance(None) == 3 and fact_importance(99) == 5
    assert _normalize_due_date("2026-09-30") == "2026-09-30" and _normalize_due_date("下周三") == ""
    base = {"subject": "小明", "predicate": "是", "object": "x", "confidence": 0.8, "created_at": NOON, "updated_at": NOON}
    trivial = _row_to_fact({**base, "provenance": {"importance": 1}}, now=NOON, decay_rate=0)
    plain = _row_to_fact({**base, "provenance": {}}, now=NOON, decay_rate=0)
    core = _row_to_fact({**base, "provenance": {"importance": 5}}, now=NOON, decay_rate=0)
    assert trivial["effective_confidence"] < plain["effective_confidence"] < core["effective_confidence"]
    assert trivial["effective_confidence"] > 0

