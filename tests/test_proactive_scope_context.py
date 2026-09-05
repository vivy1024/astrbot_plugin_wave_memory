import ast
import asyncio
import copy
import hashlib
import math
import time
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.belief_gating import snapshot_from_relationship
from services.identity_safety import is_identity_contamination
from services.proactive_audit import read_proactive_relationship_context


def load_context_reader():
    source_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    plugin_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WaveMemoryPlugin")
    method = copy.deepcopy(next(
        node for node in plugin_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_read_proactive_relationship_context"
    ))
    method.decorator_list = []
    for argument in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs):
        argument.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = {
        "asyncio": asyncio,
        "hashlib": hashlib,
        "math": math,
        "time": time,
        "RuntimeScope": RuntimeScope,
        "snapshot_from_relationship": snapshot_from_relationship,
        "logger": SimpleNamespace(warning=lambda *args, **kwargs: None),
        "is_identity_contamination": is_identity_contamination,
        "read_proactive_relationship_context": read_proactive_relationship_context,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["_read_proactive_relationship_context"]


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
