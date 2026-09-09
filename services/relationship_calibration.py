"""Writer-owned scoped relationship calibration gateway."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:
    from ..domain.commands import CommandRejectedError, DomainCommand, EntityChange, IdempotencyConflictError
    from ..domain.scope import RuntimeScope, scope_to_dict
    from ..engine.db.scoped_soul_repo import ScopedSoulScopeError
    from ..engine.write_coordinator import MutationOutcome, OutboxEventDraft
except ImportError:  # pragma: no cover
    from domain.commands import CommandRejectedError, DomainCommand, EntityChange, IdempotencyConflictError
    from domain.scope import RuntimeScope, scope_to_dict
    from engine.db.scoped_soul_repo import ScopedSoulScopeError
    from engine.write_coordinator import MutationOutcome, OutboxEventDraft


_ACTIONS = frozenset({"adjust", "override", "clear_override", "restore_auto"})


class RelationshipCalibrationError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.reason_code = self.code
        super().__init__(message or self.code)


@dataclass(frozen=True)
class RelationshipCalibrationResult:
    operation_id: str
    calibration_id: str
    revision: int
    status: str
    subject_principal_id: str
    dimension: str
    action: str
    before: dict[str, Any]
    after: dict[str, Any]
    affinity: int
    state: str
    evidence: list[dict[str, Any]]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        raise RelationshipCalibrationError("scope_required")
    return scope.bot_id, scope.session.id, scope.visibility


def _normalize_evidence(value: Any, scope: RuntimeScope) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise RelationshipCalibrationError("relationship_evidence_required")
    if len(value) > 20:
        raise RelationshipCalibrationError("relationship_evidence_too_large")
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise RelationshipCalibrationError("relationship_evidence_invalid")
        # 必需键必须齐全；type/title/locator 是 resolver 产出的展示补充键，允许保留；
        # summary 是人工校准补充说明，作为审计内容透传落盘
        required = {"kind", "id", "content_hash", "captured_at", "source_scope", "available"}
        optional = {"type", "title", "locator", "summary"}
        keys = set(item)
        if not required.issubset(keys):
            raise RelationshipCalibrationError("relationship_evidence_invalid")
        unknown = keys - required - optional
        if unknown:
            raise RelationshipCalibrationError("relationship_evidence_invalid")
        if not all(isinstance(item[key], str) and item[key].strip() == item[key] and item[key] for key in ("kind", "id", "content_hash")):
            raise RelationshipCalibrationError("relationship_evidence_invalid")
        summary = item.get("summary")
        if summary is not None:
            if not isinstance(summary, str) or summary != summary.strip() or not summary or len(summary) > 500:
                raise RelationshipCalibrationError("relationship_evidence_invalid")
        if item["available"] is not True:
            raise RelationshipCalibrationError("relationship_evidence_invalid")
        source_scope = item["source_scope"]
        if isinstance(source_scope, Mapping):
            try:
                from ..domain.scope import RuntimeScope as ScopeType
            except ImportError:  # pragma: no cover
                from domain.scope import RuntimeScope as ScopeType
            try:
                source_scope = ScopeType.from_dict(source_scope)
            except Exception as exc:
                raise RelationshipCalibrationError("relationship_evidence_invalid") from exc
        if source_scope != scope:
            raise RelationshipCalibrationError("relationship_evidence_scope_mismatch")
        try:
            captured_at = float(item["captured_at"])
        except (TypeError, ValueError) as exc:
            raise RelationshipCalibrationError("relationship_evidence_invalid") from exc
        if not math.isfinite(captured_at) or captured_at < 0:
            raise RelationshipCalibrationError("relationship_evidence_invalid")
        normalized.append({**dict(item), "source_scope": scope_to_dict(scope), "captured_at": captured_at})
    if len(_json(normalized).encode("utf-8")) > 64 * 1024:
        raise RelationshipCalibrationError("relationship_evidence_too_large")
    return normalized


RELATIONSHIP_CALIBRATE_COMMAND = "relationship.webui.calibrate.v1"


def apply_relationship_calibration(
    connection,
    *,
    repository: Any,
    scope: RuntimeScope,
    operation_id: str,
    now: float,
    subject: str,
    expected_revision: int,
    action: str,
    dimension: str,
    delta: float | None,
    value: float | None,
    reason: str,
    evidence: Sequence[Mapping[str, Any]],
) -> MutationOutcome:
    """Apply one manual calibration inside the coordinator-owned transaction.

    Does not write write_operations / domain_outbox; the coordinator owns the ledger.
    """
    _scope_params(scope)
    calibration_id = f"relationship-calibration:{operation_id}"
    try:
        result = repository.calibrate_relationship(
            scope,
            subject_principal_id=subject,
            expected_revision=int(expected_revision),
            action=action,
            dimension=dimension,
            delta=delta,
            value=value,
            reason=reason,
            evidence=evidence,
            operation_id=operation_id,
            created_at=now,
            connection=connection,
        )
    except ScopedSoulScopeError as exc:
        raise RelationshipCalibrationError(exc.code) from exc
    connection.execute(
        """INSERT INTO scoped_soul_relationship_calibration_events(
               calibration_id, operation_id, bot_id, session_id, visibility,
               subject_principal_id, dimension, action, before_json, after_json,
               reason, evidence, actor, relationship_revision, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            calibration_id,
            operation_id,
            *_scope_params(scope),
            subject,
            dimension,
            action,
            _json(result["before"]),
            _json(result["after"]),
            reason,
            _json(list(evidence)),
            "webui.relationship.calibration",
            int(result["revision"]),
            now,
        ),
    )
    timeline_scope = scope
    if timeline_scope.subject_principal_id != subject:
        try:
            from ..domain.scope import RuntimeScope as ScopeType
        except ImportError:  # pragma: no cover
            from domain.scope import RuntimeScope as ScopeType
        timeline_scope = ScopeType(scope.bot_id, scope.visibility, scope.session, subject)
    repository.add_timeline_event(
        timeline_scope,
        event_summary=f"人工校准关系：{dimension} / {action}；理由：{reason}",
        event_type="relationship.manual_calibration",
        emotional_weight=0.5,
        timestamp=now,
        evidence=[{"calibration_id": calibration_id}, *list(evidence)],
        connection=connection,
    )
    aggregate_id = f"{scope.bot_id}:{scope.session.id}:{scope.visibility}:{subject}"
    revision = int(result["revision"])
    details = {
        "operation_id": operation_id,
        "calibration_id": calibration_id,
        "revision": revision,
        "status": "succeeded",
        "subject_principal_id": subject,
        "dimension": dimension,
        "action": action,
        "before": result["before"],
        "after": result["after"],
        "affinity": int(result["affinity"]),
        "state": str(result["state"]),
        "evidence": list(evidence),
    }
    event_payload = {
        "calibration_id": calibration_id,
        "operation_id": operation_id,
        "scope": scope_to_dict(scope),
        "subject_principal_id": subject,
        "dimension": dimension,
        "action": action,
        "before": result["before"],
        "after": result["after"],
        "reason": reason,
        "evidence": list(evidence),
        "actor": "webui.relationship.calibration",
        "relationship_revision": revision,
        "created_at": now,
        "affinity": int(result["affinity"]),
        "state": str(result["state"]),
    }
    return MutationOutcome(
        entities=(EntityChange("relationship", aggregate_id, revision, "calibrated"),),
        events=(
            OutboxEventDraft(
                "relationship",
                aggregate_id,
                revision,
                "relationship.calibrated",
                event_payload,
            ),
        ),
        details=details,
    )


