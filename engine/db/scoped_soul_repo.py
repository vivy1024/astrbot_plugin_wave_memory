"""正式 Scoped Soul 仓储，不向 legacy Soul 表回退。"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

try:
    from ...domain.relationship_policy import (
        DIMENSION_RANGES,
        DIMENSION_WEIGHTS,
        NOISY_EVENT_REASONS,
        NOISY_EVENT_TYPES,
        attitude_level,
        cap_automatic_delta,
        cap_manual_adjustment_delta,
        clamp_dimension,
        compute_affinity,
        is_noisy_relationship_event,
        validate_event,
    )
    from ...domain.scope import RuntimeScope
except ImportError:  # pragma: no cover
    from domain.relationship_policy import (
        DIMENSION_RANGES,
        DIMENSION_WEIGHTS,
        NOISY_EVENT_REASONS,
        NOISY_EVENT_TYPES,
        attitude_level,
        cap_automatic_delta,
        cap_manual_adjustment_delta,
        clamp_dimension,
        compute_affinity,
        is_noisy_relationship_event,
        validate_event,
    )
    from domain.scope import RuntimeScope

from .connection import ConnectionManager
try:
    from ...services.soul_context import resolve_soul_context
except ImportError:  # pragma: no cover - top-level repository imports
    from services.soul_context import resolve_soul_context

try:
    from ...services.relationship_evidence_display import extract_historical_audit_summaries
except ImportError:  # pragma: no cover - top-level repository imports
    from services.relationship_evidence_display import extract_historical_audit_summaries


def _evidence_summaries(evidence: Any) -> list[str]:
    """Read-only extract of historical_audit_summary texts; never mutates affinity."""
    return extract_historical_audit_summaries(evidence, max_items=3)


_DIMENSION_WEIGHTS = DIMENSION_WEIGHTS
_DIMENSION_RANGES = DIMENSION_RANGES


class ScopedSoulScopeError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message or code)


def _require_scope(scope: RuntimeScope | None) -> RuntimeScope:
    if not isinstance(scope, RuntimeScope):
        raise ScopedSoulScopeError("scope_required")
    if scope.visibility != "group" or scope.session is None or scope.session.kind != "group":
        raise ScopedSoulScopeError("soul_scope_visibility_unsupported")
    return scope


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    assert scope.session is not None
    return scope.bot_id, scope.session.id, scope.visibility


def _exact_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty exact string")
    return value


def _json_mapping(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise TypeError("value must be a mapping")
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_evidence(value: Sequence[Mapping[str, Any]] | None) -> str:
    if value is None:
        return "[]"
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("evidence must be a sequence of mappings")
    items = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("every evidence item must be a mapping")
        items.append(dict(item))
    return json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_evidence_list(raw: Any) -> list[dict[str, Any]]:
    """Best-effort parse of stored evidence JSON into a list of dicts."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [dict(x) for x in raw if isinstance(x, Mapping)]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [dict(x) for x in data if isinstance(x, Mapping)]


def _historical_audit_summary_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("kind") == "historical_audit_summary":
            out.append(dict(item))
    return out


def _merge_relationship_evidence(
    existing: Any,
    new_items: Sequence[Mapping[str, Any]] | None,
    *,
    prefer_new_first: bool = True,
) -> list[dict[str, Any]]:
    """Merge live evidence onto formal row while preserving audit summaries.

    Live paths historically *replaced* evidence with only the latest event
    fragment, which dropped ``historical_audit_summary`` objects written by
    Wave2. Preserve those kinds; de-dupe by kind+summary text.

    If ``new_items`` is None, keep the existing list unchanged (including
    non-summary fragments).
    """
    old = _parse_evidence_list(existing)
    if new_items is None:
        return old
    preserved = _historical_audit_summary_items(old)
    fresh: list[dict[str, Any]] = []
    for item in new_items:
        if not isinstance(item, Mapping):
            continue
        fresh.append(dict(item))
    # If caller already supplied summaries, do not duplicate from existing.
    if _historical_audit_summary_items(fresh):
        merged = list(fresh)
    elif prefer_new_first:
        merged = list(fresh) + preserved
    else:
        merged = preserved + list(fresh)
    # Dedupe historical_audit_summary by summary text (keep first).
    seen_summary: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in merged:
        if item.get("kind") == "historical_audit_summary":
            key = str(item.get("summary") or "")
            if key in seen_summary:
                continue
            seen_summary.add(key)
        out.append(item)
    return out


def _state_for_affinity(affinity: int) -> str:
    return attitude_level(affinity)


def _compute_affinity(dimensions: Mapping[str, float]) -> int:
    return compute_affinity(dimensions)


