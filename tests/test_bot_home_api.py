"""Bot 主页聚合 API：以 Bot 为中心的只读汇总。

锁定的行为：
- 窗口内记忆计数/按群分布/最活跃发言人（昵称经 sanitize_display_name 清洗，排除 Bot 自己）；
- 高光记忆 = Bot 自己说的话、source=core 或 importance>1，排除隔离与噪声；
- 心情/关切复用 ScopedSoulRepository.get_state，已了结关切不算「主要关切」；
- 关系变化按窗口内事件聚合，噪声事件不计；
- trace 按 ``bot_profile_id = ? OR bot_id = ?`` 匹配（AstrBot 写入的 bot_id 是 QQ 号）；
- 缺表只让对应区块 unavailable，不拖垮整页；参数错误 400、未知 Bot 404、库不可用 503。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.injection.trace_store import InjectionTraceStore
from webui.blueprints.bot_home import bot_home_bp, build_bot_home_payload, parse_bot_home_args, BotHomeInputError
from webui.container import get_container

NOW = 1_800_000_000.0
HOUR = 3600.0
SESSION_A = "羽书:group:111"
SESSION_B = "羽书:group:222"
GROUP_NAMES = {"111": "3-5层群"}


def _scope(session_id: str, subject: str | None = None) -> RuntimeScope:
    platform, kind, conversation = session_id.split(":", 2)
    return RuntimeScope("yushu", "group", SessionRef(session_id, platform, kind, conversation), subject_principal_id=subject)


def _memory(conn, *, session_id, sender_id, sender_name, content, ts, bot_id="yushu", source="chat",
            importance=1.0, quarantine=0, memory_type="message"):
    conn.execute(
        """INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp, importance,
                                 memory_type, source, bot_id, session_id, visibility, quarantine, resolution_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'group', ?, 'resolved')""",
        (session_id.rsplit(":", 1)[-1], sender_id, sender_name, content, ts, importance,
         memory_type, source, bot_id, session_id, quarantine),
    )


@pytest.fixture
def seeded(tmp_path):
    path = str(tmp_path / "bot_home.sqlite")
    db = WaveMemoryDB(path, dimension=3)
    repo = db.soul_repository
    # ── Soul：心情与关切（复用正式仓储写入）
    repo.upsert_mood(_scope(SESSION_B), valence=-0.2, arousal=0.3, cause="早些时候有点闷", observed_at=NOW - 5 * HOUR)
    repo.upsert_mood(_scope(SESSION_A), valence=0.4, arousal=0.7, cause="群里很热闹", observed_at=NOW - HOUR)
    repo.replace_concerns(_scope(SESSION_A), concerns=[
        {"topic": "期末考试周", "intensity": 0.9, "urgency": 0.8, "concern_type": "event", "created_at": NOW - 2 * HOUR},
        {"topic": "已经解决的事", "intensity": 0.95, "status": "resolved", "created_at": NOW - 2 * HOUR},
    ])
    # ── 关系事件：u1 在窗口内两次加分，u2 一次；窗口外与其他 Bot 的不计
    repo.record_relationship_event(_scope(SESSION_A, "羽书:user:u1"), event_type="joke", dimension="fun",
                                   delta=3.0, reason="一起玩梗", created_at=NOW - 3 * HOUR)
    repo.record_relationship_event(_scope(SESSION_A, "羽书:user:u1"), event_type="deep_talk", dimension="trust",
                                   delta=4.0, reason="聊到很晚", created_at=NOW - 2 * HOUR)
    repo.record_relationship_event(_scope(SESSION_B, "羽书:user:u2"), event_type="joke", dimension="fun",
                                   delta=1.0, reason="小玩笑", created_at=NOW - HOUR)
    repo.record_relationship_event(_scope(SESSION_B, "羽书:user:u3"), event_type="joke", dimension="fun",
                                   delta=5.0, reason="很久以前", created_at=NOW - 48 * HOUR)
    InjectionTraceStore(db.conn).ensure_schema()

    conn = sqlite3.connect(path)
    # ── 记忆
    garbled = "\n\x11\x12\x0f猫猫副队长\n\t\n\x07$ÿĀ\x11\x10\x10\x00"
    for i in range(5):
        _memory(conn, session_id=SESSION_A, sender_id="u1", sender_name=garbled, content=f"u1 说 {i}", ts=NOW - HOUR - i)
    for i in range(2):
        _memory(conn, session_id=SESSION_B, sender_id="u2", sender_name="小红", content=f"u2 说 {i}", ts=NOW - 2 * HOUR - i)
    _memory(conn, session_id=SESSION_A, sender_id="bot", sender_name="羽书", content="我是羽书，我记住了", ts=NOW - 30, source="core")
    _memory(conn, session_id=SESSION_A, sender_id="u2", sender_name="小红", content="很重要的一句话", ts=NOW - 40, importance=1.5)
    _memory(conn, session_id=SESSION_A, sender_id="u1", sender_name="小明", content="被隔离的", ts=NOW - 50, quarantine=1, source="core")
    _memory(conn, session_id=SESSION_A, sender_id="u1", sender_name="小明", content="窗口外", ts=NOW - 30 * HOUR, source="core")
    _memory(conn, session_id="白真真:group:111", sender_id="u9", sender_name="别家", content="别的 Bot", ts=NOW - 10, bot_id="baizz", source="core")
    _memory(conn, session_id=SESSION_A, sender_id="u2", sender_name="小红", content="嗯", ts=NOW - 60, source="noise")
    # ── 噪声关系事件（validate_event 不允许写，直接落表模拟历史残留）
    conn.execute(
        """INSERT INTO scoped_soul_relationship_events
               (bot_id, session_id, visibility, subject_principal_id, event_type, dimension, delta, reason, revision, created_at)
           VALUES ('yushu', ?, 'group', '羽书:user:u4', 'message_seen', 'familiarity', 9.0, '看见一条群友消息', 1, ?)""",
        (SESSION_A, NOW - HOUR),
    )
    # ── 学到的
    for i, status in enumerate(("pending", "active", "pending", "rejected")):
        conn.execute(
            """INSERT INTO scoped_facts (bot_id, session_id, visibility, subject, predicate, object, status, created_at, updated_at)
               VALUES ('yushu', ?, 'group', '小明', ?, ?, ?, ?, ?)""",
            (SESSION_A, f"喜欢{i}", f"东西{i}", status, NOW - HOUR * (i + 1), NOW),
        )
    conn.execute(
        """INSERT INTO scoped_facts (bot_id, session_id, visibility, subject, predicate, object, status, created_at, updated_at)
           VALUES ('yushu', ?, 'group', '旧', '事实', '窗口外', 'active', ?, ?)""",
        (SESSION_A, NOW - 40 * HOUR, NOW),
    )
    conn.execute(
        """INSERT INTO scoped_jargon (bot_id, session_id, visibility, word, meaning, status, created_at, updated_at)
           VALUES ('yushu', ?, 'group', '器灵', '羽书的外号', 'pending', ?, ?)""",
        (SESSION_A, NOW - HOUR, NOW),
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS experience_episodes (
               id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, group_id TEXT NOT NULL,
               user_id TEXT, episode_type TEXT NOT NULL, trigger_text TEXT, bot_inner_thought TEXT,
               bot_action TEXT, bot_reply TEXT, user_reaction TEXT, outcome TEXT,
               source_memory_ids TEXT DEFAULT '[]', emotional_weight REAL DEFAULT 0, created_at REAL NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO experience_episodes (bot_id, group_id, episode_type, trigger_text, created_at) VALUES ('yushu', '222', 'shared_event', '一起看了流星雨', ?)",
        (NOW - HOUR,),
    )
    # ── trace：AstrBot 写 QQ 号到 bot_id，Cortico 写 db_id；另一个 Bot 的不算
    traces = [
        ("t-astrbot", NOW - 100, "111", "2500447291", "yushu", 120.5, None),
        ("t-cortico", NOW - 50, "222", "yushu", "yushu", 88.0,
         '{"source": "cortico", "runtime_scope": {"payload": {"visibility": "group", "session": {"id": "羽书:group:222"}}}}'),
        ("t-other", NOW - 10, "111", "999", "baizz", 10.0, None),
    ]
    for trace_id, ts, group_id, bot_col, profile, latency, metadata in traces:
        conn.execute(
            """INSERT INTO injection_traces (trace_id, timestamp, mode, group_id, sender_id, sender_name, bot_id,
                                             bot_profile_id, message_preview, total_latency_ms, status, metadata_json)
               VALUES (?, ?, 'full', ?, 'u1', '小明', ?, ?, '你还记得吗', ?, 'ok', ?)""",
            (trace_id, ts, group_id, bot_col, profile, latency, metadata),
        )
        conn.execute(
            "INSERT INTO injection_trace_channels (trace_id, channel, status, item_count, tokens) VALUES (?, 'memory', 'hit', 3, 40)",
            (trace_id,),
        )
        conn.execute(
            "INSERT INTO injection_trace_channels (trace_id, channel, status, item_count, tokens) VALUES (?, 'jargon', 'empty', 0, 0)",
            (trace_id,),
        )
    conn.commit()
    conn.close()
    yield db
    db.close()


def _build(db, **overrides):
    kwargs = dict(
        bot_id="yushu",
        days=1,
        now=NOW,
        bot_name="羽书",
        self_ids=["2500447291"],
        platform_ids=["羽书"],
        soul_repository=db.soul_repository,
        group_name_resolver=lambda bot_id, group_id: GROUP_NAMES.get(group_id),
    )
    kwargs.update(overrides)
    return build_bot_home_payload(db.conn, **kwargs)


def test_memories_section_counts_groups_speakers_and_highlights(seeded):
    payload = _build(seeded)
    memories = payload["memories"]
    assert memories["status"] == "ready"
    # 5 + 2 + bot + 重要 + noise = 10；隔离 1 条单独计；窗口外与别家 Bot 不计。
    assert memories["total"] == 10
    assert memories["quarantined"] == 1
    assert memories["by_source"] == {"chat": 8, "core": 1, "noise": 1}

    top = memories["top_groups"][0]
    assert (top["session_id"], top["group_name"], top["label"], top["count"]) == (SESSION_A, "3-5层群", "3-5层群", 8)
    assert memories["top_groups"][1]["label"] == "222"  # 拿不到群名就显示群号

    speakers = memories["top_speakers"]
    assert [s["sender_id"] for s in speakers] == ["u1", "u2"]  # Bot 自己不算发言人
    assert speakers[0]["display_name"] == "猫猫副队长"  # 残片昵称被清洗
    assert speakers[0]["count"] == 5

    highlights = memories["highlights"]
    assert [h["content"] for h in highlights] == ["我是羽书，我记住了", "很重要的一句话"]
    assert highlights[0]["reason"] == "self" and highlights[0]["is_self"] is True
    assert highlights[1]["reason"] == "important"
    assert highlights[0]["session_id"] == SESSION_A and highlights[0]["group_name"] == "3-5层群"


def test_soul_section_reuses_scoped_state(seeded):
    soul = _build(seeded)["soul"]
    assert soul["status"] == "ready"
    mood = soul["mood"]
    assert (mood["valence"], mood["arousal"], mood["cause"]) == (0.4, 0.7, "群里很热闹")
    assert mood["session_id"] == SESSION_A and mood["group_name"] == "3-5层群"
    assert [c["topic"] for c in soul["concerns"]] == ["期末考试周"]  # 已解决的不算
    assert soul["concerns"][0]["urgency"] == 0.8


def test_soul_section_without_repository_is_unavailable(seeded):
    soul = _build(seeded, soul_repository=None)["soul"]
    assert soul == {"status": "unavailable", "reason_code": "soul_scoped_repository_unavailable"}


def test_relationship_changes_rank_by_affinity_shift_and_skip_noise(seeded):
    rel = _build(seeded)["relationships"]
    assert rel["status"] == "ready"
    items = rel["items"]
    assert [item["user_id"] for item in items] == ["u1", "u2"]  # u3 窗口外、u4 噪声
    first = items[0]
    assert first["dimensions"] == {"fun": 3.0, "trust": 4.0}
    assert first["event_count"] == 2
    assert first["latest_reason"] == "聊到很晚"
    assert first["display_name"] == "猫猫副队长"
    assert first["affinity_after"] is not None and first["affinity_before"] is not None
    assert first["affinity_after"] >= first["affinity_before"]
    assert first["group_name"] == "3-5层群"
    assert rel["people_changed"] == 2


def test_learned_section_counts_and_samples(seeded):
    learned = _build(seeded)["learned"]
    facts = learned["facts"]
    assert facts["total"] == 4
    assert facts["by_status"] == {"pending": 2, "active": 1, "rejected": 1}
    assert len(facts["samples"]) == 3
    assert facts["samples"][0]["text"] == "小明 喜欢0 东西0"
    assert facts["samples"][0]["status"] == "pending"
    assert learned["jargon"]["samples"][0]["text"] == "器灵：羽书的外号"
    assert learned["beliefs"]["total"] == 0 and learned["beliefs"]["samples"] == []
    episode = learned["experiences"]["samples"][0]
    assert (episode["text"], episode["status"], episode["session_id"]) == ("一起看了流星雨", "recorded", SESSION_B)


def test_replies_match_profile_id_or_bot_id_and_list_hit_channels(seeded):
    replies = _build(seeded)["replies"]
    items = replies["items"]
    assert [item["trace_id"] for item in items] == ["t-cortico", "t-astrbot"]
    cortico, astrbot = items
    assert cortico["session_id"] == "羽书:group:222" and cortico["source"] == "cortico"
    assert astrbot["session_id"] == SESSION_A and astrbot["group_name"] == "3-5层群"
    assert astrbot["hit_channels"] == [{"channel": "memory", "item_count": 3, "tokens": 40}]
    assert astrbot["latency_ms"] == 120.5


def test_missing_tables_only_degrade_their_section(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "bare.sqlite"), dimension=3)
    try:
        payload = build_bot_home_payload(db.conn, bot_id="yushu", now=NOW, soul_repository=db.soul_repository)
    finally:
        db.close()
    assert payload["memories"]["status"] == "ready" and payload["memories"]["total"] == 0
    assert payload["soul"]["status"] == "ready" and payload["soul"]["mood"] is None
    assert payload["relationships"]["items"] == []
    assert payload["learned"]["experiences"]["status"] == "unavailable"
    assert payload["replies"] == {"status": "unavailable", "reason_code": "trace_store_missing"}
    assert set(payload["meta"]["section_ms"]) == {"memories", "soul", "relationships", "learned", "replies"}


def test_section_failure_is_isolated(seeded):
    class Broken:
        def get_state(self, *args, **kwargs):
            raise RuntimeError("boom")

    payload = _build(seeded, soul_repository=Broken())
    assert payload["soul"] == {"status": "error", "reason_code": "soul_read_failed"}
    assert payload["memories"]["status"] == "ready"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, "bot_id_required"),
        ({"bot_id": "2500447291"}, "invalid_bot_id"),
        ({"bot_id": "default"}, "invalid_bot_id"),
        ({"bot_id": "yushu", "days": "0"}, "invalid_days"),
        ({"bot_id": "yushu", "days": "8"}, "invalid_days"),
        ({"bot_id": "yushu", "days": "abc"}, "invalid_days"),
    ],
)
def test_parse_args_rejects_bad_input(args, code):
    with pytest.raises(BotHomeInputError) as excinfo:
        parse_bot_home_args(args)
    assert excinfo.value.code == code


def test_parse_args_defaults_to_one_day():
    assert parse_bot_home_args({"bot_id": "yushu"}) == ("yushu", 1)
    assert parse_bot_home_args({"bot_id": "yushu", "days": "7"}) == ("yushu", 7)


class _Registry:
    def __init__(self, profiles):
        self.profiles = profiles

    def get_any(self, db_id):
        return self.profiles.get(db_id)


@pytest.fixture
def api(seeded):
    container = get_container()
    saved = (container.db, container.bot_registry, container.soul_repository, container.password)
    container.db = seeded
    container.soul_repository = None
    container.password = ""
    container.bot_registry = _Registry({
        "yushu": SimpleNamespace(name="羽书", self_ids=["2500447291"], platform_ids=["羽书"]),
    })
    app = Quart(__name__)
    app.register_blueprint(bot_home_bp)
    yield app.test_client()
    container.db, container.bot_registry, container.soul_repository, container.password = saved


@pytest.mark.asyncio
async def test_endpoint_returns_payload(api):
    res = await api.get("/api/bot-home?bot_id=yushu&days=7")
    assert res.status_code == 200
    data = await res.get_json()
    assert data["bot"] == {"db_id": "yushu", "name": "羽书"}
    assert data["window"]["days"] == 7
    for key in ("memories", "soul", "relationships", "learned", "replies"):
        assert data[key]["status"] in {"ready", "unavailable"}, (key, data[key])
    # 容器里没有 soul_repository 时回落到 db.soul_repository
    assert data["soul"]["status"] == "ready"


@pytest.mark.asyncio
async def test_endpoint_errors(api):
    res = await api.get("/api/bot-home")
    assert res.status_code == 400
    assert (await res.get_json())["error"]["code"] == "bot_id_required"

    res = await api.get("/api/bot-home?bot_id=nobody")
    assert res.status_code == 404
    assert (await res.get_json())["error"]["code"] == "bot_not_found"

    container = get_container()
    db = container.db
    container.db = None
    try:
        res = await api.get("/api/bot-home?bot_id=yushu")
        assert res.status_code == 503
    finally:
        container.db = db
