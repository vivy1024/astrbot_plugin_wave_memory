from __future__ import annotations

import pytest

from domain.scope import RuntimeScope, ScopeValidationError, SessionRef
from services.relationship_events import RelationshipEventService


def test_relationship_writes_fail_closed_without_scope_or_scoped_repository():
    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("legacy relationship tables must not be touched")

    service = RelationshipEventService(Connection())
    with pytest.raises(ScopeValidationError) as missing_scope:
        service.record_event(
            bot_id="bot-alpha",
            group_id="g1",
            user_id="u1",
            event_type="direct_reply",
            dimension="trust",
            delta=1,
            reason="test",
        )
    assert missing_scope.value.reason_code == "scope_required"

    scope = RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )
    with pytest.raises(ScopeValidationError) as missing_repo:
        service.record_event(
            scope=scope,
            event_type="direct_reply",
            dimension="trust",
            delta=1,
            reason="test",
        )
    assert missing_repo.value.reason_code == "scoped_repository_required"


def test_relationship_service_rejects_passing_noise_event_type():
    class Repo:
        def record_relationship_event(self, *_args, **_kwargs):
            raise AssertionError("noise must not reach the repository")

    service = RelationshipEventService(object(), repository=Repo())
    scope = RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )
    with pytest.raises(ValueError, match="invalid relationship event_type"):
        service.record_event(
            scope=scope,
            event_type="message_seen",
            dimension="familiarity",
            delta=0.05,
            reason="看见一条群友消息",
        )
