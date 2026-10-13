"""v5.1.2：原写死的缓存时长/条数改为 9876 热参数；消费点按热参数生效并做范围钳制。"""

from __future__ import annotations

import sqlite3

import pytest

import services.hot_config as hc
from services.system_convergence_runtime import _catalog_neighbors_for_admission, clear_catalog_neighbor_cache


@pytest.fixture
def hot():
    hc.HotConfig.reset()
    inst = hc.HotConfig()
    yield inst
    hc.HotConfig.reset()


def test_new_tunables_registered_with_ranges(hot):
    keys = hot.tunable_keys()
    for key in (
        "tags.admission_neighbor_limit", "tags.admission_cache_seconds",
        "jargon.person_alias_cache_seconds", "people.nickname_cache_seconds",
        "webui.stats_cache_seconds", "webui.vector_count_cache_seconds", "webui.coverage_cache_seconds",
        "webui.legacy_quality_cache_seconds", "webui.list_count_cache_seconds", "webui.list_count_wait_seconds",
    ):
        assert key in keys


def test_hot_value_casts_and_clamps(hot):
    hot.update({"webui.stats_cache_seconds": 10**9, "tags.admission_neighbor_limit": "3"})
    assert hc.hot_value("webui.stats_cache_seconds", 15) == 3600
    assert hc.hot_value("tags.admission_neighbor_limit", 64) == 3
    assert hc.hot_value("not.registered", 7) == 7


def test_hot_value_without_runtime_returns_default():
    hc.HotConfig.reset()
    assert hc.hot_value("webui.stats_cache_seconds", 15) == 15


def _catalog_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tag_catalog (id INTEGER PRIMARY KEY, normalized_name TEXT, display_name TEXT, tag_type TEXT,"
        " embedding BLOB, status TEXT, created_at REAL, updated_at REAL)"
    )
    for i in range(5):
        conn.execute("INSERT INTO tag_catalog VALUES (?, ?, ?, 'keyword', NULL, 'active', ?, ?)", (i + 1, f"t{i}", f"T{i}", i, i))
    return conn


def test_catalog_neighbors_limit_and_cache_follow_hot_params(hot):
    clear_catalog_neighbor_cache()
    conn = _catalog_conn()
    hot.update({"tags.admission_neighbor_limit": 2, "tags.admission_cache_seconds": 60})
    first = _catalog_neighbors_for_admission(conn, tag_type="keyword")
    assert [n.catalog_id for n in first] == [5, 4]
    conn.execute("INSERT INTO tag_catalog VALUES (99, 'new', 'NEW', 'keyword', NULL, 'active', 100, 100)")
    cached = _catalog_neighbors_for_admission(conn, tag_type="keyword")
    assert [n.catalog_id for n in cached] == [5, 4], "TTL 内应命中缓存"
    hot.update({"tags.admission_cache_seconds": 0})
    fresh = _catalog_neighbors_for_admission(conn, tag_type="keyword")
    assert [n.catalog_id for n in fresh] == [99, 5], "缓存关闭后直接读库"
    hot.update({"tags.admission_neighbor_limit": 0})
    assert _catalog_neighbors_for_admission(conn, tag_type="keyword") == []
    clear_catalog_neighbor_cache()


def test_removed_dead_features_are_gone():
    import importlib.util
    import json
    import pathlib

    assert importlib.util.find_spec("services.dream") is None
    assert importlib.util.find_spec("services.storage_capacity_policy") is None
    schema = json.loads(pathlib.Path("_conf_schema.json").read_text(encoding="utf-8-sig"))
    lifecycle = schema["Lifecycle_Settings"].get("items", schema["Lifecycle_Settings"])
    assert not any(key.startswith(("enable_dream", "dream_")) for key in lifecycle)
    assert set(schema["Eviction_Settings"]["items"]) == {"interval_hours"}
    assert not {"max_memories", "canonical_capacity_enabled", "cold_chat_when_over_capacity"} & set(schema["Storage_Settings"]["items"])
    src = pathlib.Path("app/injection.py").read_text(encoding="utf-8")
    for dead in ("_build_shadow_persona_realtime_ctx", "_hourly_reply_count", "_run_injection_shadow_trace", "realtime_ctx",
                 "_get_recent_messages", "recent_context"):
        assert dead not in src
    assert "PatternAggregator" not in pathlib.Path("services/lifecycle.py").read_text(encoding="utf-8")


def test_cold_recall_skips_broad_tags(hot, tmp_path):
    """关联数超过 query.cold_tag_max_links 的万能标签不参与冷召回展开；0 = 不过滤。"""
    from engine.database import WaveMemoryDB
    from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo

    db = WaveMemoryDB(str(tmp_path / "w.sqlite"), dimension=3)
    try:
        conn = db.conn
        now = 1.0
        for tag_id, links in ((1, 5), (2, 1)):
            conn.execute(
                "INSERT INTO scoped_tags (id, bot_id, session_id, visibility, name, created_at, updated_at)"
                " VALUES (?, 'b', 'qq:group:g', 'group', ?, ?, ?)", (tag_id, f"t{tag_id}", now, now))
            for mid in range(tag_id * 100, tag_id * 100 + links):
                conn.execute(
                    "INSERT INTO scoped_memory_tags (bot_id, session_id, visibility, memory_id, tag_id, created_at)"
                    " VALUES ('b', 'qq:group:g', 'group', ?, ?, ?)", (mid, tag_id, now))
        conn.commit()
        repo = db.scoped_knowledge
        ScopedKnowledgeRepo._BROAD_TAG_CACHE.clear()
        hot.update({"query.cold_tag_max_links": 3})
        assert repo._drop_broad_tags([1, 2]) == [2]
        ScopedKnowledgeRepo._BROAD_TAG_CACHE.clear()
        hot.update({"query.cold_tag_max_links": 0})
        assert repo._drop_broad_tags([1, 2]) == [1, 2]
    finally:
        ScopedKnowledgeRepo._BROAD_TAG_CACHE.clear()
        db.close()
