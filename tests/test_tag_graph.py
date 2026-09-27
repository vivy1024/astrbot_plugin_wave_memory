from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from webui.api_contract import ObjectRefRegistry
from webui.graph_projection import build_tag_graph_projection, find_tag_graph_path


def _scope(bot_id: str = "bot-a", session_id: str = "qq:group:g1") -> RuntimeScope:
    platform_id, kind, conversation_id = session_id.split(":", 2)
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(session_id, platform_id, kind, conversation_id),
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE scoped_tags(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            name TEXT NOT NULL, tag_type TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE scoped_memory_tags(
            bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            memory_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, position INTEGER NOT NULL,
            relevance REAL NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE scoped_tag_relations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            source_tag_id INTEGER NOT NULL, target_tag_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL, weight REAL NOT NULL, confidence REAL NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active',
            valid_until REAL, revision INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE memories(
            id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            content TEXT NOT NULL, sender_id TEXT, sender_name TEXT, timestamp REAL, importance REAL,
            source TEXT, version INTEGER NOT NULL DEFAULT 1, quarantine INTEGER NOT NULL DEFAULT 0,
            resolution_state TEXT NOT NULL DEFAULT 'resolved', memory_type TEXT NOT NULL DEFAULT 'message'
        );
        """
    )
    return conn


def _seed(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    tags = []
    for name, tag_type, confidence in (
        ("Alpha", "topic", 0.95),
        ("Beta", "person", 0.9),
        ("Gamma", "topic", 0.85),
    ):
        cursor = conn.execute(
            """INSERT INTO scoped_tags(
                   bot_id,session_id,visibility,name,tag_type,description,confidence,metadata,status,revision,created_at,updated_at)
               VALUES ('bot-a','qq:group:g1','group',?,?,?,?,'{}','active',1,1,100)""",
            (name, tag_type, f"{name} description", confidence),
        )
        tags.append(int(cursor.lastrowid))
    foreign = int(conn.execute(
        """INSERT INTO scoped_tags(
               bot_id,session_id,visibility,name,tag_type,description,confidence,metadata,status,revision,created_at,updated_at)
           VALUES ('bot-b','qq:group:g1','group','Foreign','topic','',1,'{}','active',1,1,100)"""
    ).lastrowid)
    conn.execute(
        """INSERT INTO memories(id,bot_id,session_id,visibility,content,sender_id,sender_name,timestamp,importance,source,version)
           VALUES (10,'bot-a','qq:group:g1','group','Alpha 与 Beta 的真实记忆','u1','Alice',900,0.8,'chat',3)"""
    )
    conn.executemany(
        """INSERT INTO scoped_memory_tags(bot_id,session_id,visibility,memory_id,tag_id,position,relevance,created_at)
           VALUES ('bot-a','qq:group:g1','group',10,?,?,?,900)""",
        [(tags[0], 1, 0.9), (tags[1], 2, 0.8)],
    )
    conn.execute(
        """INSERT INTO scoped_tag_relations(
               bot_id,session_id,visibility,source_tag_id,target_tag_id,relation_type,weight,confidence,metadata,status,revision,created_at,updated_at)
           VALUES ('bot-a','qq:group:g1','group',?,?,'supports',0.7,0.88,'{"source":"reviewed"}','active',2,800,950)""",
        (tags[1], tags[2]),
    )
    conn.commit()
    return tags[0], tags[1], tags[2], foreign


def test_tag_graph_projection_uses_real_scoped_tags_directed_edges_and_pulse_fields():
    conn = _connection()
    alpha, beta, gamma, foreign = _seed(conn)

    payload = build_tag_graph_projection(
        conn=conn,
        scope=_scope(),
        layers=("cooccurrence", "relations"),
        include_pulse=True,
        pulse_half_life_hours=1,
        now=1000,
    )

    node_ids = {node["id"] for node in payload["nodes"]}
    assert f"tag:{foreign}" not in node_ids
    assert {f"tag:{alpha}", f"tag:{beta}", f"tag:{gamma}"} <= node_ids
    alpha_node = next(node for node in payload["nodes"] if node["id"] == f"tag:{alpha}")
    assert alpha_node["memory_count"] == 1
    assert alpha_node["sources"] == ["automatic"]
    assert alpha_node["out_degree"] >= 1
    assert alpha_node["associated_memories"][0]["content"] == "Alpha 与 Beta 的真实记忆"

    cooccurrence = next(edge for edge in payload["edges"] if edge["source"] == f"tag:{alpha}" and edge["target"] == f"tag:{beta}")
    assert cooccurrence["layer"] == "cooccurrence"
    assert cooccurrence["kind"] == "directed_cooccurrence"
    assert cooccurrence["type"] == "ordinal_cooccurrence"
    assert cooccurrence["frequency"] == 1
    assert 0 < cooccurrence["weight"] <= 1
    assert 0 < cooccurrence["confidence"] <= 1
    assert 0 < cooccurrence["pulse_decay"] <= 1
    assert cooccurrence["pulse_energy"] > 0

    relation = next(edge for edge in payload["edges"] if edge["layer"] == "relations")
    assert relation["source"] == f"tag:{beta}"
    assert relation["target"] == f"tag:{gamma}"
    assert relation["type"] == "supports"
    assert relation["frequency"] == 1
    assert relation["source_kind"] == "scoped_tag_relations"
    conn.close()


def test_hidden_layers_change_directed_path_calculation():
    conn = _connection()
    alpha, _beta, gamma, _foreign = _seed(conn)
    graph = build_tag_graph_projection(conn=conn, scope=_scope(), layers=("cooccurrence", "relations"), max_nodes=100)

    with_relations = find_tag_graph_path(
        graph,
        source_id=f"tag:{alpha}",
        target_id=f"tag:{gamma}",
        layers=("cooccurrence", "relations"),
    )
    without_relations = find_tag_graph_path(
        graph,
        source_id=f"tag:{alpha}",
        target_id=f"tag:{gamma}",
        layers=("cooccurrence",),
    )

    assert with_relations["found"] is True
    assert [edge["layer"] for edge in with_relations["edges"]] == ["cooccurrence", "relations"]
    assert without_relations["found"] is False
    conn.close()


def test_tag_graph_blueprint_is_registered_in_shared_registry():
    from webui.blueprints import get_blueprints

    assert "tag_graph" in {blueprint.name for blueprint in get_blueprints()}


@pytest.mark.asyncio
async def test_tag_graph_api_issues_object_refs_and_path_accepts_no_raw_ids(monkeypatch):
    from webui.blueprints import tag_graph as module

    conn = _connection()
    alpha, _beta, gamma, _foreign = _seed(conn)
    scope = _scope()
    container = SimpleNamespace(db=SimpleNamespace(conn=conn), password="", sessions=set())
    monkeypatch.setattr(module, "get_container", lambda: container)

    class Provider:
        def get_request_scope(self):
            return scope

    app = Quart(__name__)
    app.extensions["wave_api_contract"] = {
        "request_scope_provider": Provider(),
        "object_refs": ObjectRefRegistry(),
    }
    app.register_blueprint(module.tag_graph_bp)
    client = app.test_client()
    query = "?bot_id=bot-a&session_id=qq:group:g1&visibility=group&include_pulse=1"

    graph_response = await client.get(f"/api/tag-graph{query}")
    assert graph_response.status_code == 200
    graph_payload = await graph_response.get_json()
    nodes = {node["name"]: node for node in graph_payload["nodes"]}
    assert nodes["Alpha"]["ref"].startswith("oref.")
    assert nodes["Alpha"]["object_ref"]["scope_query"]["session_id"] == "qq:group:g1"
    assert nodes["Alpha"]["associated_memories"][0]["object_ref"]["kind"] == "memory"

    path_response = await client.post(
        f"/api/tag-graph/path{query}",
        json={
            "source_ref": nodes["Alpha"]["ref"],
            "target_ref": nodes["Gamma"]["ref"],
            "layers": ["cooccurrence", "relations"],
        },
    )
    assert path_response.status_code == 200
    path_payload = await path_response.get_json()
    assert path_payload["found"] is True
    assert path_payload["path"] == [f"tag:{alpha}", f"tag:{nodes['Beta']['locator']}", f"tag:{gamma}"]

    raw_id_response = await client.post(
        f"/api/tag-graph/path{query}",
        json={"source_id": alpha, "target_id": gamma, "layers": ["relations"]},
    )
    assert raw_id_response.status_code == 404
    conn.close()


def _seed_timeline(conn: sqlite3.Connection) -> dict[str, int]:
    """三个标签：Old（很早创建、记忆多但都很旧）、Hot（旧标签、最近被频繁提到）、Fresh（刚创建）。"""
    ids: dict[str, int] = {}
    for name, created_at, updated_at in (("Old", 10.0, 20.0), ("Hot", 50.0, 9_000.0), ("Fresh", 9_500.0, 9_500.0)):
        cursor = conn.execute(
            """INSERT INTO scoped_tags(
                   bot_id,session_id,visibility,name,tag_type,description,confidence,metadata,status,revision,created_at,updated_at)
               VALUES ('bot-a','qq:group:g1','group',?,'topic','',0.9,'{}','active',1,?,?)""",
            (name, created_at, updated_at),
        )
        ids[name] = int(cursor.lastrowid)
    memories = [
        # Old：三条很旧的记忆
        (1, 100.0, ["Old"]), (2, 110.0, ["Old"]), (3, 120.0, ["Old", "Hot"]),
        # Hot：窗口内两条
        (4, 9_100.0, ["Hot"]), (5, 9_200.0, ["Hot", "Fresh"]),
    ]
    for memory_id, ts, names in memories:
        conn.execute(
            """INSERT INTO memories(id,bot_id,session_id,visibility,content,sender_id,sender_name,timestamp,importance,source,version)
               VALUES (?,'bot-a','qq:group:g1','group',?,'u1','Alice',?,0.5,'chat',1)""",
            (memory_id, f"记忆 {memory_id}", ts),
        )
        for position, name in enumerate(names, start=1):
            conn.execute(
                """INSERT INTO scoped_memory_tags(bot_id,session_id,visibility,memory_id,tag_id,position,relevance,created_at)
                   VALUES ('bot-a','qq:group:g1','group',?,?,?,0.9,?)""",
                (memory_id, ids[name], position, ts),
            )
    conn.commit()
    return ids


def test_tag_graph_nodes_expose_real_time_fields_for_views():
    conn = _connection()
    ids = _seed_timeline(conn)
    payload = build_tag_graph_projection(conn=conn, scope=_scope(), now=10_000.0, recent_window_hours=1.0)
    nodes = {node["name"]: node for node in payload["nodes"]}
    assert nodes["Old"]["created_at"] == 10.0
    assert nodes["Old"]["updated_at"] == 20.0
    assert nodes["Old"]["last_seen_ts"] == 120.0
    assert nodes["Old"]["recent_memory_count"] == 0
    # 窗口 1 小时 = [6400, 10000]：Hot 有 4、5 两条
    assert nodes["Hot"]["last_seen_ts"] == 9_200.0
    assert nodes["Hot"]["recent_memory_count"] == 2
    assert nodes["Fresh"]["recent_memory_count"] == 1
    assert payload["rank_by"] == "links"
    assert payload["recent_window_hours"] == 1.0
    assert set(ids) == set(nodes)
    conn.close()


def test_tag_graph_rank_by_selects_current_topics_and_new_tags_within_max_nodes():
    conn = _connection()
    _seed_timeline(conn)
    by_links = build_tag_graph_projection(conn=conn, scope=_scope(), now=10_000.0, max_nodes=1, recent_window_hours=1.0)
    by_recent = build_tag_graph_projection(conn=conn, scope=_scope(), now=10_000.0, max_nodes=1, recent_window_hours=1.0, rank_by="recent")
    by_created = build_tag_graph_projection(conn=conn, scope=_scope(), now=10_000.0, max_nodes=1, rank_by="created")
    # 默认（links）：Old 与 Hot 各 3 条关联记忆，按 degree/id 决出；recent/created 必须改变入选结果
    assert [node["name"] for node in by_links["nodes"]][0] in {"Old", "Hot"}
    assert [node["name"] for node in by_recent["nodes"]] == ["Hot"]
    assert [node["name"] for node in by_created["nodes"]] == ["Fresh"]
    assert by_recent["rank_by"] == "recent"
    with pytest.raises(ValueError):
        build_tag_graph_projection(conn=conn, scope=_scope(), rank_by="random")
    conn.close()


@pytest.mark.asyncio
async def test_tag_graph_api_passes_rank_by_and_window(monkeypatch):
    from webui.blueprints import tag_graph as module

    conn = _connection()
    _seed_timeline(conn)
    scope = _scope()
    container = SimpleNamespace(db=SimpleNamespace(conn=conn), password="", sessions=set())
    monkeypatch.setattr(module, "get_container", lambda: container)

    class Provider:
        def get_request_scope(self):
            return scope

    app = Quart(__name__)
    app.extensions["wave_api_contract"] = {"request_scope_provider": Provider(), "object_refs": ObjectRefRegistry()}
    app.register_blueprint(module.tag_graph_bp)
    client = app.test_client()
    base = "/api/tag-graph?bot_id=bot-a&session_id=qq:group:g1&visibility=group&max_nodes=10"

    response = await client.get(base + "&rank_by=created&recent_window_hours=48")
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["rank_by"] == "created"
    assert payload["recent_window_hours"] == 48.0
    assert [node["name"] for node in payload["nodes"]][0] == "Fresh"
    assert all("last_seen_ts" in node and "created_at" in node for node in payload["nodes"])

    invalid = await client.get(base + "&rank_by=random")
    assert invalid.status_code == 400
    conn.close()
