import asyncio
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.proactive_audit import read_proactive_relationship_context


def load_context_reader():
    """适配层：直接调用正式实现（main.py 的转发包装已删除）。

    这里保留 plugin 形参签名，让用例继续表达「插件在真实装配下如何读取」；
    内部不再从 main.py 反编译方法，避免测试与实现位置耦合。
    """
    plugin_holder = {}

    async def _reader(plugin, scope, event, *, now=None):
        plugin_holder["plugin"] = plugin
        return await read_proactive_relationship_context(
            scope,
            event,
            coordinator=getattr(getattr(plugin, "write_gateway", None), "coordinator", None),
            repository=getattr(getattr(plugin, "db", None), "soul_repository", None),
            bot_ids=getattr(plugin, "_bot_qq_ids", ()),
            now=now,
        )

    return _reader


class FakeCoordinator:
    def __init__(self, connection):
        self.connection = connection

    async def read(self, callback):
        return callback(self.connection)


class FakeRepository:
    def __init__(self, relationships):
        self.relationships = relationships
        self.calls = []

    def list_relationships(self, scope, **kwargs):
        self.calls.append((scope, kwargs))
        relationship = self.relationships.get(scope.subject_principal_id)
        return [relationship] if relationship is not None else []


def make_scope(subject="qq:user:u1"):
    return RuntimeScope(
        bot_id="yushu",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id=subject,
    )


def relationship(trust=70, hostility=5, depth=55):
    return {
        "affinity": 50,
        "state": "friendly",
        "revision": 3,
        "dimensions": {
            "familiarity": 40,
            "trust": trust,
            "fun": 45,
            "hostility": hostility,
            "depth": depth,
        },
    }


def test_context_uses_current_resolved_sender_and_recent_canonical_cospeakers():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute("""CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        sender_id TEXT,
        timestamp REAL,
        bot_id TEXT,
        session_id TEXT,
        visibility TEXT,
        resolution_state TEXT,
        quarantine INTEGER
    )""")
    connection.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (10, "u2", 980.0, "yushu", "qq:group:g1", "group", "resolved", 0),
            (11, "bot-qq", 985.0, "yushu", "qq:group:g1", "group", "resolved", 0),
            (12, "u3", 930.0, "yushu", "qq:group:g1", "group", "resolved", 0),
            (13, "u4", 900.0, "other", "qq:group:g1", "group", "resolved", 0),
        ],
    )
    connection.commit()
    repo = FakeRepository({"qq:user:u1": relationship(), "qq:user:u2": relationship(depth=30)})
    plugin = SimpleNamespace(
        write_gateway=SimpleNamespace(coordinator=FakeCoordinator(connection)),
        db=SimpleNamespace(soul_repository=repo),
        _bot_qq_ids=["bot-qq"],
    )
    event = SimpleNamespace(get_self_id=lambda: "bot-qq", message_id="evt-1")
    context_reader = load_context_reader()

    result = asyncio.run(context_reader(plugin, make_scope(), event, now=1000.0))

    assert result["ok"] is True
    assert result["subjects"] == ["qq:user:u1", "qq:user:u2"]
    assert result["source_memories"][0]["role"] == "primary"
    assert result["source_memories"][0]["event_id"] == "evt-1"
    assert result["source_memories"][1] == {
        "memory_id": 10,
        "sender_id": "u2",
        "timestamp": 980.0,
        "role": "co_subject",
        "source": "canonical_memory",
    }
    assert len(repo.calls) == 2
    assert all(call[0].session.id == "qq:group:g1" for call in repo.calls)


def test_context_fails_closed_when_canonical_memory_schema_is_incomplete():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, sender_id TEXT, timestamp REAL)")
    connection.commit()
    repo = FakeRepository({"qq:user:u1": relationship()})
    plugin = SimpleNamespace(
        write_gateway=SimpleNamespace(coordinator=FakeCoordinator(connection)),
        db=SimpleNamespace(soul_repository=repo),
        _bot_qq_ids=["bot-qq"],
    )
    event = SimpleNamespace(get_self_id=lambda: "bot-qq", message_id="evt-1")
    context_reader = load_context_reader()

    result = asyncio.run(context_reader(plugin, make_scope(), event, now=1000.0))

    assert result["ok"] is False
    assert result["reason_code"] == "proactive_memory_read_failed"
    assert result.get("error")
    assert repo.calls == []
