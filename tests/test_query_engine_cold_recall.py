from __future__ import annotations

import asyncio
import sys
import types

import numpy as np


if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


class _Embedding:
    async def get_embedding(self, text):
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float32)


class _EmptyMemoryIndex:
    count = 0

    def search(self, vector, k):
        return []


class _TagIndex:
    count = 100

    def search(self, vector, k):
        return [(101, 0.02)]


class _ColdDb:
    def __init__(self):
        self.scopes = []
        self.touched = []

    def get_memories_by_ids(self, ids, *, scope):
        self.scopes.append(scope)
        return []

    def list_scoped_catalog_links(self, scope, catalog_ids):
        self.scopes.append(scope)
        assert catalog_ids == [101]
        return [{"catalog_id": 101, "scoped_tag_id": 17}]

    def list_scoped_cold_memory_candidates(self, scope, tag_ids, *, limit):
        self.scopes.append(scope)
        assert tag_ids == [17]
        assert limit == 16
        return [
            {
                "id": 41,
                "vector": np.asarray([0.98, 0.05, 0.0], dtype=np.float32).tobytes(),
                "content": "只在冷层的已标记记忆",
                "timestamp": 1_800_000_000.0,
                "importance": 1.0,
                "access_count": 0,
                "source": "chat",
                "memory_type": "message",
                "group_id": "g1",
                "tag_score": 1.0,
                "tag_count": 1,
            }
        ]

    def touch_memories(self, ids, importance_boost=0.01):
        assert importance_boost == 0.0, "召回不应再抬重要度"
        self.touched.append(ids)


class _ScopeFactory:
    @staticmethod
    def make():
        from domain.scope import RuntimeScope, SessionRef

        return RuntimeScope(
            bot_id="bot-a",
            visibility="group",
            session=SessionRef(
                id="qq:group:g1",
                platform_id="qq",
                kind="group",
                conversation_id="g1",
            ),
        )


def _engine(*, cold_enabled=True):
    from engine.query_engine import QueryEngine

    db = _ColdDb()
    engine = QueryEngine(
        db,
        _EmptyMemoryIndex(),
        _Embedding(),
        {
            "min_similarity": 0.0,
            "cold_recall_enabled": cold_enabled,
            "cold_candidate_limit": 16,
        },
        tag_index=_TagIndex(),
    )
    return engine, db


def test_query_uses_scoped_tag_cold_recall_when_hot_hnsw_is_empty():
    from engine.query_engine import QueryDebugCollector

    engine, db = _engine()
    collector = QueryDebugCollector()

    result = asyncio.run(engine.query("标记话题", scope=_ScopeFactory.make(), collector=collector))

    assert [item["id"] for item in result] == [41]
    assert result[0]["_retrieval_tier"] == "cold"
    assert db.touched == [[41]]
    trace = collector.snapshot()
    assert trace["vector_search"]["cold"]["available"] is True
    assert trace["vector_search"]["cold"]["accepted_count"] == 1


def test_query_can_disable_cold_recall_without_unscoped_fallback():
    engine, db = _engine(cold_enabled=False)

    result = asyncio.run(engine.query("标记话题", scope=_ScopeFactory.make()))

    assert result == []
    assert db.touched == []


def test_shotgun_query_reuses_the_scoped_cold_candidate_path():
    engine, db = _engine()

    result = asyncio.run(engine.shotgun_query("标记话题", scope=_ScopeFactory.make()))

    assert [item["id"] for item in result] == [41]
    assert result[0]["_retrieval_tier"] == "cold"
    assert db.touched == [[41]]


class _CrossGroupColdDb:
    def __init__(self):
        self.catalog_calls = []
        self.touched = []

    def get_memories_by_ids(self, ids, *, scope, allow_cross_group_recall=False):
        return []

    def list_scoped_catalog_links(self, scope, catalog_ids, *, allow_cross_group_recall=False):
        self.catalog_calls.append((catalog_ids, allow_cross_group_recall))
        assert catalog_ids == [101]
        links = [{"catalog_id": 101, "scoped_tag_id": 17}]
        if allow_cross_group_recall:
            links.append({"catalog_id": 101, "scoped_tag_id": 18})
        return links

    @staticmethod
    def _row(memory_id, group_id, content):
        return {
            "id": memory_id,
            "vector": np.asarray([0.98, 0.05, 0.0], dtype=np.float32).tobytes(),
            "content": content,
            "timestamp": 1_800_000_000.0,
            "importance": 1.0,
            "access_count": 0,
            "source": "chat",
            "memory_type": "message",
            "group_id": group_id,
            "tag_score": 1.0,
            "tag_count": 1,
        }

    def list_scoped_cold_memory_candidates(self, scope, tag_ids, *, limit, allow_cross_group_recall=False):
        self.catalog_calls.append((tag_ids, allow_cross_group_recall))
        rows = [self._row(41, "g1", "当前 Scope Catalog")]
        if allow_cross_group_recall:
            rows.append(self._row(43, "g2", "跨群跨 Bot Catalog"))
        return rows

    def touch_memories(self, ids, importance_boost=0.01):
        assert importance_boost == 0.0, "召回不应再抬重要度"
        self.touched.append(ids)


