from __future__ import annotations

import asyncio
import sqlite3
import types

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository


def group_scope(conversation: str = "g1", *, subject: str | None = None) -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef(f"qq:group:{conversation}", "qq", "group", conversation),
        subject_principal_id=subject,
    )


def test_scoped_soul_migration_is_additive_and_never_backfills_legacy(tmp_path):
    path = tmp_path / "soul.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE concerns(id INTEGER PRIMARY KEY, topic TEXT, bot_id TEXT)")
    raw.execute("INSERT INTO concerns(topic, bot_id) VALUES ('legacy', 'bot-alpha')")
    raw.commit()
    raw.close()

    manager = ConnectionManager(str(path))
    try:
        ensure_scoped_soul_schema(manager)
        ensure_scoped_soul_schema(manager)
        columns = {
            row[1]
            for row in manager.execute_read("PRAGMA table_info(scoped_soul_mood)").fetchall()
        }
        assert {"bot_id", "session_id", "visibility", "revision", "evidence"} <= columns
        assert manager.execute_read("SELECT COUNT(*) FROM concerns").fetchone()[0] == 1
        assert manager.execute_read("SELECT COUNT(*) FROM scoped_soul_concerns").fetchone()[0] == 0
    finally:
        manager.close()


def test_repository_returns_real_state_with_exact_scope_and_subject_isolation(tmp_path):
    manager = ConnectionManager(str(tmp_path / "repo.db"))
    try:
        ensure_scoped_soul_schema(manager)
        repo = ScopedSoulRepository(manager)
        scope = group_scope(subject="qq:user:u1")
        other = group_scope("g2", subject="qq:user:u1")
        repo.upsert_mood(scope, valence=0.6, arousal=0.4, cause="被夸奖", evidence=[{"memory_id": 11}])
        repo.replace_concerns(
            scope,
            concerns=[{"topic": "发布", "intensity": 0.8, "origin_memory_id": 11}],
            evidence=[{"memory_id": 11}],
        )
        repo.add_timeline_event(
            scope,
            event_summary="完成发布",
            emotional_weight=0.9,
            timestamp=123.0,
            evidence=[{"memory_id": 11}],
        )
        repo.upsert_relationship(
            scope,
            subject_principal_id="qq:user:u1",
            affinity=42,
            state="friendly",
            dimensions={"trust": 50},
            evidence=[{"event_id": 9}],
        )
        repo.upsert_mood(other, valence=-0.5, arousal=0.2, cause="other")

        state = repo.get_state(scope, subject_principal_id="qq:user:u1", limit=25, offset=0)
        assert state["mood"]["value"] == 0.6
        assert state["mood"]["components"] == {"valence": 0.6, "arousal": 0.4}
        assert state["mood"]["revision"] == 1
        assert state["mood"]["evidence"] == [{"memory_id": 11}]
        assert state["concerns"]["items"][0]["topic"] == "发布"
        assert state["concerns"]["items"][0]["status"] == "active"
        assert state["timeline"]["items"][0]["event_summary"] == "完成发布"
        assert state["relationship"]["affinity"] == 42
        assert state["relationship"]["revision"] == 1
        assert state["revision"] >= 1
        assert repo.get_state(group_scope("g3"), limit=25, offset=0)["mood"]["state"] == "unknown"

        # 强制自省与 get_state 同为只读投影：结果一致、revision 不变（无副作用）
        refreshed = repo.refresh_state(scope, subject_principal_id="qq:user:u1", limit=25, offset=0)
        assert refreshed == repo.get_state(scope, subject_principal_id="qq:user:u1", limit=25, offset=0)
        assert refreshed["revision"] == state["revision"]
    finally:
        manager.close()


class _Coordinator:
    def __init__(self) -> None:
        self.calls = 0

    def transaction_blocking(self, callback):
        self.calls += 1
        return callback(object())


class _InjectedRepo:
    def __init__(self) -> None:
        self.calls = []

    def get_state(self, scope, **_kwargs):
        return {
            "mood": {"state": "unknown"},
            "concerns": {"items": []},
            "timeline": {"items": []},
        }

    def upsert_mood(self, scope, **kwargs):
        self.calls.append(("mood", scope, kwargs))

    def replace_concerns(self, scope, **kwargs):
        self.calls.append(("concerns", scope, kwargs))

    def add_timeline_event(self, scope, **kwargs):
        self.calls.append(("timeline", scope, kwargs))

    def record_relationship_event(self, scope, **kwargs):
        self.calls.append(("relationship", scope, kwargs))
        return {
            "event_id": 7,
            "dimension": kwargs["dimension"],
            "requested_delta": kwargs["delta"],
            "applied_delta": kwargs["delta"],
            "before_affinity": 0,
            "after_affinity": 2,
            "reason": kwargs["reason"],
        }


