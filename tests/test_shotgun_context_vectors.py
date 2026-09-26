"""shotgun 检索：上下文消息复用库里已存的向量，其余与当前消息合并成一次向量调用。"""

from __future__ import annotations

import asyncio
import sqlite3
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
    def __init__(self):
        self.batches: list[list[str]] = []

    async def get_embedding(self, text):
        return (await self.get_embeddings([text]))[0]

    async def get_embeddings(self, texts):
        self.batches.append(list(texts))
        return [np.asarray([1.0, 0.0, 0.0], dtype=np.float32) for _ in texts]


class _Index:
    count = 10

    def __init__(self):
        self.queries = 0

    def search(self, vector, k):
        self.queries += 1
        return []


class _Db:
    def __init__(self, rows):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT, content TEXT, vector BLOB)"
        )
        self.conn.executemany(
            "INSERT INTO memories(id, bot_id, session_id, visibility, content, vector) VALUES (?, ?, ?, ?, ?, ?)", rows
        )

    def get_memories_by_ids(self, ids, *, scope):
        return []


def _scope():
    from domain.scope import RuntimeScope, SessionRef

    return RuntimeScope(
        bot_id="bot-a", visibility="group",
        session=SessionRef(id="qq:group:g1", platform_id="qq", kind="group", conversation_id="g1"),
    )


def _vec(*values):
    return np.asarray(values, dtype=np.float32).tobytes()


def _engine(rows):
    from engine.query_engine import QueryEngine

    embedding = _Embedding()
    engine = QueryEngine(_Db(rows), _Index(), embedding, {"min_similarity": 0.0, "cold_recall_enabled": False})
    return engine, embedding


def test_stored_context_vectors_match_same_scope_only_and_prefer_newest():
    rows = [
        (1, "bot-a", "qq:group:g1", "group", "早上好", _vec(0.0, 1.0, 0.0)),
        (2, "bot-a", "qq:group:g1", "group", "早上好", _vec(0.0, 0.0, 1.0)),
        (3, "bot-a", "qq:group:other", "group", "别的群", _vec(1.0, 0.0, 0.0)),
        (4, "bot-a", "qq:group:g1", "group", "没向量", None),
    ]
    engine, _ = _engine(rows)
    found = engine._stored_context_vectors(["早上好", "别的群", "没向量"], _scope())
    assert set(found) == {"早上好"}
    assert found["早上好"].tolist() == [0.0, 0.0, 1.0]


def test_shotgun_embeds_query_and_missing_context_in_one_call():
    rows = [
        (1, "bot-a", "qq:group:g1", "group", "上一句", _vec(1.0, 0.0, 0.0)),
        (2, "bot-a", "qq:group:g1", "group", "再上一句", _vec(0.9, 0.1, 0.0)),
    ]
    engine, embedding = _engine(rows)
    asyncio.run(engine.shotgun_query("现在这句", context_messages=["再上一句", "上一句", "还没入库"], scope=_scope()))
    assert embedding.batches == [["现在这句", "还没入库"]]


def test_shotgun_with_all_context_stored_calls_embedding_once_for_query():
    rows = [(1, "bot-a", "qq:group:g1", "group", "上一句", _vec(1.0, 0.0, 0.0))]
    engine, embedding = _engine(rows)
    asyncio.run(engine.shotgun_query("现在这句", context_messages=["上一句"], scope=_scope()))
    assert embedding.batches == [["现在这句"]]


def test_stored_context_vectors_only_scan_recent_window(monkeypatch):
    import engine.query_engine as qe

    rows = [
        (1, "bot-a", "qq:group:g1", "group", "很久以前", _vec(1.0, 0.0, 0.0)),
        (10, "bot-a", "qq:group:g1", "group", "刚才", _vec(0.0, 1.0, 0.0)),
    ]
    engine, _ = _engine(rows)
    monkeypatch.setattr(qe, "CONTEXT_VECTOR_WINDOW", 5)
    assert set(engine._stored_context_vectors(["很久以前", "刚才"], _scope())) == {"刚才"}
