from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields
from enum import Enum

import pytest

from domain.evidence import EvidenceBinding, EvidenceDerivation, EvidenceRef, FULL_EVIDENCE_DERIVATION_CHAIN
from domain.scope import (
    CatalogScope,
    COMMAND_SCOPE_MATRIX,
    RuntimeScope,
    ScopeCodec,
    ScopePolicyRegistry,
    ScopeRequirement,
    ScopeValidationError,
    ScopeValidator,
    build_command_scope_policy_registry,
    validate_formal_command_scope,
    SessionRef,
    UnresolvedScopeRef,
    resolution_state,
    scope_from_dict,
    scope_from_value,
    scope_to_dict,
)
from services.scopes import (
    BotIdentityBinding,
    ScopeResolutionError,
    ScopeResolver,
)


class _MessageType(Enum):
    GROUP = "GroupMessage"
    PRIVATE = "FriendMessage"
    OTHER = "OtherMessage"


class _Event:
    def __init__(
        self,
        *,
        self_id: str = "10001",
        platform_id: str = "qq-main",
        message_type: object = _MessageType.GROUP,
        group_id: str = "20001",
        session_id: str = "private-session-1",
        sender_id: str = "30001",
    ) -> None:
        self._self_id = self_id
        self._platform_id = platform_id
        self._message_type = message_type
        self._group_id = group_id
        self._session_id = session_id
        self._sender_id = sender_id

    def get_self_id(self):
        return self._self_id

    def get_platform_id(self):
        return self._platform_id

    def get_message_type(self):
        return self._message_type

    def get_group_id(self):
        return self._group_id

    def get_session_id(self):
        return self._session_id

    def get_sender_id(self):
        return self._sender_id


class _RaisingEvent(_Event):
    def __init__(self, failing_method: str) -> None:
        super().__init__()
        self._failing_method = failing_method

    def __getattribute__(self, name):
        if name.startswith("get_") and name == object.__getattribute__(self, "_failing_method"):
            def fail():
                raise RuntimeError("accessor failed")

            return fail
        return super().__getattribute__(name)


@pytest.fixture()
def resolver() -> ScopeResolver:
    return ScopeResolver(
        [
            BotIdentityBinding(self_id="10001", db_id="bot-alpha", display_name="Alpha"),
            BotIdentityBinding(self_id="10002", db_id="bot-beta", display_name="Beta"),
        ]
    )


def _group_scope(*, bot_id: str = "bot-alpha", group_id: str = "20001") -> RuntimeScope:
    session = SessionRef(
        id=f"qq-main:group:{group_id}",
        platform_id="qq-main",
        kind="group",
        conversation_id=group_id,
    )
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=session,
        subject_principal_id="qq-main:user:30001",
    )


def _bot_private_scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="bot_private",
        session=None,
        subject_principal_id=None,
    )


def _catalog_scope() -> CatalogScope:
    return CatalogScope(catalog_id="book-lore", corpus_id="novel-a", version="v1")