class _ScopeFactory:
    @staticmethod
    def make():
        from domain.scope import RuntimeScope, SessionRef

        return RuntimeScope(
            bot_id="bot-a",
            visibility="group",
            session=SessionRef(
                id="qq:group:g1",
                platform_id="qq",
                kind="group",
                conversation_id="g1",
            ),
        )


def _engine(*, cold_enabled=True):
    from engine.query_engine import QueryEngine

    db = _ColdDb()
    engine = QueryEngine(
        db,
        _EmptyMemoryIndex(),
        _Embedding(),
        {
            "min_similarity": 0.0,
            "cold_recall_enabled": cold_enabled,
            "cold_candidate_limit": 16,
        },
        tag_index=_TagIndex(),
    )
    return engine, db


def test_query_uses_scoped_tag_cold_recall_when_hot_hnsw_is_empty():
    from engine.query_engine import QueryDebugCollector

    engine, db = _engine()
    collector = QueryDebugCollector()

    result = asyncio.run(engine.query("标记话题", scope=_ScopeFactory.make(), collector=collector))

    assert [item["id"] for item in result] == [41]
    assert result[0]["_retrieval_tier"] == "cold"
    assert db.touched == [[41]]
    trace = collector.snapshot()
    assert trace["vector_search"]["cold"]["available"] is True
    assert trace["vector_search"]["cold"]["accepted_count"] == 1


def test_query_can_disable_cold_recall_without_unscoped_fallback():
    engine, db = _engine(cold_enabled=False)

    result = asyncio.run(engine.query("标记话题", scope=_ScopeFactory.make()))

    assert result == []
    assert db.touched == []


def test_shotgun_query_reuses_the_scoped_cold_candidate_path():
    engine, db = _engine()

    result = asyncio.run(engine.shotgun_query("标记话题", scope=_ScopeFactory.make()))

    assert [item["id"] for item in result] == [41]
    assert result[0]["_retrieval_tier"] == "cold"
    assert db.touched == [[41]]


class _CrossGroupColdDb:
    def __init__(self):
        self.catalog_calls = []
        self.touched = []

    def get_memories_by_ids(self, ids, *, scope, allow_cross_group_recall=False):
        return []

    def list_scoped_catalog_links(self, scope, catalog_ids, *, allow_cross_group_recall=False):
        self.catalog_calls.append((catalog_ids, allow_cross_group_recall))
        assert catalog_ids == [101]
        links = [{"catalog_id": 101, "scoped_tag_id": 17}]
        if allow_cross_group_recall:
            links.append({"catalog_id": 101, "scoped_tag_id": 18})
        return links

    @staticmethod
    def _row(memory_id, group_id, content):
        return {
            "id": memory_id,
            "vector": np.asarray([0.98, 0.05, 0.0], dtype=np.float32).tobytes(),
            "content": content,
            "timestamp": 1_800_000_000.0,
            "importance": 1.0,
            "access_count": 0,
            "source": "chat",
            "memory_type": "message",
            "group_id": group_id,
            "tag_score": 1.0,
            "tag_count": 1,
        }

    def list_scoped_cold_memory_candidates(self, scope, tag_ids, *, limit, allow_cross_group_recall=False):
        self.catalog_calls.append((tag_ids, allow_cross_group_recall))
        rows = [self._row(41, "g1", "当前 Scope Catalog")]
        if allow_cross_group_recall:
            rows.append(self._row(43, "g2", "跨群跨 Bot Catalog"))
        return rows

    def touch_memories(self, ids, importance_boost=0.01):
        assert importance_boost == 0.0, "召回不应再抬重要度"
        self.touched.append(ids)


def _cross_group_cold_engine(enabled):
    from engine.query_engine import QueryEngine

    db = _CrossGroupColdDb()
    return QueryEngine(
        db,
        _EmptyMemoryIndex(),
        _Embedding(),
        {"min_similarity": 0.0, "cold_recall_enabled": True, "cross_group_enabled": enabled},
        tag_index=_TagIndex(),
    ), db


def test_cross_group_cold_catalog_lane_expands_only_when_enabled():
    enabled_engine, enabled_db = _cross_group_cold_engine(True)
    disabled_engine, disabled_db = _cross_group_cold_engine(False)
    scope = _ScopeFactory.make()

    expanded = asyncio.run(enabled_engine.query("跨群冷召回", scope=scope))
    exact = asyncio.run(disabled_engine.query("精确冷召回", scope=scope))

    assert {row["id"] for row in expanded} == {41, 43}
    assert {row["id"] for row in exact} == {41}
    assert next(row for row in expanded if row["id"] == 43)["_is_cross_group"] is True
    # `_wave_boost` performs an exact-scope Catalog lookup before cold recall;
    # the cold lane then explicitly expands the same Catalog ID to both scoped IDs.
    assert enabled_db.catalog_calls == [([101], False), ([101], True), ([17, 18], True)]
    assert disabled_db.catalog_calls == [([101], False), ([101], False), ([17], False)]
