"""人工事实审核：状态机、冲突语义、原子审计、幂等与 Scope 隔离。"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.migrations.scoped_fact_review import ensure_scoped_fact_review_schema
from engine.db.outbox_repo import OutboxRepository
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.scoped_knowledge_mutations import (
    ScopedKnowledgeMutationGateway,
    ScopedKnowledgeMutationTarget,
    ScopedKnowledgeNotFound,
    ScopedKnowledgeRevisionConflict,
)


def _scope(bot: str = "bot-alpha", session: str = "qq:group:g1") -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(session, "qq", "group", session.split(":")[-1]))


class _Coordinator:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._consumer_names = ("projection",)
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
def env(tmp_path):
    manager = ConnectionManager(str(tmp_path / "fact-review.sqlite3"))
    ensure_scoped_derived_knowledge_schema(manager)
    ensure_scoped_fact_review_schema(manager)
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


def _fact(repo, *, subject="小明", predicate="住在", obj="上海", confidence=0.85, status="pending"):
    return repo.upsert_scoped_fact(
        _scope(), subject=subject, predicate=predicate, object=obj,
        confidence=confidence, status=status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,has_conflict,status,event_type",
    [("approve", False, "active", "scoped_fact.approved"),
     ("approve", True, "conflict", "scoped_fact.conflict"),
     ("reject", False, "rejected", "scoped_fact.rejected")],
)
async def test_review_receipt_matches_outbox_and_persisted_status(env, action, has_conflict, status, event_type):
    _, connection, _, gateway, repo = env
    if has_conflict:
        _fact(repo, obj="上海", status="active")
    fact_id = _fact(repo, obj="北京")
    result = await gateway.review_fact(
        scope=_scope(), target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        action=action, idempotency_key="receipt-review",
    )
    receipt = json.loads(connection.execute(
        "SELECT result_json FROM write_operations WHERE operation_id=?", (result.operation_id,),
    ).fetchone()[0])
    effect = receipt["effects"][0]
    assert effect["event_type"] == event_type
    assert receipt["entities"][0]["change_type"] == event_type.rsplit(".", 1)[-1]
    assert receipt["entities"][0]["status"] == status
    assert connection.execute("SELECT status FROM scoped_facts WHERE id=?", (fact_id,)).fetchone() == (status,)
    assert connection.execute(
        "SELECT event_type FROM domain_outbox WHERE event_id=?", (effect["event_id"],),
    ).fetchone() == (event_type,)
    replay = await gateway.review_fact(
        scope=_scope(), target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        action=action, idempotency_key="receipt-review",
    )
    assert replay == result


@pytest.mark.asyncio
async def test_approve_pending_fact_becomes_active_with_audit(env):
    _, connection, _, gateway, repo = env
    fact_id = _fact(repo)

    result = await gateway.review_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        action="approve",
        reason="原话清晰",
    )

    assert result.status == "active"
    assert connection.execute(
        "SELECT status FROM scoped_facts WHERE id=?", (fact_id,)
    ).fetchone() == ("active",)
    audit = connection.execute(
        "SELECT fact_id, action, from_status, to_status, relation, reason FROM scoped_fact_reviews"
    ).fetchone()
    assert audit == (fact_id, "approve", "pending", "active", "compatible", "原话清晰")


@pytest.mark.asyncio
async def test_approve_conflicting_fact_lands_conflict_not_active(env):
    """与既有事实冲突时不能批成 active —— 与 review_scoped_fact_history 同口径。"""
    _, connection, _, gateway, repo = env
    _fact(repo, predicate="住在", obj="上海", status="active")
    candidate = _fact(repo, predicate="住在", obj="北京")

    result = await gateway.review_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", candidate, 1),
        action="approve",
    )

    assert result.status == "conflict"
    assert connection.execute(
        "SELECT status FROM scoped_facts WHERE id=?", (candidate,)
    ).fetchone() == ("conflict",)
    assert connection.execute(
        "SELECT relation, conflict_with_fact_id FROM scoped_fact_reviews"
    ).fetchone()[0] == "conflicts"


@pytest.mark.asyncio
async def test_reject_marks_rejected_and_keeps_audit(env):
    _, connection, _, gateway, repo = env
    fact_id = _fact(repo)

    result = await gateway.review_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        action="reject",
        reason="没有原话支撑",
    )

    assert result.status == "rejected"
    assert connection.execute(
        "SELECT action, to_status, reason FROM scoped_fact_reviews"
    ).fetchone() == ("reject", "rejected", "没有原话支撑")


@pytest.mark.asyncio
async def test_invalid_transition_and_terminal_states_rejected(env):
    _, connection, _, gateway, repo = env
    active = _fact(repo, status="active")
    rejected = _fact(repo, subject="小红", obj="北京", status="rejected")
    deleted = _fact(repo, subject="小刚", obj="广州", status="deleted")

    with pytest.raises(ValueError):
        await gateway.review_fact(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("fact", active, 1),
            action="approve",
        )
    with pytest.raises(ValueError):
        await gateway.review_fact(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("fact", rejected, 1),
            action="approve",
        )
    with pytest.raises(ScopedKnowledgeNotFound):
        await gateway.review_fact(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("fact", deleted, 1),
            action="approve",
        )
    assert connection.execute("SELECT COUNT(*) FROM scoped_fact_reviews").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cross_scope_fact_is_not_found(env):
    _, _, _, gateway, repo = env
    fact_id = _fact(repo)

    other = RuntimeScope("bot-alpha", "group", SessionRef("qq:group:g2", "qq", "group", "g2"))
    with pytest.raises(ValueError):
        await gateway.review_fact(
            scope=other,
            target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
            action="approve",
        )
    other_bot = _scope(bot="bot-beta")
    with pytest.raises(ScopedKnowledgeNotFound):
        await gateway.review_fact(
            scope=other_bot,
            target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
            action="approve",
        )


@pytest.mark.asyncio
async def test_review_is_idempotent_per_key_and_stale_revision_conflicts(env):
    _, connection, _, gateway, repo = env
    fact_id = _fact(repo)

    first = await gateway.review_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        action="approve",
        idempotency_key="review-1",
    )
    replay = await gateway.review_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
        action="approve",
        idempotency_key="review-1",
    )
    assert replay == first
    assert connection.execute("SELECT COUNT(*) FROM scoped_fact_reviews").fetchone()[0] == 1

    with pytest.raises(ScopedKnowledgeRevisionConflict):
        await gateway.review_fact(
            scope=_scope(),
            target=ScopedKnowledgeMutationTarget("fact", fact_id, 1),
            action="approve",
            idempotency_key="review-2",
        )


@pytest.mark.asyncio
async def test_supersede_retires_existing_fact_in_same_transaction(env):
    _, connection, _, gateway, repo = env
    existing = _fact(repo, predicate="住在", obj="上海", status="active")
    repo.upsert_scoped_fact(
        _scope(), subject="小明", predicate="住在", object="北京", confidence=0.9, status="pending",
        provenance={"supersedes": True, "supersedes_fact_id": existing},
    )
    candidate = connection.execute(
        "SELECT id FROM scoped_facts WHERE object='北京'"
    ).fetchone()[0]

    result = await gateway.review_fact(
        scope=_scope(),
        target=ScopedKnowledgeMutationTarget("fact", int(candidate), 1),
        action="approve",
    )

    assert result.status == "active"
    assert connection.execute(
        "SELECT status FROM scoped_facts WHERE id=?", (existing,)
    ).fetchone() == ("superseded",)


@pytest.mark.asyncio
async def test_fact_object_ref_revision_matches_db_and_passes_validation(env):
    from webui.facts_evidence import fact_object_ref, fact_revision
    from webui.blueprints.facts import _require_object_ref
    from webui.blueprints.facts import _find_scoped_fact
    from webui.api_contract import ObjectRefRegistry
    from quart import Quart

    cm, connection, _, gateway, repo = env
    fact_id = _fact(repo, predicate="住在", obj="杭州", status="pending")
    scope = _scope()
    row = _find_scoped_fact(cm, scope, fact_id)
    assert row["revision"] == 1

    app = Quart(__name__)
    reg = ObjectRefRegistry()
    app.extensions["wave_api_contract"] = {"object_refs": reg}
    async with app.app_context():
        ref_payload = fact_object_ref(row, scope, reg)
        assert ref_payload is not None
        assert ref_payload["version"] == 1
        assert fact_revision(row) == 1

        body = {
            "object_ref": ref_payload,
            "revision": row["revision"],
        }
        # 必须顺利通过，不抛出 object_ref_stale
        _require_object_ref(body, locator=fact_id, scope=scope, item=row)
