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
    from ..domain.scope import RuntimeScope, scope_to_dict
    from ..engine.db.outbox_repo import OutboxRepository
    from ..engine.db.scoped_soul_repo import ScopedSoulScopeError
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope, scope_to_dict
    from engine.db.outbox_repo import OutboxRepository
    from engine.db.scoped_soul_repo import ScopedSoulScopeError


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


class RelationshipCalibrationGateway:
    """All manual relationship writes share one coordinator-owned SQLite transaction."""

    def __init__(self, write_gateway: Any, repository: Any, *, clock: Any | None = None) -> None:
        coordinator = getattr(write_gateway, "coordinator", None)
        if coordinator is None:
            raise ValueError("write gateway coordinator is required")
        self._coordinator = coordinator
        self._repository = repository
        self._clock = clock
        consumers = getattr(write_gateway, "_consumers", None)
        if isinstance(consumers, Mapping):
            self._consumer_names = tuple(sorted(str(name) for name in consumers))
        else:
            self._consumer_names = tuple(sorted(str(name) for name in getattr(coordinator, "_consumer_names", ())))

    def _now(self) -> float:
        if self._clock is not None and callable(getattr(self._clock, "now", None)):
            return float(self._clock.now())
        return time.time()

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
        command_type = "relationship.webui.calibrate.v1"
        idempotency_key = f"{command_type}:{request_hash}"
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"wave-memory:{command_type}:{request_hash}").hex
        calibration_id = f"relationship-calibration:{operation_id}"
        now = self._now()

        def transaction(connection):
            existing = connection.execute(
                "SELECT request_hash, status, result_json FROM write_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_hash:
                    raise RelationshipCalibrationError("relationship_idempotency_conflict")
                if str(existing[1]) != "committed" or not existing[2]:
                    raise RelationshipCalibrationError("relationship_operation_incomplete")
                payload = json.loads(str(existing[2]))
                return RelationshipCalibrationResult(**payload)
            sequence = OutboxRepository.next_write_sequence(connection)
            connection.execute(
                """INSERT INTO write_operations(
                       operation_id, idempotency_key, request_hash, command_type, scope_json,
                       status, write_sequence, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (operation_id, idempotency_key, request_hash, command_type, _json(scope_to_dict(scope)), sequence, now),
            )
            try:
                result = self._repository.calibrate_relationship(
                    scope,
                    subject_principal_id=subject,
                    expected_revision=expected_revision,
                    action=normalized_action,
                    dimension=normalized_dimension,
                    delta=delta,
                    value=value,
                    reason=normalized_reason,
                    evidence=normalized_evidence,
                    operation_id=operation_id,
                    created_at=now,
                    connection=connection,
                )
            except ScopedSoulScopeError as exc:
                raise RelationshipCalibrationError(exc.code) from exc
            calibration = {
                "calibration_id": calibration_id,
                "operation_id": operation_id,
                "scope": scope_to_dict(scope),
                "subject_principal_id": subject,
                "dimension": normalized_dimension,
                "action": normalized_action,
                "before": result["before"],
                "after": result["after"],
                "reason": normalized_reason,
                "evidence": normalized_evidence,
                "actor": "webui.relationship.calibration",
                "relationship_revision": int(result["revision"]),
                "created_at": now,
            }
            connection.execute(
                """INSERT INTO scoped_soul_relationship_calibration_events(
                       calibration_id, operation_id, bot_id, session_id, visibility,
                       subject_principal_id, dimension, action, before_json, after_json,
                       reason, evidence, actor, relationship_revision, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (calibration_id, operation_id, *_scope_params(scope), subject, normalized_dimension,
                 normalized_action, _json(result["before"]), _json(result["after"]), normalized_reason,
                 _json(normalized_evidence), "webui.relationship.calibration", int(result["revision"]), now),
            )
            timeline_scope = scope
            if timeline_scope.subject_principal_id != subject:
                try:
                    from ..domain.scope import RuntimeScope as ScopeType
                except ImportError:  # pragma: no cover - direct repository imports
                    from domain.scope import RuntimeScope as ScopeType
                timeline_scope = ScopeType(scope.bot_id, scope.visibility, scope.session, subject)
            self._repository.add_timeline_event(
                timeline_scope,
                event_summary=f"人工校准关系：{normalized_dimension} / {normalized_action}；理由：{normalized_reason}",
                event_type="relationship.manual_calibration",
                emotional_weight=0.5,
                timestamp=now,
                evidence=[{"calibration_id": calibration_id}, *normalized_evidence],
                connection=connection,
            )
            event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"wave-memory:{operation_id}:relationship").hex
            aggregate_id = f"{scope.bot_id}:{scope.session.id}:{scope.visibility}:{subject}"
            payload = {**calibration, "affinity": result["affinity"], "state": result["state"]}
            connection.execute(
                """INSERT INTO domain_outbox(
                       event_id, operation_id, aggregate_kind, aggregate_id, aggregate_version,
                       event_type, payload_version, payload_json, created_at)
                   VALUES (?, ?, 'relationship', ?, ?, 'relationship.calibrated', 1, ?, ?)""",
                (event_id, operation_id, aggregate_id, int(result["revision"]), _json(payload), now),
            )
            OutboxRepository.add_deliveries(connection, event_id, self._consumer_names, now)
            result_payload = {
                "operation_id": operation_id,
                "calibration_id": calibration_id,
                "revision": int(result["revision"]),
                "status": "succeeded",
                "subject_principal_id": subject,
                "dimension": normalized_dimension,
                "action": normalized_action,
                "before": result["before"],
                "after": result["after"],
                "affinity": int(result["affinity"]),
                "state": str(result["state"]),
                "evidence": normalized_evidence,
            }
            connection.execute(
                "UPDATE write_operations SET status='committed', result_json=?, committed_at=? WHERE operation_id=?",
                (_json(result_payload), now, operation_id),
            )
            return RelationshipCalibrationResult(**result_payload)

        return await self._coordinator.transaction(transaction, actor=command_type)


__all__ = ["RelationshipCalibrationError", "RelationshipCalibrationGateway", "RelationshipCalibrationResult"]
