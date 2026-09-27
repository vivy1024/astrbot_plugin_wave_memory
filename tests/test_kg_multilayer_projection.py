from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import numpy as np

from domain.scope import RuntimeScope, SessionRef
from webui.blueprints import kg
from webui.container import ServiceContainer, get_container
from webui.graph_projection import SUPPORTED_LAYERS, build_graph_projection


def _scope(bot_id="bot-a", session_id="qq:group:g1"):
    return RuntimeScope(bot_id, "group", SessionRef(session_id, "qq", "group", session_id.rsplit(":", 1)[-1]))


def _database():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            content TEXT, vector BLOB, timestamp REAL, importance REAL, source TEXT,
            sender_id TEXT, sender_name TEXT, resolution_state TEXT, quarantine INTEGER
        );
        CREATE TABLE scoped_facts (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject TEXT, predicate TEXT, object TEXT, confidence REAL, status TEXT,
            source_memory_id INTEGER, provenance TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            name TEXT, tag_type TEXT, confidence REAL, description TEXT
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER,
            tag_id INTEGER, relevance REAL, created_at REAL
        );
        CREATE TABLE scoped_tag_relations (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            source_tag_id INTEGER, target_tag_id INTEGER, relation_type TEXT,
            weight REAL, confidence REAL, metadata TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE scoped_beliefs (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            belief_key TEXT, content TEXT, belief_type TEXT, strength REAL, status TEXT,
            source_memory_id INTEGER, provenance TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE scoped_jargon (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            word TEXT, meaning TEXT, status TEXT, frequency INTEGER, confidence REAL,
            contexts TEXT, source_memory_id INTEGER, source_context TEXT, provenance TEXT,
            created_at REAL, updated_at REAL
        );
        CREATE TABLE scoped_soul_concerns (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            topic TEXT, intensity REAL, origin_memory_id INTEGER, revision INTEGER,
            evidence TEXT, created_at REAL, last_triggered REAL
        );
        CREATE TABLE scoped_soul_mood (
            bot_id TEXT, session_id TEXT, visibility TEXT, valence REAL, arousal REAL,
            cause TEXT, policy_version TEXT, revision INTEGER, evidence TEXT,
            observed_at REAL, updated_at REAL
        );
        CREATE TABLE scoped_soul_timeline (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, event_summary TEXT, event_type TEXT,
            emotional_weight REAL, occurred_at REAL, revision INTEGER, evidence TEXT, created_at REAL
        );
        CREATE TABLE scoped_soul_relationships (
            bot_id TEXT, session_id TEXT, visibility TEXT, subject_principal_id TEXT,
            affinity INTEGER, state TEXT, dimensions TEXT, revision INTEGER, evidence TEXT, updated_at REAL
        );
        CREATE TABLE scoped_soul_relationship_events (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, event_type TEXT, dimension TEXT, delta REAL,
            reason TEXT, source_episode_id INTEGER, source_memory_id INTEGER,
            revision INTEGER, created_at REAL
        );
        """
    )
    vector_a = np.array([1.0, 0.0], dtype=np.float32).tobytes()
    vector_b = np.array([0.9, 0.1], dtype=np.float32).tobytes()
    conn.executemany(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "bot-a", "qq:group:g1", "group", "同域记忆一", vector_a, 10, 1, "chat", "u1", "用户一", "resolved", 0),
            (2, "bot-a", "qq:group:g1", "group", "同域记忆二", vector_b, 11, 1, "chat", "u2", "用户二", "resolved", 0),
            (99, "bot-b", "qq:group:g2", "group", "跨域秘密", vector_a, 12, 1, "chat", "u9", "跨域", "resolved", 0),
        ],
    )
    conn.execute("INSERT INTO scoped_facts VALUES (1,'bot-a','qq:group:g1','group','用户一','喜欢','猫',.9,'reviewed',1,'{}',10,10)")
    conn.execute("INSERT INTO scoped_facts VALUES (99,'bot-b','qq:group:g2','group','跨域','知道','秘密',1,'reviewed',99,'{}',10,10)")
    conn.executemany("INSERT INTO scoped_tags VALUES (?,?,?,?,?,?,?,?)", [
        (1, "bot-a", "qq:group:g1", "group", "猫", "entity", .9, ""),
        (2, "bot-a", "qq:group:g1", "group", "宠物", "topic", .8, ""),
    ])
    conn.execute("INSERT INTO scoped_memory_tags VALUES ('bot-a','qq:group:g1','group',1,1,.95,10)")
    conn.execute("INSERT INTO scoped_tag_relations VALUES (1,'bot-a','qq:group:g1','group',1,2,'属于',.8,.8,'{}',10,10)")
    conn.execute("INSERT INTO scoped_beliefs VALUES (1,'bot-a','qq:group:g1','group','kind','善意很重要','world_view',.8,'approved',1,'{}',10,10)")
    conn.execute("INSERT INTO scoped_jargon VALUES (1,'bot-a','qq:group:g1','group','开摆','暂时放松','approved',4,.9,'[]',1,'','{}',10,10)")
    conn.execute("INSERT INTO scoped_soul_concerns VALUES (1,'bot-a','qq:group:g1','group','项目进展',.7,1,1,'[]',10,11)")
    conn.execute("INSERT INTO scoped_soul_mood VALUES ('bot-a','qq:group:g1','group',.4,.6,'进展顺利','v1',1,'[]',10,11)")
    conn.execute("INSERT INTO scoped_soul_timeline VALUES (1,'bot-a','qq:group:g1','group','qq:user:u1','完成升级','milestone',.8,11,1,'[]',11)")
    conn.execute("INSERT INTO scoped_soul_relationships VALUES ('bot-a','qq:group:g1','group','qq:user:u1',60,'trusted','{}',1,'[]',11)")
    conn.execute("INSERT INTO scoped_soul_relationship_events VALUES (1,'bot-a','qq:group:g1','group','qq:user:u1','help','trust',2,'协助升级',NULL,1,1,11)")
    conn.commit()
    return conn


class _Index:
    def search(self, vector, k=10):
        return [(99, 0.01), (1, 0.0), (2, 0.1)]


class _FewShot:
    def list_approved(self, *, scope, limit, offset):
        return [{"id": 1, "content": "温和回答", "score": .9, "traits": ("温和",), "revision": 1, "updated_at": 10, "evidence_refs": ()}]

    def count_approved(self, *, scope):
        return 1


class _BookLore:
    def list_approved(self, *, scope, limit, offset):
        return [{"id": 1, "title": "世界设定", "summary": "摘要", "content": "内容", "rank": .8, "revision": 1, "updated_at": 10, "community_id": "c1", "evidence_refs": ()}]

    def count_approved(self, *, scope):
        return 1


def test_multilayer_projection_uses_all_formal_layers_and_filters_hnsw_cross_scope():
    conn = _database()
    try:
        payload = build_graph_projection(
            conn=conn, scope=_scope(), layers=SUPPORTED_LAYERS, memory_index=_Index(),
            fewshot_repository=_FewShot(),
            memory_limit=20, similarity_k=3, similarity_threshold=.5,
        )
    finally:
        conn.close()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "跨域秘密" not in serialized
    assert "entity:秘密" not in serialized
    assert "memory:99" not in serialized
    assert "hnsw:1:2" in serialized
    # book_lore 层不再从 reviewed projection 产出边；仅有 Catalog 直读警告。
    assert {"fact", "memory_tag", "hnsw_neighbor", "belief", "jargon", "concern", "mood", "timeline_event", "affinity", "relationship_event", "few_shot", "community_member"} <= {edge["kind"] for edge in payload["edges"]}
    assert "book_lore" not in {edge["kind"] for edge in payload["edges"]}
    assert payload["scope"]["payload"]["bot_id"] == "bot-a"
    assert payload["read_only"] is True


def test_requested_layers_and_cache_keys_are_isolated():
    ServiceContainer.reset()
    conn = _database()
    try:
        container = get_container()
        container.db = SimpleNamespace(conn=conn)
        container.memory_index = _Index()
        kg.clear_kg_cache()
        facts = kg.build_full_graph_data("facts", scope=_scope(), use_cache=True)
        memories = kg.build_full_graph_data("memories", scope=_scope(), use_cache=True, memory_limit=20)
        assert facts["layers"] == ["facts"]
        assert memories["layers"] == ["memories"]
        assert {edge["kind"] for edge in facts["edges"]} <= {"fact", "tag_relation"}
        assert "hnsw_neighbor" in {edge["kind"] for edge in memories["edges"]}
        assert len(kg._overview_cache) == 2
    finally:
        conn.close()
        ServiceContainer.reset()
        kg.clear_kg_cache()


def test_missing_optional_tables_degrade_without_legacy_fallback():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE beliefs(id INTEGER, content TEXT)")
    try:
        payload = build_graph_projection(conn=conn, scope=_scope(), layers=("beliefs", "mood"))
    finally:
        conn.close()
    assert payload["edges"] == []
    assert {item["reason"] for item in payload["warnings"]} == {"scoped_beliefs_unavailable", "scoped_soul_mood_unavailable"}


def test_fact_and_tag_relation_edges_carry_created_and_updated_time():
    conn = _database()
    try:
        conn.execute("UPDATE scoped_facts SET created_at=5, updated_at=50 WHERE id=1")
        conn.execute("UPDATE scoped_tag_relations SET created_at=7, updated_at=70 WHERE id=1")
        payload = build_graph_projection(conn=conn, scope=_scope(), layers=("facts",))
    finally:
        conn.close()
    edges = {edge["id"]: edge for edge in payload["edges"]}
    assert edges["fact:1"]["created_ts"] == 5.0
    assert edges["fact:1"]["ts"] == 50.0
    assert edges["tagrel:1"]["created_ts"] == 7.0
    assert edges["tagrel:1"]["ts"] == 70.0
