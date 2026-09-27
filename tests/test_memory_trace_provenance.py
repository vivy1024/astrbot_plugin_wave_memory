"""回复溯源：记忆 → 最近用到它的 trace、trace 详情跳转字段、记忆按编号定位与上下文。"""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from services.injection.channel_base import InjectionResult
from services.injection.trace_store import InjectionTraceStore
from webui.api_contract import ObjectRefRegistry


def _store() -> tuple[sqlite3.Connection, InjectionTraceStore]:
    conn = sqlite3.connect(":memory:")
    store = InjectionTraceStore(conn)
    store.ensure_schema()
    return conn, store


def _trace(trace_id: str, ts: float, *, bot_id: str = "2500447291", bot_profile_id: str | None = "yushu", group_id: str = "g1") -> dict:
    return {
        "trace_id": trace_id,
        "timestamp": ts,
        "mode": "full",
        "group_id": group_id,
        "sender_id": "u1",
        "sender_name": "群友",
        "bot_id": bot_id,
        "bot_profile_id": bot_profile_id,
        "message": f"消息 {trace_id}",
        "final_text": "注入文本",
        "status": "ok",
    }


def _memory_hit(*ids_scores, channel: str = "memory", filtered=()):
    result = InjectionResult.hit(
        channel,
        "记忆",
        items=[{"id": memory_id, "group_id": "g1", "preview": f"记忆 {memory_id}", "score": score} for memory_id, score in ids_scores],
    )
    result.filtered = [{"id": memory_id, "preview": "过滤", "filter_reason": "top_k"} for memory_id in filtered]
    return result


def test_details_serialization_matches_instr_pattern():
    """线上 details 用 sort_keys + 默认分隔符，id 后面紧跟 "," 或 "}"。"""
    conn, store = _store()
    store.record(_trace("t1", 100), [_memory_hit((123, 0.9))])
    details = conn.execute("SELECT details FROM injection_trace_channels WHERE channel='memory'").fetchone()[0]
    assert '"id": 123,' in details
    assert json.loads(details)["items"][0]["id"] == 123


def test_find_traces_for_memory_exact_id_items_only_and_newest_first():
    _, store = _store()
    store.record(_trace("old", 100), [_memory_hit((123, 0.5))])
    store.record(_trace("new", 300), [_memory_hit((7, 0.2), (123, 0.8))])
    store.record(_trace("prefix", 400), [_memory_hit((1234, 0.9))])
    store.record(_trace("filtered-only", 500), [_memory_hit((5, 0.1), filtered=(123,))])
    store.record(_trace("other-channel", 600), [InjectionResult.hit("facts", "事实", items=[{"rowid": 123, "id": 123}])])

    items = store.find_traces_for_memory(123, bot_id="yushu", limit=10)

    assert [item["trace_id"] for item in items] == ["new", "old"]
    first = items[0]
    assert first["channel"] == "memory"
    assert first["score"] == 0.8
    assert first["channels"][0]["rank"] == 2
    assert first["group_id"] == "g1"
    assert first["message_preview"] == "消息 new"


def test_find_traces_for_memory_is_bot_scoped_by_profile_or_bot_id():
    _, store = _store()
    store.record(_trace("astrbot", 100, bot_id="2500447291", bot_profile_id="yushu"), [_memory_hit((9, 1.0))])
    store.record(_trace("cortico", 200, bot_id="yushu", bot_profile_id=None), [_memory_hit((9, 1.0))])
    store.record(_trace("other-bot", 300, bot_id="baizz", bot_profile_id="baizz"), [_memory_hit((9, 1.0))])

    assert [item["trace_id"] for item in store.find_traces_for_memory(9, bot_id="yushu")] == ["cortico", "astrbot"]
    assert [item["trace_id"] for item in store.find_traces_for_memory(9, bot_id="baizz")] == ["other-bot"]
    assert store.find_traces_for_memory(9, bot_id="") == []


def test_find_traces_for_memory_merges_channels_per_trace_and_limits():
    _, store = _store()
    for index in range(5):
        store.record(
            _trace(f"t{index}", 100 + index),
            [_memory_hit((42, 0.7)), _memory_hit((42, 1.0), channel="fts5")],
        )

    items = store.find_traces_for_memory(42, bot_id="yushu", limit=3)

    assert [item["trace_id"] for item in items] == ["t4", "t3", "t2"]
    assert sorted(hit["channel"] for hit in items[0]["channels"]) == ["fts5", "memory"]


def test_build_memory_traces_payload_requires_bot_id():
    from webui.blueprints.injection_observatory import build_memory_traces_payload

    _, store = _store()
    store.record(_trace("t1", 100), [_memory_hit((1, 0.3))])

    payload, status = build_memory_traces_payload(store, 1, {})
    assert status == 400
    assert payload["error"]["code"] == "scope_required"

    payload, status = build_memory_traces_payload(store, 1, {"bot_id": "yushu", "limit": "5"})
    assert status == 200
    assert payload["count"] == 1
    assert payload["items"][0]["detail_url"] == "/api/observatory/traces/t1"
    assert payload["limit"] == 5
    assert payload["elapsed_ms"] >= 0


