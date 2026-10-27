"""正式关系随时间冷却：长期没来往会变淡，人工校准不被冲掉，审计可追溯。"""

from __future__ import annotations

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository

DAY = 86400.0
T0 = 1_800_000_000.0


def scope(group: str = "g1", subject: str = "qq:user:u1", bot: str = "bot-alpha") -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(f"qq:group:{group}", "qq", "group", group), subject_principal_id=subject)


@pytest.fixture
def repo(tmp_path):
    manager = ConnectionManager(str(tmp_path / "decay.db"))
    ensure_scoped_soul_schema(manager)
    repository = ScopedSoulRepository(manager)
    yield repository
    manager.close() if hasattr(manager, "close") else None


def _seed(repo, value_scope, *, at: float):
    # 真实互动事件：建立维度值并记下最后来往时间
    for dimension, delta in (("familiarity", 5), ("trust", 5), ("fun", 5), ("depth", 5)):
        for i in range(3):
            repo.record_relationship_event(value_scope, event_type="deep_talk", dimension=dimension,
                                           delta=delta, reason="聊得很投机", created_at=at + i)


def _values(repo, value_scope):
    state = repo.get_state(value_scope, subject_principal_id=value_scope.subject_principal_id)
    return state["relationship"]


def test_recent_relationship_is_untouched_and_idle_one_cools(repo):
    fresh, idle = scope(subject="qq:user:fresh"), scope(subject="qq:user:idle")
    _seed(repo, fresh, at=T0 + 59 * DAY)
    _seed(repo, idle, at=T0)
    before_idle = _values(repo, idle)
    before_fresh = _values(repo, fresh)

    stats = repo.apply_time_decay(now=T0 + 60 * DAY)

    assert stats["decayed_subjects"] == 1
    after_idle = _values(repo, idle)
    assert after_idle["values"]["familiarity"]["automatic_value"] < before_idle["values"]["familiarity"]["automatic_value"]
    assert after_idle["affinity"] < before_idle["affinity"]
    assert after_idle["revision"] > before_idle["revision"]
    assert _values(repo, fresh)["values"] == before_fresh["values"], "一天前刚聊过：不冷却"


def test_decay_writes_auditable_events_hidden_from_history_and_daily_cap(repo):
    value_scope = scope()
    _seed(repo, value_scope, at=T0)
    repo.apply_time_decay(now=T0 + 30 * DAY)
    rows = repo.cm.execute_read(
        "SELECT event_type, dimension, delta, before_json, after_json FROM scoped_soul_relationship_events WHERE event_type='time_decay'"
    ).fetchall()
    assert rows and all(row[2] < 0 and row[3] and row[4] for row in rows)

    history = repo.list_relationship_history(value_scope, subject_principal_id=value_scope.subject_principal_id)
    assert history["items"], "真实互动仍在历史里"
    assert all(item.get("event_type") != "time_decay" for item in history["items"]), "冷却不算一次互动"

    # 冷却后马上有新互动：衰减的负值不占当天正向变化额度
    result = repo.record_relationship_event(value_scope, event_type="bot_praised", dimension="familiarity",
                                            delta=5, reason="夸了一句", created_at=T0 + 30 * DAY + 10)
    assert result["applied_delta"] == 5


def test_last_interaction_excludes_decay_events_so_cooling_continues(repo):
    value_scope = scope()
    _seed(repo, value_scope, at=T0)
    first = _values(repo, value_scope)["values"]["familiarity"]["automatic_value"]
    repo.apply_time_decay(now=T0 + 30 * DAY)
    second = _values(repo, value_scope)["values"]["familiarity"]["automatic_value"]
    repo.apply_time_decay(now=T0 + 31 * DAY)
    third = _values(repo, value_scope)["values"]["familiarity"]["automatic_value"]
    assert first > second > third, "衰减事件本身不算来往"


def test_manual_override_and_adjustment_survive(repo):
    value_scope = scope()
    _seed(repo, value_scope, at=T0)
    state = _values(repo, value_scope)
    with repo.cm.write_transaction() as tx:
        repo.calibrate_relationship(
            value_scope, subject_principal_id=value_scope.subject_principal_id, expected_revision=state["revision"],
            action="override", dimension="trust", value=80, reason="管理员确认", evidence=[{
                "kind": "raw_message", "id": "message:1", "content_hash": "sha256:x", "captured_at": T0,
                "source_scope": value_scope.to_dict(), "available": True,
            }], operation_id="op-1", connection=tx,
        )
    repo.apply_time_decay(now=T0 + 60 * DAY)
    trust = _values(repo, value_scope)["values"]["trust"]
    assert trust["manual_override"] == 80 and trust["effective_value"] == 80
    assert trust["automatic_value"] < 15, "自动层照常冷却"


def test_negative_trust_moves_toward_zero_and_hostility_softens(repo):
    value_scope = scope()
    for i in range(3):
        repo.record_relationship_event(value_scope, event_type="bot_attacked", dimension="trust", delta=-5, reason="骂人", created_at=T0 + i)
        repo.record_relationship_event(value_scope, event_type="bot_attacked", dimension="hostility", delta=5, reason="骂人", created_at=T0 + i)
    before = _values(repo, value_scope)["values"]
    repo.apply_time_decay(now=T0 + 60 * DAY)
    after = _values(repo, value_scope)["values"]
    assert before["trust"]["automatic_value"] < after["trust"]["automatic_value"] <= 0
    assert 0 <= after["hostility"]["automatic_value"] < before["hostility"]["automatic_value"]
    host_drop = before["hostility"]["automatic_value"] - after["hostility"]["automatic_value"]
    trust_rise = after["trust"]["automatic_value"] - before["trust"]["automatic_value"]
    assert host_drop < trust_rise, "消气比信任恢复慢（f/2）"


def test_lifecycle_runs_formal_decay_through_coordinator():
    from services.lifecycle import LifecycleService

    calls = []

    class _Repo:
        def apply_time_decay(self, *, now, connection=None):
            calls.append(("decay", connection))
            return {"decayed_subjects": 2, "events": 5}

    class _Coord:
        def transaction_blocking(self, fn):
            return fn("conn")

    class _Svc:
        repository = _Repo()
        coordinator = _Coord()

    manager = LifecycleService.__new__(LifecycleService)
    manager._relationship_service = _Svc()
    assert manager._decay_formal_relationships(T0) == {"decayed_subjects": 2, "events": 5}
    assert calls == [("decay", "conn")]
