from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart

from services.injection.trace_store import InjectionTraceStore
from tests.test_tag_graph import _connection, _scope, _seed_timeline
from webui.api_contract import ObjectRefRegistry
from webui.graph_projection import build_tag_graph_projection
from webui.recall_replay import build_recall_replay


def _record(store: InjectionTraceStore, trace_id: str, ts: float, memory_ids: list[int], *, bot: str = "bot-a",
            session: str = "qq:group:g1", message: str = "群友的一句话") -> None:
    store.record(
        {
            "trace_id": trace_id, "timestamp": ts, "group_id": session.rsplit(":", 1)[-1],
            "sender_id": "u1", "sender_name": "Alice", "bot_id": "10001", "bot_profile_id": bot,
            "message": message, "final_text": "注入给模型的上下文，不是回复",
            "metadata": {"runtime_scope": {"session": {"id": session}}},
        },
        [
            {"channel": "memory", "status": "ok", "items": [{"id": mid, "score": 0.9, "preview": f"记忆 {mid}"} for mid in memory_ids]},
            # fts5 与 memory 命中同一条记忆时只算一次；filtered 里的不算被想起
            {"channel": "fts5", "status": "ok", "items": [{"id": memory_ids[0], "score": 1.0}], "filtered": [{"id": 999}]},
            {"channel": "persona", "status": "ok", "items": [{"id": 3}]},
        ],
    )


def _setup():
    conn = _connection()
    ids = _seed_timeline(conn)
    store = InjectionTraceStore(conn)
    store.ensure_schema()
    return conn, ids, store


def test_recall_events_reads_injected_memories_in_time_order_and_scope():
    conn, _ids, store = _setup()
    _record(store, "t2", 9_800.0, [5])
    _record(store, "t1", 9_700.0, [4, 5])
    _record(store, "other-bot", 9_750.0, [4], bot="bot-b")
    _record(store, "other-group", 9_760.0, [4], session="qq:group:g2")
    _record(store, "too-old", 100.0, [4])

    events = store.recall_events(from_ts=9_000.0, to_ts=10_000.0, bot_id="bot-a", session_id="qq:group:g1")

    assert [event["trace_id"] for event in events] == ["t1", "t2"]
    assert [memory["id"] for memory in events[0]["memories"]] == [4, 5]
    assert events[0]["memories"][0]["channel"] == "memory"
    assert "final_text_preview" not in events[0]
    conn.close()


def test_replay_lights_tags_of_recalled_memories_and_puts_them_on_the_graph():
    conn, ids, store = _setup()
    _record(store, "t1", 9_700.0, [1])     # Old
    _record(store, "t2", 9_800.0, [3, 5])  # Old+Hot, Hot+Fresh

    payload = build_recall_replay(conn=conn, trace_store=store, scope=_scope(), hours=1, max_nodes=10, now=10_000.0)

    assert [event["trace_id"] for event in payload["events"]] == ["t1", "t2"]
    assert payload["events"][0]["tags"] == [f"tag:{ids['Old']}"]
    assert set(payload["events"][1]["tags"]) == {f"tag:{ids['Old']}", f"tag:{ids['Hot']}", f"tag:{ids['Fresh']}"}
    assert payload["events"][1]["memories"][0]["preview"] == "记忆 3"
    node_ids = {node["id"] for node in payload["graph"]["nodes"]}
    assert set(payload["events"][1]["tags"]) <= node_ids
    assert payload["stats"] == {
        "event_count": 2, "memory_hits": 3, "tagged_memory_hits": 3,
        "recalled_tags": 3, "recalled_tags_on_graph": 3, "events_with_tags": 2,
    }
    conn.close()