def test_trace_detail_attaches_link_fields_with_memory_scope():
    from webui.blueprints.injection_observatory import build_trace_detail_payload

    conn, store = _store()
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT, group_id TEXT)")
    conn.execute("INSERT INTO memories VALUES (123, 'yushu', '羽书:group:g1', 'group', 'g1')")
    facts = InjectionResult.hit("facts", "事实", items=[{"rowid": 8, "subject": "张三", "predicate": "喜欢", "object": "苹果", "preview": "张三 喜欢 苹果"}])
    store.record(_trace("t1", 100), [_memory_hit((123, 0.9)), facts])

    payload = build_trace_detail_payload(store, None, "t1")

    channels = {channel["channel"]: channel for channel in payload["channels"]}
    memory_link = channels["memory"]["hit_items"][0]["link"]
    assert memory_link["kind"] == "memory"
    assert memory_link["memory_id"] == 123
    assert memory_link["memory_scope"] == {"bot_id": "yushu", "session_id": "羽书:group:g1", "visibility": "group", "group_id": "g1"}
    fact_link = channels["facts"]["hit_items"][0]["link"]
    assert fact_link["subject"] == "张三"
    assert fact_link["rowid"] == 8
    assert "kind" not in fact_link


def test_jargon_channel_items_are_json_serializable_in_trace():
    """jargon 通道曾把 RuntimeScope 对象塞进 items，导致整条通道明细被记为不可序列化。"""
    import asyncio

    from services.injection.channels.jargon import JargonChannel
    from services.injection.context import InjectionContext

    scope = RuntimeScope(bot_id="yushu", visibility="group", session=SessionRef("qq:group:g1", "qq", "group", "g1"))

    class Service:
        def get_injection(self, text, runtime_scope):
            return "【黑话】\n- v我50 → 请我吃饭"

        def get_last_injection_items(self):
            return [{"word": "v我50", "meaning": "请我吃饭", "preview": "v我50 → 请我吃饭"}]

    ctx = InjectionContext(
        event=object(), req=object(), message="v我50", group_id="g1", sender_id="u1", sender_name="用户",
        bot_id="yushu", bot_profile_id="yushu", scope=scope, config={"channels": {"jargon": {}}},
    )
    result = asyncio.run(JargonChannel(jargon_service=Service()).build(ctx))
    conn, store = _store()
    store.record(_trace("t-jargon", 100), [result])
    details = json.loads(conn.execute("SELECT details FROM injection_trace_channels").fetchone()[0])

    assert details["error"] == ""
    assert details["items"][0]["word"] == "v我50"
    assert details["items"][0]["scope"] == {"bot_id": "yushu", "visibility": "group", "session_id": "qq:group:g1"}


@pytest.mark.asyncio
async def test_memory_traces_route_returns_scoped_items(monkeypatch):
    from webui.blueprints import injection_observatory as module

    conn, store = _store()
    store.record(_trace("t1", 100), [_memory_hit((77, 0.6))])
    container = SimpleNamespace(db=SimpleNamespace(conn=conn, closed=False), password="", sessions=set())
    monkeypatch.setattr(module, "get_container", lambda: container)
    app = Quart(__name__)
    app.register_blueprint(module.injection_observatory_bp)
    client = app.test_client()

    response = await client.get("/api/observatory/memories/77/traces?bot_id=yushu&limit=10")
    assert response.status_code == 200
    body = await response.get_json()
    assert [item["trace_id"] for item in body["items"]] == ["t1"]

    missing_bot = await client.get("/api/observatory/memories/77/traces")
    assert missing_bot.status_code == 400


def _memories_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT, content TEXT,
            vector BLOB, timestamp REAL, importance REAL, access_count INTEGER, memory_type TEXT,
            source TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT,
            version INTEGER, quarantine INTEGER DEFAULT 0
        )"""
    )
    rows = [
        (1, "g1", "yushu", "早先"),
        (2, "g1", "baizz", "别的 Bot"),
        (3, "g1", "yushu", "前一句"),
        (4, "g2", "yushu", "别的群"),
        (5, "g1", "yushu", "锚点"),
        (6, "g1", "yushu", "后一句"),
        (7, "g1", "yushu", "再后一句"),
    ]
    for memory_id, group_id, bot_id, content in rows:
        conn.execute(
            """INSERT INTO memories (id, group_id, sender_id, sender_name, content, timestamp, importance,
                   access_count, memory_type, source, bot_id, session_id, visibility, resolution_state, version)
               VALUES (?, ?, 'u1', '群友', ?, ?, 0.5, 0, 'message', 'core', ?, ?, 'group', 'resolved', 1)""",
            (memory_id, group_id, content, 1000 + memory_id, bot_id, f"qq:group:{group_id}"),
        )
    return conn


@pytest.mark.asyncio
async def test_memory_list_id_filter_and_context_endpoint(monkeypatch):
    from webui.blueprints import memories as module
    from webui.middleware import auth as auth_module

    conn = _memories_conn()
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
    scope_query = "bot_id=yushu&session_id=qq:group:g1&visibility=group"

    listed = await (await client.get(f"/api/memories?{scope_query}&id=5")).get_json()
    assert [item["id"] for item in listed["items"]] == [5]
    ref = listed["items"][0]["ref"]
    assert ref.startswith("oref.")

    other_group = await (await client.get(f"/api/memories?{scope_query}&id=4")).get_json()
    assert other_group["items"] == []

    no_ref = await client.get(f"/api/memories/5/context?{scope_query}")
    assert no_ref.status_code == 400

    response = await client.get(f"/api/memories/5/context?{scope_query}&ref={ref}&before=2&after=1")
    assert response.status_code == 200
    body = await response.get_json()
    assert [(message["id"], message["role"]) for message in body["messages"]] == [
        (1, "before"), (3, "before"), (5, "anchor"), (6, "after"),
    ]
