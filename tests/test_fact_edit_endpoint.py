"""/api/facts/<id>/edit：人工修正客体与原话，改完回到待审，不绕过证据门禁。

线上 317 条待审事实卡在 ``unsupported_object``（其中 226 条 object 是模型自己新写的、
原话里根本不存在），只靠改提审提示词救不回来；必须让人对着证据原话把客体改成逐字片段。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from quart import Quart, jsonify

from domain.scope import RuntimeScope, ScopeCodec, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.fact_policy_repo import auto_review_reason, source_memory
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.migrations.scoped_fact_review import ensure_scoped_fact_review_schema
from engine.db.outbox_repo import OutboxRepository
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.scoped_knowledge_mutations import ScopedKnowledgeMutationGateway
from webui.api_contract import ObjectRefRegistry
from webui.container import ServiceContainer, get_container

MEMORY_ID = 11
QUOTE = "千铃以前下血本给我的k3"


def _scope(bot: str = "bot-alpha", session: str = "qq:group:g1") -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(session, "qq", "group", session.split(":")[-1]))


class _Coordinator:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._consumer_names = ("projection",)

    async def transaction(self, callback, *, actor=None):
        del actor
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = callback(self.connection)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise


@pytest.fixture
def endpoint_env(tmp_path):
    ServiceContainer.reset()
    manager = ConnectionManager(str(tmp_path / "fact-edit.sqlite3"))
    manager.executescript(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            resolution_state TEXT, quarantine INTEGER DEFAULT 0, content TEXT, sender_id TEXT,
            sender_name TEXT, timestamp REAL, source TEXT DEFAULT 'live', memory_type TEXT DEFAULT 'message');"""
    )
    ensure_scoped_derived_knowledge_schema(manager)
    ensure_scoped_fact_review_schema(manager)
    connection = manager._write_conn
    OutboxRepository.migrate(connection)
    connection.commit()
    repo = ScopedKnowledgeRepo(manager)
    gateway = ScopedKnowledgeMutationGateway(SimpleNamespace(coordinator=_Coordinator(connection)))
    registry = ObjectRefRegistry()

    container = get_container()
    container.db = SimpleNamespace(scoped_knowledge=repo)
    container.scoped_knowledge_mutations = gateway
    container.password = ""

    scope = _scope()
    connection.execute(
        """INSERT INTO memories(id, bot_id, session_id, visibility, resolution_state, quarantine, content,
               sender_id, sender_name, timestamp, source, memory_type)
           VALUES (?, ?, ?, ?, 'resolved', 0, ?, 'u1', '千铃', 100.0, 'live', 'message')""",
        (MEMORY_ID, scope.bot_id, scope.session.id, scope.visibility, QUOTE),
    )
    connection.commit()
    try:
        yield manager, connection, repo, registry, scope
    finally:
        manager.close()
        ServiceContainer.reset()


def _jsonify(value):
    """Quart 的 jsonify 会把 dict 包成 Response；测试里直接要 dict。"""
    return value


def _unpack(result):
    """成功路径只返回 jsonify(dict)，失败路径返回 (jsonify(dict), status)。"""
    return result if isinstance(result, tuple) else (result, 200)


class _Request:
    def __init__(self, body):
        self._body = body
        self.args: dict[str, str] = {}
        self.headers: dict[str, str] = {}

    async def get_json(self, silent=False):
        del silent
        return self._body


def _fact(repo, scope, *, obj="千铃以前给的k3", quote=QUOTE, status="pending"):
    return repo.upsert_scoped_fact(
        scope, subject="千铃", predicate="送过", object=obj, confidence=0.8, status=status,
        source_memory_id=MEMORY_ID, provenance={"source_quote": quote},
    )


def _row(manager, scope, fact_id):
    from webui.blueprints.facts import _find_scoped_fact

    return _find_scoped_fact(manager, scope, fact_id)


async def _call_edit(fact_id, revision, fields, registry, scope, *, bot=None, session=None, ref_revision=None):
    from webui.blueprints import facts as facts_blueprint

    request_scope = _scope(bot=bot, session=session) if bot or session else scope
    issued = int(revision if ref_revision is None else ref_revision)
    ref = registry.issue(kind="fact", locator=int(fact_id), scope=scope, revision=issued)
    payload = {
        "scope": ScopeCodec.to_dict(request_scope),
        "object_ref": {"ref": ref, "version": issued},
        "revision": int(revision),
        **fields,
    }
    app = Quart(__name__)
    app.extensions["wave_api_contract"] = {"object_refs": registry}
    with patch.object(facts_blueprint, "request", _Request(payload)):
        with patch.object(facts_blueprint, "jsonify", _jsonify):
            async with app.app_context():
                return _unpack(await facts_blueprint.edit_fact.__wrapped__(int(fact_id)))


