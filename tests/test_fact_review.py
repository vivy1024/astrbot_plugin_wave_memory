"""人工事实审核：状态机、冲突语义、原子审计、幂等与 Scope 隔离。"""

from __future__ import annotations

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
