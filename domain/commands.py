"""Canonical command and write-result contracts for coordinated domain writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .scope import CatalogScope, RuntimeScope


def _mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class DomainCommand:
    operation_id: str
    idempotency_key: str
    actor: str
    scope: RuntimeScope | CatalogScope
    command_type: str
    payload: Mapping[str, Any]
    request_hash: str

    def __post_init__(self) -> None:
        for name in ("operation_id", "idempotency_key", "actor", "command_type", "request_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "payload", _mapping(self.payload))


@dataclass(frozen=True)
class EntityChange:
    aggregate_kind: str
    aggregate_id: str
    aggregate_version: int
    change_type: str


@dataclass(frozen=True)
class OutboxEventRef:
    event_id: str
    event_type: str
    aggregate_kind: str
    aggregate_id: str
    aggregate_version: int


@dataclass(frozen=True)
class DomainWriteResult:
    operation_id: str
    committed_at: float
    write_sequence: int
    entities: tuple[EntityChange, ...] = field(default_factory=tuple)
    effects: tuple[OutboxEventRef, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 必须是普通 dict：WriteCoordinator 跨线程传结果时需要 pickle。
        object.__setattr__(self, "details", dict(self.details or {}))


class CommandRejectedError(RuntimeError):
    """A stable machine-readable command rejection."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message or code)


class IdempotencyConflictError(CommandRejectedError):
    def __init__(self, message: str = "idempotency key was reused with different input") -> None:
        super().__init__("idempotency_conflict", message)
