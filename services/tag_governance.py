"""Scoped Tag 治理、预检与审批写入网关。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:
    from ..domain.commands import CommandRejectedError, DomainCommand, EntityChange, IdempotencyConflictError
    from ..domain.scope import RuntimeScope, scope_to_dict
    from ..engine.db.scoped_tag_projection import rebuild_scope_effective_tags
    from ..engine.write_coordinator import MutationOutcome, OutboxEventDraft
except ImportError:  # pragma: no cover
    from domain.commands import CommandRejectedError, DomainCommand, EntityChange, IdempotencyConflictError
    from domain.scope import RuntimeScope, scope_to_dict
    from engine.db.scoped_tag_projection import rebuild_scope_effective_tags
    from engine.write_coordinator import MutationOutcome, OutboxEventDraft


_ACTIONS = frozenset({"merge", "retype", "alias", "deactivate"})
_STATUSES = frozenset({"pending", "approved", "rejected", "conflict", "expired"})
TAG_GOVERNANCE_COMMANDS = frozenset({
    "tags.governance.suggestion.create.v1",
    "tags.governance.approve.v1",
    "tags.governance.reject.v1",
    "tags.governance.compensate.v1",
    "tags.governance.batch.approve.v1",
    "tags.governance.batch.reject.v1",
})


class TagGovernanceError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.reason_code = self.code
        super().__init__(message or self.code)


@dataclass(frozen=True)
class TagGovernanceResult:
    operation_id: str
    suggestion_id: str | None
    revision: int | None
    status: str
    impact: dict[str, Any]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        raise TagGovernanceError("scope_required", "a canonical group RuntimeScope is required")
    return scope.bot_id, scope.session.id, scope.visibility


def _normalize_ids(values: Sequence[Any]) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise TagGovernanceError("invalid_tag_id", "tag ids must be positive integers")
        try:
            tag_id = int(value)
        except (TypeError, ValueError) as exc:
            raise TagGovernanceError("invalid_tag_id", "tag ids must be positive integers") from exc
        if tag_id <= 0:
            raise TagGovernanceError("invalid_tag_id", "tag ids must be positive integers")
        if tag_id not in seen:
            result.append(tag_id)
            seen.add(tag_id)
    if not result:
        raise TagGovernanceError("tags_required", "at least one scoped Tag is required")
    return tuple(result)


def _normalize_aliases(values: Sequence[Any]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = str(value or "").strip()
        if not alias or alias.casefold() in seen:
            continue
        if len(alias) > 200:
            raise TagGovernanceError("alias_too_long", "aliases must not exceed 200 characters")
        aliases.append(alias)
        seen.add(alias.casefold())
    return aliases


def _normalize_action(action: Any) -> str:
    value = str(action or "").strip().lower()
    if value not in _ACTIONS:
        raise TagGovernanceError("invalid_tag_governance_action", "unsupported scoped Tag governance action")
    return value


def _table_rows(connection, table: str, predicate: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    cursor = connection.execute(f"SELECT * FROM {table} WHERE {predicate}", tuple(params))
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _tag_governance_snapshot(connection, *, scope: RuntimeScope, tag_ids: Sequence[int]) -> dict[str, list[dict[str, Any]]]:
    bot_id, session_id, visibility = _scope_params(scope)
    ids = tuple(sorted({int(value) for value in tag_ids}))
    placeholders = ",".join("?" for _ in ids)
    if not ids:
        raise TagGovernanceError("tags_required")
    return {
        "scoped_tags": _table_rows(
            connection,
            "scoped_tags",
            f"bot_id=? AND session_id=? AND visibility=? AND id IN ({placeholders})",
            (bot_id, session_id, visibility, *ids),
        ),
        "scoped_memory_tags": _table_rows(
            connection,
            "scoped_memory_tags",
            f"bot_id=? AND session_id=? AND visibility=? AND tag_id IN ({placeholders})",
            (bot_id, session_id, visibility, *ids),
        ),
        "scoped_tag_relations": _table_rows(
            connection,
            "scoped_tag_relations",
            f"bot_id=? AND session_id=? AND visibility=? AND (source_tag_id IN ({placeholders}) OR target_tag_id IN ({placeholders}))",
            (bot_id, session_id, visibility, *ids, *ids),
        ),
    }


def _store_tag_governance_snapshot(
    connection,
    *,
    operation_id: str,
    suggestion_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    now: float,
) -> None:
    sequence = 0
    for entity_kind in ("scoped_tags", "scoped_memory_tags", "scoped_tag_relations"):
        connection.execute(
            """INSERT INTO scoped_tag_governance_changes(
                   operation_id, suggestion_id, sequence, entity_kind, entity_key,
                   before_json, after_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_id,
                suggestion_id,
                sequence,
                entity_kind,
                f"{suggestion_id}:{entity_kind}",
                _json(list(before.get(entity_kind) or ())),
                _json(list(after.get(entity_kind) or ())),
                now,
            ),
        )
        sequence += 1


def _snapshot_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _json(left) == _json(right)