def test_scope_value_objects_have_exact_fields_are_frozen_and_roundtrip():
    session = _group_scope().session
    runtime = _group_scope()
    catalog = _catalog_scope()
    unresolved = UnresolvedScopeRef(
        original_fields={"bot_id": "10001", "group_id": "20001"},
        reason_code="legacy_identity_unresolved",
        provenance={"table": "memories", "row_id": 7, "details": {"source": "legacy"}},
    )
    assert tuple(field.name for field in fields(SessionRef)) == (
        "id",
        "platform_id",
        "kind",
        "conversation_id",
    )
    assert tuple(field.name for field in fields(RuntimeScope)) == (
        "bot_id",
        "visibility",
        "session",
        "subject_principal_id",
    )
    assert tuple(field.name for field in fields(CatalogScope)) == (
        "catalog_id",
        "corpus_id",
        "version",
    )
    assert tuple(field.name for field in fields(UnresolvedScopeRef)) == (
        "original_fields",
        "reason_code",
        "provenance",
    )

    for value in (session, runtime, catalog, unresolved):
        with pytest.raises(FrozenInstanceError):
            value.__setattr__(fields(value)[0].name, "changed")

    assert SessionRef.from_dict(asdict(session)) == session
    assert scope_from_dict("RuntimeScope", scope_to_dict(runtime)) == runtime
    assert scope_from_dict("CatalogScope", scope_to_dict(catalog)) == catalog
    assert scope_from_dict("UnresolvedScopeRef", scope_to_dict(unresolved)) == unresolved
    for value in (runtime, catalog, unresolved):
        envelope = ScopeCodec.to_dict(value)
        assert ScopeCodec.from_dict(envelope) == value
        assert ScopeCodec.decode(ScopeCodec.encode(value)) == value
    assert resolution_state(runtime) == "resolved"
    assert resolution_state(catalog) == "resolved"
    assert resolution_state(unresolved) == "unresolved_legacy"
    assert hash(unresolved) == hash(UnresolvedScopeRef(
        original_fields={"bot_id": "10001", "group_id": "20001"},
        reason_code="legacy_identity_unresolved",
        provenance={"table": "memories", "row_id": 7, "details": {"source": "legacy"}},
    ))
    with pytest.raises(ScopeValidationError) as unhashable:
        UnresolvedScopeRef(
            original_fields={"invalid": bytearray(b"not-serializable")},
            reason_code="legacy_identity_unresolved",
            provenance={"source": "legacy"},
        )
    assert unhashable.value.reason_code == "unserializable_scope_value"
    with pytest.raises(TypeError):
        unresolved.original_fields["bot_id"] = "mutated"
    with pytest.raises(TypeError):
        unresolved.provenance["details"]["source"] = "mutated"


def test_scope_and_evidence_payloads_decode_without_legacy_kind_inference():
    runtime = _group_scope()
    catalog = _catalog_scope()
    assert scope_from_value(scope_to_dict(runtime)) == runtime
    assert scope_from_value(scope_to_dict(catalog)) == catalog
    with pytest.raises(ScopeValidationError) as unknown:
        scope_from_value({"group_id": "legacy-group"})
    assert unknown.value.reason_code == "invalid_serialized_scope"

    reference = EvidenceRef(
        kind="raw_message",
        id="message:1",
        content_hash="sha256:message:1",
        captured_at=1.0,
        source_scope=runtime,
        available=True,
    )
    binding = EvidenceBinding(
        evidence_id=reference.id,
        target_scope=runtime,
        derivation_chain=("raw_chat", "reviewed_candidate"),
        policy_version="scope-test/v1",
    )
    assert EvidenceRef.from_dict(reference.to_dict()) == reference
    assert EvidenceBinding.from_dict(binding.to_dict()) == binding


def test_runtime_scope_rejects_missing_or_fake_sessions_with_stable_reasons():
    with pytest.raises(ScopeValidationError) as missing:
        RuntimeScope(
            bot_id="bot-alpha",
            visibility="group",
            session=None,
            subject_principal_id="qq-main:user:30001",
        )
    assert missing.value.reason_code == "session_required"

    with pytest.raises(ScopeValidationError) as forbidden:
        RuntimeScope(
            bot_id="bot-alpha",
            visibility="bot_private",
            session=_group_scope().session,
            subject_principal_id=None,
        )
    assert forbidden.value.reason_code == "session_forbidden"

    with pytest.raises(ScopeValidationError) as qq_id:
        RuntimeScope(
            bot_id="10001",
            visibility="bot_private",
            session=None,
            subject_principal_id=None,
        )
    assert qq_id.value.reason_code == "invalid_bot_id"

    with pytest.raises(ScopeValidationError) as sentinel:
        RuntimeScope(
            bot_id="default",
            visibility="bot_private",
            session=None,
            subject_principal_id=None,
        )
    assert sentinel.value.reason_code == "invalid_bot_id"


