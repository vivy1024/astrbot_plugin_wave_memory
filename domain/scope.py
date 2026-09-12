"""Canonical scope value objects and fail-closed compatibility validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

SessionKind: TypeAlias = Literal["group", "private", "system"]
RuntimeVisibility: TypeAlias = Literal["group", "private", "bot_private", "system"]
ScopeKind: TypeAlias = Literal["runtime", "catalog"]

_SESSION_KINDS = frozenset({"group", "private", "system"})
_RUNTIME_VISIBILITIES = frozenset({"group", "private", "bot_private", "system"})
_RUNTIME_POLICY_VERSION = "runtime-exact/v1"
_DERIVATION_POLICY_VERSION = "scope-derivation/v1"
_FULL_DERIVATION_CHAIN = (
    "raw",
    "reviewed_projection",
    "scoped_candidate",
    "domain_object",
)


class ScopeValidationError(ValueError):
    """A scope rejection carrying a stable machine-readable reason code."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        self.code = reason_code
        self.message = message or reason_code
        super().__init__(f"{reason_code}: {self.message}")


class _FrozenMapping(Mapping[str, Any]):
    """Deeply immutable mapping whose deepcopy is a serializable plain dict."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any]) -> None:
        if any(not isinstance(key, str) for key in value):
            raise ScopeValidationError("invalid_mapping_key", "scope mapping keys must be strings")
        self._data = {key: _freeze_value(item) for key, item in value.items()}

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(self._data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._data.items())))

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        return {key: _thaw_value(value) for key, value in self._data.items()}


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    try:
        hash(value)
    except TypeError as exc:
        raise ScopeValidationError(
            "unserializable_scope_value",
            "scope mappings may contain only immutable, serializable values",
        ) from exc
    return value


def _thaw_value(value: Any) -> Any:
    if isinstance(value, _FrozenMapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_thaw_value(item) for item in value)
    if isinstance(value, frozenset):
        return tuple(_thaw_value(item) for item in value)
    return value


def _require_nonempty_string(value: Any, *, reason_code: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ScopeValidationError(reason_code, f"{field_name} must be a non-empty exact string")
    return value


def _validate_bot_id(bot_id: Any) -> str:
    value = _require_nonempty_string(
        bot_id,
        reason_code="invalid_bot_id",
        field_name="bot_id",
    )
    if value.isdecimal() or value.casefold() in {"bot", "default"}:
        raise ScopeValidationError(
            "invalid_bot_id",
            "bot_id must be a stable BotProfile.db_id, not a QQ id or fallback sentinel",
        )
    return value


@dataclass(frozen=True)
class SessionRef:
    id: str
    platform_id: str
    kind: SessionKind
    conversation_id: str

    def __post_init__(self) -> None:
        platform_id = _require_nonempty_string(
            self.platform_id,
            reason_code="platform_id_required",
            field_name="platform_id",
        )
        if self.kind not in _SESSION_KINDS:
            raise ScopeValidationError("invalid_session_kind", f"unsupported session kind: {self.kind!r}")
        conversation_id = _require_nonempty_string(
            self.conversation_id,
            reason_code="conversation_id_required",
            field_name="conversation_id",
        )
        expected_id = f"{platform_id}:{self.kind}:{conversation_id}"
        if self.id != expected_id:
            raise ScopeValidationError(
                "invalid_session_id",
                f"session id must equal canonical id {expected_id!r}",
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionRef":
        return cls(**_exact_fields(payload, ("id", "platform_id", "kind", "conversation_id"), "SessionRef"))


@dataclass(frozen=True)
class RuntimeScope:
    bot_id: str
    visibility: RuntimeVisibility
    session: SessionRef | None
    subject_principal_id: str | None = None

    def __post_init__(self) -> None:
        _validate_bot_id(self.bot_id)
        if self.visibility not in _RUNTIME_VISIBILITIES:
            raise ScopeValidationError(
                "invalid_visibility",
                f"unsupported runtime visibility: {self.visibility!r}",
            )

        if self.visibility in {"group", "private"}:
            if self.session is None:
                raise ScopeValidationError("session_required", f"{self.visibility} scope requires a session")
            if not isinstance(self.session, SessionRef):
                raise ScopeValidationError("invalid_session", "runtime session must be a SessionRef")
            if self.session.kind != self.visibility:
                raise ScopeValidationError(
                    "session_kind_mismatch",
                    "session kind must equal runtime visibility",
                )
        elif self.session is not None:
            raise ScopeValidationError(
                "session_forbidden",
                f"{self.visibility} scope must not carry a session",
            )

        if self.subject_principal_id is not None:
            principal = _require_nonempty_string(
                self.subject_principal_id,
                reason_code="invalid_subject_principal",
                field_name="subject_principal_id",
            )
            if self.visibility in {"bot_private", "system"}:
                raise ScopeValidationError(
                    "subject_principal_forbidden",
                    f"{self.visibility} scope must not carry a subject principal",
                )
            assert self.session is not None
            prefix = f"{self.session.platform_id}:user:"
            if not principal.startswith(prefix) or principal == prefix:
                raise ScopeValidationError(
                    "invalid_subject_principal",
                    f"subject principal must use {prefix!r} namespace",
                )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeScope":
        values = _exact_fields(
            payload,
            ("bot_id", "visibility", "session", "subject_principal_id"),
            "RuntimeScope",
        )
        session = values["session"]
        if isinstance(session, Mapping):
            values["session"] = SessionRef.from_dict(session)
        return cls(**values)


@dataclass(frozen=True)
class CatalogScope:
    catalog_id: str
    corpus_id: str
    version: str

    def __post_init__(self) -> None:
        for field_name in ("catalog_id", "corpus_id", "version"):
            _require_nonempty_string(
                getattr(self, field_name),
                reason_code=f"{field_name}_required",
                field_name=field_name,
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogScope":
        return cls(**_exact_fields(payload, ("catalog_id", "corpus_id", "version"), "CatalogScope"))


@dataclass(frozen=True)
class UnresolvedScopeRef:
    original_fields: Mapping[str, Any]
    reason_code: str
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.original_fields, Mapping):
            raise ScopeValidationError("invalid_original_fields", "original_fields must be a mapping")
        _require_nonempty_string(
            self.reason_code,
            reason_code="unresolved_reason_required",
            field_name="reason_code",
        )
        if not isinstance(self.provenance, Mapping):
            raise ScopeValidationError("invalid_provenance", "provenance must be a mapping")
        object.__setattr__(self, "original_fields", _FrozenMapping(self.original_fields))
        object.__setattr__(self, "provenance", _FrozenMapping(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "UnresolvedScopeRef":
        return cls(
            **_exact_fields(
                payload,
                ("original_fields", "reason_code", "provenance"),
                "UnresolvedScopeRef",
            )
        )


ScopeRef: TypeAlias = RuntimeScope | CatalogScope | UnresolvedScopeRef
ResolvedScope: TypeAlias = RuntimeScope | CatalogScope


@dataclass(frozen=True)
class ScopeValidationResult:
    allowed: bool
    reason_code: str | None = None
    policy_version: str | None = None

    @property
    def code(self) -> str | None:
        return self.reason_code

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class ScopeRequirement:
    scope_types: tuple[ScopeKind, ...]
    visibilities: tuple[RuntimeVisibility, ...] = ()
    subject_required: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope_types, tuple)
            or not self.scope_types
            or any(kind not in {"runtime", "catalog"} for kind in self.scope_types)
            or len(set(self.scope_types)) != len(self.scope_types)
        ):
            raise ScopeValidationError(
                "invalid_scope_policy",
                "scope_types must be a non-empty unique tuple of runtime/catalog",
            )
        if (
            not isinstance(self.visibilities, tuple)
            or any(value not in _RUNTIME_VISIBILITIES for value in self.visibilities)
            or len(set(self.visibilities)) != len(self.visibilities)
        ):
            raise ScopeValidationError(
                "invalid_scope_policy",
                "visibilities must be a unique tuple of RuntimeVisibility values",
            )
        if self.visibilities and "runtime" not in self.scope_types:
            raise ScopeValidationError(
                "invalid_scope_policy",
                "catalog-only requirements cannot declare runtime visibilities",
            )
        if not isinstance(self.subject_required, bool):
            raise ScopeValidationError("invalid_scope_policy", "subject_required must be bool")


class ScopePolicyRegistry:
    policy_version = "scope-policy/v1"

    def __init__(
        self,
        policies: Mapping[str, ScopeRequirement] | None = None,
    ) -> None:
        self._policies: dict[str, ScopeRequirement] = {}
        if policies is None:
            return
        if not isinstance(policies, Mapping):
            raise ScopeValidationError("invalid_scope_policy", "policies must be a mapping")
        for command_type, requirement in policies.items():
            self.register(command_type, requirement)

    def register(self, command_type: str, requirement: ScopeRequirement) -> None:
        _require_nonempty_string(
            command_type,
            reason_code="invalid_scope_policy",
            field_name="command_type",
        )
        if not isinstance(requirement, ScopeRequirement):
            raise ScopeValidationError(
                "invalid_scope_policy",
                "requirement must be ScopeRequirement",
            )
        previous = self._policies.get(command_type)
        if previous is not None and previous != requirement:
            raise ScopeValidationError(
                "scope_policy_conflict",
                f"command {command_type!r} already has a different scope policy",
            )
        self._policies[command_type] = requirement

    def get(self, command_type: str) -> ScopeRequirement | None:
        if not isinstance(command_type, str):
            return None
        return self._policies.get(command_type)

    def validate(self, command_type: str, scope: ScopeRef | None) -> ScopeValidationResult:
        requirement = self.get(command_type)
        if requirement is None:
            return ScopeValidationResult(
                False,
                "scope_policy_missing",
                self.policy_version,
            )
        if scope is None:
            return ScopeValidationResult(False, "scope_required", self.policy_version)
        if isinstance(scope, UnresolvedScopeRef):
            return ScopeValidationResult(False, "unresolved_scope", self.policy_version)
        if isinstance(scope, RuntimeScope):
            scope_kind = "runtime"
        elif isinstance(scope, CatalogScope):
            scope_kind = "catalog"
        else:
            return ScopeValidationResult(False, "scope_type_not_allowed", self.policy_version)
        if scope_kind not in requirement.scope_types:
            return ScopeValidationResult(False, "scope_type_not_allowed", self.policy_version)
        if isinstance(scope, RuntimeScope):
            if requirement.visibilities and scope.visibility not in requirement.visibilities:
                return ScopeValidationResult(
                    False,
                    "scope_visibility_not_allowed",
                    self.policy_version,
                )
            if requirement.subject_required and scope.subject_principal_id is None:
                return ScopeValidationResult(
                    False,
                    "scope_subject_required",
                    self.policy_version,
                )
        elif requirement.subject_required:
            return ScopeValidationResult(False, "scope_subject_required", self.policy_version)
        return ScopeValidationResult(True, policy_version=self.policy_version)


# 正式领域命令的唯一 Scope Compatibility Matrix。它只声明当前已落地的
# 接受边界；未登记命令一律在 ScopePolicyRegistry 中 fail closed，而不是从
# group_id、Bot 显示名或 legacy 字段猜测 Scope。
COMMAND_SCOPE_MATRIX: Mapping[str, ScopeRequirement] = MappingProxyType({
    # 已持久化为 v2 的群会话资源。
    "memory.message.write": ScopeRequirement(("runtime",), ("group", "private")),
    "memory.message.read": ScopeRequirement(("runtime",), ("group", "private")),
    "tag.extract": ScopeRequirement(("runtime",), ("group",)),
    "jargon.mine": ScopeRequirement(("runtime",), ("group",)),
    "jargon.inject": ScopeRequirement(("runtime",), ("group",)),
    "jargon.manage": ScopeRequirement(("runtime",), ("group",)),
    # 广域黑话提升由 group scope 进入（必须校验源黑话归属某个群），只取 bot_id 落库；
    # Bot 级表的直管则明确声明为 bot_private——它不带 session，是 Bot 私有认知而非群会话资源。
    "jargon.promote_global": ScopeRequirement(("runtime",), ("group",)),
    "jargon.global.manage": ScopeRequirement(("runtime",), ("bot_private",)),
    "consolidation.run": ScopeRequirement(("runtime",), ("group",)),
    "fact.read": ScopeRequirement(("runtime",), ("group",)),
    "fact.write": ScopeRequirement(("runtime",), ("group",)),
    # Agent tools must declare their boundaries explicitly; unknown commands fail closed.
    "affinity.read": ScopeRequirement(("runtime",), ("group",)),
    "tag.graph.read": ScopeRequirement(("runtime",), ("group",)),
    "person.search": ScopeRequirement(("runtime",), ("group",)),
    "injection.trace.read": ScopeRequirement(("runtime",), ("group",)),
    "feedback.record": ScopeRequirement(("runtime",), ("group",)),
    "review.candidate.submit": ScopeRequirement(("runtime",), ("group",)),
    "config.suggest": ScopeRequirement(("runtime",), ("group",)),
    "catalog.read": ScopeRequirement(("catalog",)),
    "belief.extract": ScopeRequirement(("runtime",), ("group",)),
    "belief.inject": ScopeRequirement(("runtime",), ("group",)),
    "persona.inject": ScopeRequirement(("runtime",), ("group",)),
    # 可选风格人格包：与 persona.inject 同为群会话内的注入，默认关闭由通道配置决定。
    "holyman_persona.inject": ScopeRequirement(("runtime",), ("group",)),
    "affinity.update": ScopeRequirement(("runtime",), ("group",), subject_required=True),
    "social_impression.record": ScopeRequirement(("runtime",), ("group",), subject_required=True),
    "social_anchor.note": ScopeRequirement(("runtime",), ("group",), subject_required=True),
    "cultural_moment.mark": ScopeRequirement(("runtime",), ("group",)),
    "fact_proposal.propose": ScopeRequirement(("runtime",), ("group",)),
    "belief_proposal.propose": ScopeRequirement(("runtime",), ("group",)),
    "episode.note": ScopeRequirement(("runtime",), ("group",)),
    "diary.record": ScopeRequirement(("runtime",), ("group",)),
    "concern.note": ScopeRequirement(("runtime",), ("group",)),
    "relationship.record": ScopeRequirement(("runtime",), ("group",), subject_required=True),
    "self_reflect.candidate": ScopeRequirement(("runtime",), ("group",)),
    "livingmemory.compat.write": ScopeRequirement(("runtime",), ("group",), subject_required=True),

    # Catalog 与 Bot 私有认知不伪装成群会话；实际领域服务会在后续阶段接入。
    "catalog.raw.import": ScopeRequirement(("catalog",)),
    "catalog.reviewed_projection": ScopeRequirement(("catalog",)),
    "worldview.internalization.write": ScopeRequirement(("runtime",), ("bot_private",)),
})


def build_command_scope_policy_registry() -> ScopePolicyRegistry:
    """Return an isolated registry backed by the immutable formal command matrix."""
    return ScopePolicyRegistry(COMMAND_SCOPE_MATRIX)


def validate_formal_command_scope(
    command_type: str,
    scope: ScopeRef | None,
) -> ScopeValidationResult:
    """Validate a formal entry against the immutable command Scope matrix.

    The registry is constructed per call so a caller cannot mutate shared policy
    state and affect another formal entry.
    """
    return build_command_scope_policy_registry().validate(command_type, scope)


class ScopeValidator:
    """Central fail-closed validation for targets and evidence scope derivation."""

    def __init__(self, policy_registry: ScopePolicyRegistry | None = None) -> None:
        if policy_registry is not None and not isinstance(policy_registry, ScopePolicyRegistry):
            raise TypeError("policy_registry must be ScopePolicyRegistry")
        self.policy_registry = policy_registry or ScopePolicyRegistry()

    def validate_command(self, command_type: str, scope: ScopeRef) -> ScopeValidationResult:
        return self.policy_registry.validate(command_type, scope)

    def require_target(self, scope: ScopeRef, *, purpose: str) -> ResolvedScope:
        _require_nonempty_string(purpose, reason_code="purpose_required", field_name="purpose")
        if isinstance(scope, UnresolvedScopeRef):
            raise ScopeValidationError(
                "unresolved_scope",
                f"unresolved scope cannot be used for {purpose}",
            )
        if not isinstance(scope, (RuntimeScope, CatalogScope)):
            raise ScopeValidationError("invalid_scope_type", f"unsupported target scope: {type(scope)!r}")
        return scope

    def runtime_compatibility(
        self,
        *,
        required: RuntimeScope,
        actual: RuntimeScope,
    ) -> ScopeValidationResult:
        if not isinstance(required, RuntimeScope) or not isinstance(actual, RuntimeScope):
            return ScopeValidationResult(False, "runtime_scope_required", _RUNTIME_POLICY_VERSION)
        if required != actual:
            return ScopeValidationResult(False, "runtime_scope_mismatch", _RUNTIME_POLICY_VERSION)
        return ScopeValidationResult(True, policy_version=_RUNTIME_POLICY_VERSION)

    def compatibility(
        self,
        *,
        catalog: CatalogScope,
        runtime: RuntimeScope,
        evidence_derivation: Mapping[str, Any] | Any,
    ) -> ScopeValidationResult:
        if not isinstance(catalog, CatalogScope):
            return ScopeValidationResult(False, "catalog_scope_required", _DERIVATION_POLICY_VERSION)
        if not isinstance(runtime, RuntimeScope):
            return ScopeValidationResult(False, "runtime_scope_required", _DERIVATION_POLICY_VERSION)

        payload = _derivation_payload(evidence_derivation)
        if payload is None:
            return ScopeValidationResult(False, "evidence_derivation_required", _DERIVATION_POLICY_VERSION)
        if payload.get("kind") != "EvidenceDerivation":
            return ScopeValidationResult(False, "invalid_derivation_kind", _DERIVATION_POLICY_VERSION)
        if payload.get("reviewed") is not True or payload.get("review_status") != "reviewed":
            return ScopeValidationResult(False, "derivation_not_reviewed", _DERIVATION_POLICY_VERSION)
        if not _is_nonempty_exact_string(payload.get("derivation_version")):
            return ScopeValidationResult(False, "derivation_version_required", _DERIVATION_POLICY_VERSION)
        policy_version = payload.get("policy_version")
        if not _is_nonempty_exact_string(policy_version):
            return ScopeValidationResult(False, "derivation_policy_required", _DERIVATION_POLICY_VERSION)
        if policy_version != _DERIVATION_POLICY_VERSION:
            return ScopeValidationResult(False, "derivation_policy_unsupported", _DERIVATION_POLICY_VERSION)
        if tuple(payload.get("derivation_chain") or ()) != _FULL_DERIVATION_CHAIN:
            return ScopeValidationResult(False, "invalid_derivation_chain", _DERIVATION_POLICY_VERSION)
        if payload.get("source") != asdict(catalog):
            return ScopeValidationResult(False, "derivation_source_mismatch", _DERIVATION_POLICY_VERSION)
        if payload.get("target") != asdict(runtime):
            return ScopeValidationResult(False, "derivation_target_mismatch", _DERIVATION_POLICY_VERSION)
        return ScopeValidationResult(True, policy_version=_DERIVATION_POLICY_VERSION)


def scope_to_dict(scope: ScopeRef) -> dict[str, Any]:
    if not isinstance(scope, (RuntimeScope, CatalogScope, UnresolvedScopeRef)):
        raise ScopeValidationError("invalid_scope_type", f"unsupported scope: {type(scope)!r}")
    return asdict(scope)


def scope_from_dict(kind: str, payload: Mapping[str, Any]) -> ScopeRef:
    scope_types = {
        "RuntimeScope": RuntimeScope,
        "CatalogScope": CatalogScope,
        "UnresolvedScopeRef": UnresolvedScopeRef,
    }
    scope_type = scope_types.get(kind)
    if scope_type is None:
        raise ScopeValidationError("invalid_scope_type", f"unsupported scope kind: {kind!r}")
    return scope_type.from_dict(payload)


def scope_from_value(value: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    """Decode a ScopeRef value without guessing its kind from legacy strings."""
    if isinstance(value, (RuntimeScope, CatalogScope, UnresolvedScopeRef)):
        return value
    if not isinstance(value, Mapping):
        raise ScopeValidationError("invalid_serialized_scope", "scope value must be a ScopeRef or mapping")
    fields = set(value)
    if fields == {"bot_id", "visibility", "session", "subject_principal_id"}:
        return RuntimeScope.from_dict(value)
    if fields == {"catalog_id", "corpus_id", "version"}:
        return CatalogScope.from_dict(value)
    if fields == {"original_fields", "reason_code", "provenance"}:
        return UnresolvedScopeRef.from_dict(value)
    raise ScopeValidationError("invalid_serialized_scope", "scope fields do not identify a supported ScopeRef")


class ScopeCodec:
    """Self-describing serialization envelope for a ScopeRef."""

    @staticmethod
    def to_dict(scope: ScopeRef) -> dict[str, Any]:
        return {"kind": type(scope).__name__, "payload": scope_to_dict(scope)}

    @staticmethod
    def from_dict(envelope: Mapping[str, Any]) -> ScopeRef:
        if not isinstance(envelope, Mapping) or set(envelope) != {"kind", "payload"}:
            raise ScopeValidationError(
                "invalid_serialized_scope",
                "scope envelope must contain exactly kind and payload",
            )
        kind = envelope["kind"]
        payload = envelope["payload"]
        if not isinstance(kind, str) or not isinstance(payload, Mapping):
            raise ScopeValidationError("invalid_serialized_scope", "scope envelope types are invalid")
        return scope_from_dict(kind, payload)

    encode = to_dict
    decode = from_dict


def resolution_state(scope: ScopeRef) -> str:
    if isinstance(scope, UnresolvedScopeRef):
        return "unresolved_legacy"
    if isinstance(scope, (RuntimeScope, CatalogScope)):
        return "resolved"
    raise ScopeValidationError("invalid_scope_type", f"unsupported scope: {type(scope)!r}")


def _exact_fields(
    payload: Mapping[str, Any],
    expected: tuple[str, ...],
    type_name: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ScopeValidationError("invalid_serialized_scope", f"{type_name} payload must be a mapping")
    if set(payload) != set(expected):
        raise ScopeValidationError(
            "invalid_serialized_scope",
            f"{type_name} fields must be exactly {expected!r}",
        )
    return {name: payload[name] for name in expected}


def _is_nonempty_exact_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _derivation_payload(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, Mapping) else None
    return None


__all__ = [
    "CatalogScope",
    "COMMAND_SCOPE_MATRIX",
    "RuntimeScope",
    "RuntimeVisibility",
    "ScopeCodec",
    "ScopeKind",
    "ScopePolicyRegistry",
    "ScopeRef",
    "ScopeRequirement",
    "ScopeValidationError",
    "ScopeValidationResult",
    "ScopeValidator",
    "build_command_scope_policy_registry",
    "validate_formal_command_scope",
    "SessionRef",
    "UnresolvedScopeRef",
    "scope_from_dict",
    "scope_from_value",
]