def test_runtime_services_use_injected_coordinator_and_repository():
    from services.concern_tracker import ConcernTracker
    from services.mood_trajectory import MoodTrajectory
    from services.relationship_events import RelationshipEventService
    from services.subjective_time import SubjectiveTime

    coordinator = _Coordinator()
    repo = _InjectedRepo()
    scope = group_scope(subject="qq:user:u1")
    other_scope = group_scope("g2", subject="qq:user:u2")
    db = types.SimpleNamespace(conn=types.SimpleNamespace(execute=lambda *_a, **_k: None, commit=lambda: None))

    mood = MoodTrajectory(db, bot_id="bot-alpha", repository=repo, coordinator=coordinator)
    mood.record(0.4, 0.2, "cause", scope=scope, evidence=[{"memory_id": 1}])
    mood.record(-0.4, 0.1, "other", scope=other_scope, evidence=[{"memory_id": 5}])
    concerns = ConcernTracker(db, bot_id="bot-alpha", repository=repo, coordinator=coordinator)
    # 关切写入已迁出 tracker：只能经 ProductionWriteGateway.transition_concern 命令链落库，
    # 旧的全量替换（_persist）与物理剔除（tick）不得回流。
    for removed_writer in ("add", "tick", "_persist"):
        assert not hasattr(concerns, removed_writer), f"ConcernTracker 不应保留写路径: {removed_writer}"
    subjective_time = SubjectiveTime(db, bot_id="bot-alpha", repository=repo, coordinator=coordinator)
    subjective_time.add_anchor("anchor", timestamp=10.0, scope=scope, evidence=[{"memory_id": 3}])
    subjective_time.add_anchor("other anchor", timestamp=20.0, scope=other_scope, evidence=[{"memory_id": 7}])
    result = RelationshipEventService(
        db.conn, repository=repo, coordinator=coordinator
    ).record_event(
        scope=scope,
        event_type="direct_reply",
        dimension="trust",
        delta=2,
        reason="reply",
        source_memory_id=4,
    )

    assert coordinator.calls == 5
    assert [call[0] for call in repo.calls] == [
        "mood", "mood", "timeline", "timeline", "relationship"
    ]
    assert [call[1] for call in repo.calls[:4]] == [
        scope, other_scope, scope, other_scope
    ]
    assert all("connection" in call[2] for call in repo.calls)
    assert result.after_affection == 2


def test_soul_state_reads_scoped_repository_and_legacy_mutations_are_410(monkeypatch):
    from webui.blueprints import soul as module

    class Repo:
        def get_state(self, scope, **kwargs):
            assert scope.session.id == "qq:group:g1"
            assert kwargs["subject_principal_id"] == "qq:user:u1"
            return {
                "revision": 5,
                "evidence": [{"memory_id": 11}],
                "mood": {"value": 0.5, "state": "known", "components": {"valence": 0.5, "arousal": 0.2}, "policy_version": "mood/v1", "revision": 2, "evidence": []},
                "concerns": {"items": [{"topic": "发布"}], "total": 1},
                "timeline": {"items": [{"event_summary": "完成"}], "total": 1},
                "relationship_history": {"items": [], "total": 0, "revision": None},
                "soul_context": {"status": "unavailable", "reason_code": "formal_soul_context_unavailable", "timezone": None, "circadian": None, "energy": None, "sleepiness": None},
                "relationship": {"affinity": 42, "state": "friendly", "revision": 5, "evidence": [], "people_ref": "qq:user:u1"},
            }

    monkeypatch.setattr(module, "get_container", lambda: types.SimpleNamespace(soul_repository=Repo()))
    monkeypatch.setattr(module, "jsonify", lambda payload: payload)
    monkeypatch.setattr(module, "current_runtime_scope", lambda _provider: group_scope(subject="qq:user:u1"))
    monkeypatch.setattr(module, "current_app", types.SimpleNamespace(extensions={"wave_api_contract": {"request_scope_provider": object(), "object_refs": None}}))
    monkeypatch.setattr(
        module,
        "request",
        types.SimpleNamespace(
            args={"limit": "25", "offset": "0"},
            method="GET",
            path="/api/soul/state",
        ),
    )

    payload = asyncio.run(module.soul_state.__wrapped__())
    assert payload["revision"] == 5
    assert payload["mood"]["value"] == 0.5
    assert payload["concerns"]["items"][0]["topic"] == "发布"
    assert payload["relationship"]["affinity"] == 42
    assert payload["relationship_history"]["page"]["total"] == 0
    assert payload["soul_context"]["status"] == "unavailable"
    assert payload["capabilities"]["mutate"]["available"] is False
    # refresh 通道已由 POST /api/soul/state/refresh 提供，GET 必须如实声明可用
    assert payload["capabilities"]["runtime_refresh"] == {"available": True, "reason_code": None}
    assert payload["runtime_refresh"]["status"] == "available"

    module.request = types.SimpleNamespace(args={}, method="POST", path="/api/concerns")
    rejected, status = asyncio.run(module._reject_unscoped_soul_mutations())
    assert status == 410
    assert rejected == {"error": {"code": "legacy_mutation_disabled"}}

    # 唯一的 POST 白名单：强制自省（只读重算，非 legacy 写路径）
    module.request = types.SimpleNamespace(args={}, method="POST", path="/api/soul/state/refresh")
    assert asyncio.run(module._reject_unscoped_soul_mutations()) is None