class ScopedSoulRepository:
    """以 bot_id + session_id + visibility（关系再加 subject）隔离 Soul。"""

    def __init__(self, cm: ConnectionManager, soul_context_provider: Any | None = None):
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm
        self.soul_context_provider = soul_context_provider
        # Direct repository users (including focused tests) receive the same idempotent
        # additive schema as the production DB facade.
        try:
            from .migrations.scoped_relationship_calibration import ensure_scoped_relationship_calibration_schema
            ensure_scoped_relationship_calibration_schema(cm)
        except ImportError:  # pragma: no cover - top-level repository imports
            from engine.db.migrations.scoped_relationship_calibration import ensure_scoped_relationship_calibration_schema
            ensure_scoped_relationship_calibration_schema(cm)

    def set_soul_context_provider(self, provider: Any | None) -> None:
        """设置可选 Soul Context provider；传入 None 即关闭该能力。"""
        self.soul_context_provider = provider

    def _write(self, callback: Callable[[Any], Any], connection=None):
        if connection is not None:
            return callback(connection)
        with self.cm.write_transaction() as tx:
            return callback(tx)

    @staticmethod
    def _next_revision(tx, scope: RuntimeScope, component: str, subject: str = "", now: float | None = None) -> int:
        timestamp = float(now or time.time())
        params = (*_scope_params(scope), component, subject)
        tx.execute(
            """INSERT INTO scoped_soul_revisions
                   (bot_id, session_id, visibility, component, subject_principal_id, revision, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(bot_id, session_id, visibility, component, subject_principal_id)
               DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at""",
            (*params, timestamp),
        )
        row = tx.execute(
            """SELECT revision FROM scoped_soul_revisions
               WHERE bot_id=? AND session_id=? AND visibility=?
                 AND component=? AND subject_principal_id=?""",
            params,
        ).fetchone()
        return int(row[0])

    def upsert_mood(
        self,
        scope: RuntimeScope,
        *,
        valence: float,
        arousal: float,
        cause: str = "",
        evidence: Sequence[Mapping[str, Any]] | None = None,
        policy_version: str = "scoped-mood/v1",
        observed_at: float | None = None,
        connection=None,
    ) -> int:
        scope = _require_scope(scope)
        valence = max(-1.0, min(1.0, float(valence)))
        arousal = max(0.0, min(1.0, float(arousal)))
        encoded_evidence = _json_evidence(evidence)
        now = float(observed_at or time.time())

        def persist(tx):
            revision = self._next_revision(tx, scope, "mood", now=now)
            tx.execute(
                """INSERT INTO scoped_soul_mood
                       (bot_id, session_id, visibility, valence, arousal, cause, policy_version,
                        revision, evidence, observed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility) DO UPDATE SET
                       valence=excluded.valence, arousal=excluded.arousal, cause=excluded.cause,
                       policy_version=excluded.policy_version, revision=excluded.revision,
                       evidence=excluded.evidence, observed_at=excluded.observed_at,
                       updated_at=excluded.updated_at""",
                (*_scope_params(scope), valence, arousal, str(cause or ""), str(policy_version),
                 revision, encoded_evidence, now, now),
            )
            return revision

        return int(self._write(persist, connection))

    def replace_concerns(
        self,
        scope: RuntimeScope,
        *,
        concerns: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]] | None = None,
        connection=None,
    ) -> int:
        scope = _require_scope(scope)
        if isinstance(concerns, (str, bytes)) or not isinstance(concerns, Sequence):
            raise TypeError("concerns must be a sequence")
        encoded_default_evidence = _json_evidence(evidence)
        normalized = []
        for item in concerns:
            if not isinstance(item, Mapping):
                raise TypeError("every concern must be a mapping")
            topic = _exact_string(item.get("topic"), "topic")
            intensity = max(0.0, min(1.0, float(item.get("intensity", 0.7))))
            created_at = float(item.get("created_at") or time.time())
            last_triggered = float(item.get("last_triggered") or created_at)
            status = str(item.get("status") or "active").strip() or "active"
            if status not in {"active", "dormant", "progressing", "resolved", "expired", "archived"}:
                raise ValueError("invalid_concern_status")
            item_evidence = item.get("evidence")
            normalized.append((
                topic,
                intensity,
                item.get("origin_memory_id") or None,
                item.get("origin_episode_id") or None,
                str(item.get("concern_type") or "").strip(),
                status,
                max(0.0, min(1.0, float(item.get("urgency") or 0.0))),
                item.get("last_progress_at"),
                item.get("expected_resolution_at"),
                str(item.get("resolution_note") or "").strip(),
                created_at,
                last_triggered,
                _json_evidence(item_evidence) if item_evidence is not None else encoded_default_evidence,
            ))

        def persist(tx):
            now = time.time()
            revision = self._next_revision(tx, scope, "concerns", now=now)
            tx.execute(
                "DELETE FROM scoped_soul_concerns WHERE bot_id=? AND session_id=? AND visibility=?",
                _scope_params(scope),
            )
            for row in normalized:
                tx.execute(
                    """INSERT INTO scoped_soul_concerns
                           (bot_id, session_id, visibility, topic, intensity, origin_memory_id,
                            origin_episode_id, concern_type, status, urgency, last_progress_at,
                            expected_resolution_at, resolution_note, created_at, last_triggered,
                            revision, evidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*_scope_params(scope), *row[:12], revision, row[12]),
                )
            return revision

        return int(self._write(persist, connection))

    def add_timeline_event(
        self,
        scope: RuntimeScope,
        *,
        event_summary: str,
        emotional_weight: float = 0.5,
        timestamp: float | None = None,
        event_type: str = "time_anchor",
        subject_principal_id: str | None = None,
        evidence: Sequence[Mapping[str, Any]] | None = None,
        connection=None,
    ) -> int:
        scope = _require_scope(scope)
        summary = _exact_string(event_summary, "event_summary")
        subject = subject_principal_id or scope.subject_principal_id
        if subject is not None:
            subject = _exact_string(subject, "subject_principal_id")
        occurred_at = float(timestamp or time.time())
        weight = max(0.0, min(1.0, float(emotional_weight)))
        encoded_evidence = _json_evidence(evidence)

        def persist(tx):
            revision = self._next_revision(tx, scope, "timeline", now=occurred_at)
            cur = tx.execute(
                """INSERT INTO scoped_soul_timeline
                       (bot_id, session_id, visibility, subject_principal_id, event_summary,
                        event_type, emotional_weight, occurred_at, revision, evidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*_scope_params(scope), subject, summary, str(event_type), weight, occurred_at,
                 revision, encoded_evidence, time.time()),
            )
            return int(getattr(cur, "lastrowid", 0) or 0)

        return int(self._write(persist, connection))

    def _relationship_row(self, tx, scope: RuntimeScope, subject: str):
        return tx.execute(
            """SELECT affinity, state, dimensions, revision, evidence, updated_at
                 FROM scoped_soul_relationships
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            (*_scope_params(scope), subject),
        ).fetchone()

    @staticmethod
    def _relationship_revision(tx, scope: RuntimeScope, subject: str) -> int:
        row = tx.execute(
            """SELECT revision FROM scoped_soul_revisions
                WHERE bot_id=? AND session_id=? AND visibility=?
                  AND component='relationship' AND subject_principal_id=?""",
            (*_scope_params(scope), subject),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def _value_rows(self, tx, scope: RuntimeScope, subject: str) -> dict[str, dict[str, Any]]:
        rows = tx.execute(
            """SELECT dimension, automatic_value, manual_adjustment, manual_override,
                      effective_value, relationship_revision, evidence, updated_at
                 FROM scoped_soul_relationship_values
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            (*_scope_params(scope), subject),
        ).fetchall()
        return {
            str(row[0]): {
                "dimension": str(row[0]),
                "automatic_value": float(row[1]),
                "manual_adjustment": None if row[2] is None else float(row[2]),
                "manual_override": None if row[3] is None else float(row[3]),
                "effective_value": float(row[4]),
                "relationship_revision": int(row[5]),
                "evidence": json.loads(str(row[6] or "[]")),
                "updated_at": float(row[7]),
            }
            for row in rows
        }

    @staticmethod
    def _effective_value(automatic: float, adjustment: float | None, override: float | None, dimension: str) -> float:
        if override is not None:
            return clamp_dimension(dimension, override)
        return clamp_dimension(dimension, automatic + (adjustment or 0.0))

    def _ensure_formal_values(self, tx, scope: RuntimeScope, subject: str, now: float) -> dict[str, dict[str, Any]]:
        rows = self._value_rows(tx, scope, subject)
        relationship = self._relationship_row(tx, scope, subject)
        if relationship is None:
            return rows
        try:
            dimensions = json.loads(str(relationship[2] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ScopedSoulScopeError("relationship_projection_invalid")
        revision = int(relationship[3] or self._relationship_revision(tx, scope, subject))
        evidence = json.loads(str(relationship[4] or "[]"))
        for dimension, value in dimensions.items():
            if dimension not in _DIMENSION_RANGES or isinstance(value, bool):
                continue
            try:
                automatic = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(automatic):
                continue
            current = rows.get(dimension)
            if current is None:
                tx.execute(
                    """INSERT INTO scoped_soul_relationship_values(
                           bot_id, session_id, visibility, subject_principal_id, dimension,
                           automatic_value, manual_adjustment, manual_override, effective_value,
                           relationship_revision, evidence, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)""",
                    (*_scope_params(scope), subject, dimension, clamp_dimension(dimension, automatic),
                     clamp_dimension(dimension, automatic), revision, _json_evidence(evidence), now),
                )
        return self._value_rows(tx, scope, subject)

    def upsert_relationship(
        self,
        scope: RuntimeScope,
        *,
        subject_principal_id: str,
        affinity: int,
        state: str | None = None,
        dimensions: Mapping[str, Any] | None = None,
        evidence: Sequence[Mapping[str, Any]] | None = None,
        connection=None,
    ) -> int:
        scope = _require_scope(scope)
        subject = _exact_string(subject_principal_id, "subject_principal_id")
        if scope.subject_principal_id is not None and scope.subject_principal_id != subject:
            raise ScopedSoulScopeError("scope_subject_mismatch")
        affinity = int(max(-100, min(100, int(affinity))))
        normalized_dimensions = {
            str(key): clamp_dimension(str(key), float(value))
            for key, value in dict(dimensions or {}).items()
            if str(key) in _DIMENSION_RANGES and not isinstance(value, bool)
        }

        def persist(tx):
            now = time.time()
            existing = self._relationship_row(tx, scope, subject)
            existing_values = self._ensure_formal_values(tx, scope, subject, now)
            revision = self._next_revision(tx, scope, "relationship", subject, now)
            # Preserve historical_audit_summary when callers pass partial/new evidence.
            existing_evidence = existing[4] if existing is not None and len(existing) > 4 else None
            merged_evidence = _merge_relationship_evidence(existing_evidence, evidence)
            encoded_evidence = _json_evidence(merged_evidence)
            tx.execute(
                "UPDATE scoped_soul_relationship_values SET relationship_revision=? WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?",
                (revision, *_scope_params(scope), subject),
            )
            for dimension, value in normalized_dimensions.items():
                current = existing_values.get(dimension)
                adjustment = None if current is None else current["manual_adjustment"]
                override = None if current is None else current["manual_override"]
                effective = self._effective_value(value, adjustment, override, dimension)
                tx.execute(
                    """INSERT INTO scoped_soul_relationship_values(
                           bot_id, session_id, visibility, subject_principal_id, dimension,
                           automatic_value, manual_adjustment, manual_override, effective_value,
                           relationship_revision, evidence, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bot_id, session_id, visibility, subject_principal_id, dimension)
                       DO UPDATE SET automatic_value=excluded.automatic_value,
                           effective_value=excluded.effective_value, relationship_revision=excluded.relationship_revision,
                           evidence=excluded.evidence, updated_at=excluded.updated_at""",
                    (*_scope_params(scope), subject, dimension, value, adjustment, override, effective,
                     revision, encoded_evidence, now),
                )
            if normalized_dimensions:
                effective_dimensions = {key: row["effective_value"] for key, row in self._value_rows(tx, scope, subject).items()}
            else:
                effective_dimensions = {key: row["effective_value"] for key, row in existing_values.items()}
            final_affinity = affinity if existing is None else (_compute_affinity(effective_dimensions) if effective_dimensions else affinity)
            tx.execute(
                """INSERT INTO scoped_soul_relationships
                       (bot_id, session_id, visibility, subject_principal_id, affinity, state,
                        dimensions, revision, evidence, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility, subject_principal_id) DO UPDATE SET
                       affinity=excluded.affinity, state=excluded.state, dimensions=excluded.dimensions,
                       revision=excluded.revision, evidence=excluded.evidence, updated_at=excluded.updated_at""",
                (*_scope_params(scope), subject, final_affinity,
                 state or _state_for_affinity(final_affinity), _json_mapping(effective_dimensions),
                 revision, encoded_evidence, now),
            )
            return revision

        return int(self._write(persist, connection))

    def record_relationship_event(
        self,
        scope: RuntimeScope,
        *,
        event_type: str,
        dimension: str,
        delta: float,
        reason: str,
        source_episode_id: int | None = None,
        source_memory_id: int | None = None,
        created_at: float | None = None,
        connection=None,
    ) -> dict[str, Any]:
        scope = _require_scope(scope)
        subject = scope.subject_principal_id
        if subject is None:
            raise ScopedSoulScopeError("scope_subject_required")
        try:
            normalized_event, dimension, reason, requested_delta = validate_event(event_type, dimension, reason, delta)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        now = float(created_at or time.time())

        def persist(tx):
            row = self._relationship_row(tx, scope, subject)
            before = int(row[0] or 0) if row else 0
            existing_values = self._ensure_formal_values(tx, scope, subject, now)
            current = existing_values.get(dimension)
            before_snapshot = dict(current) if current is not None else None
            daily_row = tx.execute(
                """SELECT COALESCE(SUM(delta), 0) FROM scoped_soul_relationship_events
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                      AND dimension=? AND created_at>=?""",
                (*_scope_params(scope), subject, dimension, now - 86400),
            ).fetchone()
            applied_delta = cap_automatic_delta(
                dimension=dimension,
                requested_delta=requested_delta,
                daily_total=float(daily_row[0] or 0.0),
            )
            if current is None:
                automatic = clamp_dimension(dimension, applied_delta)
                adjustment = None
                override = None
            else:
                automatic = clamp_dimension(dimension, current["automatic_value"] + applied_delta)
                adjustment = current["manual_adjustment"]
                override = current["manual_override"]
            revision = self._next_revision(tx, scope, "relationship", subject, now)
            tx.execute(
                "UPDATE scoped_soul_relationship_values SET relationship_revision=? WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?",
                (revision, *_scope_params(scope), subject),
            )
            evidence = [{"dimension": dimension, "value_layer": "automatic"}]
            if source_episode_id is not None:
                evidence.append({"episode_id": source_episode_id})
            if source_memory_id is not None:
                evidence.append({"memory_id": source_memory_id})
            encoded_evidence = _json_evidence(evidence)
            effective = self._effective_value(automatic, adjustment, override, dimension)
            tx.execute(
                """INSERT INTO scoped_soul_relationship_values(
                       bot_id, session_id, visibility, subject_principal_id, dimension,
                       automatic_value, manual_adjustment, manual_override, effective_value,
                       relationship_revision, evidence, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility, subject_principal_id, dimension)
                   DO UPDATE SET automatic_value=excluded.automatic_value, effective_value=excluded.effective_value,
                       relationship_revision=excluded.relationship_revision, evidence=excluded.evidence,
                       updated_at=excluded.updated_at""",
                (*_scope_params(scope), subject, dimension, automatic, adjustment, override, effective,
                 revision, encoded_evidence, now),
            )
            values = self._value_rows(tx, scope, subject)
            dimensions = {key: item["effective_value"] for key, item in values.items()}
            after = _compute_affinity(dimensions)
            cur = tx.execute(
                """INSERT INTO scoped_soul_relationship_events(
                       bot_id, session_id, visibility, subject_principal_id, event_type,
                       dimension, delta, reason, source_episode_id, source_memory_id,
                       revision, created_at, operation_id, evidence, value_layer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'automatic')""",
                (*_scope_params(scope), subject, normalized_event, dimension, applied_delta,
                 reason, source_episode_id, source_memory_id, revision, now, encoded_evidence),
            )
            event_id = int(getattr(cur, "lastrowid", 0) or 0)
            after_snapshot = dict(values[dimension])
            encoded_before = None if before_snapshot is None else json.dumps(before_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            encoded_after = json.dumps(after_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            tx.execute(
                "UPDATE scoped_soul_relationship_events SET before_json=?, after_json=? WHERE id=?",
                (encoded_before, encoded_after, event_id),
            )
            # Keep Wave2 historical_audit_summary objects across live affinity events.
            existing_rel_evidence = row[4] if row is not None and len(row) > 4 else None
            relationship_evidence = _merge_relationship_evidence(
                existing_rel_evidence,
                [{"relationship_event_id": event_id}, *evidence],
            )
            tx.execute(
                """INSERT INTO scoped_soul_relationships(
                       bot_id, session_id, visibility, subject_principal_id, affinity, state,
                       dimensions, revision, evidence, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility, subject_principal_id) DO UPDATE SET
                       affinity=excluded.affinity, state=excluded.state, dimensions=excluded.dimensions,
                       revision=excluded.revision, evidence=excluded.evidence, updated_at=excluded.updated_at""",
                (*_scope_params(scope), subject, after, _state_for_affinity(after),
                 _json_mapping(dimensions), revision, _json_evidence(relationship_evidence), now),
            )
            return {
                "event_id": event_id,
                "dimension": dimension,
                "requested_delta": requested_delta,
                "applied_delta": applied_delta,
                "before_affinity": before,
                "after_affinity": after,
                "reason": reason,
                "revision": revision,
                "value_layer": "automatic",
            }

        return dict(self._write(persist, connection))

    def calibrate_relationship(
        self,
        scope: RuntimeScope,
        *,
        subject_principal_id: str,
        expected_revision: int,
        action: str,
        dimension: str,
        delta: float | None = None,
        value: float | None = None,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        operation_id: str,
        created_at: float | None = None,
        connection=None,
    ) -> dict[str, Any]:
        """Apply one manual layer transition inside an existing writer transaction."""
        scope = _require_scope(scope)
        subject = _exact_string(subject_principal_id, "subject_principal_id")
        if scope.subject_principal_id is not None and scope.subject_principal_id != subject:
            raise ScopedSoulScopeError("scope_subject_mismatch")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"adjust", "override", "clear_override", "restore_auto"}:
            raise ValueError("relationship_action_invalid")
        if dimension not in _DIMENSION_RANGES:
            raise ValueError("relationship_dimension_unknown")
        reason = _exact_string(reason, "reason")
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)) or not evidence:
            raise ValueError("relationship_evidence_required")
        encoded_evidence = _json_evidence(evidence)
        now = float(created_at or time.time())
        expected_revision = int(expected_revision)

        def persist(tx):
            relationship = self._relationship_row(tx, scope, subject)
            if relationship is None:
                raise ScopedSoulScopeError("relationship_unknown")
            current_revision = int(relationship[3] or 0)
            if current_revision != expected_revision:
                raise ScopedSoulScopeError("relationship_revision_conflict")
            values = self._ensure_formal_values(tx, scope, subject, now)
            current = values.get(dimension)
            if current is None:
                raise ScopedSoulScopeError("relationship_automatic_value_unavailable")
            before = dict(current)
            lo, hi = _DIMENSION_RANGES[dimension]
            adjustment = current["manual_adjustment"]
            override = current["manual_override"]
            if normalized_action == "adjust":
                if delta is None or isinstance(delta, bool):
                    raise ValueError("relationship_delta_invalid")
                applied_delta = cap_manual_adjustment_delta(delta)
                adjustment = (adjustment or 0.0) + applied_delta
                adjustment = max(-(hi - lo), min(hi - lo, adjustment))
                if adjustment == 0.0:
                    adjustment = None
            elif normalized_action == "override":
                if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
                    raise ValueError("relationship_value_invalid")
                if float(value) < lo or float(value) > hi:
                    raise ValueError("relationship_value_out_of_range")
                override = float(value)
            elif normalized_action == "clear_override":
                if override is None:
                    raise ScopedSoulScopeError("relationship_manual_layer_unavailable")
                override = None
            else:
                adjustment = None
                override = None
            effective = self._effective_value(current["automatic_value"], adjustment, override, dimension)
            revision = self._next_revision(tx, scope, "relationship", subject, now)
            if revision != expected_revision + 1:
                raise ScopedSoulScopeError("relationship_revision_conflict")
            tx.execute(
                "UPDATE scoped_soul_relationship_values SET relationship_revision=? WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?",
                (revision, *_scope_params(scope), subject),
            )
            tx.execute(
                """UPDATE scoped_soul_relationship_values
                      SET manual_adjustment=?, manual_override=?, effective_value=?,
                          relationship_revision=?, evidence=?, updated_at=?
                    WHERE bot_id=? AND session_id=? AND visibility=?
                      AND subject_principal_id=? AND dimension=?""",
                (adjustment, override, effective, revision, encoded_evidence, now,
                 *_scope_params(scope), subject, dimension),
            )
            values_after = self._value_rows(tx, scope, subject)
            dimensions = {key: item["effective_value"] for key, item in values_after.items()}
            affinity = _compute_affinity(dimensions)
            rel_row = self._relationship_row(tx, scope, subject)
            existing_rel_evidence = rel_row[4] if rel_row is not None and len(rel_row) > 4 else None
            relationship_evidence = _merge_relationship_evidence(
                existing_rel_evidence,
                [{"calibration_operation_id": operation_id}, *list(evidence)],
            )
            tx.execute(
                """UPDATE scoped_soul_relationships
                      SET affinity=?, state=?, dimensions=?, revision=?, evidence=?, updated_at=?
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                      AND revision=?""",
                (affinity, _state_for_affinity(affinity), _json_mapping(dimensions), revision,
                 _json_evidence(relationship_evidence), now, *_scope_params(scope), subject, expected_revision),
            )
            after = dict(values_after[dimension])
            return {
                "subject_principal_id": subject,
                "dimension": dimension,
                "action": normalized_action,
                "before": before,
                "after": after,
                "affinity": affinity,
                "state": _state_for_affinity(affinity),
                "revision": revision,
                "evidence": list(evidence),
            }

        return dict(self._write(persist, connection))

    def list_relationship_history(
        self,
        scope: RuntimeScope,
        *,
        subject_principal_id: str | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return auditable automatic/manual relationship transitions for one subject."""
        scope = _require_scope(scope)
        subject = subject_principal_id or scope.subject_principal_id
        if subject is None:
            return {"items": [], "total": 0, "revision": None}
        subject = _exact_string(subject, "subject_principal_id")
        if scope.subject_principal_id is not None and scope.subject_principal_id != subject:
            raise ScopedSoulScopeError("scope_subject_mismatch")
        if limit not in {25, 50, 100} or offset < 0:
            raise ValueError("invalid_pagination")
        if from_ts is not None and not math.isfinite(float(from_ts)):
            raise ValueError("invalid_from_ts")
        if to_ts is not None and not math.isfinite(float(to_ts)):
            raise ValueError("invalid_to_ts")
        if from_ts is not None and to_ts is not None and float(from_ts) > float(to_ts):
            raise ValueError("invalid_time_range")

        def time_filter(column: str) -> tuple[str, list[Any]]:
            clauses = []
            values: list[Any] = []
            if from_ts is not None:
                clauses.append(f" AND {column}>=?")
                values.append(float(from_ts))
            if to_ts is not None:
                clauses.append(f" AND {column}<=?")
                values.append(float(to_ts))
            return "".join(clauses), values

        scope_params = (*_scope_params(scope), subject)
        auto_time, auto_times = time_filter("created_at")
        manual_time, manual_times = time_filter("created_at")
        noise_types = tuple(sorted(NOISY_EVENT_TYPES))
        noise_reasons = tuple(sorted(NOISY_EVENT_REASONS))
        type_placeholders = ",".join("?" for _ in noise_types) or "NULL"
        reason_placeholders = ",".join("?" for _ in noise_reasons) or "NULL"
        noise_filter = (
            f" AND event_type NOT IN ({type_placeholders})"
            f" AND COALESCE(reason, '') NOT IN ({reason_placeholders})"
        )
        auto_count = int(self.cm.execute_read(
            f"""SELECT COUNT(*)
                    FROM scoped_soul_relationship_events
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                         {auto_time}{noise_filter}""",
            (*scope_params, *auto_times, *noise_types, *noise_reasons),
        ).fetchone()[0])
        manual_count = int(self.cm.execute_read(
            f"""SELECT COUNT(*)
                    FROM scoped_soul_relationship_calibration_events
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                         {manual_time}""",
            (*scope_params, *manual_times),
        ).fetchone()[0])
        history_window = int(offset + limit)
        auto_rows = self.cm.execute_read(
            f"""SELECT id, event_type, dimension, delta, reason, source_episode_id,
                         source_memory_id, revision, created_at, operation_id, evidence,
                         value_layer, before_json, after_json
                    FROM scoped_soul_relationship_events
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                         {auto_time}{noise_filter}
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
            (*scope_params, *auto_times, *noise_types, *noise_reasons, history_window),
        ).fetchall()
        manual_rows = self.cm.execute_read(
            f"""SELECT calibration_id, operation_id, dimension, action, reason, evidence,
                         relationship_revision, created_at, before_json, after_json, actor
                    FROM scoped_soul_relationship_calibration_events
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                         {manual_time}
                   ORDER BY created_at DESC, calibration_id DESC LIMIT ?""",
            (*scope_params, *manual_times, history_window),
        ).fetchall()
        auto_revision = self.cm.execute_read(
            f"""SELECT MAX(revision)
                    FROM scoped_soul_relationship_events
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                         {auto_time}{noise_filter}""",
            (*scope_params, *auto_times, *noise_types, *noise_reasons),
        ).fetchone()[0]
        manual_revision = self.cm.execute_read(
            f"""SELECT MAX(relationship_revision)
                    FROM scoped_soul_relationship_calibration_events
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                         {manual_time}""",
            (*scope_params, *manual_times),
        ).fetchone()[0]

        def decoded(value: Any) -> Any:
            if value is None:
                return None
            try:
                return json.loads(str(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None

        def source_ids(evidence: Any) -> tuple[Any, Any]:
            memory_id = None
            episode_id = None
            if isinstance(evidence, list):
                for item in evidence:
                    if not isinstance(item, Mapping):
                        continue
                    if memory_id is None and item.get("memory_id") is not None:
                        memory_id = item.get("memory_id")
                    if episode_id is None and item.get("episode_id") is not None:
                        episode_id = item.get("episode_id")
            return memory_id, episode_id

        items: list[dict[str, Any]] = []
        for row in auto_rows:
            evidence = decoded(row[10])
            if not isinstance(evidence, list):
                evidence = []
            memory_id, episode_id = source_ids(evidence)
            if is_noisy_relationship_event(row[1], row[4]):
                continue
            items.append({
                "id": f"relationship-event:{row[0]}",
                "event_id": row[0],
                "kind": "automatic",
                "event_type": row[1],
                "action": None,
                "dimension": row[2],
                "delta": row[3],
                "reason": row[4],
                "source_episode_id": row[5] if row[5] is not None else episode_id,
                "source_memory_id": row[6] if row[6] is not None else memory_id,
                "revision": row[7],
                "timestamp": row[8],
                "operation_id": row[9],
                "actor": None,
                "value_layer": row[11] or "automatic",
                "before": decoded(row[12]),
                "after": decoded(row[13]),
                "evidence": evidence,
            })
        for row in manual_rows:
            evidence = decoded(row[5])
            if not isinstance(evidence, list):
                evidence = []
            memory_id, episode_id = source_ids(evidence)
            items.append({
                "id": f"relationship-calibration:{row[0]}",
                "event_id": row[0],
                "kind": "manual",
                "event_type": "relationship.manual_calibration",
                "action": row[3],
                "dimension": row[2],
                "delta": None,
                "reason": row[4],
                "source_episode_id": episode_id,
                "source_memory_id": memory_id,
                "revision": row[6],
                "timestamp": row[7],
                "operation_id": row[1],
                "actor": row[10],
                "value_layer": "manual",
                "before": decoded(row[8]),
                "after": decoded(row[9]),
                "evidence": evidence,
            })
        items.sort(key=lambda item: (float(item.get("timestamp") or 0), str(item.get("id") or "")), reverse=True)
        total = auto_count + manual_count
        revision_values = [int(value) for value in (auto_revision, manual_revision) if value is not None]
        return {"items": items[offset:offset + limit], "total": total,
                "revision": max(revision_values, default=None)}

    def list_legacy_relationship_audit_summary(
        self,
        scope: RuntimeScope,
        *,
        subject_principal_id: str | None = None,
        recent_limit: int = 5,
    ) -> dict[str, Any]:
        """Read-only summary of historical legacy events (audit table only).

        Never mutates formal affinity/values. Missing table returns empty summary.
        """
        scope = _require_scope(scope)
        subject = subject_principal_id or scope.subject_principal_id
        if subject is None:
            return {"total": 0, "by_type": [], "recent": [], "available": False}
        subject = _exact_string(subject, "subject_principal_id")
        if scope.subject_principal_id is not None and scope.subject_principal_id != subject:
            raise ScopedSoulScopeError("scope_subject_mismatch")
        recent_limit = max(1, min(int(recent_limit or 5), 20))

        tables = {
            str(row[0])
            for row in self.cm.execute_read(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "scoped_soul_relationship_legacy_events" not in tables:
            return {"total": 0, "by_type": [], "recent": [], "available": False}

        scope_params = (*_scope_params(scope), subject)
        total = int(
            self.cm.execute_read(
                """SELECT COUNT(*)
                     FROM scoped_soul_relationship_legacy_events
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
                scope_params,
            ).fetchone()[0]
        )
        by_type_rows = self.cm.execute_read(
            """SELECT event_type, COUNT(*) AS n
                 FROM scoped_soul_relationship_legacy_events
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                GROUP BY event_type
                ORDER BY n DESC, event_type ASC""",
            scope_params,
        ).fetchall()
        recent_rows = self.cm.execute_read(
            """SELECT event_type, dimension, delta, reason, occurred_at, legacy_event_id
                 FROM scoped_soul_relationship_legacy_events
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                ORDER BY COALESCE(occurred_at, 0) DESC, id DESC
                LIMIT ?""",
            (*scope_params, recent_limit),
        ).fetchall()
        return {
            "available": True,
            "total": total,
            "by_type": [
                {"event_type": str(row[0] or ""), "count": int(row[1] or 0)}
                for row in by_type_rows
            ],
            "recent": [
                {
                    "event_type": str(row[0] or ""),
                    "dimension": str(row[1] or ""),
                    "delta": row[2],
                    "reason": str(row[3] or ""),
                    "occurred_at": row[4],
                    "legacy_event_id": str(row[5] or ""),
                }
                for row in recent_rows
            ],
        }

    def list_relationships(self, scope: RuntimeScope, *, subject_principal_id: str | None = None) -> list[dict[str, Any]]:
        scope = _require_scope(scope)
        subject = subject_principal_id or scope.subject_principal_id
        params: list[Any] = [*_scope_params(scope)]
        predicate = "bot_id=? AND session_id=? AND visibility=?"
        if subject is not None:
            subject = _exact_string(subject, "subject_principal_id")
            if scope.subject_principal_id is not None and scope.subject_principal_id != subject:
                raise ScopedSoulScopeError("scope_subject_mismatch")
            predicate += " AND subject_principal_id=?"
            params.append(subject)
        rows = self.cm.execute_read(
            f"""SELECT subject_principal_id, affinity, state, dimensions, revision, evidence, updated_at
                   FROM scoped_soul_relationships WHERE {predicate}
                  ORDER BY subject_principal_id""",
            params,
        ).fetchall()
        result = []
        for row in rows:
            subject_id = str(row[0])
            value_rows = self.cm.execute_read(
                """SELECT dimension, automatic_value, manual_adjustment, manual_override,
                          effective_value, relationship_revision, evidence, updated_at
                     FROM scoped_soul_relationship_values
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                    ORDER BY dimension""",
                (*_scope_params(scope), subject_id),
            ).fetchall()
            values = {
                str(item[0]): {
                    "dimension": str(item[0]),
                    "automatic_value": float(item[1]),
                    "manual_adjustment": None if item[2] is None else float(item[2]),
                    "manual_override": None if item[3] is None else float(item[3]),
                    "effective_value": float(item[4]),
                    "relationship_revision": int(item[5]),
                    "evidence": json.loads(str(item[6] or "[]")),
                    "updated_at": float(item[7]),
                }
                for item in value_rows
            }
            evidence = json.loads(str(row[5] or "[]"))
            result.append({
                "subject_principal_id": subject_id,
                "affinity": int(row[1]),
                "state": str(row[2]),
                "dimensions": json.loads(str(row[3] or "{}")),
                "revision": int(row[4]),
                "evidence": evidence,
                "evidence_summaries": _evidence_summaries(evidence),
                "updated_at": float(row[6]),
                "values": values,
                "calibration": {"available": bool(values), "reason_code": None if values else "relationship_values_unknown"},
            })
        return result

    def get_state(
        self,
        scope: RuntimeScope,
        *,
        subject_principal_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> dict[str, Any]:
        scope = _require_scope(scope)
        if limit not in {25, 50, 100} or offset < 0:
            raise ValueError("invalid_pagination")
        if from_ts is not None and not math.isfinite(float(from_ts)):
            raise ValueError("invalid_from_ts")
        if to_ts is not None and not math.isfinite(float(to_ts)):
            raise ValueError("invalid_to_ts")
        if from_ts is not None and to_ts is not None and float(from_ts) > float(to_ts):
            raise ValueError("invalid_time_range")
        subject = subject_principal_id or scope.subject_principal_id
        if subject is not None:
            subject = _exact_string(subject, "subject_principal_id")
            if scope.subject_principal_id is not None and scope.subject_principal_id != subject:
                raise ScopedSoulScopeError("scope_subject_mismatch")
        params = _scope_params(scope)
        if from_ts is not None and to_ts is not None and float(from_ts) > float(to_ts):
            raise ValueError("invalid_time_range")
        concern_time = ""
        concern_time_params: list[Any] = []
        timeline_time = ""
        timeline_time_params: list[Any] = []
        timeline_subject = ""
        timeline_subject_params: list[Any] = []
        if subject is not None:
            timeline_subject = " AND (subject_principal_id IS NULL OR subject_principal_id=?)"
            timeline_subject_params.append(subject)
        if from_ts is not None:
            concern_time += " AND last_triggered>=?"
            concern_time_params.append(float(from_ts))
            timeline_time += " AND occurred_at>=?"
            timeline_time_params.append(float(from_ts))
        if to_ts is not None:
            concern_time += " AND last_triggered<=?"
            concern_time_params.append(float(to_ts))
            timeline_time += " AND occurred_at<=?"
            timeline_time_params.append(float(to_ts))
        mood_row = self.cm.execute_read(
            """SELECT valence, arousal, cause, policy_version, revision, evidence, observed_at
               FROM scoped_soul_mood WHERE bot_id=? AND session_id=? AND visibility=?""",
            params,
        ).fetchone()
        if mood_row:
            mood = {
                "value": mood_row[0],
                "state": "known",
                "components": {"valence": mood_row[0], "arousal": mood_row[1]},
                "cause": mood_row[2],
                "policy_version": mood_row[3],
                "revision": mood_row[4],
                "evidence": json.loads(mood_row[5]),
                "observed_at": mood_row[6],
            }
        else:
            mood = {"value": None, "state": "unknown", "components": None,
                    "policy_version": None, "revision": None, "evidence": []}

        concern_total = int(self.cm.execute_read(
            f"SELECT COUNT(*) FROM scoped_soul_concerns WHERE bot_id=? AND session_id=? AND visibility=?{concern_time}",
            (*params, *concern_time_params),
        ).fetchone()[0])
        concern_rows = self.cm.execute_read(
            f"""SELECT id, topic, intensity, origin_memory_id, origin_episode_id, concern_type,
                      status, urgency, last_progress_at, expected_resolution_at, resolution_note,
                      created_at, last_triggered, revision, evidence
               FROM scoped_soul_concerns
               WHERE bot_id=? AND session_id=? AND visibility=?{concern_time}
               ORDER BY intensity DESC, last_triggered DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, *concern_time_params, limit, offset),
        ).fetchall()
        concerns = [{
            "id": row[0], "topic": row[1], "intensity": row[2],
            "origin_memory_id": row[3], "origin_episode_id": row[4],
            "concern_type": row[5] or "", "status": row[6] or "active",
            "urgency": row[7] or 0, "last_progress_at": row[8],
            "expected_resolution_at": row[9], "resolution_note": row[10] or "",
            "created_at": row[11], "last_triggered": row[12],
            "revision": row[13], "evidence": json.loads(row[14]),
        } for row in concern_rows]

        timeline_total = int(self.cm.execute_read(
            f"SELECT COUNT(*) FROM scoped_soul_timeline WHERE bot_id=? AND session_id=? AND visibility=?{timeline_subject}{timeline_time}",
            (*params, *timeline_subject_params, *timeline_time_params),
        ).fetchone()[0])
        timeline_rows = self.cm.execute_read(
            f"""SELECT id, subject_principal_id, event_summary, event_type, emotional_weight,
                      occurred_at, revision, evidence
               FROM scoped_soul_timeline
               WHERE bot_id=? AND session_id=? AND visibility=?{timeline_subject}{timeline_time}
               ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, *timeline_subject_params, *timeline_time_params, limit, offset),
        ).fetchall()
        timeline = [{
            "id": row[0], "subject_principal_id": row[1], "event_summary": row[2],
            "event_type": row[3], "emotional_weight": row[4], "timestamp": row[5],
            "revision": row[6], "evidence": json.loads(row[7]),
        } for row in timeline_rows]

        relationship = {"affinity": None, "state": "unknown", "revision": None,
                        "evidence": [], "evidence_summaries": [], "people_ref": None, "dimensions": None,
                        "values": None, "calibration": {"available": False, "reason_code": "relationship_unknown"}}
        if subject is not None:
            row = self.cm.execute_read(
                """SELECT affinity, state, dimensions, revision, evidence, updated_at
                   FROM scoped_soul_relationships
                   WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
                (*params, subject),
            ).fetchone()
            if row:
                value_rows = self.cm.execute_read(
                    """SELECT dimension, automatic_value, manual_adjustment, manual_override,
                              effective_value, relationship_revision, evidence, updated_at
                         FROM scoped_soul_relationship_values
                        WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                        ORDER BY dimension""",
                    (*params, subject),
                ).fetchall()
                values = {
                    str(item[0]): {
                        "dimension": str(item[0]),
                        "automatic_value": float(item[1]),
                        "manual_adjustment": None if item[2] is None else float(item[2]),
                        "manual_override": None if item[3] is None else float(item[3]),
                        "effective_value": float(item[4]),
                        "relationship_revision": int(item[5]),
                        "evidence": json.loads(str(item[6] or "[]")),
                        "updated_at": float(item[7]),
                    }
                    for item in value_rows
                }
                rel_evidence = json.loads(str(row[4] or "[]"))
                relationship = {
                    "affinity": row[0], "state": row[1], "dimensions": json.loads(str(row[2] or "{}")),
                    "revision": row[3], "evidence": rel_evidence,
                    "evidence_summaries": _evidence_summaries(rel_evidence),
                    "updated_at": row[5], "people_ref": subject, "values": values,
                    "calibration": {"available": bool(values), "reason_code": None if values else "relationship_values_unknown"},
                }

        relationship_history = self.list_relationship_history(
            scope,
            subject_principal_id=subject,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            offset=offset,
        )
        # Side-channel only: never mixes into relationship_history / affinity.
        if subject is not None:
            historical_audit = self.list_legacy_relationship_audit_summary(
                scope,
                subject_principal_id=subject,
                recent_limit=5,
            )
            if isinstance(historical_audit, dict):
                historical_audit = {
                    **historical_audit,
                    "readonly": True,
                    "affects_affinity": False,
                    "source_table": "scoped_soul_relationship_legacy_events",
                }
        else:
            historical_audit = {
                "available": False,
                "total": 0,
                "by_type": [],
                "recent": [],
                "readonly": True,
                "affects_affinity": False,
                "reason_code": "relationship_subject_required",
            }
        revision_params: list[Any] = list(params)
        revision_where = "bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=''"
        if subject is not None:
            revision_where = "bot_id=? AND session_id=? AND visibility=? AND (subject_principal_id='' OR subject_principal_id=?)"
            revision_params.append(subject)
        revision_row = self.cm.execute_read(
            f"SELECT COALESCE(MAX(revision), 0) FROM scoped_soul_revisions WHERE {revision_where}",
            revision_params,
        ).fetchone()
        revision = int(revision_row[0] or 0)
        aggregate_evidence = []
        for component in (mood, relationship, *concerns, *timeline):
            for item in component.get("evidence", []):
                if item not in aggregate_evidence:
                    aggregate_evidence.append(item)
        return {
            "revision": revision,
            "evidence": aggregate_evidence,
            "mood": mood,
            "concerns": {"items": concerns, "total": concern_total, "revision": max((item["revision"] for item in concerns), default=None)},
            "timeline": {"items": timeline, "total": timeline_total, "revision": max((item["revision"] for item in timeline), default=None)},
            "relationship": relationship,
            "relationship_history": relationship_history,
            "historical_audit": historical_audit,
            "soul_context": resolve_soul_context(
                self.soul_context_provider,
                scope=scope,
            ),
        }

    def refresh_state(
        self,
        scope: RuntimeScope,
        *,
        subject_principal_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> dict[str, Any]:
        """显式触发的自省重算入口（WebUI「强制自省」）。

        与 get_state 同为只读投影：不写数据、不调用 LLM、不新增事件，revision 不变。
        保留独立方法作为刷新语义接缝，未来可在此接入缓存失效或重调度；
        任何实现都不得把该入口变成写路径。
        """
        return self.get_state(
            scope,
            subject_principal_id=subject_principal_id,
            limit=limit,
            offset=offset,
            from_ts=from_ts,
            to_ts=to_ts,
        )


__all__ = ["ScopedSoulRepository", "ScopedSoulScopeError"]
