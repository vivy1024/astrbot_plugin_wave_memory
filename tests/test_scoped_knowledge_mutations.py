from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.outbox_repo import OutboxRepository
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.scoped_knowledge_mutations import (
    ScopedKnowledgeMutationGateway,
    ScopedKnowledgeMutationTarget,
    ScopedKnowledgeRevisionConflict,
)


def _scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha", "group", SessionRef("qq:group:g1", "qq", "group", "g1")
    )


class _Coordinator:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._consumer_names = ("projection", "runtime_refresh")
        self.actors: list[str | None] = []

    async def transaction(self, callback, *, actor=None):
        self.actors.append(actor)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = callback(self.connection)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise


@pytest.fixture
def mutation_env(tmp_path):
    manager = ConnectionManager(str(tmp_path / "scoped-mutations.sqlite3"))
    ensure_scoped_derived_knowledge_schema(manager)
    connection = manager._write_conn
    OutboxRepository.migrate(connection)
    connection.commit()
    coordinator = _Coordinator(connection)
    gateway = ScopedKnowledgeMutationGateway(SimpleNamespace(coordinator=coordinator))
    repo = ScopedKnowledgeRepo(manager)
    try:
        yield manager, connection, coordinator, gateway, repo
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_fact_cas_replacement_tombstone_and_idempotent_outbox(mutation_env):
    manager, connection, coordinator, gateway, repo = mutation_env
    fact_id = repo.upsert_scoped_fact(
        _scope(), subject="甲", predicate="喜欢", object="猫", confidence=0.4, status="reviewed"
    )

    updated = await gateway.update_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        fields={"confidence": 0.8},
    )
    replayed = await gateway.update_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        fields={"confidence": 0.8},
    )
    assert updated == replayed
    assert (updated.locator, updated.revision) == (fact_id, 2)
    assert connection.execute(
        "SELECT confidence, revision, status FROM scoped_facts WHERE id=?", (fact_id,)
    ).fetchone() == (0.8, 2, "reviewed")
    assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM domain_outbox").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM outbox_deliveries").fetchone()[0] == 2

    with pytest.raises(ScopedKnowledgeRevisionConflict):
        await gateway.update_fact(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
            fields={"confidence": 0.7},
        )

    replaced = await gateway.update_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 2),
        fields={"subject": "乙"},
    )
    assert replaced.locator != fact_id
    assert replaced.revision == 1
    assert replaced.previous_locator == fact_id
    assert connection.execute(
        "SELECT status, revision FROM scoped_facts WHERE id=?", (fact_id,)
    ).fetchone() == ("superseded", 3)
    assert [row["subject"] for row in repo.list_scoped_facts(_scope())] == ["乙"]

    deleted = await gateway.delete_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", replaced.locator, 1),
    )
    replayed_delete = await gateway.delete_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", replaced.locator, 1),
    )
    assert replayed_delete == deleted
    assert deleted.status == "deleted"
    assert repo.list_scoped_facts(_scope()) == []

    tombstone_id = repo.upsert_scoped_fact(
        _scope(), subject="乙", predicate="喜欢", object="猫", confidence=1.0, status="reviewed"
    )
    assert tombstone_id == replaced.locator
    assert connection.execute(
        "SELECT status, revision FROM scoped_facts WHERE id=?", (replaced.locator,)
    ).fetchone() == ("deleted", 2)
    assert coordinator.actors == [
        "webui.kg.fact.update",
        "webui.kg.fact.update",
        "webui.kg.fact.update",
        "webui.kg.fact.update",
        "webui.kg.fact.delete",
        "webui.kg.fact.delete",
    ]