def _relationship_calibration_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    payload = dict(command.payload)
    repository = payload.get("repository")
    if repository is None:
        raise CommandRejectedError("relationship_repository_unavailable")
    return apply_relationship_calibration(
        connection,
        repository=repository,
        scope=command.scope,  # type: ignore[arg-type]
        operation_id=command.operation_id,
        now=now,
        subject=str(payload["subject_principal_id"]),
        expected_revision=int(payload["expected_revision"]),
        action=str(payload["action"]),
        dimension=str(payload["dimension"]),
        delta=payload.get("delta"),
        value=payload.get("value"),
        reason=str(payload["reason"]),
        evidence=list(payload.get("evidence") or ()),
    )


class RelationshipCalibrationGateway:
    """人工关系校准入口：业务校验留在这里，账本交给 WriteCoordinator.submit。"""

    def __init__(self, write_gateway: Any, repository: Any, *, clock: Any | None = None) -> None:
        coordinator = getattr(write_gateway, "coordinator", None)
        if coordinator is None:
            raise ValueError("write gateway coordinator is required")
        self._write_gateway = write_gateway
        self._coordinator = coordinator
        self._repository = repository
        self._clock = clock

    def _now(self) -> float:
        if self._clock is not None and callable(getattr(self._clock, "now", None)):
            return float(self._clock.now())
        return time.time()

    @staticmethod
    def _result_from_details(details: Mapping[str, Any]) -> RelationshipCalibrationResult:
        return RelationshipCalibrationResult(
            operation_id=str(details["operation_id"]),
            calibration_id=str(details["calibration_id"]),
            revision=int(details["revision"]),
            status=str(details.get("status") or "succeeded"),
            subject_principal_id=str(details["subject_principal_id"]),
            dimension=str(details["dimension"]),
            action=str(details["action"]),
            before=dict(details.get("before") or {}),
            after=dict(details.get("after") or {}),
            affinity=int(details["affinity"]),
            state=str(details["state"]),
            evidence=list(details.get("evidence") or ()),
        )

    async def _submit(self, command: DomainCommand) -> RelationshipCalibrationResult:
        submit = getattr(self._coordinator, "submit", None)
        if callable(submit):
            try:
                result = await submit(command)
            except CommandRejectedError as exc:
                raise RelationshipCalibrationError(exc.code, str(exc)) from exc
            except IdempotencyConflictError as exc:
                raise RelationshipCalibrationError("relationship_idempotency_conflict", str(exc)) from exc
            details = dict(getattr(result, "details", None) or {})
            if details:
                details.setdefault("operation_id", result.operation_id)
                return self._result_from_details(details)
        transaction = getattr(self._coordinator, "transaction", None)
        if not callable(transaction):
            raise RelationshipCalibrationError("relationship_writer_unavailable")

        def persist(connection):
            outcome = apply_relationship_calibration(
                connection,
                repository=self._repository,
                scope=command.scope,  # type: ignore[arg-type]
                operation_id=command.operation_id,
                now=self._now(),
                subject=str(command.payload["subject_principal_id"]),
                expected_revision=int(command.payload["expected_revision"]),
                action=str(command.payload["action"]),
                dimension=str(command.payload["dimension"]),
                delta=command.payload.get("delta"),
                value=command.payload.get("value"),
                reason=str(command.payload["reason"]),
                evidence=list(command.payload.get("evidence") or ()),
            )
            return self._result_from_details(outcome.details)

        return await transaction(persist, actor=command.command_type)

    async def calibrate(
        self,
        *,
        scope: RuntimeScope,
        subject_principal_id: str,
        expected_revision: int,
        action: str,
        dimension: str,
        delta: float | None = None,
        value: float | None = None,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        object_ref: str | None = None,
    ) -> RelationshipCalibrationResult:
        _scope_params(scope)
        subject = str(subject_principal_id or "").strip()
        if not subject:
            raise RelationshipCalibrationError("relationship_subject_required")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in _ACTIONS:
            raise RelationshipCalibrationError("relationship_action_invalid")
        normalized_dimension = str(dimension or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise RelationshipCalibrationError("relationship_reason_required")
        if len(normalized_reason) > 1000:
            raise RelationshipCalibrationError("relationship_reason_too_long")
        normalized_evidence = _normalize_evidence(evidence, scope)
        expected_revision = int(expected_revision)
        request_shape = {
            "scope": scope_to_dict(scope),
            "subject_principal_id": subject,
            "expected_revision": expected_revision,
            "action": normalized_action,
            "dimension": normalized_dimension,
            "delta": delta,
            "value": value,
            "reason": normalized_reason,
            "evidence": normalized_evidence,
            "object_ref": object_ref,
        }
        request_hash = _digest(request_shape)
        command_type = RELATIONSHIP_CALIBRATE_COMMAND
        idempotency_key = f"{command_type}:{request_hash}"
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"wave-memory:{command_type}:{request_hash}").hex
        typed = getattr(self._write_gateway, "calibrate_relationship", None)
        if callable(typed):
            result = await typed(
                scope=scope,
                subject_principal_id=subject,
                expected_revision=expected_revision,
                action=normalized_action,
                dimension=normalized_dimension,
                delta=delta,
                value=value,
                reason=normalized_reason,
                evidence=normalized_evidence,
                object_ref=object_ref,
                repository=self._repository,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
                request_hash=request_hash,
            )
            if isinstance(result, RelationshipCalibrationResult):
                return result
            if hasattr(result, "details"):
                details = dict(result.details or {})
                details.setdefault("operation_id", result.operation_id)
                return self._result_from_details(details)
        command = DomainCommand(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            actor="webui.relationship.calibration",
            scope=scope,
            command_type=command_type,
            payload={
                **request_shape,
                "repository": self._repository,
            },
            request_hash=request_hash,
        )
        return await self._submit(command)


__all__ = [
    "RELATIONSHIP_CALIBRATE_COMMAND",
    "RelationshipCalibrationError",
    "RelationshipCalibrationGateway",
    "RelationshipCalibrationResult",
    "apply_relationship_calibration",
]