def test_soul_history_preserves_manual_memory_evidence_summary():
    from webui.blueprints import soul as module

    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE memories(
               id INTEGER PRIMARY KEY, content TEXT, timestamp REAL,
               version INTEGER, bot_id TEXT, session_id TEXT, visibility TEXT,
               resolution_state TEXT, quarantine INTEGER DEFAULT 0
           )"""
    )
    connection.execute(
        """INSERT INTO memories VALUES(
               11, '原始记忆正文', 100, 2, 'bot-alpha', 'qq:group:g1',
               'group', 'resolved', 0)"""
    )
    scope = group_scope()
    evidence = module._normalize_evidence_items(
        connection,
        scope=scope,
        values=[{"kind": "memory", "id": "11", "summary": "人工校准说明"}],
        refs=None,
    )
    assert evidence[0]["summary"] == "人工校准说明"


def test_soul_state_refresh_recomputes_projection_without_writes(monkeypatch):
    from webui.blueprints import soul as module

    calls = {"refresh": 0}

    def _state():
        return {
            "revision": 5,
            "evidence": [],
            "mood": {"value": 0.5, "state": "known", "components": {"valence": 0.5, "arousal": 0.2}, "policy_version": "mood/v1", "revision": 2, "evidence": []},
            "concerns": {"items": [], "total": 0},
            "timeline": {"items": [], "total": 0},
            "relationship_history": {"items": [], "total": 0, "revision": None},
            "soul_context": {"status": "unavailable", "reason_code": "formal_soul_context_unavailable", "timezone": None, "circadian": None, "energy": None, "sleepiness": None},
            "relationship": {"affinity": 42, "state": "friendly", "revision": 5, "evidence": [], "people_ref": "qq:user:u1"},
        }

    class Repo:
        def get_state(self, scope, **kwargs):
            return _state()

        def refresh_state(self, scope, **kwargs):
            calls["refresh"] += 1
            assert scope.session.id == "qq:group:g1"
            assert kwargs["subject_principal_id"] == "qq:user:u1"
            return _state()

    monkeypatch.setattr(module, "get_container", lambda: types.SimpleNamespace(soul_repository=Repo()))
    monkeypatch.setattr(module, "jsonify", lambda payload: payload)
    monkeypatch.setattr(module, "current_runtime_scope", lambda _provider: group_scope(subject="qq:user:u1"))
    monkeypatch.setattr(module, "current_app", types.SimpleNamespace(extensions={"wave_api_contract": {"request_scope_provider": object(), "object_refs": None}}))
    monkeypatch.setattr(
        module,
        "request",
        types.SimpleNamespace(args={"limit": "25", "offset": "0"}, method="POST", path="/api/soul/state/refresh"),
    )

    payload = asyncio.run(module.soul_state_refresh.__wrapped__())
    assert calls["refresh"] == 1
    assert payload["revision"] == 5
    assert payload["mood"]["value"] == 0.5
    assert payload["relationship"]["affinity"] == 42
    assert payload["runtime_refresh"]["status"] == "refreshed"
    assert payload["runtime_refresh"]["refreshed_at"] > 0
    assert payload["runtime_refresh"]["reason_code"] is None
    assert payload["capabilities"]["runtime_refresh"] == {"available": True, "reason_code": None}

    # 仓储缺失时 refresh 诚实 503，不伪装
    monkeypatch.setattr(module, "get_container", lambda: types.SimpleNamespace(soul_repository=None))
    rejected, status = asyncio.run(module.soul_state_refresh.__wrapped__())
    assert status == 503
    assert rejected == {"error": {"code": "soul_scoped_repository_unavailable"}}