def test_focus_tags_take_priority_over_rank_when_max_nodes_is_small():
    conn, ids, _store = _setup()
    # 按 recent 排 Hot 第一；聚焦 Old 后 Old 必须进图
    plain = build_tag_graph_projection(conn=conn, scope=_scope(), max_nodes=1, rank_by="recent", now=10_000.0)
    assert [node["name"] for node in plain["nodes"]] == ["Hot"]
    focused = build_tag_graph_projection(
        conn=conn, scope=_scope(), max_nodes=1, rank_by="recent", now=10_000.0, focus_tag_weights={ids["Old"]: 2.0},
    )
    assert [node["name"] for node in focused["nodes"]] == ["Old"]
    conn.close()


def test_replay_without_traces_still_returns_a_graph():
    conn, _ids, store = _setup()
    payload = build_recall_replay(conn=conn, trace_store=store, scope=_scope(), hours=24, max_nodes=10, now=10_000.0)
    assert payload["events"] == []
    assert payload["graph"]["nodes"]
    assert payload["stats"]["event_count"] == 0
    conn.close()


@pytest.mark.asyncio
async def test_replay_api_decorates_graph_and_bounds_params(monkeypatch):
    from webui.blueprints import tag_graph as module

    conn, _ids, store = _setup()
    import time as time_module
    now = time_module.time()
    _record(store, "t1", now - 60, [5])
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

    response = await client.get("/api/tag-graph/replay?bot_id=bot-a&session_id=qq:group:g1&visibility=group&hours=99999&max_nodes=1")
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["window"]["hours"] == 168.0
    assert payload["events"][0]["trace_id"] == "t1"
    assert payload["graph"]["nodes"][0]["ref"].startswith("oref.")
    conn.close()


def test_all_sessions_maps_other_session_tags_by_name_and_since_is_incremental():
    conn, ids, store = _setup()
    # 另一个会话（直播弹幕）里有同名标签 "Hot" 与一个星空上没有的标签
    other = [int(conn.execute(
        """INSERT INTO scoped_tags(bot_id,session_id,visibility,name,tag_type,description,confidence,metadata,status,revision,created_at,updated_at)
           VALUES ('bot-a','bilibili:group:9','group',?,'topic','',0.9,'{}','active',1,1,1)""", (name,)).lastrowid)
        for name in ("hot", "OnlyStream")]
    conn.execute(
        """INSERT INTO memories(id,bot_id,session_id,visibility,content,sender_id,sender_name,timestamp,importance,source,version)
           VALUES (50,'bot-a','bilibili:group:9','group','弹幕里的记忆','u9','观众',9600,0.5,'chat',1)""")
    conn.executemany(
        """INSERT INTO scoped_memory_tags(bot_id,session_id,visibility,memory_id,tag_id,position,relevance,created_at)
           VALUES ('bot-a','bilibili:group:9','group',50,?,?,0.9,9600)""", [(other[0], 1), (other[1], 2)])
    conn.commit()
    _record(store, "qq", 9_700.0, [1])
    _record(store, "live", 9_800.0, [50], session="bilibili:group:9", message="弹幕：你好")

    only_session = build_recall_replay(conn=conn, trace_store=store, scope=_scope(), hours=1, max_nodes=10, now=10_000.0)
    assert [event["trace_id"] for event in only_session["events"]] == ["qq"]

    payload = build_recall_replay(conn=conn, trace_store=store, scope=_scope(), hours=1, max_nodes=10, now=10_000.0, all_sessions=True)
    live = payload["events"][-1]
    assert live["session_id"] == "bilibili:group:9"
    assert live["tags"] == [f"tag:{ids['Hot']}"]
    assert live["tag_names"] == ["hot", "OnlyStream"]

    incremental = build_recall_replay(
        conn=conn, trace_store=store, scope=_scope(), hours=1, now=10_000.0,
        all_sessions=True, since=9_750.0, include_graph=False,
    )
    assert incremental["graph"] is None
    assert [event["trace_id"] for event in incremental["events"]] == ["live"]
    assert incremental["events"][0]["tags"] == [f"tag:{ids['Hot']}"]
    conn.close()
