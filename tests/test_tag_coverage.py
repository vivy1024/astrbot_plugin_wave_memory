"""标签覆盖率：活跃记忆按类互斥拆分；有效覆盖率去掉按设计不打的；重新排队只动可重试的类。"""

from __future__ import annotations

import sqlite3
import sys
import types
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

import pytest

from services.tag_coverage import build_tag_coverage, requeue_ids

LONG = "这是一条足够长、应该被打标签的群聊消息"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE memories (id INTEGER PRIMARY KEY, group_id TEXT, content TEXT, timestamp REAL,
            bot_id TEXT, session_id TEXT, visibility TEXT, memory_type TEXT, source TEXT, quarantine INTEGER);
        CREATE TABLE scoped_memory_tags (bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER);
        CREATE TABLE memory_tags (memory_id INTEGER, tag_id INTEGER);
        CREATE TABLE tag_extraction_status (memory_id INTEGER PRIMARY KEY, status TEXT, attempts INTEGER,
            last_error TEXT, last_run_at REAL, updated_at REAL);
    """)
    rows = [
        # id, content, bot, session, vis, type, quarantine
        (1, LONG, "yushu", "羽书:group:1", "group", "message", 0),   # 正式标签
        (2, LONG, "", "", "", "message", 0),                          # 旧标签
        (3, "短", "yushu", "羽书:group:1", "group", "message", 0),    # 太短
        (4, LONG, "yushu", "羽书:group:1", "group", "message", 0),    # skipped
        (5, LONG, "yushu", "羽书:group:1", "group", "message", 0),    # failed（已用尽）
        (6, LONG, "yushu", "羽书:group:1", "group", "message", 0),    # done 但标签丢了
        (7, LONG, "yushu", "羽书:group:1", "group", "message", 0),    # 待处理
        (8, LONG, "yushu", "羽书:private:9", "private", "message", 0),  # 私聊：不在范围
        (9, LONG, "yushu", "羽书:group:1", "group", "noise", 0),      # 噪声：不计入
        (10, LONG, "yushu", "羽书:group:1", "group", "message", 1),   # 隔离：不计入
        (11, LONG, "baizz", "白真真:group:1", "group", "message", 0),  # 标签挂在别的作用域下 → 仍算丢失
    ]
    for rid, content, bot, session, vis, mtype, quarantine in rows:
        conn.execute("INSERT INTO memories VALUES (?, '1', ?, 1.0, ?, ?, ?, ?, 'chat', ?)",
                     (rid, content, bot, session, vis, mtype, quarantine))
    conn.execute("INSERT INTO scoped_memory_tags VALUES ('yushu', '羽书:group:1', 'group', 1, 100)")
    conn.execute("INSERT INTO scoped_memory_tags VALUES ('yushu', '羽书:group:1', 'group', 11, 100)")
    conn.execute("INSERT INTO memory_tags VALUES (2, 100)")
    conn.executemany("INSERT INTO tag_extraction_status VALUES (?, ?, ?, NULL, 0, ?)", [
        (4, "skipped", 0, 9e9), (5, "failed", 5, 9e9), (6, "done", 0, 9e9), (11, "done", 0, 0),
    ])
    return conn


def test_categories_are_exclusive_and_exclude_inactive():
    report = build_tag_coverage(_db(), worker_status={"batch_size": 100, "interval_seconds": 300})
    assert report["counts"] == {
        "tagged": 2, "too_short": 1, "skipped": 1, "failed": 1, "lost": 2, "pending": 1, "not_eligible": 1,
    }
    assert report["active_memories"] == 9
    assert report["coverage"] == pytest.approx(2 / 9, abs=1e-4)
    # 该打的：tagged + failed + lost + pending = 6
    assert report["effective_coverage"] == pytest.approx(2 / 6, abs=1e-4)
    assert report["failed_exhausted"] == 1
    assert report["backlog"] == 3  # pending 1 + lost 2（失败已用尽不算）
    assert report["worker_capacity_per_hour"] == 1200.0
    assert report["by_bot"]["yushu"]["tagged"] == 1 and report["by_bot"]["(无归属旧行)"]["tagged"] == 1
    assert [s["id"] for s in report["samples"]["lost"]] == [11, 6]
    assert report["throughput"]["last_24h"] == 2  # 只数最近更新的 done/skipped（4、6），11 太旧、5 是失败


def test_requeue_ids_only_for_retryable_categories():
    conn = _db()
    assert requeue_ids(conn, "lost") == [11, 6]
    assert requeue_ids(conn, "skipped") == [4]
    with pytest.raises(ValueError):
        requeue_ids(conn, "pending")


def test_tag_worker_requeue_clears_status_and_wake_sets_event():
    import asyncio

    from services.tag_worker import TagWorker

    conn = _db()
    worker = TagWorker.__new__(TagWorker)
    worker.db = SimpleNamespace(conn=conn)
    assert worker.requeue([6, 11]) == 2
    assert conn.execute("SELECT COUNT(*) FROM tag_extraction_status WHERE memory_id IN (6, 11)").fetchone()[0] == 0
    assert build_tag_coverage(conn)["counts"]["lost"] == 0

    async def scenario():
        worker._wake_event = asyncio.Event()
        worker.wake()
        return worker._wake_event.is_set()

    assert asyncio.run(scenario())
