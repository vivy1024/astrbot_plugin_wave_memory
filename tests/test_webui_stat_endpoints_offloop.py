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
