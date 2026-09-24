"""Shared test-only assertions and frozen binding protocols for system convergence."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, fields
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable


def contract_fail(reason: str, detail: str) -> None:
    raise AssertionError(f"{reason}: {detail}")


def contract_assert(condition: Any, reason: str, detail: str) -> None:
    if not condition:
        contract_fail(reason, detail)


def require_module(module_name: str, symbols: Iterable[str], reason: str):
    """Import one normative module without hiding defects in that module's imports."""
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name or module_name.startswith(f"{exc.name}."):
            contract_fail(reason, f"missing_contract: module {module_name!r} does not exist")
        raise
    missing = [name for name in symbols if getattr(module, name, None) is None]
    contract_assert(
        not missing,
        reason,
        f"missing_contract: module {module_name!r} lacks public symbols {missing!r}",
    )
    return module


def reason_code(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        nested = value.get("error")
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"])
        for key in ("reason_code", "error_code", "code"):
            if value.get(key):
                return str(value[key])
        return None
    for key in ("reason_code", "error_code", "code"):
        item = getattr(value, key, None)
        if item:
            return str(item)
    return None


def strict_allowed(result: Any, reason: str) -> bool:
    if isinstance(result, bool):
        return result
    value = getattr(result, "allowed", None)
    contract_assert(
        isinstance(value, bool),
        reason,
        "invalid_contract_result: validator result.allowed must be bool",
    )
    return value


@runtime_checkable
class ScopeValidationAdapter(Protocol):
    """Pure test adapter that may only delegate to the supplied real ScopeValidator.

    These method names are test-port names, not production requirements. The adapter must not
    implement scope rules, normalize inputs, or manufacture validation results.
    """

    @property
    def calls(self) -> list[Any]: ...

    def validate_runtime_pair(self, *, required: Any, actual: Any) -> Any: ...

    def validate_unresolved_target(self, *, scope: Any, purpose: str) -> Any: ...

    def validate_evidence_derivation(
        self, *, source_catalog: Any, target_runtime: Any, evidence_derivation: Mapping[str, Any]
    ) -> Any: ...


def load_scope_validation_adapter(validator: Any, reason: str) -> ScopeValidationAdapter:
    module = require_module(
        "tests.system_convergence.scope_bindings", ("bind_scope_validator",), reason
    )
    adapter = module.bind_scope_validator(validator)
    contract_assert(
        isinstance(adapter, ScopeValidationAdapter),
        reason,
        "invalid_test_binding: scope adapter does not satisfy ScopeValidationAdapter",
    )
    return adapter


@runtime_checkable
class OutboxTestPortAdapter(Protocol):
    """Thin forwarding boundary to services.migrations_archive.system_convergence_test_port."""

    def create_runtime(
        self, database_path: str, *, consumers: Mapping[str, Any], clock: Any
    ) -> Any: ...

    def make_probe_command(self, **kwargs: Any) -> Any: ...


def load_outbox_test_port(reason: str) -> OutboxTestPortAdapter:
    module = require_module(
        "tests.system_convergence.outbox_bindings", ("bind_outbox_test_port",), reason
    )
    adapter = module.bind_outbox_test_port(reason)
    contract_assert(
        isinstance(adapter, OutboxTestPortAdapter),
        reason,
        "invalid_test_binding: outbox adapter does not satisfy OutboxTestPortAdapter",
    )
    return adapter


@runtime_checkable
class ApiCompositionAdapter(Protocol):
    """Thin caller of the frozen create_app keyword-only dependency slots."""

    def create_app(
        self,
        app_factory: Any,
        *,
        registry_input: Any | None,
        request_scope_input: Any | None,
    ) -> Any: ...


@runtime_checkable
class QualityPromotionAdapter(Protocol):
    """Test-only canonical target_scope mapping into supplied real promotion services."""

    @property
    def calls(self) -> list[Any]: ...

    def promote_fact(self, *, candidate: dict, bot_id: str, target_kind: str) -> Any: ...

    def promote_worldview(self, *, candidate: dict, bot_id: str, target_kind: str) -> Any: ...


def load_quality_promotion_adapter(
    reason: str, *, fact_service: Any = None, worldview_service: Any = None
) -> QualityPromotionAdapter:
    module = require_module(
        "tests.system_convergence.quality_bindings",
        ("create_quality_promotion_adapter",),
        reason,
    )
    adapter = module.create_quality_promotion_adapter(
        fact_service=fact_service,
        worldview_service=worldview_service,
    )
    contract_assert(
        isinstance(adapter, QualityPromotionAdapter),
        reason,
        "invalid_test_binding: quality adapter does not satisfy QualityPromotionAdapter",
    )
    return adapter


def load_api_composition_adapter(reason: str) -> ApiCompositionAdapter:
    module = require_module(
        "tests.system_convergence.api_bindings", ("create_api_composition_adapter",), reason
    )
    adapter = module.create_api_composition_adapter()
    contract_assert(
        isinstance(adapter, ApiCompositionAdapter),
        reason,
        "invalid_test_binding: API adapter does not expose frozen create_app composition",
    )
    return adapter


@dataclass
class ScopeWorld:
    SessionRef: type
    RuntimeScope: type
    CatalogScope: type
    UnresolvedScopeRef: type
    ScopeValidator: type
    validator: Any
    group_a: Any
    group_b: Any
    private: Any
    runtime_alpha_group_a: Any
    runtime_beta_group_a: Any
    runtime_alpha_group_b: Any
    runtime_alpha_private: Any
    runtime_alpha_bot_private: Any
    catalog: Any
    unresolved: Any


def _assert_dataclass_fields(scope_type: type, expected: tuple[str, ...], reason: str) -> None:
    try:
        actual = tuple(field.name for field in fields(scope_type))
    except TypeError:
        contract_fail(reason, f"invalid_contract: {scope_type.__name__} is not a dataclass")
    contract_assert(
        actual == expected,
        reason,
        f"invalid_contract: {scope_type.__name__} fields={actual!r}, expected={expected!r}",
    )


def build_scope_world(reason: str) -> ScopeWorld:
    module = require_module(
        "domain.scope",
        ("SessionRef", "RuntimeScope", "CatalogScope", "UnresolvedScopeRef", "ScopeValidator"),
        reason,
    )
    SessionRef = module.SessionRef
    RuntimeScope = module.RuntimeScope
    CatalogScope = module.CatalogScope
    UnresolvedScopeRef = module.UnresolvedScopeRef
    ScopeValidator = module.ScopeValidator
    _assert_dataclass_fields(
        SessionRef, ("id", "platform_id", "kind", "conversation_id"), reason
    )
    _assert_dataclass_fields(
        RuntimeScope, ("bot_id", "visibility", "session", "subject_principal_id"), reason
    )
    _assert_dataclass_fields(CatalogScope, ("catalog_id", "corpus_id", "version"), reason)
    _assert_dataclass_fields(
        UnresolvedScopeRef, ("original_fields", "reason_code", "provenance"), reason
    )
    principal = "qq:user:900000001"
    group_a = SessionRef(
        id="qq:group:group-alpha",
        platform_id="qq",
        kind="group",
        conversation_id="group-alpha",
    )
    group_b = SessionRef(
        id="qq:group:group-beta",
        platform_id="qq",
        kind="group",
        conversation_id="group-beta",
    )
    private = SessionRef(
        id="qq:private:900000001",
        platform_id="qq",
        kind="private",
        conversation_id="900000001",
    )
    return ScopeWorld(
        SessionRef=SessionRef,
        RuntimeScope=RuntimeScope,
        CatalogScope=CatalogScope,
        UnresolvedScopeRef=UnresolvedScopeRef,
        ScopeValidator=ScopeValidator,
        validator=ScopeValidator(),
        group_a=group_a,
        group_b=group_b,
        private=private,
        runtime_alpha_group_a=RuntimeScope(
            bot_id="bot-alpha",
            visibility="group",
            session=group_a,
            subject_principal_id=principal,
        ),
        runtime_beta_group_a=RuntimeScope(
            bot_id="bot-beta",
            visibility="group",
            session=group_a,
            subject_principal_id=principal,
        ),
        runtime_alpha_group_b=RuntimeScope(
            bot_id="bot-alpha",
            visibility="group",
            session=group_b,
            subject_principal_id=principal,
        ),
        runtime_alpha_private=RuntimeScope(
            bot_id="bot-alpha",
            visibility="private",
            session=private,
            subject_principal_id=principal,
        ),
        runtime_alpha_bot_private=RuntimeScope(
            bot_id="bot-alpha",
            visibility="bot_private",
            session=None,
            subject_principal_id=None,
        ),
        catalog=CatalogScope(
            catalog_id="book-lore-alpha", corpus_id="corpus-alpha", version="v1"
        ),
        unresolved=UnresolvedScopeRef(
            original_fields={"bot_id": "900000001", "group_id": "group-alpha"},
            reason_code="legacy_identity_unresolved",
            provenance={"source": "legacy-row", "table": "memories"},
        ),
    )