def test_resolver_uses_exact_self_id_registry_and_isolates_same_group_two_bots(resolver):
    alpha = resolver.resolve_event(_Event(self_id="10001"))
    beta = resolver.resolve_event(_Event(self_id="10002"))

    assert alpha.scope.bot_id == "bot-alpha"
    assert beta.scope.bot_id == "bot-beta"
    assert alpha.scope.session == beta.scope.session
    assert alpha.scope != beta.scope
    assert alpha.bot_self_id == "10001"
    assert alpha.bot_name == "Alpha"
    assert alpha.sender_local_id == "30001"
    assert alpha.conversation_local_id == "20001"


@pytest.mark.parametrize("self_id", ["", "99999", "bot-alpha", "Alpha"])
def test_resolver_unknown_display_or_db_id_fallback_is_fail_closed(resolver, self_id):
    with pytest.raises(ScopeResolutionError) as rejected:
        resolver.resolve_event(_Event(self_id=self_id))
    assert rejected.value.reason_code == "unknown_bot_self_id"


def test_resolver_rejects_invalid_or_ambiguous_registry():
    with pytest.raises(ScopeResolutionError) as numeric:
        BotIdentityBinding(self_id="10001", db_id="10001")
    assert numeric.value.reason_code == "invalid_bot_id"

    duplicate = BotIdentityBinding(self_id="10001", db_id="bot-alpha")
    with pytest.raises(ScopeResolutionError) as ambiguous:
        ScopeResolver([duplicate, duplicate])
    assert ambiguous.value.reason_code == "ambiguous_bot_registry"

    with pytest.raises(ScopeResolutionError) as duplicate_db:
        ScopeResolver(
            [
                duplicate,
                BotIdentityBinding(self_id="10002", db_id="bot-alpha"),
            ]
        )
    assert duplicate_db.value.reason_code == "ambiguous_bot_registry"


def test_private_resolution_uses_structured_message_type_and_session_id(resolver):
    context = resolver.resolve_event(
        _Event(
            message_type=_MessageType.PRIVATE,
            group_id="group:must-not-be-parsed",
            session_id="private-session-42",
            sender_id="30042",
        )
    )
    assert context.scope.visibility == "private"
    assert context.scope.session == SessionRef(
        id="qq-main:private:private-session-42",
        platform_id="qq-main",
        kind="private",
        conversation_id="private-session-42",
    )
    assert context.scope.subject_principal_id == "qq-main:user:30042"
    assert context.conversation_local_id == "private-session-42"


@pytest.mark.parametrize(
    ("message_type", "visibility"),
    [("GroupMessage", "group"), ("FriendMessage", "private")],
)
def test_resolver_accepts_only_exact_framework_message_type_strings(
    resolver, message_type, visibility
):
    context = resolver.resolve_event(_Event(message_type=message_type))
    assert context.scope.visibility == visibility


def test_resolver_never_guesses_chat_kind_from_raw_group_or_private_strings(resolver):
    event = _Event(message_type="private:30001", group_id="group:20001")
    with pytest.raises(ScopeResolutionError) as rejected:
        resolver.resolve_event(event)
    assert rejected.value.reason_code == "chat_kind_unresolved"


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (_Event(platform_id=""), "platform_unresolved"),
        (_Event(group_id=""), "conversation_id_required"),
        (_Event(message_type=_MessageType.PRIVATE, session_id=""), "conversation_id_required"),
        (_Event(sender_id=""), "sender_id_required"),
    ],
)
def test_resolver_missing_structured_event_fields_fail_closed(resolver, event, reason):
    with pytest.raises(ScopeResolutionError) as rejected:
        resolver.resolve_event(event)
    assert rejected.value.reason_code == reason
    assert rejected.value.code == reason