@pytest.mark.asyncio
async def test_tag_relation_cas_replacement_and_tombstone(mutation_env):
    _, connection, _, gateway, repo = mutation_env
    source = repo.upsert_scoped_tag(_scope(), name="甲")
    target = repo.upsert_scoped_tag(_scope(), name="乙")
    relation_id = repo.upsert_scoped_tag_relation(
        _scope(), source_tag_id=source, target_tag_id=target, relation_type="相关", weight=0.5
    )

    weighted = await gateway.update_tag_relation(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("tag_relation", relation_id, 1),
        fields={"weight": 0.9},
    )
    assert (weighted.locator, weighted.revision) == (relation_id, 2)

    replaced = await gateway.update_tag_relation(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("tag_relation", relation_id, 2),
        fields={"relation_type": "包含"},
    )
    assert replaced.locator != relation_id
    assert connection.execute(
        "SELECT status, revision FROM scoped_tag_relations WHERE id=?", (relation_id,)
    ).fetchone() == ("superseded", 3)

    deleted = await gateway.delete_tag_relation(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("tag_relation", replaced.locator, 1),
    )
    assert deleted.status == "deleted"
    assert connection.execute(
        "SELECT status, revision FROM scoped_tag_relations WHERE id=?", (replaced.locator,)
    ).fetchone() == ("deleted", 2)

    same_id = repo.upsert_scoped_tag_relation(
        _scope(), source_tag_id=source, target_tag_id=target, relation_type="包含", weight=1.0
    )
    assert same_id == replaced.locator
    assert connection.execute(
        "SELECT status, revision FROM scoped_tag_relations WHERE id=?", (replaced.locator,)
    ).fetchone() == ("deleted", 2)


@pytest.mark.asyncio
async def test_gateway_rejects_fields_outside_explore_mutation_contract(mutation_env):
    _, _, _, gateway, repo = mutation_env
    fact_id = repo.upsert_scoped_fact(
        _scope(), subject="甲", predicate="喜欢", object="猫", status="reviewed"
    )
    source = repo.upsert_scoped_tag(_scope(), name="甲")
    target = repo.upsert_scoped_tag(_scope(), name="乙")
    relation_id = repo.upsert_scoped_tag_relation(
        _scope(), source_tag_id=source, target_tag_id=target, relation_type="相关"
    )

    with pytest.raises(ValueError, match="unsupported fact fields"):
        await gateway.update_fact(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
            fields={"status": "reviewed"},
        )
    with pytest.raises(ValueError, match="unsupported tag relation fields"):
        await gateway.update_tag_relation(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("tag_relation", relation_id, 1),
            fields={"metadata": {"forbidden": True}},
        )


@pytest.mark.asyncio
async def test_fact_source_quote_is_editable_in_provenance_and_leaves_history(mutation_env):
    """人工可修正证据原话：改的是 provenance 里的 source_quote，旧值留痕。

    线上 317 条待审事实卡在 ``object in quote``，其中一部分是原话本身记错了，
    需要人工对着聊天上下文改 ``source_quote``；这一改必须留在审计里可回溯。
    """
    _, connection, _, gateway, repo = mutation_env
    fact_id = repo.upsert_scoped_fact(
        _scope(), subject="甲", predicate="住在", object="北港",
        status="pending", provenance={"source_quote": "我住的地方在北边"},
    )

    result = await gateway.update_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        fields={"source_quote": "我住在北港"},
    )
    assert (result.locator, result.revision) == (fact_id, 2), "只改原话：原地更新，不重开一行"
    stored = json.loads(
        connection.execute("SELECT provenance FROM scoped_facts WHERE id=?", (fact_id,)).fetchone()[0]
    )
    assert stored["source_quote"] == "我住在北港"
    assert [item["previous"] for item in stored["quote_history"]] == ["我住的地方在北边"]
    assert connection.execute("SELECT object, status FROM scoped_facts WHERE id=?", (fact_id,)).fetchone() == ("北港", "pending")

    # 身份字段（object）改动仍然按「取代旧行、新行回到待审」处理，不能用编辑绕过证据门禁
    replaced = await gateway.update_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 2),
        fields={"object": "南湾", "source_quote": "我住在南湾"},
    )
    assert replaced.locator != fact_id and replaced.status == "pending"
    assert connection.execute(
        "SELECT status FROM scoped_facts WHERE id=?", (fact_id,)
    ).fetchone() == ("superseded",)
    fresh = json.loads(
        connection.execute("SELECT provenance FROM scoped_facts WHERE id=?", (replaced.locator,)).fetchone()[0]
    )
    assert fresh["source_quote"] == "我住在南湾"