@pytest.mark.asyncio
async def test_edit_aligns_object_with_quote_and_returns_it_to_pending(endpoint_env):
    manager, connection, repo, registry, scope = endpoint_env
    fact_id = _fact(repo, scope)
    assert auto_review_reason(connection, scope, _row(manager, scope, fact_id)) == "unsupported_object"

    response, status = await _call_edit(fact_id, 1, {"object": "下血本给我的k3"}, registry, scope)
    assert status == 200 and response["ok"] is True
    assert response["item"]["object"] == "下血本给我的k3"
    assert response["item"]["status"] == "pending", "改完必须回到待审核，不能直接生效"
    assert response["revision"] == 1, "重开的新行从 revision 1 开始"

    # 改客体属于身份变更：旧行 superseded、按新内容重开一行（新的 fact id），仍回到待审
    new_id = response["item"]["id"]
    assert new_id != fact_id
    assert connection.execute(
        "SELECT status FROM scoped_facts WHERE id=?", (fact_id,)
    ).fetchone() == ("superseded",)

    fixed = _row(manager, scope, new_id)
    assert auto_review_reason(connection, scope, fixed) is None, "原话没动、object 逐字出现后审核就该放行"
    fixed_prov = json.loads(fixed["provenance"]) if isinstance(fixed["provenance"], str) else fixed["provenance"]
    assert fixed_prov["source_quote"] == QUOTE, "改客体不该动证据原话"
    assert source_memory(connection, scope, fixed["source_memory_id"], fixed_prov["source_quote"]) is not None


@pytest.mark.asyncio
async def test_edit_quote_is_recorded_in_provenance_history(endpoint_env):
    manager, connection, repo, registry, scope = endpoint_env
    fact_id = _fact(repo, scope, obj="下血本给我的k3", quote="千铃给过的东西")

    response, status = await _call_edit(fact_id, 1, {"source_quote": QUOTE}, registry, scope)
    assert status == 200
    stored = json.loads(
        connection.execute("SELECT provenance FROM scoped_facts WHERE id=?", (fact_id,)).fetchone()[0]
    )
    assert stored["source_quote"] == QUOTE
    assert [item["previous"] for item in stored["quote_history"]] == ["千铃给过的东西"]
    assert response["item"]["revision"] == 2


@pytest.mark.asyncio
async def test_edit_rejects_already_active_facts(endpoint_env):
    manager, _, repo, registry, scope = endpoint_env
    active_id = _fact(repo, scope, status="active")
    body, code = await _call_edit(active_id, 1, {"object": "下血本给我的k3"}, registry, scope)
    assert code == 409 and body["error"]["code"] == "fact_not_editable"


@pytest.mark.asyncio
async def test_edit_rejects_stale_object_ref(endpoint_env):
    manager, _, repo, registry, scope = endpoint_env
    fact_id = _fact(repo, scope)
    # 列表页签发的是 revision=1，用户点到的是已经被改过的 revision=2 → 过期点击
    body, code = await _call_edit(fact_id, 2, {"object": "下血本给我的k3"}, registry, scope, ref_revision=1)
    assert code == 422 and body["error"]["code"] == "object_ref_stale"


@pytest.mark.asyncio
async def test_edit_rejects_blank_or_missing_fields(endpoint_env):
    manager, _, repo, registry, scope = endpoint_env
    fact_id = _fact(repo, scope)
    blank, blank_code = await _call_edit(fact_id, 1, {"object": "  "}, registry, scope)
    assert blank_code == 400 and blank["error"]["code"] == "invalid_fact_object"

    nothing, nothing_code = await _call_edit(fact_id, 1, {}, registry, scope)
    assert nothing_code == 400 and nothing["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_edit_is_scope_bound(endpoint_env):
    manager, _, repo, registry, scope = endpoint_env
    fact_id = _fact(repo, scope)
    body, code = await _call_edit(fact_id, 1, {"object": "下血本给我的k3"}, registry, scope,
                            bot="bot-other", session="qq:group:g2")
    assert code == 404 and body["error"]["code"] == "scoped_object_not_found"