def test_resolver_failure_order_is_stable_when_multiple_fields_are_missing(resolver):
    with pytest.raises(ScopeResolutionError) as rejected:
        resolver.resolve_event(_Event(sender_id="", platform_id="", group_id=""))
    assert rejected.value.reason_code == "sender_id_required"


@pytest.mark.parametrize(
    ("method_name", "reason"),
    [
        ("get_self_id", "unknown_bot_self_id"),
        ("get_sender_id", "sender_id_required"),
        ("get_platform_id", "platform_unresolved"),
        ("get_message_type", "chat_kind_unresolved"),
        ("get_group_id", "conversation_id_required"),
    ],
)
def test_resolver_maps_accessor_exceptions_and_preserves_cause(resolver, method_name, reason):
    with pytest.raises(ScopeResolutionError) as rejected:
        resolver.resolve_event(_RaisingEvent(method_name))
    assert rejected.value.reason_code == reason
    assert isinstance(rejected.value.__cause__, RuntimeError)


def test_validator_runtime_matrix_and_unresolved_targets_are_fail_closed():
    validator = ScopeValidator()
    alpha = _group_scope()
    beta = _group_scope(bot_id="bot-beta")
    other_group = _group_scope(group_id="20002")

    assert validator.runtime_compatibility(required=alpha, actual=alpha).allowed is True
    mismatch = validator.runtime_compatibility(required=alpha, actual=beta)
    assert mismatch.allowed is False
    assert mismatch.reason_code == "runtime_scope_mismatch"
    assert not validator.runtime_compatibility(required=alpha, actual=other_group)

    unresolved = UnresolvedScopeRef(
        original_fields={"bot": "unknown"},
        reason_code="legacy_identity_unresolved",
        provenance={"source": "legacy-row"},
    )
    with pytest.raises(ScopeValidationError) as rejected:
        validator.require_target(unresolved, purpose="domain")
    assert rejected.value.reason_code == "unresolved_scope"


def test_command_scope_policy_matrix_is_explicit_and_fail_closed():
    registry = ScopePolicyRegistry(
        {
            "fact.create": ScopeRequirement(
                scope_types=("runtime",),
                visibilities=("group", "private"),
                subject_required=True,
            ),
            "catalog.import": ScopeRequirement(scope_types=("catalog",)),
            "worldview.internalize": ScopeRequirement(
                scope_types=("runtime",),
                visibilities=("bot_private",),
            ),
        }
    )
    validator = ScopeValidator(registry)
    assert validator.validate_command("fact.create", _group_scope()).allowed is True
    assert validator.validate_command("catalog.import", _catalog_scope()).allowed is True
    assert validator.validate_command("worldview.internalize", _bot_private_scope()).allowed is True

    missing = validator.validate_command("unknown.command", _group_scope())
    assert missing.reason_code == "scope_policy_missing"
    assert missing.policy_version == "scope-policy/v1"
    assert validator.validate_command("fact.create", _catalog_scope()).reason_code == "scope_type_not_allowed"
    assert (
        validator.validate_command("fact.create", _bot_private_scope()).reason_code
        == "scope_visibility_not_allowed"
    )
    no_subject = RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=_group_scope().session,
        subject_principal_id=None,
    )
    assert validator.validate_command("fact.create", no_subject).reason_code == "scope_subject_required"
    unresolved = UnresolvedScopeRef(
        original_fields={"bot": "unknown"},
        reason_code="legacy_identity_unresolved",
        provenance={"source": "legacy"},
    )
    assert validator.validate_command("fact.create", unresolved).reason_code == "unresolved_scope"


