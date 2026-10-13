"""总览/记忆/标签统计接口：重 SQL 不再占 WebUI 事件循环，覆盖率报告可用时跳过白算的全表扫描。"""

from __future__ import annotations

import threading

import pytest
from quart import Quart

import webui.blueprints.system as system_mod
import webui.blueprints.tags as tags_mod
from engine.database import WaveMemoryDB
from webui.blueprints.memories import memories_bp
from webui.blueprints.system import system_bp
from webui.blueprints.tags import tags_bp
from webui.container import get_container
from webui.stat_cache import clear_stat_cache

_REPORT = {
    "counts": {"tagged": 7, "pending": 1, "lost": 0, "failed": 0, "too_short": 2},
    "backlog": 1,
    "active_memories": 10,
    "coverage": 0.7,
    "effective_coverage": 0.875,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    clear_stat_cache()
    db = WaveMemoryDB(str(tmp_path / "w.sqlite"), dimension=3)
    db.conn.execute(
        "INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp, source) VALUES ('g','u','n','hello world!!',1,'chat')"
    )
    db.conn.commit() if hasattr(db.conn, "commit") else None
    container = get_container()
    saved = (container.db, container.password)
    container.db, container.password = db, ""

    async def fake_coverage(_c, *, refresh=False):
        return _REPORT

    monkeypatch.setattr(tags_mod, "cached_tag_coverage", fake_coverage)
    app = Quart(__name__)
    for bp in (system_bp, memories_bp, tags_bp):
        app.register_blueprint(bp)
    yield app.test_client(), monkeypatch
    container.db, container.password = saved
    db.close()
    clear_stat_cache()


@pytest.mark.asyncio
async def test_system_skips_full_scan_tag_counts_when_coverage_available(client):
    test_client, monkeypatch = client

    def boom(_conn):
        raise AssertionError("覆盖率报告可用时不应再跑这条全表扫描")

    monkeypatch.setattr(system_mod, "count_existing_tagged_memories", boom)
    monkeypatch.setattr(system_mod, "count_untagged_memories", boom)
    res = await test_client.get("/api/system")
    assert res.status_code == 200
    body = await res.get_json()
    assert body["memories"]["with_tags"] == 7
    assert body["todos"]["untagged_count"] == 1
    assert body["coverage"]["tag_pct"] == 70.0


@pytest.mark.asyncio
async def test_system_db_stats_run_on_worker_thread(client):
    test_client, monkeypatch = client
    seen = []
    original = system_mod._system_db_stats_sync

    def spy(c, *, need_tag_counts):
        seen.append(threading.get_ident())
        return original(c, need_tag_counts=need_tag_counts)

    monkeypatch.setattr(system_mod, "_system_db_stats_sync", spy)
    res = await test_client.get("/api/system")
    assert res.status_code == 200
    assert seen and seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_memory_stats_and_tag_quality_still_answer(client):
    test_client, _ = client
    res = await test_client.get("/api/memories/stats")
    body = await res.get_json()
    assert res.status_code == 200 and body["total"] == 1 and body["by_source"] == {"chat": 1}
    res = await test_client.get("/api/tags/quality")
    body = await res.get_json()
    assert res.status_code == 200
    assert body["tagged_memories"] == 7 and body["coverage"] == 0.7
    assert body["orphan_memory_tag_refs"] == 0



@pytest.mark.asyncio
async def test_memory_list_returns_page_without_waiting_for_slow_count(monkeypatch):
    """列表走 group_id 索引；同条件的 COUNT 慢时先返回列表、总数标为统计中，算完后给准确值。"""
    import asyncio as _asyncio
    import sqlite3
    from types import SimpleNamespace

    from domain.scope import RuntimeScope, SessionRef
    from webui.blueprints import memories as module
    from webui.middleware import auth as auth_module
    from webui.api_contract import ObjectRefRegistry

    clear_stat_cache()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT,
           content TEXT, vector BLOB, timestamp REAL, importance REAL, access_count INTEGER, memory_type TEXT,
           source TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT,
           version INTEGER, quarantine INTEGER DEFAULT 0)"""
    )
    conn.execute("CREATE INDEX idx_memories_group ON memories(group_id)")
    for mid in range(1, 4):
        conn.execute(
            "INSERT INTO memories (id, group_id, sender_id, content, timestamp, memory_type, source, bot_id,"
            " session_id, visibility, resolution_state, version) VALUES (?, 'g1', 'u', 'hi', ?, 'message', 'chat',"
            " 'yushu', 'qq:group:g1', 'group', 'resolved', 1)",
            (mid, 1000 + mid),
        )
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM memories WHERE group_id = ? ORDER BY id DESC LIMIT 25", ("g1",)
    ).fetchall()
    assert any("idx_memories_group" in str(row[3]) for row in plan)

    scope = RuntimeScope(bot_id="yushu", visibility="group", session=SessionRef("qq:group:g1", "qq", "group", "g1"))
    container = SimpleNamespace(db=SimpleNamespace(conn=conn), password="", sessions=set())
    monkeypatch.setattr(module, "get_container", lambda: container)
    monkeypatch.setattr(auth_module, "get_container", lambda: container)

    class Provider:
        def get_request_scope(self):
            return scope

    app = Quart(__name__)
    app.extensions["wave_api_contract"] = {"request_scope_provider": Provider(), "object_refs": ObjectRefRegistry()}
    app.register_blueprint(module.memories_bp)
    client = app.test_client()
    url = "/api/memories?bot_id=yushu&session_id=qq:group:g1&visibility=group&limit=25"

    import webui.stat_cache as stat_cache

    real_cached = stat_cache.cached_stat

    def slow_compute(compute):
        def run():
            import time as _time

            _time.sleep(0.3)  # 模拟线上 11 秒的群内 COUNT
            return compute()
        return run

    async def slow_cached(key, ttl, compute, **kw):
        if ":count:" in key:
            compute = slow_compute(compute)
        return await real_cached(key, ttl, compute, **kw)

    monkeypatch.setattr(stat_cache, "cached_stat", slow_cached)
    monkeypatch.setattr(module, "_COUNT_WAIT", 0.05)
    first = await (await client.get(url)).get_json()
    assert [item["id"] for item in first["items"]] == [3, 2, 1]
    assert first["page"]["total_status"] == "unavailable"
    assert first["page"]["reason_code"] == "total_counting"

    await _asyncio.sleep(0.4)  # 后台计数算完写进缓存
    second = await (await client.get(url)).get_json()
    assert second["page"]["total_status"] == "exact" and second["page"]["total"] == 3
    clear_stat_cache()
