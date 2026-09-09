from __future__ import annotations

import pytest

from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.soul_concerns import apply_concern_transition
from tests.test_scoped_soul import group_scope


def test_concern_status_transitions_and_expiry():
    item = {"status": "active", "last_progress_at": 1, "expected_resolution_at": 100, "topic": "考研"}
    progressed = apply_concern_transition(item, action="progress", now=10)
    assert progressed["status"] == "progressing"
    resolved = apply_concern_transition(progressed, action="resolve", now=20, resolution_note="录取结果已公布")
    assert resolved["status"] == "resolved"
    assert resolved["resolution_note"] == "录取结果已公布"
    expired = apply_concern_transition({"status": "active", "expected_resolution_at": 5}, action="tick", now=10)
    assert expired["status"] == "expired"
    dormant = apply_concern_transition({"status": "active", "last_progress_at": 1}, action="tick", now=1 + 14 * 86400)
    assert dormant["status"] == "dormant"
    with pytest.raises(ValueError):
        apply_concern_transition({"status": "archived"}, action="reopen", now=1)


def test_repository_persists_concern_status_fields(tmp_path):
    manager = ConnectionManager(str(tmp_path / "concerns.db"))
    try:
        ensure_scoped_soul_schema(manager)
        repo = ScopedSoulRepository(manager)
        scope = group_scope()
        repo.replace_concerns(scope, concerns=[{
            "topic": "考研结果",
            "intensity": 0.8,
            "status": "progressing",
            "concern_type": "follow_up",
            "origin_episode_id": 9,
            "expected_resolution_at": 99,
            "resolution_note": "",
        }])
        item = repo.get_state(scope, limit=25, offset=0)["concerns"]["items"][0]
        assert item["status"] == "progressing"
        assert item["concern_type"] == "follow_up"
        assert item["origin_episode_id"] == 9
        assert item["expected_resolution_at"] == 99
    finally:
        manager.close()