def test_formal_command_scope_matrix_declares_runtime_catalog_and_bot_private_boundaries():
    registry = build_command_scope_policy_registry()
    validator = ScopeValidator(registry)

    assert "memory.message.write" in COMMAND_SCOPE_MATRIX
    assert "catalog.raw.import" in COMMAND_SCOPE_MATRIX
    assert "worldview.internalization.write" in COMMAND_SCOPE_MATRIX
    for command in (
        "affinity.read", "tag.graph.read", "person.search", "injection.trace.read",
        "feedback.record", "review.candidate.submit", "config.suggest", "catalog.read",
        "fact_proposal.propose", "belief_proposal.propose",
    ):
        assert command in COMMAND_SCOPE_MATRIX

    assert validator.validate_command("jargon.inject", _group_scope()).allowed is True
    assert validate_formal_command_scope("fact.read", _group_scope()).allowed is True
    assert validate_formal_command_scope("fact.read", None).reason_code == "scope_required"
    assert validator.validate_command("catalog.raw.import", _catalog_scope()).allowed is True
    assert validator.validate_command("catalog.read", _catalog_scope()).allowed is True
    assert validator.validate_command("injection.trace.read", _group_scope()).allowed is True
    assert validator.validate_command(
        "worldview.internalization.write", _bot_private_scope()
    ).allowed is True

    private = RuntimeScope(
        bot_id="bot-alpha",
        visibility="private",
        session=SessionRef("qq:private:user-1", "qq", "private", "user-1"),
        subject_principal_id="qq:user:user-1",
    )
    assert validator.validate_command("jargon.inject", private).reason_code == "scope_visibility_not_allowed"
    assert validator.validate_command("catalog.raw.import", _group_scope()).reason_code == "scope_type_not_allowed"
    assert validator.validate_command("catalog.read", _group_scope()).reason_code == "scope_type_not_allowed"

    unresolved = UnresolvedScopeRef(
        original_fields={"group_id": "unknown"},
        reason_code="legacy_scope_unresolved",
        provenance={"source": "legacy"},
    )
    assert validator.validate_command("belief.inject", unresolved).reason_code == "unresolved_scope"
    assert validate_formal_command_scope("belief.inject", unresolved).reason_code == "unresolved_scope"
    assert validator.validate_command("not.registered", _group_scope()).reason_code == "scope_policy_missing"


def test_validator_accepts_only_complete_reviewed_catalog_to_runtime_derivation():
    validator = ScopeValidator()
    catalog = _catalog_scope()
    target = _bot_private_scope()
    derivation = EvidenceDerivation(
        kind="EvidenceDerivation",
        reviewed=True,
        review_status="reviewed",
        derivation_version="v1",
        policy_version="scope-derivation/v1",
        source=catalog,
        target=target,
        derivation_chain=FULL_EVIDENCE_DERIVATION_CHAIN,
    )
    accepted = validator.compatibility(
        catalog=catalog,
        runtime=target,
        evidence_derivation=derivation,
    )
    assert accepted.allowed is True
    assert EvidenceDerivation.from_dict(derivation.to_dict()) == derivation

    raw = derivation.to_dict()
    raw.update(reviewed=False, review_status="raw", derivation_chain=("raw",))
    rejected = validator.compatibility(
        catalog=catalog,
        runtime=target,
        evidence_derivation=raw,
    )
    assert rejected.allowed is False
    assert rejected.reason_code == "derivation_not_reviewed"

    for missing_stage in ("reviewed_projection", "scoped_candidate", "domain_object"):
        malformed = derivation.to_dict()
        malformed["derivation_chain"] = tuple(
            stage for stage in FULL_EVIDENCE_DERIVATION_CHAIN if stage != missing_stage
        )
        rejected = validator.compatibility(
            catalog=catalog,
            runtime=target,
            evidence_derivation=malformed,
        )
        assert rejected.allowed is False
        assert rejected.reason_code == "invalid_derivation_chain"

    unsupported_policy = derivation.to_dict()
    unsupported_policy["policy_version"] = "scope-derivation/v999"
    rejected = validator.compatibility(
        catalog=catalog,
        runtime=target,
        evidence_derivation=unsupported_policy,
    )
    assert rejected.allowed is False
    assert rejected.reason_code == "derivation_policy_unsupported"
    assert rejected.policy_version == "scope-derivation/v1"

