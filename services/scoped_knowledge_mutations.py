"""Scoped fact/tag-relation mutations through the production writer transaction."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    from ..domain.scope import RuntimeScope, scope_to_dict
    from ..engine.db.outbox_repo import OutboxRepository
    from .facts_conflict import FactConflictClassifier
except ImportError:  # pragma: no cover - focused tests import top-level packages
    from domain.scope import RuntimeScope, scope_to_dict
    from engine.db.outbox_repo import OutboxRepository
    from services.facts_conflict import FactConflictClassifier


_TERMINAL_STATUSES = frozenset({"deleted", "superseded"})

# 人工审核允许的来源状态与目标状态。与 scoped_facts.status 既有词表保持一致：
# 批准落 active（冲突时落 conflict，与 review_scoped_fact_history 同口径），拒绝落 rejected。
_FACT_REVIEW_SOURCE_STATUSES = frozenset({"pending", "quarantined", "conflict"})
_FACT_REVIEW_APPROVE_TARGET = "active"
_FACT_REVIEW_REJECT_TARGET = "rejected"


class ScopedKnowledgeMutationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        self.reason_code = code
        super().__init__(code)


class ScopedKnowledgeNotFound(ScopedKnowledgeMutationError):
    def __init__(self) -> None:
        super().__init__("scoped_knowledge_not_found")


class ScopedKnowledgeRevisionConflict(ScopedKnowledgeMutationError):
    def __init__(self) -> None:
        super().__init__("scoped_knowledge_revision_conflict")


class ScopedKnowledgeIdentityConflict(ScopedKnowledgeMutationError):
    def __init__(self) -> None:
        super().__init__("scoped_knowledge_identity_conflict")


class ScopedKnowledgeIdempotencyConflict(ScopedKnowledgeMutationError):
    def __init__(self) -> None:
        super().__init__("scoped_knowledge_idempotency_conflict")


@dataclass(frozen=True)
class ScopedKnowledgeMutationTarget:
    kind: str
    locator: int
    revision: int


@dataclass(frozen=True)
class ScopedKnowledgeMutationResult:
    operation_id: str
    kind: str
    locator: int
    revision: int
    status: str
    previous_locator: int | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_or_empty(value: Any) -> Mapping[str, Any]:
    """scoped_facts.provenance 是 TEXT；解析失败时返回空映射而不是抛错。"""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    if not isinstance(scope, RuntimeScope) or scope.session is None or scope.visibility != "group":
        raise ValueError("canonical group RuntimeScope is required")
    return scope.bot_id, scope.session.id, scope.visibility


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(value)


def _exact_string(value: Any, field: str, *, maximum_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty exact string")
    if len(value) > maximum_length:
        raise ValueError(f"{field} is too long")
    return value


def _number(value: Any, field: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} is below minimum")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field} is above maximum")
    return parsed


class ScopedKnowledgeMutationGateway:
    """Mutation facade that only enters SQLite through ProductionWriteGateway.coordinator."""

    def __init__(self, write_gateway: Any, *, clock: Any | None = None) -> None:
        coordinator = getattr(write_gateway, "coordinator", None)
        if coordinator is None or not callable(getattr(coordinator, "transaction", None)):
            raise ValueError("write gateway coordinator is required")
        self._coordinator = coordinator
        self._clock = clock
        consumers = getattr(write_gateway, "_consumers", None)
        if isinstance(consumers, Mapping):
            self._consumer_names = tuple(sorted(str(name) for name in consumers))
        else:
            self._consumer_names = tuple(
                sorted(str(name) for name in getattr(coordinator, "_consumer_names", ()))
            )

    def _now(self) -> float:
        if self._clock is not None and callable(getattr(self._clock, "now", None)):
            return float(self._clock.now())
        return time.time()

    async def _commit(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        command_type: str,
        actor: str,
        event_type: str,
        request_shape: Mapping[str, Any],
        mutate,
        idempotency_key: str | None,
    ) -> ScopedKnowledgeMutationResult:
        _scope_params(scope)
        request_hash = _digest(request_shape)
        supplied_key = str(idempotency_key or "").strip()
        stable_key = (
            f"{command_type}:{supplied_key}"
            if supplied_key
            else f"{command_type}:{request_hash}"
        )
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"wave-memory:{command_type}:{stable_key}:{request_hash}",
        ).hex
        now = self._now()

        def transaction(connection):
            existing = connection.execute(
                "SELECT request_hash, status, result_json FROM write_operations WHERE idempotency_key=?",
                (stable_key,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_hash:
                    raise ScopedKnowledgeIdempotencyConflict()
                if existing[1] != "committed" or not existing[2]:
                    raise ScopedKnowledgeIdempotencyConflict()
                payload = json.loads(str(existing[2]))
                entity = payload["entities"][0]
                return ScopedKnowledgeMutationResult(
                    operation_id=str(payload["operation_id"]),
                    kind=str(entity["aggregate_kind"]),
                    locator=int(entity["aggregate_id"]),
                    revision=int(entity["aggregate_version"]),
                    status=str(entity["status"]),
                    previous_locator=(
                        None if entity.get("previous_locator") is None
                        else int(entity["previous_locator"])
                    ),
                )

            write_sequence = OutboxRepository.next_write_sequence(connection)
            connection.execute(
                """INSERT INTO write_operations(
                       operation_id, idempotency_key, request_hash, command_type, scope_json,
                       status, write_sequence, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    operation_id,
                    stable_key,
                    request_hash,
                    command_type,
                    _canonical_json(scope_to_dict(scope)),
                    write_sequence,
                    now,
                ),
            )
            record = dict(mutate(connection, now))
            aggregate_kind = str(record["kind"])
            locator = int(record["locator"])
            revision = int(record["revision"])
            status = str(record["status"])
            event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"wave-memory:{operation_id}:0").hex
            connection.execute(
                """INSERT INTO domain_outbox(
                       event_id, operation_id, aggregate_kind, aggregate_id,
                       aggregate_version, event_type, payload_version, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    event_id,
                    operation_id,
                    aggregate_kind,
                    str(locator),
                    revision,
                    event_type,
                    _canonical_json({
                        "scope": scope_to_dict(scope),
                        "locator": locator,
                        "revision": revision,
                        "status": status,
                        "previous_locator": record.get("previous_locator"),
                        "previous_revision": target.revision,
                    }),
                    now,
                ),
            )
            OutboxRepository.add_deliveries(
                connection, event_id, self._consumer_names, now
            )
            entity = {
                "aggregate_kind": aggregate_kind,
                "aggregate_id": str(locator),
                "aggregate_version": revision,
                "change_type": event_type.rsplit(".", 1)[-1],
                "status": status,
                "previous_locator": record.get("previous_locator"),
            }
            result_payload = {
                "operation_id": operation_id,
                "committed_at": now,
                "write_sequence": write_sequence,
                "entities": [entity],
                "effects": [{
                    "event_id": event_id,
                    "event_type": event_type,
                    "aggregate_kind": aggregate_kind,
                    "aggregate_id": str(locator),
                    "aggregate_version": revision,
                }],
                "warnings": [],
            }
            connection.execute(
                "UPDATE write_operations SET status='committed', result_json=?, committed_at=? WHERE operation_id=?",
                (_canonical_json(result_payload), now, operation_id),
            )
            return ScopedKnowledgeMutationResult(
                operation_id=operation_id,
                kind=aggregate_kind,
                locator=locator,
                revision=revision,
                status=status,
                previous_locator=record.get("previous_locator"),
            )

        return await self._coordinator.transaction(transaction, actor=actor)

    @staticmethod
    def _fact_row(connection, scope: RuntimeScope, locator: int):
        return connection.execute(
            """SELECT id, subject, predicate, object, confidence, status, source_memory_id,
                      provenance, valid_from, valid_until, revision
                 FROM scoped_facts
                WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (locator, *_scope_params(scope)),
        ).fetchone()

    @staticmethod
    def _relation_row(connection, scope: RuntimeScope, locator: int):
        return connection.execute(
            """SELECT id, source_tag_id, target_tag_id, relation_type, weight, confidence,
                      metadata, status, valid_until, revision
                 FROM scoped_tag_relations
                WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (locator, *_scope_params(scope)),
        ).fetchone()

    @staticmethod
    def _tag_row(connection, scope: RuntimeScope, locator: int):
        return connection.execute(
            """SELECT id, name, tag_type, description, confidence, metadata, status, revision
                 FROM scoped_tags
                WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (locator, *_scope_params(scope)),
        ).fetchone()

    @staticmethod
    def _require_current(row, expected_revision: int) -> None:
        if row is None:
            raise ScopedKnowledgeNotFound()
        status = row[5] if len(row) == 11 else row[7]
        if str(status) in _TERMINAL_STATUSES:
            raise ScopedKnowledgeNotFound()
        if int(row[-1]) != expected_revision:
            raise ScopedKnowledgeRevisionConflict()

    async def update_fact(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        fields: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> ScopedKnowledgeMutationResult:
        if target.kind != "fact":
            raise ValueError("fact target is required")
        locator = _positive_int(target.locator, "locator")
        revision = _positive_int(target.revision, "revision")
        if not isinstance(fields, Mapping):
            raise ValueError("fields must be an object")
        allowed = {"subject", "predicate", "object", "confidence"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("unsupported fact fields")
        normalized: dict[str, Any] = {}
        text_limits = {"subject": 500, "predicate": 200, "object": 4000}
        for name, maximum_length in text_limits.items():
            if name in fields:
                normalized[name] = _exact_string(
                    fields[name], name, maximum_length=maximum_length
                )
        if "confidence" in fields:
            normalized["confidence"] = _number(
                fields["confidence"], "confidence", minimum=0.0, maximum=1.0
            )
        if not normalized:
            raise ValueError("at least one mutable fact field is required")
        request_shape = {
            "scope": scope_to_dict(scope),
            "target": {"kind": "fact", "locator": locator, "revision": revision},
            "fields": normalized,
        }

        def mutate(connection, now):
            row = self._fact_row(connection, scope, locator)
            self._require_current(row, revision)
            current = {
                "subject": row[1], "predicate": row[2], "object": row[3],
                "confidence": row[4], "status": row[5], "source_memory_id": row[6],
                "provenance": row[7], "valid_from": row[8], "valid_until": row[9],
            }
            updated = {**current, **normalized}
            identity_changed = any(updated[name] != current[name] for name in ("subject", "predicate", "object"))
            if identity_changed:
                cursor = connection.execute(
                    """UPDATE scoped_facts SET status='superseded', valid_until=?, revision=revision+1,
                              updated_at=?
                         WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                           AND status NOT IN ('deleted','superseded')""",
                    (now, now, locator, *_scope_params(scope), revision),
                )
                if cursor.rowcount != 1:
                    raise ScopedKnowledgeRevisionConflict()
                try:
                    inserted = connection.execute(
                        """INSERT INTO scoped_facts(
                               bot_id, session_id, visibility, subject, predicate, object,
                               confidence, status, source_memory_id, provenance, valid_from,
                               valid_until, created_at, updated_at, revision)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                        (
                            *_scope_params(scope), updated["subject"], updated["predicate"],
                            updated["object"], updated["confidence"], updated["status"],
                            updated["source_memory_id"], updated["provenance"],
                            updated["valid_from"], updated["valid_until"], now, now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ScopedKnowledgeIdentityConflict() from exc
                return {
                    "kind": "fact", "locator": int(inserted.lastrowid), "revision": 1,
                    "status": updated["status"], "previous_locator": locator,
                }
            assignments = [f"{name}=?" for name in normalized]
            values = [normalized[name] for name in normalized]
            cursor = connection.execute(
                f"""UPDATE scoped_facts SET {', '.join(assignments)}, revision=revision+1, updated_at=?
                       WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                         AND status NOT IN ('deleted','superseded')""",
                (*values, now, locator, *_scope_params(scope), revision),
            )
            if cursor.rowcount != 1:
                raise ScopedKnowledgeRevisionConflict()
            return {
                "kind": "fact", "locator": locator, "revision": revision + 1,
                "status": str(updated["status"]),
            }

        return await self._commit(
            scope=scope,
            target=target,
            command_type="scoped.fact.update.v1",
            actor="webui.kg.fact.update",
            event_type="scoped_fact.updated",
            request_shape=request_shape,
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def delete_fact(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        idempotency_key: str | None = None,
    ) -> ScopedKnowledgeMutationResult:
        if target.kind != "fact":
            raise ValueError("fact target is required")
        locator = _positive_int(target.locator, "locator")
        revision = _positive_int(target.revision, "revision")
        request_shape = {
            "scope": scope_to_dict(scope),
            "target": {"kind": "fact", "locator": locator, "revision": revision},
        }

        def mutate(connection, now):
            row = self._fact_row(connection, scope, locator)
            self._require_current(row, revision)
            cursor = connection.execute(
                """UPDATE scoped_facts SET status='deleted', valid_until=?, revision=revision+1,
                          updated_at=?
                     WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                       AND status NOT IN ('deleted','superseded')""",
                (now, now, locator, *_scope_params(scope), revision),
            )
            if cursor.rowcount != 1:
                raise ScopedKnowledgeRevisionConflict()
            return {"kind": "fact", "locator": locator, "revision": revision + 1, "status": "deleted"}

        return await self._commit(
            scope=scope,
            target=target,
            command_type="scoped.fact.delete.v1",
            actor="webui.kg.fact.delete",
            event_type="scoped_fact.deleted",
            request_shape=request_shape,
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def review_fact(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        action: str,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> ScopedKnowledgeMutationResult:
        """人工审核事实：pending/quarantined/conflict → active / conflict / rejected。

        状态变更与审计行在同一事务内完成；批准前先用 FactConflictClassifier 判定与既有
        事实的关系，判定为 conflicts 时落 conflict 而不是 active（与历史的
        review_scoped_fact_history 同口径，不弱化冲突语义）。
        """
        if target.kind != "fact":
            raise ValueError("fact target is required")
        if action not in {"approve", "reject"}:
            raise ValueError("unsupported fact review action")
        locator = _positive_int(target.locator, "locator")
        revision = _positive_int(target.revision, "revision")
        review_reason = str(reason or "").strip()[:500]
        request_shape = {
            "scope": scope_to_dict(scope),
            "target": {"kind": "fact", "locator": locator, "revision": revision},
            "action": action,
            "reason": review_reason,
        }

        resolution: dict[str, str] = {"event_type": "scoped_fact.rejected"}

        def mutate(connection, now):
            row = self._fact_row(connection, scope, locator)
            self._require_current(row, revision)
            from_status = str(row[5])
            if from_status not in _FACT_REVIEW_SOURCE_STATUSES:
                raise ValueError("invalid_fact_review_transition")
            if action == "approve":
                relation, conflict_with = self._classify_fact_conflict(connection, scope, locator)
                to_status = "conflict" if relation == "conflicts" else _FACT_REVIEW_APPROVE_TARGET
                resolution["event_type"] = (
                    "scoped_fact.conflict" if to_status == "conflict" else "scoped_fact.approved"
                )
                if relation == "supersedes" and conflict_with is not None:
                    connection.execute(
                        """UPDATE scoped_facts SET status='superseded', revision=revision+1, updated_at=?
                             WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
                               AND status NOT IN ('deleted','superseded')""",
                        (now, int(conflict_with), *_scope_params(scope)),
                    )
            else:
                relation, conflict_with = "", None
                to_status = _FACT_REVIEW_REJECT_TARGET
                resolution["event_type"] = "scoped_fact.rejected"
            cursor = connection.execute(
                """UPDATE scoped_facts SET status=?, revision=revision+1, updated_at=?
                     WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                       AND status NOT IN ('deleted','superseded')""",
                (to_status, now, locator, *_scope_params(scope), revision),
            )
            if cursor.rowcount != 1:
                raise ScopedKnowledgeRevisionConflict()
            connection.execute(
                """INSERT INTO scoped_fact_reviews(
                       bot_id, session_id, visibility, fact_id, action, actor, reason,
                       from_status, to_status, relation, conflict_with_fact_id,
                       idempotency_key, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility, idempotency_key) DO NOTHING""",
                (
                    *_scope_params(scope), locator, action, "webui.facts.review",
                    review_reason, from_status, to_status, relation, conflict_with,
                    str(idempotency_key or f"fact.review:{locator}:{revision}:{action}"),
                    now,
                ),
            )
            return {
                "kind": "fact",
                "locator": locator,
                "revision": revision + 1,
                "status": to_status,
            }

        event_type = (
            "scoped_fact.rejected" if action == "reject"
            else ("scoped_fact.conflict" if action == "approve" else "scoped_fact.approved")
        )
        return await self._commit(            scope=scope,
            target=target,
            command_type="scoped.fact.review.v1",
            actor="webui.facts.review",
            event_type=event_type,
            request_shape=request_shape,
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _classify_fact_conflict(
        connection: Any,
        scope: RuntimeScope,
        locator: int,
    ) -> tuple[str, int | None]:
        """用与历史 scoped_fact_history 相同的分类器判定同主体+谓词内的关系。"""
        row = connection.execute(
            "SELECT subject, predicate, object, valid_from, valid_until, provenance FROM scoped_facts WHERE id=? AND bot_id=? AND session_id=? AND visibility=?",
            (locator, *_scope_params(scope)),
        ).fetchone()
        if row is None:
            raise ScopedKnowledgeNotFound()
        provenance = _json_or_empty(row[5])
        candidate = {
            "subject": row[0], "predicate": row[1], "object": row[2],
            "valid_from": row[3], "valid_until": row[4], "provenance": provenance,
        }
        others = [
            {
                "id": other[0], "subject": other[1], "predicate": other[2], "object": other[3],
                "valid_from": other[4], "valid_until": other[5], "provenance": {},
            }
            for other in connection.execute(
                """SELECT id, subject, predicate, object, valid_from, valid_until
                     FROM scoped_facts
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject=? AND predicate=?
                      AND id!=? AND status NOT IN ('deleted','superseded','rejected')""",
                (*_scope_params(scope), row[0], row[1], locator),
            ).fetchall()
        ]
        decision = FactConflictClassifier().classify(candidate, others)
        return str(decision.relation), decision.existing_id

    async def update_tag_relation(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        fields: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> ScopedKnowledgeMutationResult:
        if target.kind != "tag_relation":
            raise ValueError("tag_relation target is required")
        locator = _positive_int(target.locator, "locator")
        revision = _positive_int(target.revision, "revision")
        if not isinstance(fields, Mapping):
            raise ValueError("fields must be an object")
        allowed = {"relation_type", "weight", "confidence"}
        if set(fields) - allowed:
            raise ValueError("unsupported tag relation fields")
        normalized: dict[str, Any] = {}
        if "relation_type" in fields:
            normalized["relation_type"] = _exact_string(
                fields["relation_type"], "relation_type", maximum_length=200
            )
        if "weight" in fields:
            normalized["weight"] = _number(
                fields["weight"], "weight", minimum=0.0
            )
        if "confidence" in fields:
            normalized["confidence"] = _number(
                fields["confidence"], "confidence", minimum=0.0, maximum=1.0
            )
        if not normalized:
            raise ValueError("at least one mutable tag relation field is required")
        request_shape = {
            "scope": scope_to_dict(scope),
            "target": {"kind": "tag_relation", "locator": locator, "revision": revision},
            "fields": normalized,
        }

        def mutate(connection, now):
            row = self._relation_row(connection, scope, locator)
            self._require_current(row, revision)
            current = {
                "source_tag_id": row[1], "target_tag_id": row[2], "relation_type": row[3],
                "weight": row[4], "confidence": row[5], "metadata": row[6],
                "status": row[7], "valid_until": row[8],
            }
            updated = {**current, **normalized}
            if updated["relation_type"] != current["relation_type"]:
                cursor = connection.execute(
                    """UPDATE scoped_tag_relations SET status='superseded', valid_until=?,
                              revision=revision+1, updated_at=?
                         WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                           AND status NOT IN ('deleted','superseded')""",
                    (now, now, locator, *_scope_params(scope), revision),
                )
                if cursor.rowcount != 1:
                    raise ScopedKnowledgeRevisionConflict()
                try:
                    inserted = connection.execute(
                        """INSERT INTO scoped_tag_relations(
                               bot_id, session_id, visibility, source_tag_id, target_tag_id,
                               relation_type, weight, confidence, metadata, status, valid_until,
                               revision, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            *_scope_params(scope), updated["source_tag_id"], updated["target_tag_id"],
                            updated["relation_type"], updated["weight"], updated["confidence"],
                            updated["metadata"], updated["status"], updated["valid_until"], now, now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ScopedKnowledgeIdentityConflict() from exc
                return {
                    "kind": "tag_relation", "locator": int(inserted.lastrowid), "revision": 1,
                    "status": updated["status"], "previous_locator": locator,
                }
            assignments = [f"{name}=?" for name in normalized]
            values = [normalized[name] for name in normalized]
            cursor = connection.execute(
                f"""UPDATE scoped_tag_relations SET {', '.join(assignments)}, revision=revision+1,
                           updated_at=?
                       WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                         AND status NOT IN ('deleted','superseded')""",
                (*values, now, locator, *_scope_params(scope), revision),
            )
            if cursor.rowcount != 1:
                raise ScopedKnowledgeRevisionConflict()
            return {
                "kind": "tag_relation", "locator": locator, "revision": revision + 1,
                "status": str(updated["status"]),
            }

        return await self._commit(
            scope=scope,
            target=target,
            command_type="scoped.tag_relation.update.v1",
            actor="webui.kg.tag_relation.update",
            event_type="scoped_tag_relation.updated",
            request_shape=request_shape,
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def delete_tag_relation(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        idempotency_key: str | None = None,
    ) -> ScopedKnowledgeMutationResult:
        if target.kind != "tag_relation":
            raise ValueError("tag_relation target is required")
        locator = _positive_int(target.locator, "locator")
        revision = _positive_int(target.revision, "revision")
        request_shape = {
            "scope": scope_to_dict(scope),
            "target": {"kind": "tag_relation", "locator": locator, "revision": revision},
        }

        def mutate(connection, now):
            row = self._relation_row(connection, scope, locator)
            self._require_current(row, revision)
            cursor = connection.execute(
                """UPDATE scoped_tag_relations SET status='deleted', valid_until=?,
                          revision=revision+1, updated_at=?
                     WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?
                       AND status NOT IN ('deleted','superseded')""",
                (now, now, locator, *_scope_params(scope), revision),
            )
            if cursor.rowcount != 1:
                raise ScopedKnowledgeRevisionConflict()
            return {
                "kind": "tag_relation", "locator": locator,
                "revision": revision + 1, "status": "deleted",
            }

        return await self._commit(
            scope=scope,
            target=target,
            command_type="scoped.tag_relation.delete.v1",
            actor="webui.kg.tag_relation.delete",
            event_type="scoped_tag_relation.deleted",
            request_shape=request_shape,
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def update_tag(
        self,
        *,
        scope: RuntimeScope,
        target: ScopedKnowledgeMutationTarget,
        fields: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> ScopedKnowledgeMutationResult:
        if target.kind != "tag":
            raise ValueError("tag target is required")
        locator = _positive_int(target.locator, "locator")
        revision = _positive_int(target.revision, "revision")
        if not isinstance(fields, Mapping):
            raise ValueError("fields must be an object")
        allowed = {"name", "tag_type", "description", "aliases"}
        if set(fields) - allowed:
            raise ValueError("unsupported tag mutation fields")
        normalized: dict[str, Any] = {}
        if "name" in fields:
            normalized["name"] = _exact_string(fields["name"], "name", maximum_length=120)
        if "tag_type" in fields:
            normalized["tag_type"] = _exact_string(fields["tag_type"], "tag_type", maximum_length=60)
        if "description" in fields:
            desc = fields["description"]
            normalized["description"] = "" if desc is None else str(desc).strip()
        if "aliases" in fields:
            aliases_raw = fields["aliases"]
            if not isinstance(aliases_raw, (list, tuple)):
                raise ValueError("aliases must be a list of strings")
            normalized["aliases"] = [str(item).strip() for item in aliases_raw if str(item).strip()]
        if not normalized:
            raise ValueError("at least one mutable tag field is required")
        request_shape = {
            "scope": scope_to_dict(scope),
            "target": {"kind": "tag", "locator": locator, "revision": revision},
            "fields": normalized,
        }

        def mutate(connection, now):
            row = self._tag_row(connection, scope, locator)
            if row is None:
                raise ScopedKnowledgeNotFound()
            if int(row[7] or 1) != revision:
                raise ScopedKnowledgeRevisionConflict()
            current_name = str(row[1])
            new_name = normalized.get("name", current_name)
            if new_name != current_name:
                conflict = connection.execute(
                    """SELECT 1 FROM scoped_tags
                        WHERE bot_id=? AND session_id=? AND visibility=? AND name=? AND id!=?""",
                    (*_scope_params(scope), new_name, locator),
                ).fetchone()
                if conflict is not None:
                    raise ScopedKnowledgeIdentityConflict()
            metadata_raw = row[5]
            metadata = json.loads(metadata_raw) if metadata_raw and isinstance(metadata_raw, str) else {}
            if "aliases" in normalized:
                metadata["aliases"] = normalized["aliases"]
            assignments = ["revision=revision+1", "updated_at=?"]
            params = [now]
            if "name" in normalized:
                assignments.append("name=?")
                params.append(normalized["name"])
            if "tag_type" in normalized:
                assignments.append("tag_type=?")
                params.append(normalized["tag_type"])
            if "description" in normalized:
                assignments.append("description=?")
                params.append(normalized["description"])
            assignments.append("metadata=?")
            params.append(_canonical_json(metadata, "metadata"))
            params.extend([locator, *_scope_params(scope), revision])
            cursor = connection.execute(
                f"""UPDATE scoped_tags SET {', '.join(assignments)}
                     WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=?""",
                params,
            )
            if cursor.rowcount != 1:
                raise ScopedKnowledgeRevisionConflict()
            return {
                "kind": "tag", "locator": locator, "revision": revision + 1, "status": "active",
            }

        return await self._commit(
            scope=scope,
            target=target,
            command_type="scoped.tag.update.v1",
            actor="webui.kg.tag.update",
            event_type="scoped_tag.updated",
            request_shape=request_shape,
            mutate=mutate,
            idempotency_key=idempotency_key,
        )


__all__ = [
    "ScopedKnowledgeIdentityConflict",
    "ScopedKnowledgeIdempotencyConflict",
    "ScopedKnowledgeMutationError",
    "ScopedKnowledgeMutationGateway",
    "ScopedKnowledgeMutationResult",
    "ScopedKnowledgeMutationTarget",
    "ScopedKnowledgeNotFound",
    "ScopedKnowledgeRevisionConflict",
]
