"""信念冷启动：经历证据路径可在没有已批准事实时升格，且必须 fail-closed。"""

from __future__ import annotations

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.memory_repo import MemoryRepo
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.belief_engine import first_memory_id_from_episode, is_episode_backed
from services.belief_lifecycle import BeliefLifecycleService


def _scope(bot: str = "bot-alpha", session: str = "qq:group:g1") -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(session, "qq", "group", session.split(":")[-1]))


@pytest.fixture
def repo(tmp_path):
    manager = ConnectionManager(str(tmp_path / "belief-cold-start.sqlite3"))
    # memories 表由 MemoryRepo 建立，memories_v2 迁移必须在它之后执行；
    # 这里只需 id/content/scope 列，直接建表即可（避免依赖迁移顺序）。
    manager.execute_write(
        """CREATE TABLE memories(
               id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, bot_id TEXT, session_id TEXT,
               visibility TEXT, timestamp REAL, resolution_state TEXT DEFAULT 'resolved',
               quarantine INTEGER DEFAULT 0)"""
    )
    manager.commit()
    ensure_scoped_derived_knowledge_schema(manager)
    try:
        yield ScopedKnowledgeRepo(manager), manager
    finally:
        manager.close()


def _memory(manager, scope: RuntimeScope, *, content: str, resolution_state: str = "resolved", quarantine: int = 0) -> int:
    connection = manager._write_conn
    connection.execute(
        """INSERT INTO memories(content, bot_id, session_id, visibility, timestamp, resolution_state, quarantine)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content, scope.bot_id, scope.session.id, scope.visibility, 1.0, resolution_state, quarantine),
    )
    connection.commit()
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _provenance(episode_id=7, memory_ids=(1, 2)):
    return {
        "producer": "belief_emergence",
        "episode_id": episode_id,
        "source_memory_ids": list(memory_ids),
        "confidence_policy_version": "evidence-v1",
    }


def test_episode_backed_requires_two_healthy_memories(repo):
    scoped_repo, manager = repo
    scope = _scope()
    first = _memory(manager, scope, content="他说过压力很大")
    second = _memory(manager, scope, content="他昨天没来")

    assert is_episode_backed(scoped_repo, scope, _provenance(memory_ids=(first, second))) is True
    assert is_episode_backed(scoped_repo, scope, _provenance(memory_ids=(first,))) is False
    assert is_episode_backed(scoped_repo, scope, _provenance(episode_id=None, memory_ids=(first, second))) is False
    assert is_episode_backed(scoped_repo, scope, {"producer": "other", "episode_id": 1, "source_memory_ids": [first, second]}) is False


@pytest.mark.parametrize("kwargs", [{"resolution_state": "unresolved"}, {"quarantine": 1}])
def test_episode_backed_rejects_unhealthy_or_quarantined(repo, kwargs):
    scoped_repo, manager = repo
    scope = _scope()
    healthy = _memory(manager, scope, content="健康记忆")
    unhealthy = _memory(manager, scope, content="不健康记忆", **kwargs)

    assert is_episode_backed(scoped_repo, scope, _provenance(memory_ids=(healthy, unhealthy))) is False


def test_episode_backed_rejects_cross_scope_memory(repo):
    """跨群 / 跨 Bot 的记忆不能用来给本群的信念做证据。"""
    scoped_repo, manager = repo
    scope = _scope()
    mine = _memory(manager, scope, content="本群记忆")
    other_group = _memory(manager, _scope(session="qq:group:g2"), content="别的群的记忆")

    assert is_episode_backed(scoped_repo, scope, _provenance(memory_ids=(mine, other_group))) is False


def test_approve_activates_belief_from_episode_without_any_approved_fact(repo):
    """冷启动核心断言：一条已批准事实都没有时，信念仍可升格为 active。"""
    scoped_repo, manager = repo
    scope = _scope()
    first = _memory(manager, scope, content="他说过压力很大")
    second = _memory(manager, scope, content="他昨天没来")
    belief_id = scoped_repo.upsert_scoped_belief(
        scope,
        belief_key="episode-v1:abc",
        content="他最近状态不太好",
        belief_type="world_view",
        strength=0.0,
        status="pending",
        provenance=_provenance(memory_ids=(first, second)),
    )

    assert scoped_repo.list_scoped_facts(scope) == []
    result = BeliefLifecycleService(scoped_repo).transition(scope, belief_id, "approve")

    assert result["status"] == "active"
    active = scoped_repo.list_scoped_beliefs(scope, status="active")
    assert [row["id"] for row in active] == [belief_id]
    assert active[0]["source_memory_id"] in {first, second}


def test_approve_still_rejects_belief_without_any_evidence(repo):
    """双路都不满足时仍然拒绝，不能因为新开了经历路径就放宽成无条件通过。"""
    scoped_repo, manager = repo
    scope = _scope()
    belief_id = scoped_repo.upsert_scoped_belief(
        scope,
        belief_key="episode-v1:no-evidence",
        content="凭空判断",
        belief_type="world_view",
        strength=0.0,
        status="pending",
        provenance={"producer": "belief_emergence", "episode_id": 9, "source_memory_ids": [999]},
    )

    with pytest.raises(ValueError, match="belief_facts_required"):
        BeliefLifecycleService(scoped_repo).transition(scope, belief_id, "approve")


def test_first_memory_id_from_episode_prefers_healthy(repo):
    scoped_repo, manager = repo
    scope = _scope()
    quarantined = _memory(manager, scope, content="被隔离", quarantine=1)
    healthy = _memory(manager, scope, content="健康")

    assert first_memory_id_from_episode(scoped_repo, scope, _provenance(memory_ids=(quarantined, healthy))) == healthy
    assert first_memory_id_from_episode(scoped_repo, scope, _provenance(memory_ids=(quarantined,))) is None
