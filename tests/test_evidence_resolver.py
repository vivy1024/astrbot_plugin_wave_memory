from __future__ import annotations

import hashlib
import sqlite3

import pytest

from domain.scope import RuntimeScope, SessionRef
from services.evidence_resolver import EvidenceResolutionError, resolve_relationship_evidence
from services.relationship_calibration import RelationshipCalibrationError, _normalize_evidence


def scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )


def db() -> sqlite3.Connection:
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
    conn.execute(
        """CREATE TABLE experience_episodes(
               id INTEGER PRIMARY KEY, bot_id TEXT, group_id TEXT, user_id TEXT,
               episode_type TEXT, trigger_text TEXT, bot_inner_thought TEXT,
               bot_action TEXT, bot_reply TEXT, user_reaction TEXT, outcome TEXT,
               source_memory_ids TEXT, created_at REAL
           )"""
    )
    conn.execute(
        """INSERT INTO experience_episodes VALUES(
               21, 'bot-alpha', 'g1', 'u1', 'reply', '触发', '内心',
               '回复', '答复', '满意', 'positive', '[11]', 101)"""
    )
    conn.commit()
    return conn


def descriptor(value: RuntimeScope, memory_id: str = "11") -> dict:
    return {"kind": "memory", "id": memory_id, "source_scope": value.to_dict()}


def test_resolver_reads_legacy_group_memory_and_computes_hash():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories(
               id INTEGER PRIMARY KEY, content TEXT, timestamp REAL,
               version INTEGER, bot_id TEXT, session_id TEXT, visibility TEXT,
               group_id TEXT, resolution_state TEXT, quarantine INTEGER DEFAULT 0
           )"""
    )
    # Memory without full bot_id/session_id but with group_id matching
    conn.execute(
        """INSERT INTO memories VALUES(
               12, '群聊旧消息', 105, 1, NULL, NULL, NULL, 'g1', NULL, 0)"""
    )
    conn.commit()
    result = resolve_relationship_evidence(conn, scope=scope(), values=[descriptor(scope(), "12")])
    assert result == [{
        "kind": "memory",
        "type": "memory",
        "id": "12",
        "content_hash": hashlib.sha256("群聊旧消息".encode()).hexdigest(),
        "captured_at": 105.0,
        "source_scope": scope().to_dict(),
        "available": True,
    }]



def test_resolver_rejects_cross_scope_and_unknown_objects():
    connection = db()
    other = RuntimeScope("bot-alpha", "group", SessionRef("qq:group:g2", "qq", "group", "g2"), subject_principal_id="qq:user:u1")
    with pytest.raises(EvidenceResolutionError) as mismatch:
        resolve_relationship_evidence(connection, scope=scope(), values=[descriptor(other)])
    assert mismatch.value.code == "relationship_evidence_scope_mismatch"
    with pytest.raises(EvidenceResolutionError) as missing:
        resolve_relationship_evidence(connection, scope=scope(), values=[descriptor(scope(), "99")])
    assert missing.value.code == "relationship_evidence_not_found"


def test_resolver_reads_scoped_experience_episode():
    connection = db()
    result = resolve_relationship_evidence(connection, scope=scope(), values=[{
        "kind": "episode",
        "id": 21,
        "source_scope": scope().to_dict(),
    }])
    assert result[0]["kind"] == "episode"
    assert result[0]["type"] == "episode"
    assert result[0]["id"] == "21"
    assert result[0]["captured_at"] == 101.0
    assert result[0]["available"] is True
    assert result[0]["title"] == "reply"
    assert result[0]["locator"] == {"episode_id": 21}


def test_resolver_rejects_hash_mismatch_and_free_text_notes():
    connection = db()
    bad_hash = {**descriptor(scope()), "content_hash": "not-the-real-hash"}
    with pytest.raises(EvidenceResolutionError) as mismatch:
        resolve_relationship_evidence(connection, scope=scope(), values=[bad_hash])
    assert mismatch.value.code == "relationship_evidence_hash_mismatch"
    with pytest.raises(EvidenceResolutionError) as note:
        resolve_relationship_evidence(connection, scope=scope(), values=[{
            "kind": "webui_note",
            "id": "note:1",
            "source_scope": scope().to_dict(),
        }])
    assert note.value.code == "relationship_evidence_object_required"


def test_evidence_required_is_reserved_for_missing_or_empty_values():
    connection = db()
    for value in (None, [], "", {}):
        with pytest.raises(EvidenceResolutionError) as error:
            resolve_relationship_evidence(connection, scope=scope(), values=value)
        assert error.value.code == "relationship_evidence_required"


def test_resolver_outputs_are_accepted_by_calibration_normalizer():
    connection = db()
    target_scope = scope()
    for kind, item_id in (("memory", "11"), ("episode", "21")):
        resolved = resolve_relationship_evidence(
            connection,
            scope=target_scope,
            values=[{
                "kind": kind,
                "id": item_id,
                "source_scope": target_scope.to_dict(),
            }],
        )
        normalized = _normalize_evidence(resolved, target_scope)
        assert normalized[0]["kind"] == kind
        assert normalized[0]["available"] is True


def test_calibration_normalizer_rejects_missing_or_unknown_evidence_fields():
    target_scope = scope()
    base = {
        "kind": "memory",
        "id": "11",
        "content_hash": "hash",
        "captured_at": 100.0,
        "source_scope": target_scope.to_dict(),
        "available": True,
    }
    with pytest.raises(RelationshipCalibrationError) as missing:
        _normalize_evidence([{key: value for key, value in base.items() if key != "content_hash"}], target_scope)
    assert missing.value.code == "relationship_evidence_invalid"
    with pytest.raises(RelationshipCalibrationError) as unknown:
        _normalize_evidence([{**base, "client_note": "unexpected"}], target_scope)
    assert unknown.value.code == "relationship_evidence_invalid"


def test_evidence_summary_is_carried_into_calibration_audit_payload():
    connection = db()
    target_scope = scope()
    resolved = resolve_relationship_evidence(
        connection,
        scope=target_scope,
        values=[{**descriptor(target_scope), "summary": "这条消息体现了明显信任提升"}],
    )
    assert resolved[0]["summary"] == "这条消息体现了明显信任提升"
    normalized = _normalize_evidence(resolved, target_scope)
    assert normalized[0]["summary"] == "这条消息体现了明显信任提升"


def test_evidence_summary_whitespace_only_is_dropped():
    connection = db()
    resolved = resolve_relationship_evidence(
        connection,
        scope=scope(),
        values=[{**descriptor(scope()), "summary": "   "}],
    )
    assert "summary" not in resolved[0]


def test_evidence_summary_rejects_non_string_and_trims_oversize():
    connection = db()
    with pytest.raises(EvidenceResolutionError) as bad_type:
        resolve_relationship_evidence(
            connection,
            scope=scope(),
            values=[{**descriptor(scope()), "summary": 123}],
        )
    assert bad_type.value.code == "relationship_evidence_invalid"

    resolved = resolve_relationship_evidence(
        connection,
        scope=scope(),
        values=[{**descriptor(scope()), "summary": "x" * 600}],
    )
    assert len(resolved[0]["summary"]) == 500
    normalized = _normalize_evidence(resolved, scope())
    assert len(normalized[0]["summary"]) == 500
