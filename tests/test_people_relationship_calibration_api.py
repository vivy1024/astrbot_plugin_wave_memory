from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from services.relationship_calibration import _normalize_evidence
from webui.container import ServiceContainer
from webui.blueprints import people as people_module


class _RequestScopeProvider:
    def __init__(self, scope: RuntimeScope) -> None:
        self.scope = scope

    def get_request_scope(self) -> RuntimeScope:
        return self.scope


class _ObjectRefs:
    def __init__(self, locator: str = "qq:user:u1", revision: int = 1) -> None:
        self.binding = SimpleNamespace(locator=locator, revision=revision)

    def resolve_with_state(self, ref, *, kind, request_scope):
        assert ref == "relationship-ref"
        assert kind == "relationship"
        assert request_scope.visibility == "group"
        return self.binding, "ready"


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def calibrate(self, **kwargs):
        normalized = _normalize_evidence(kwargs["evidence"], kwargs["scope"])
        self.calls.append({**kwargs, "evidence": normalized})
        return SimpleNamespace(
            operation_id="operation-1",
            calibration_id="calibration-1",
            revision=2,
            status="succeeded",
            subject_principal_id=kwargs["subject_principal_id"],
            dimension=kwargs["dimension"],
            action=kwargs["action"],
            before={"effective_value": 1},
            after={"effective_value": 2},
            affinity=2,
            state="neutral",
            evidence=list(kwargs["evidence"]),
        )