def _load_tag_governance_snapshot(connection, *, operation_id: str, suggestion_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = connection.execute(
        """SELECT entity_kind, before_json, after_json
             FROM scoped_tag_governance_changes
            WHERE suggestion_id=?
            ORDER BY created_at ASC, sequence ASC""",
        (str(suggestion_id),),
    ).fetchall()
    if not rows:
        raise TagGovernanceError("governance_snapshot_missing")
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for row in rows:
        try:
            before[str(row[0])] = json.loads(str(row[1] or "[]"))
            after[str(row[0])] = json.loads(str(row[2] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TagGovernanceError("governance_snapshot_invalid") from exc
    return before, after


def _insert_snapshot_rows(connection, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    columns = tuple(str(key) for key in rows[0])
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join(columns)
    for row in rows:
        connection.execute(
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
            tuple(row.get(column) for column in columns),
        )


def _restore_tag_governance_snapshot(
    connection,
    *,
    scope: RuntimeScope,
    tag_ids: Sequence[int],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    current = _tag_governance_snapshot(connection, scope=scope, tag_ids=tag_ids)
    if not _snapshot_equal(current, after):
        raise TagGovernanceError("compensation_conflict", "governed Tag state changed after approval")
    bot_id, session_id, visibility = _scope_params(scope)
    ids = tuple(sorted({int(value) for value in tag_ids}))
    placeholders = ",".join("?" for _ in ids)
    connection.execute(
        f"DELETE FROM scoped_tag_relations WHERE bot_id=? AND session_id=? AND visibility=? AND (source_tag_id IN ({placeholders}) OR target_tag_id IN ({placeholders}))",
        (bot_id, session_id, visibility, *ids, *ids),
    )
    connection.execute(
        f"DELETE FROM scoped_memory_tags WHERE bot_id=? AND session_id=? AND visibility=? AND tag_id IN ({placeholders})",
        (bot_id, session_id, visibility, *ids),
    )
    connection.execute(
        f"DELETE FROM scoped_tags WHERE bot_id=? AND session_id=? AND visibility=? AND id IN ({placeholders})",
        (bot_id, session_id, visibility, *ids),
    )
    _insert_snapshot_rows(connection, "scoped_tags", list(before.get("scoped_tags") or ()))
    _insert_snapshot_rows(connection, "scoped_memory_tags", list(before.get("scoped_memory_tags") or ()))
    _insert_snapshot_rows(connection, "scoped_tag_relations", list(before.get("scoped_tag_relations") or ()))


class TagGovernanceGateway:
    """所有 scoped Tag 治理写入均通过该 writer-owned facade。"""

    def __init__(self, write_gateway: Any, *, clock: Any | None = None) -> None:
        coordinator = getattr(write_gateway, "coordinator", None)
        if coordinator is None:
            raise ValueError("write gateway coordinator is required")
        self._write_gateway = write_gateway
        self._coordinator = coordinator
        self._clock = clock
        self._pending_events: list[OutboxEventDraft] = []

    def _now(self) -> float:
        if self._clock is not None and callable(getattr(self._clock, "now", None)):
            return float(self._clock.now())
        return time.time()

    @staticmethod
    def _tag_rows(connection, scope: RuntimeScope, tag_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        bot_id, session_id, visibility = _scope_params(scope)
        ids = _normalize_ids(tag_ids)
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""SELECT id, name, tag_type, confidence, metadata, revision, status, aliases
                   FROM scoped_tags
                  WHERE bot_id=? AND session_id=? AND visibility=?
                    AND id IN ({placeholders})""",
            (bot_id, session_id, visibility, *ids),
        ).fetchall()
        result = {
            int(row[0]): {
                "id": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2] or "keyword"),
                "confidence": float(row[3] or 0.0),
                "metadata": json.loads(str(row[4] or "{}")),
                "revision": int(row[5] or 1),
                "status": str(row[6] or "active"),
                "aliases": json.loads(str(row[7] or "[]")),
            }
            for row in rows
        }
        missing = [tag_id for tag_id in ids if tag_id not in result]
        if missing:
            raise TagGovernanceError("tag_scope_mismatch", "one or more Tags do not belong to the RuntimeScope")
        return result

    @classmethod
    def _preview_payload(
        cls,
        connection,
        *,
        scope: RuntimeScope,
        action: str,
        tag_ids: Sequence[int],
        target_tag_id: int | None = None,
        target_name: str | None = None,
        target_type: str | None = None,
        aliases: Sequence[str] = (),
    ) -> dict[str, Any]:
        action = _normalize_action(action)
        ids = _normalize_ids(tag_ids)
        rows = cls._tag_rows(connection, scope, ids)
        if any(row["status"] != "active" for row in rows.values()):
            raise TagGovernanceError("tag_not_active", "only active scoped Tags can be governed")
        normalized_target = None if target_tag_id is None else int(target_tag_id)
        if action == "merge":
            if len(ids) < 2:
                raise TagGovernanceError("merge_targets_required", "merge requires at least two Tags")
            if normalized_target is None:
                normalized_target = ids[0]
            if normalized_target not in rows:
                raise TagGovernanceError("target_tag_required", "merge target must be one of the scoped Tags")
            normalized_name = str(target_name or rows[normalized_target]["name"]).strip()
            if not normalized_name:
                raise TagGovernanceError("target_name_required", "merge target name is required")
            normalized_type = str(target_type or rows[normalized_target]["type"]).strip()
            source_ids = [tag_id for tag_id in ids if tag_id != normalized_target]
            memory_count = int(connection.execute(
                f"""SELECT COUNT(DISTINCT memory_id) FROM scoped_memory_tags
                      WHERE bot_id=? AND session_id=? AND visibility=?
                        AND tag_id IN ({','.join('?' for _ in ids)})""",
                (*_scope_params(scope), *ids),
            ).fetchone()[0])
            relation_count = int(connection.execute(
                f"""SELECT COUNT(*) FROM scoped_tag_relations
                      WHERE bot_id=? AND session_id=? AND visibility=?
                        AND (source_tag_id IN ({','.join('?' for _ in ids)})
                          OR target_tag_id IN ({','.join('?' for _ in ids)}))""",
                (*_scope_params(scope), *ids, *ids),
            ).fetchone()[0])
            return {
                "action": action,
                "tag_ids": list(ids),
                "target_tag_id": normalized_target,
                "target_name": normalized_name,
                "target_type": normalized_type,
                "aliases": _normalize_aliases(aliases),
                "before": rows,
                "after": {
                    "target": {"id": normalized_target, "name": normalized_name, "type": normalized_type},
                    "removed_tag_ids": source_ids,
                },
                "impact": {"memory_count": memory_count, "relation_count": relation_count, "removed_tags": len(source_ids), "removed_tag_ids": list(source_ids), "related_tag_ids": list(ids), "related_tags": [row["name"] for row in rows.values()], "index_refresh": "outbox_pending"},
            }
        target = normalized_target or ids[0]
        if target not in rows or len(ids) != 1:
            raise TagGovernanceError("single_tag_required", f"{action} requires exactly one scoped Tag")
        if action == "retype":
            normalized_type = str(target_type or "").strip()
            if not normalized_type:
                raise TagGovernanceError("target_type_required", "new Tag type is required")
            after = {"id": target, "type": normalized_type}
        elif action == "alias":
            normalized_aliases = _normalize_aliases(aliases)
            if not normalized_aliases:
                raise TagGovernanceError("aliases_required", "at least one alias is required")
            current = _normalize_aliases(rows[target]["aliases"])
            merged = _normalize_aliases([*current, *normalized_aliases])
            after = {"id": target, "aliases": merged}
        else:
            after = {"id": target, "status": "inactive"}
        memory_count = int(connection.execute(
            """SELECT COUNT(DISTINCT memory_id) FROM scoped_memory_tags
                WHERE bot_id=? AND session_id=? AND visibility=? AND tag_id=?""",
            (*_scope_params(scope), target),
        ).fetchone()[0])
        relation_count = int(connection.execute(
            """SELECT COUNT(*) FROM scoped_tag_relations
                WHERE bot_id=? AND session_id=? AND visibility=?
                  AND (source_tag_id=? OR target_tag_id=?)""",
            (*_scope_params(scope), target, target),
        ).fetchone()[0])
        return {
            "action": action,
            "tag_ids": list(ids),
            "target_tag_id": target,
            "target_name": None,
            "target_type": str(target_type or "").strip() or None,
            "aliases": _normalize_aliases(aliases),
            "before": rows,
            "after": after,
            "impact": {"memory_count": memory_count, "relation_count": relation_count, "removed_tags": 0, "related_tag_ids": list(ids), "related_tags": [rows[target]["name"]], "index_refresh": "outbox_pending"},
        }

    @staticmethod
    def _suggestion_from_row(row: Any) -> dict[str, Any]:
        return {
            "suggestion_id": str(row[0]),
            "operation_id": str(row[1]),
            "scope": {"bot_id": str(row[2]), "session_id": str(row[3]), "visibility": str(row[4])},
            "action": str(row[5]),
            "tag_ids": json.loads(str(row[6] or "[]")),
            "target_tag_id": None if row[7] is None else int(row[7]),
            "target_name": row[8],
            "target_type": row[9],
            "aliases": json.loads(str(row[10] or "[]")),
            "reason": str(row[11] or ""),
            "evidence": json.loads(str(row[12] or "{}")),
            "status": str(row[13]),
            "revision": int(row[14] or 1),
            "created_at": float(row[15]),
            "expires_at": None if row[16] is None else float(row[16]),
            "resolved_at": None if row[17] is None else float(row[17]),
            "resolved_by": row[18],
            "resolution_reason": row[19],
        }

    @staticmethod
    def _suggestion_row(connection, *, scope: RuntimeScope, suggestion_id: str):
        row = connection.execute(
            """SELECT suggestion_id, operation_id, bot_id, session_id, visibility, action,
                      tag_ids_json, target_tag_id, target_name, target_type, aliases_json,
                      reason, evidence_json, status, revision, created_at, expires_at,
                      resolved_at, resolved_by, resolution_reason
                 FROM scoped_tag_audit_suggestions
                WHERE suggestion_id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (str(suggestion_id), *_scope_params(scope)),
        ).fetchone()
        if row is None:
            raise TagGovernanceError("suggestion_not_found", "scoped audit suggestion was not found")
        return row

    def _event(self, connection, *, operation_id: str, index: int, aggregate_kind: str, aggregate_id: str, version: int, event_type: str, payload: Mapping[str, Any], now: float) -> None:
        del connection, operation_id, index, now
        self._pending_events.append(
            OutboxEventDraft(aggregate_kind, str(aggregate_id), int(version), event_type, dict(payload))
        )

    def apply_mutate(self, connection, command: DomainCommand, now: float, mutate) -> MutationOutcome:
        """Run one governance mutation and return coordinator-owned events."""
        self._pending_events = []
        result = mutate(connection, command.operation_id, now)
        details = {
            "operation_id": command.operation_id,
            "suggestion_id": result.suggestion_id,
            "revision": result.revision,
            "status": result.status,
            "impact": result.impact,
        }
        entity_id = str(result.suggestion_id or command.operation_id)
        version = int(result.revision or 1)
        return MutationOutcome(
            entities=(EntityChange("tag_audit_suggestion", entity_id, version, str(result.status)),),
            events=tuple(self._pending_events),
            details=details,
        )

    async def _commit(self, *, scope: RuntimeScope, command_type: str, request_shape: Mapping[str, Any], mutate) -> TagGovernanceResult:
        request_hash = _digest(request_shape)
        idempotency_key = f"{command_type}:{request_hash}"
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"wave-memory:{command_type}:{request_hash}").hex
        command = DomainCommand(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            actor="webui.tag.governance",
            scope=scope,
            command_type=command_type,
            payload=dict(request_shape),
            request_hash=request_hash,
        )
        submit = getattr(self._coordinator, "submit", None)
        if callable(submit):
            bound = getattr(self._write_gateway, "bind_tag_governance", None)
            if callable(bound):
                bound(self, mutate)
            self._coordinator.gateway = self
            self._coordinator.mutate = mutate
            try:
                result = await submit(command)
            except CommandRejectedError as exc:
                raise TagGovernanceError(exc.code, str(exc)) from exc
            except IdempotencyConflictError as cop:
                raise TagGovernanceError("idempotency_conflict", str(cop)) from cop
            details = dict(getattr(result, "details", None) or {})
            if details:
                return TagGovernanceResult(
                    operation_id=str(details.get("operation_id") or result.operation_id),
                    suggestion_id=details.get("suggestion_id"),
                    revision=details.get("revision"),
                    status=str(details.get("status") or "committed"),
                    impact=dict(details.get("impact") or {}),
                )

        def transaction(connection):
            existing = connection.execute(
                "SELECT request_hash, status, result_json FROM write_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_hash:
                    raise TagGovernanceError("idempotency_conflict")
                if str(existing[1]) != "committed" or not existing[2]:
                    raise TagGovernanceError("operation_incomplete")
                payload = json.loads(str(existing[2]))
                details = dict(payload.get("details") or payload)
                return TagGovernanceResult(
                    operation_id=str(details.get("operation_id") or payload.get("operation_id") or operation_id),
                    suggestion_id=details.get("suggestion_id"),
                    revision=details.get("revision"),
                    status=str(details.get("status", "committed")),
                    impact=dict(details.get("impact") or {}),
                )
            outcome = self.apply_mutate(connection, command, self._now(), mutate)
            return TagGovernanceResult(
                operation_id=command.operation_id,
                suggestion_id=outcome.details.get("suggestion_id"),
                revision=outcome.details.get("revision"),
                status=str(outcome.details.get("status") or "committed"),
                impact=dict(outcome.details.get("impact") or {}),
            )

        transaction_fn = getattr(self._coordinator, "transaction", None)
        if not callable(transaction_fn):
            raise TagGovernanceError("tag_writer_unavailable")
        return await transaction_fn(transaction, actor=command_type)

    async def create_suggestion(self, *, scope: RuntimeScope, action: str, tag_ids: Sequence[Any], target_tag_id: int | None = None, target_name: str | None = None, target_type: str | None = None, aliases: Sequence[Any] = (), reason: str, evidence: Mapping[str, Any] | None = None, expires_in: float = 86400.0) -> TagGovernanceResult:
        action = _normalize_action(action)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise TagGovernanceError("reason_required", "a governance reason is required")
        ids = _normalize_ids(tag_ids)
        normalized_aliases = _normalize_aliases(aliases)
        request_shape = {"scope": scope_to_dict(scope), "action": action, "tag_ids": ids, "target_tag_id": target_tag_id, "target_name": target_name, "target_type": target_type, "aliases": normalized_aliases, "reason": normalized_reason, "evidence": dict(evidence or {})}
        suggestion_id = f"tag-suggestion:{uuid.uuid5(uuid.NAMESPACE_URL, _digest(request_shape)).hex}"

        def mutate(connection, operation_id, now):
            preview = self._preview_payload(connection, scope=scope, action=action, tag_ids=ids, target_tag_id=target_tag_id, target_name=target_name, target_type=target_type, aliases=normalized_aliases)
            connection.execute(
                """INSERT INTO scoped_tag_audit_suggestions(
                       suggestion_id, operation_id, bot_id, session_id, visibility, action,
                       tag_ids_json, target_tag_id, target_name, target_type, aliases_json,
                       reason, evidence_json, status, revision, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)""",
                (suggestion_id, operation_id, *_scope_params(scope), action, _json(ids), preview.get("target_tag_id"), preview.get("target_name"), preview.get("target_type"), _json(normalized_aliases), normalized_reason, _json(dict(evidence or {})), now, now + max(60.0, min(float(expires_in), 604800.0))),
            )
            self._event(connection, operation_id=operation_id, index=0, aggregate_kind="tag_audit_suggestion", aggregate_id=suggestion_id, version=1, event_type="tag_audit_suggestion.created", payload={"suggestion_id": suggestion_id, "action": action, "tag_ids": list(ids), "reason": normalized_reason, "evidence": dict(evidence or {}), "scope": scope_to_dict(scope)}, now=now)
            return TagGovernanceResult(operation_id=operation_id, suggestion_id=suggestion_id, revision=1, status="pending", impact=preview["impact"])

        return await self._commit(scope=scope, command_type="tags.governance.suggestion.create.v1", request_shape={**request_shape, "suggestion_id": suggestion_id}, mutate=mutate)

    def list_scoped_tags(self, connection, *, scope: RuntimeScope, search: str = "", limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        search = str(search or "").strip()
        where = ["bot_id=?", "session_id=?", "visibility=?"]
        params: list[Any] = [*_scope_params(scope)]
        if search:
            where.append("name LIKE ?")
            params.append(f"%{search}%")
        predicate = " AND ".join(where)
        total = int(connection.execute(f"SELECT COUNT(*) FROM scoped_tags WHERE {predicate}", params).fetchone()[0])
        rows = connection.execute(
            f"""SELECT id, name, tag_type, confidence, metadata, revision, status, aliases
                   FROM scoped_tags WHERE {predicate}
                  ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        return [
            {"id": int(row[0]), "name": str(row[1]), "type": str(row[2] or "keyword"), "confidence": float(row[3] or 0.0), "metadata": json.loads(str(row[4] or "{}")), "revision": int(row[5] or 1), "status": str(row[6] or "active"), "aliases": json.loads(str(row[7] or "[]"))}
            for row in rows
        ], total

    def list_suggestions(self, connection, *, scope: RuntimeScope, status: str = "pending", action: str = "", limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        status = str(status or "pending").strip()
        if status not in _STATUSES:
            raise TagGovernanceError("invalid_suggestion_status")
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        where = ["bot_id=?", "session_id=?", "visibility=?", "status=?"]
        params: list[Any] = [*_scope_params(scope), status]
        if action:
            action = _normalize_action(action)
            where.append("action=?")
            params.append(action)
        predicate = " AND ".join(where)
        total = int(connection.execute(f"SELECT COUNT(*) FROM scoped_tag_audit_suggestions WHERE {predicate}", params).fetchone()[0])
        rows = connection.execute(
            f"""SELECT suggestion_id, operation_id, bot_id, session_id, visibility, action,
                      tag_ids_json, target_tag_id, target_name, target_type, aliases_json,
                      reason, evidence_json, status, revision, created_at, expires_at,
                      resolved_at, resolved_by, resolution_reason
                 FROM scoped_tag_audit_suggestions WHERE {predicate}
                ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._suggestion_from_row(row)
            try:
                details = self._tag_rows(connection, scope, item["tag_ids"])
                item["tag_details"] = details
            except TagGovernanceError:
                item["tag_details"] = {}
            items.append(item)
        return items, total

    def preview(self, connection, *, scope: RuntimeScope, suggestion_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        row = self._suggestion_row(connection, scope=scope, suggestion_id=suggestion_id)
        suggestion = self._suggestion_from_row(row)
        if expected_revision is not None and int(expected_revision) != suggestion["revision"]:
            raise TagGovernanceError("suggestion_revision_conflict")
        now = self._now()
        if suggestion["status"] != "pending":
            raise TagGovernanceError("suggestion_already_processed")
        if suggestion["expires_at"] is not None and suggestion["expires_at"] < now:
            raise TagGovernanceError("suggestion_expired")
        preview = self._preview_payload(connection, scope=scope, action=suggestion["action"], tag_ids=suggestion["tag_ids"], target_tag_id=suggestion["target_tag_id"], target_name=suggestion["target_name"], target_type=suggestion["target_type"], aliases=suggestion["aliases"])
        token = _digest({"suggestion_id": suggestion_id, "revision": suggestion["revision"], "scope": scope_to_dict(scope), "preview": preview})
        return {"suggestion": suggestion, "preview": preview, "preflight_token": token, "expires_at": suggestion["expires_at"]}

    def _apply_preview(self, connection, *, scope: RuntimeScope, suggestion: dict[str, Any], preview: dict[str, Any], now: float) -> dict[str, Any]:
        action = suggestion["action"]
        bot_id, session_id, visibility = _scope_params(scope)
        ids = [int(value) for value in suggestion["tag_ids"]]
        target_id = int(preview["target_tag_id"])
        if action == "merge":
            sources = [tag_id for tag_id in ids if tag_id != target_id]
            for source_id in sources:
                connection.execute(
                    """INSERT OR IGNORE INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at)
                       SELECT bot_id, session_id, visibility, memory_id, ?, position, relevance, created_at
                         FROM scoped_memory_tags WHERE bot_id=? AND session_id=? AND visibility=? AND tag_id=?""",
                    (target_id, bot_id, session_id, visibility, source_id),
                )
                connection.execute(
                    "DELETE FROM scoped_memory_tags WHERE bot_id=? AND session_id=? AND visibility=? AND tag_id=?",
                    (bot_id, session_id, visibility, source_id),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO scoped_tag_relations(bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type, weight, confidence, metadata, status, valid_until, revision, created_at, updated_at)
                       SELECT bot_id, session_id, visibility, ?, target_tag_id, relation_type, weight, confidence, metadata, status, valid_until, revision, created_at, updated_at
                         FROM scoped_tag_relations WHERE bot_id=? AND session_id=? AND visibility=? AND source_tag_id=?""",
                    (target_id, bot_id, session_id, visibility, source_id),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO scoped_tag_relations(bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type, weight, confidence, metadata, status, valid_until, revision, created_at, updated_at)
                       SELECT bot_id, session_id, visibility, source_tag_id, ?, relation_type, weight, confidence, metadata, status, valid_until, revision, created_at, updated_at
                         FROM scoped_tag_relations WHERE bot_id=? AND session_id=? AND visibility=? AND target_tag_id=?""",
                    (target_id, bot_id, session_id, visibility, source_id),
                )
                connection.execute(
                    "DELETE FROM scoped_tag_relations WHERE bot_id=? AND session_id=? AND visibility=? AND (source_tag_id=? OR target_tag_id=?)",
                    (bot_id, session_id, visibility, source_id, source_id),
                )
            target = self._tag_rows(connection, scope, [target_id])[target_id]
            aliases = _normalize_aliases([*target["aliases"], *(preview["before"][source_id]["name"] for source_id in sources), *suggestion["aliases"]])
            connection.execute(
                """UPDATE scoped_tags SET name=?, tag_type=?, aliases=?, revision=revision+1, updated_at=?
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND revision=? AND status='active'""",
                (preview["target_name"], preview["target_type"], _json(aliases), now, target_id, bot_id, session_id, visibility, int(target["revision"])),
            )
            for source_id in sources:
                connection.execute("DELETE FROM scoped_tags WHERE id=? AND bot_id=? AND session_id=? AND visibility=?", (source_id, bot_id, session_id, visibility))
        elif action == "retype":
            connection.execute("UPDATE scoped_tags SET tag_type=?, revision=revision+1, updated_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND status='active'", (preview["after"]["type"], now, target_id, bot_id, session_id, visibility))
        elif action == "alias":
            connection.execute("UPDATE scoped_tags SET aliases=?, revision=revision+1, updated_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND status='active'", (_json(preview["after"]["aliases"]), now, target_id, bot_id, session_id, visibility))
        else:
            connection.execute("UPDATE scoped_tags SET status='inactive', revision=revision+1, updated_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND status='active'", (now, target_id, bot_id, session_id, visibility))
        return preview["impact"]

    async def resolve(self, *, scope: RuntimeScope, suggestion_id: str, expected_revision: int, decision: str, preview_token: str, reason: str) -> TagGovernanceResult:
        decision = str(decision or "").strip().lower()
        if decision not in {"approve", "reject"}:
            raise TagGovernanceError("invalid_suggestion_decision")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise TagGovernanceError("reason_required", "a review reason is required")
        request_shape = {"scope": scope_to_dict(scope), "suggestion_id": str(suggestion_id), "expected_revision": int(expected_revision), "decision": decision, "preview_token": str(preview_token), "reason": normalized_reason}

        def mutate(connection, operation_id, now):
            row = self._suggestion_row(connection, scope=scope, suggestion_id=suggestion_id)
            suggestion = self._suggestion_from_row(row)
            if suggestion["revision"] != int(expected_revision):
                raise TagGovernanceError("suggestion_revision_conflict")
            if suggestion["status"] != "pending":
                raise TagGovernanceError("suggestion_already_processed")

            def mark_terminal(new_status: str, event_type: str) -> TagGovernanceResult:
                cursor = connection.execute(
                    """UPDATE scoped_tag_audit_suggestions
                          SET status=?, revision=revision+1, resolved_at=?, resolved_by=?, resolution_reason=?
                        WHERE suggestion_id=? AND bot_id=? AND session_id=? AND visibility=?
                          AND status='pending' AND revision=?""",
                    (new_status, now, "webui.tag.governance", normalized_reason, suggestion_id, *_scope_params(scope), int(expected_revision)),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise TagGovernanceError("suggestion_revision_conflict")
                self._event(connection, operation_id=operation_id, index=0, aggregate_kind="tag_audit_suggestion", aggregate_id=suggestion_id, version=int(expected_revision) + 1, event_type=event_type, payload={"suggestion_id": suggestion_id, "status": new_status, "reason": normalized_reason, "scope": scope_to_dict(scope)}, now=now)
                return TagGovernanceResult(operation_id=operation_id, suggestion_id=suggestion_id, revision=int(expected_revision) + 1, status=new_status, impact={})

            try:
                preview = self._preview_payload(connection, scope=scope, action=suggestion["action"], tag_ids=suggestion["tag_ids"], target_tag_id=suggestion["target_tag_id"], target_name=suggestion["target_name"], target_type=suggestion["target_type"], aliases=suggestion["aliases"])
            except TagGovernanceError as exc:
                if exc.code in {"tag_scope_mismatch", "tag_not_active", "target_tag_required", "single_tag_required"}:
                    return mark_terminal("conflict", "tag.governance.conflict")
                raise
            expected_token = _digest({"suggestion_id": suggestion_id, "revision": suggestion["revision"], "scope": scope_to_dict(scope), "preview": preview})
            if str(preview_token) != expected_token:
                return mark_terminal("conflict", "tag.governance.conflict")
            if suggestion["expires_at"] is not None and suggestion["expires_at"] < now:
                return mark_terminal("expired", "tag.governance.expired")
            impact = preview["impact"] if decision == "approve" else {}
            event_type = "tag.governance.rejected"
            if decision == "approve":
                before_snapshot = _tag_governance_snapshot(
                    connection,
                    scope=scope,
                    tag_ids=suggestion["tag_ids"],
                )
                impact = self._apply_preview(connection, scope=scope, suggestion=suggestion, preview=preview, now=now)
                affected_memory_ids = {
                    int(row["memory_id"])
                    for row in before_snapshot.get("scoped_memory_tags", ())
                    if row.get("memory_id") is not None
                }
                affected_memory_ids.update(
                    int(row["memory_id"])
                    for row in _tag_governance_snapshot(connection, scope=scope, tag_ids=suggestion["tag_ids"]).get("scoped_memory_tags", ())
                    if row.get("memory_id") is not None
                )
                if affected_memory_ids:
                    rebuild_scope_effective_tags(
                        connection,
                        scope=scope,
                        memory_ids=affected_memory_ids,
                        now=now,
                    )
                impact = {**impact, "projection_status": "ready"}
                after_snapshot = _tag_governance_snapshot(
                    connection,
                    scope=scope,
                    tag_ids=suggestion["tag_ids"],
                )
                _store_tag_governance_snapshot(
                    connection,
                    operation_id=operation_id,
                    suggestion_id=suggestion_id,
                    before=before_snapshot,
                    after=after_snapshot,
                    now=now,
                )
                event_type = f"tag.{suggestion['action']}"
            new_status = "approved" if decision == "approve" else "rejected"
            cursor = connection.execute(
                """UPDATE scoped_tag_audit_suggestions
                      SET status=?, revision=revision+1, resolved_at=?, resolved_by=?, resolution_reason=?
                    WHERE suggestion_id=? AND bot_id=? AND session_id=? AND visibility=?
                      AND status='pending' AND revision=?""",
                (new_status, now, "webui.tag.governance", normalized_reason, suggestion_id, *_scope_params(scope), int(expected_revision)),
            )
            if int(cursor.rowcount or 0) != 1:
                raise TagGovernanceError("suggestion_revision_conflict")
            self._event(connection, operation_id=operation_id, index=0, aggregate_kind="tag_audit_suggestion", aggregate_id=suggestion_id, version=int(expected_revision) + 1, event_type=event_type, payload={"suggestion_id": suggestion_id, "decision": decision, "reason": normalized_reason, "impact": impact, "scope": scope_to_dict(scope)}, now=now)
            return TagGovernanceResult(operation_id=operation_id, suggestion_id=suggestion_id, revision=int(expected_revision) + 1, status=new_status, impact=impact)

        return await self._commit(scope=scope, command_type=f"tags.governance.{decision}.v1", request_shape=request_shape, mutate=mutate)

    async def compensate(
        self,
        *,
        scope: RuntimeScope,
        suggestion_id: str,
        expected_revision: int,
        reason: str,
    ) -> TagGovernanceResult:
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise TagGovernanceError("reason_required")
        suggestion_id = str(suggestion_id or "").strip()
        if not suggestion_id:
            raise TagGovernanceError("suggestion_required")
        request_shape = {
            "scope": scope_to_dict(scope),
            "suggestion_id": suggestion_id,
            "expected_revision": int(expected_revision),
            "reason": normalized_reason,
        }

        def mutate(connection, operation_id, now):
            row = self._suggestion_row(connection, scope=scope, suggestion_id=suggestion_id)
            suggestion = self._suggestion_from_row(row)
            if int(suggestion["revision"]) != int(expected_revision):
                raise TagGovernanceError("suggestion_revision_conflict")
            if suggestion["status"] != "approved":
                raise TagGovernanceError("suggestion_not_compensable")
            existing = connection.execute(
                """SELECT status, operation_id FROM scoped_tag_governance_compensations
                    WHERE suggestion_id=? ORDER BY created_at DESC LIMIT 1""",
                (suggestion_id,),
            ).fetchone()
            if existing is not None and str(existing[0]) == "committed":
                raise TagGovernanceError("suggestion_already_compensated")
            before, after = _load_tag_governance_snapshot(
                connection,
                operation_id=suggestion["operation_id"],
                suggestion_id=suggestion_id,
            )
            tag_ids = suggestion["tag_ids"]
            compensation_id = f"tag-compensation:{uuid.uuid5(uuid.NAMESPACE_URL, operation_id).hex}"
            try:
                _restore_tag_governance_snapshot(
                    connection,
                    scope=scope,
                    tag_ids=tag_ids,
                    before=before,
                    after=after,
                )
            except TagGovernanceError as exc:
                if exc.code != "compensation_conflict":
                    raise
                connection.execute(
                    """INSERT INTO scoped_tag_governance_compensations(
                           compensation_id, operation_id, suggestion_id, bot_id, session_id,
                           visibility, expected_revision, status, reason, created_at, resolved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'conflict', ?, ?, ?)""",
                    (compensation_id, operation_id, suggestion_id, *_scope_params(scope), int(expected_revision), normalized_reason, now, now),
                )
                self._event(
                    connection,
                    operation_id=operation_id,
                    index=0,
                    aggregate_kind="tag_audit_suggestion",
                    aggregate_id=suggestion_id,
                    version=int(expected_revision),
                    event_type="tag.governance.compensation_conflict",
                    payload={
                        "suggestion_id": suggestion_id,
                        "reason": normalized_reason,
                        "scope": scope_to_dict(scope),
                    },
                    now=now,
                )
                return TagGovernanceResult(
                    operation_id=operation_id,
                    suggestion_id=suggestion_id,
                    revision=int(expected_revision),
                    status="conflict",
                    impact={},
                )
            rebuild_scope_effective_tags(
                connection,
                scope=scope,
                now=now,
            )
            connection.execute(
                """INSERT INTO scoped_tag_governance_compensations(
                       compensation_id, operation_id, suggestion_id, bot_id, session_id,
                       visibility, expected_revision, status, reason, created_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?, ?)""",
                (compensation_id, operation_id, suggestion_id, *_scope_params(scope), int(expected_revision), normalized_reason, now, now),
            )
            impact = {
                "related_tag_ids": list(tag_ids),
                "index_refresh": "outbox_pending",
                "projection_status": "ready",
            }
            self._event(
                connection,
                operation_id=operation_id,
                index=0,
                aggregate_kind="tag_audit_suggestion",
                aggregate_id=suggestion_id,
                version=int(expected_revision) + 1,
                event_type="tag.governance.compensated",
                payload={
                    "suggestion_id": suggestion_id,
                    "reason": normalized_reason,
                    "impact": impact,
                    "scope": scope_to_dict(scope),
                },
                now=now,
            )
            return TagGovernanceResult(
                operation_id=operation_id,
                suggestion_id=suggestion_id,
                revision=int(expected_revision) + 1,
                status="compensated",
                impact=impact,
            )

        return await self._commit(
            scope=scope,
            command_type="tags.governance.compensate.v1",
            request_shape=request_shape,
            mutate=mutate,
        )

    async def resolve_batch(self, *, scope: RuntimeScope, items: Sequence[Mapping[str, Any]], decision: str, reason: str) -> TagGovernanceResult:
        decision = str(decision or "").strip().lower()
        if decision not in {"approve", "reject"}:
            raise TagGovernanceError("invalid_suggestion_decision")
        if not items:
            raise TagGovernanceError("suggestions_required")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise TagGovernanceError("reason_required", "a review reason is required")
        normalized_items = [dict(item) for item in items]
        request_shape = {"scope": scope_to_dict(scope), "items": normalized_items, "decision": decision, "reason": normalized_reason}

        def mutate(connection, operation_id, now):
            validated: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for item in normalized_items:
                suggestion_id = str(item.get("suggestion_id") or "")
                row = self._suggestion_row(connection, scope=scope, suggestion_id=suggestion_id)
                suggestion = self._suggestion_from_row(row)
                expected_revision = int(item.get("revision"))
                if suggestion["revision"] != expected_revision or suggestion["status"] != "pending":
                    raise TagGovernanceError("batch_validation_failed", "one or more suggestions changed or were already processed")
                preview = self._preview_payload(connection, scope=scope, action=suggestion["action"], tag_ids=suggestion["tag_ids"], target_tag_id=suggestion["target_tag_id"], target_name=suggestion["target_name"], target_type=suggestion["target_type"], aliases=suggestion["aliases"])
                token = _digest({"suggestion_id": suggestion_id, "revision": suggestion["revision"], "scope": scope_to_dict(scope), "preview": preview})
                if str(item.get("preflight_token") or "") != token:
                    raise TagGovernanceError("batch_validation_failed", "one or more preview tokens are invalid")
                if suggestion["expires_at"] is not None and suggestion["expires_at"] < now:
                    raise TagGovernanceError("batch_validation_failed", "one or more suggestions expired")
                validated.append((suggestion, preview))
            total_impact: dict[str, Any] = {"suggestions": len(validated), "memory_count": 0, "relation_count": 0, "removed_tags": 0, "removed_tag_ids": [], "related_tag_ids": [], "related_tags": [], "index_refresh": "outbox_pending"}
            for index, (suggestion, preview) in enumerate(validated):
                before_snapshot = (
                    _tag_governance_snapshot(connection, scope=scope, tag_ids=suggestion["tag_ids"])
                    if decision == "approve" else None
                )
                impact = self._apply_preview(connection, scope=scope, suggestion=suggestion, preview=preview, now=now) if decision == "approve" else {}
                if decision == "approve":
                    after_snapshot = _tag_governance_snapshot(
                        connection,
                        scope=scope,
                        tag_ids=suggestion["tag_ids"],
                    )
                    _store_tag_governance_snapshot(
                        connection,
                        operation_id=operation_id,
                        suggestion_id=suggestion["suggestion_id"],
                        before=before_snapshot or {},
                        after=after_snapshot,
                        now=now,
                    )
                for key in ("memory_count", "relation_count", "removed_tags"):
                    total_impact[key] += int(impact.get(key, 0))
                total_impact["removed_tag_ids"].extend(int(value) for value in impact.get("removed_tag_ids", ()))
                total_impact["related_tag_ids"].extend(int(value) for value in impact.get("related_tag_ids", ()))
                total_impact["related_tags"].extend(str(value) for value in impact.get("related_tags", ()))
                status = "approved" if decision == "approve" else "rejected"
                connection.execute(
                    """UPDATE scoped_tag_audit_suggestions SET status=?, revision=revision+1, resolved_at=?, resolved_by=?, resolution_reason=?
                        WHERE suggestion_id=? AND bot_id=? AND session_id=? AND visibility=? AND status='pending' AND revision=?""",
                    (status, now, "webui.tag.governance", normalized_reason, suggestion["suggestion_id"], *_scope_params(scope), suggestion["revision"]),
                )
                self._event(connection, operation_id=operation_id, index=index, aggregate_kind="tag_audit_suggestion", aggregate_id=suggestion["suggestion_id"], version=suggestion["revision"] + 1, event_type=f"tag.governance.batch_{decision}", payload={"suggestion_id": suggestion["suggestion_id"], "decision": decision, "reason": normalized_reason, "impact": impact, "scope": scope_to_dict(scope)}, now=now)
            return TagGovernanceResult(operation_id=operation_id, suggestion_id=None, revision=None, status="approved" if decision == "approve" else "rejected", impact=total_impact)

        return await self._commit(scope=scope, command_type=f"tags.governance.batch.{decision}.v1", request_shape=request_shape, mutate=mutate)


def apply_tag_governance_command(connection, command: DomainCommand, now: float, *, gateway: Any, mutate) -> MutationOutcome:
    return gateway.apply_mutate(connection, command, now, mutate)


__all__ = [
    "TAG_GOVERNANCE_COMMANDS",
    "TagGovernanceError",
    "TagGovernanceGateway",
    "TagGovernanceResult",
    "apply_tag_governance_command",
]