def _scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories(
               id INTEGER PRIMARY KEY, content TEXT, timestamp REAL,
               version INTEGER, bot_id TEXT, session_id TEXT, visibility TEXT,
               resolution_state TEXT, quarantine INTEGER DEFAULT 0
           )"""
    )
    conn.execute(
        """INSERT INTO memories VALUES(
               11, '真实消息', 100, 4, 'bot-alpha', 'qq:group:g1',
               'group', 'resolved', 0)"""
    )
    conn.commit()
    return conn


def _frontend_evidence(scope: RuntimeScope) -> list[dict]:
    return [{
        "kind": "memory",
        "id": "11",
        "source_scope": scope.to_dict(),
    }]


def _calibration_body(scope: RuntimeScope, evidence_value=None) -> dict:
    body = {
        "object_ref": "relationship-ref",
        "revision": 1,
        "action": "adjust",
        "dimension": "trust",
        "delta": 2,
        "reason": "确认用户持续提供可靠反馈",
    }
    if evidence_value is not None:
        body["evidence"] = evidence_value
    return body


@pytest.fixture
def route_app(monkeypatch):
    ServiceContainer.reset()
    container = ServiceContainer()
    scope = _scope()
    gateway = _Gateway()
    container.password = ""
    container.db = SimpleNamespace(conn=_db())
    container.relationship_calibration = gateway

    app = Quart(__name__)
    app.register_blueprint(people_module.people_bp)
    app.extensions["wave_api_contract"] = {
        "request_scope_provider": _RequestScopeProvider(scope),
        "object_refs": _ObjectRefs(),
    }
    yield app, scope, gateway
    ServiceContainer.reset()


@pytest.mark.asyncio
async def test_route_preserves_required_error_for_missing_empty_and_wrong_type(route_app, monkeypatch):
    app, scope, gateway = route_app
    original = people_module.resolve_relationship_evidence
    observed_values = []

    def recording_resolver(connection, *, scope, values):
        observed_values.append(values)
        return original(connection, scope=scope, values=values)

    monkeypatch.setattr(people_module, "resolve_relationship_evidence", recording_resolver)
    client = app.test_client()

    for evidence_value in (None, [], {}):
        body = _calibration_body(scope)
        if evidence_value is not None:
            body["evidence"] = evidence_value
        response = await client.post(
            "/api/people/relationships/commands/calibrate",
            json=body,
        )
        assert response.status_code == 422
        payload = await response.get_json()
        assert payload["error"]["code"] == "relationship_evidence_required"

    assert observed_values == [None, [], {}]
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_route_accepts_frontend_shaped_non_empty_evidence(route_app):
    app, scope, gateway = route_app
    response = await app.test_client().post(
        "/api/people/relationships/commands/calibrate",
        json=_calibration_body(scope, _frontend_evidence(scope)),
    )

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["ok"] is True
    assert payload["operation"]["status"] == "succeeded"
    assert len(gateway.calls) == 1
    resolved = gateway.calls[0]["evidence"]
    assert len(resolved) == 1
    assert resolved[0]["kind"] == "memory"
    assert resolved[0]["type"] == "memory"
    assert resolved[0]["available"] is True


@pytest.mark.asyncio
async def test_route_carries_summary_into_audit_evidence(route_app):
    app, scope, gateway = route_app
    evidence = _frontend_evidence(scope)
    evidence[0]["summary"] = "这条证据最能体现对我加强了信任"
    response = await app.test_client().post(
        "/api/people/relationships/commands/calibrate",
        json=_calibration_body(scope, evidence),
    )

    assert response.status_code == 200
    resolved = gateway.calls[0]["evidence"]
    assert resolved[0]["summary"] == "这条证据最能体现对我加强了信任"


@pytest.mark.asyncio
async def test_route_preserves_invalid_error_when_gateway_key_set_validation_fails(route_app, monkeypatch):
    app, scope, gateway = route_app

    def invalid_resolver(connection, *, scope, values):
        return [{
            "kind": "memory",
            "type": "memory",
            "id": "11",
            "content_hash": "hash",
            "captured_at": 100.0,
            "source_scope": scope.to_dict(),
            "available": True,
            "unexpected": "client-only-field",
        }]

    monkeypatch.setattr(people_module, "resolve_relationship_evidence", invalid_resolver)
    response = await app.test_client().post(
        "/api/people/relationships/commands/calibrate",
        json=_calibration_body(scope, _frontend_evidence(scope)),
    )

    assert response.status_code == 422
    payload = await response.get_json()
    assert payload["error"]["code"] == "relationship_evidence_invalid"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_route_reports_scope_mismatch_separately(route_app):
    app, scope, gateway = route_app
    other_scope = RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g2", "qq", "group", "g2"),
        subject_principal_id="qq:user:u1",
    )
    response = await app.test_client().post(
        "/api/people/relationships/commands/calibrate",
        json=_calibration_body(scope, _frontend_evidence(other_scope)),
    )

    assert response.status_code == 422
    payload = await response.get_json()
    assert payload["error"]["code"] == "relationship_evidence_scope_mismatch"
    assert gateway.calls == []


def _profile_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE user_profiles(
               user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT, metadata TEXT,
               PRIMARY KEY(user_id, group_id, bot_id)
           )"""
    )
    conn.execute("INSERT INTO user_profiles VALUES ('u1', 'g1', 'bot-alpha', '甲', '{}')")
    conn.execute(
        """CREATE TABLE person_timeline_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               bot_id TEXT, user_id TEXT, group_id TEXT, kind TEXT,
               summary TEXT, detail TEXT, subject TEXT, predicate TEXT, object TEXT,
               confidence REAL, occurred_at REAL, legacy_fact_id INTEGER,
               provenance TEXT, created_at REAL
           )"""
    )
    conn.execute(
        """INSERT INTO person_timeline_events (
               bot_id, user_id, group_id, kind, summary, detail, subject, predicate, object,
               confidence, occurred_at, provenance, created_at
           ) VALUES ('bot-alpha', 'u1', 'g1', 'impression', '说话谨慎，常纠正事实', '说话谨慎，常纠正事实', '', '', '', NULL, 1000, '{}', 1000)"""
    )
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_clear_impression_requires_reason_and_writes_audit(route_app):
    app, _scope, _gateway = route_app
    from engine.db.person_timeline_repo import PersonTimelineRepo

    container = ServiceContainer()
    conn = _profile_db()

    class _CM:
        def execute_write(self, sql, params=None):
            return conn.execute(sql, params or ())
        def execute_read(self, sql, params=None):
            return conn.execute(sql, params or ())
        def migration_transaction(self):
            from contextlib import contextmanager
            @contextmanager
            def _tx():
                yield conn
                conn.commit()
            return _tx()

    container.db = SimpleNamespace(conn=conn, person_timeline=PersonTimelineRepo(_CM()))
    client = app.test_client()

    missing = await client.post("/api/people/commands/clear-impression", json={"user_id": "u1"})
    assert missing.status_code == 422
    missing_payload = await missing.get_json()
    assert missing_payload["error"]["code"] == "impression_clear_reason_required"

    ok = await client.post(
        "/api/people/commands/clear-impression",
        json={"user_id": "u1", "reason": "印象过期，需要重新观察"},
    )
    assert ok.status_code == 200
    payload = await ok.get_json()
    assert payload["ok"] is True
    assert payload["operation"]["kind"] == "people.impression.clear"
    assert payload["item"]["cleared"] is True
    assert payload["item"]["previous_impression"] == "说话谨慎，常纠正事实"

    rows = conn.execute(
        "SELECT kind, summary, detail FROM person_timeline_events WHERE user_id='u1' ORDER BY id"
    ).fetchall()
    assert rows[0][1] == "说话谨慎，常纠正事实"
    assert any("已清除当前印象" in str(row[1]) for row in rows)
