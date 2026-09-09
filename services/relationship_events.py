"""Relationship event service — traceable affinity updates."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

try:  # 兼容插件包导入和仓库测试直接导入
    from ..domain.relationship_policy import (
        DIMENSION_RANGES,
        DIMENSION_WEIGHTS,
        HOSTILITY_WEIGHT,
        VALID_DIMENSIONS,
        VALID_EVENT_TYPES,
        attitude_level,
        compute_affinity,
        is_noisy_relationship_event,
    )
    from ..domain.scope import RuntimeScope, ScopeValidationError
except ImportError:  # pragma: no cover - 由仓库测试直接导入 services 使用
    from domain.relationship_policy import (
        DIMENSION_RANGES,
        DIMENSION_WEIGHTS,
        HOSTILITY_WEIGHT,
        VALID_DIMENSIONS,
        VALID_EVENT_TYPES,
        attitude_level,
        compute_affinity,
        is_noisy_relationship_event,
    )
    from domain.scope import RuntimeScope, ScopeValidationError


# 保留旧模块的公开常量名称，同时把正式写入、旧数据审计和工具统一到
# domain.relationship_policy，防止五维定义再次漂移。
DIM_RANGES = dict(DIMENSION_RANGES)
VALID_DIMENSIONS = set(VALID_DIMENSIONS)
VALID_EVENT_TYPES = set(VALID_EVENT_TYPES)
DEFAULT_DIMS = {name: 0.0 for name in DIM_RANGES}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_affection(dims: dict[str, float]) -> int:
    """Legacy-compatible alias for the single formal Affinity formula."""
    return compute_affinity(dims)


def get_attitude_level(affection: int) -> str:
    """Legacy-compatible alias for the single formal attitude policy."""
    return attitude_level(affection)


def _project_group_subject_scope(scope: RuntimeScope) -> tuple[str, str, str]:
    """Project an already-resolved group RuntimeScope into legacy relationship keys.

    This boundary never builds a Scope from bot/group/user strings.  Legacy callers
    may still supply those fields while their explicit projection adapters remain,
    but new callers must give the canonical Scope object.
    """
    if not isinstance(scope, RuntimeScope):
        raise ScopeValidationError("scope_required", "relationship event requires RuntimeScope")
    if scope.visibility != "group" or scope.session is None:
        raise ScopeValidationError(
            "scope_visibility_not_allowed",
            "relationship events currently require a group RuntimeScope",
        )
    principal = scope.subject_principal_id or ""
    prefix = f"{scope.session.platform_id}:user:"
    if not principal.startswith(prefix) or principal == prefix:
        raise ScopeValidationError(
            "scope_subject_required",
            "relationship event target must be a scoped platform user",
        )
    return scope.bot_id, scope.session.conversation_id, principal[len(prefix):]


@dataclass
class RelationshipEventResult:
    event_id: int
    bot_id: str
    group_id: str
    user_id: str
    dimension: str
    requested_delta: float
    applied_delta: float
    before_affection: int
    after_affection: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "bot_id": self.bot_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "dimension": self.dimension,
            "requested_delta": self.requested_delta,
            "applied_delta": self.applied_delta,
            "before_affection": self.before_affection,
            "after_affection": self.after_affection,
            "reason": self.reason,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


class RelationshipEventService:
    """Records relationship events and keeps user_profiles current state in sync."""

    def __init__(
        self,
        conn,
        single_delta_cap: float = 5,
        daily_delta_cap: float = 15,
        hostility_delta_cap: float = 8,
        target_profiles: dict[str, dict[str, str]] | None = None,
        repository=None,
        coordinator=None,
    ):
        self.conn = conn
        self.single_delta_cap = abs(float(single_delta_cap))
        self.daily_delta_cap = abs(float(daily_delta_cap))
        self.hostility_delta_cap = abs(float(hostility_delta_cap))
        self.target_profiles = target_profiles or {}
        self.repository = repository
        self.coordinator = coordinator

    def record_event(
        self,
        *,
        bot_id: str | None = None,
        group_id: str | None = None,
        user_id: str | None = None,
        scope: RuntimeScope | None = None,
        event_type: str,
        dimension: str,
        delta: float,
        reason: str,
        source_episode_id: int | None = None,
        source_memory_id: int | None = None,
        created_at: float | None = None,
    ) -> RelationshipEventResult:
        if scope is None:
            raise ScopeValidationError(
                "scope_required",
                "relationship event writes require a canonical RuntimeScope",
            )
        if self.repository is None:
            raise ScopeValidationError(
                "scoped_repository_required",
                "relationship event writes require the scoped repository",
            )
        legacy_bot_id = (bot_id or "").strip()
        legacy_group_id = (group_id or "").strip()
        legacy_user_id = (user_id or "").strip()
        scoped_bot_id, scoped_group_id, scoped_user_id = _project_group_subject_scope(scope)
        supplied = (legacy_bot_id, legacy_group_id, legacy_user_id)
        projected = (scoped_bot_id, scoped_group_id, scoped_user_id)
        if any(value for value in supplied) and supplied != projected:
            raise ScopeValidationError(
                "scope_legacy_mismatch",
                "relationship legacy keys must match the supplied RuntimeScope",
            )
        bot_id, group_id, user_id = projected
        event_type = (event_type or "").strip()
        dimension = (dimension or "").strip()
        reason = (reason or "").strip()
        if not bot_id or not group_id or not user_id:
            raise ValueError("bot_id, group_id and user_id are required")
        if dimension not in VALID_DIMENSIONS:
            raise ValueError(f"invalid relationship dimension: {dimension}")
        if event_type not in VALID_EVENT_TYPES or is_noisy_relationship_event(event_type, reason):
            raise ValueError(f"invalid relationship event_type: {event_type}")
        if not reason:
            raise ValueError("relationship event reason is required")

        now = float(created_at or time.time())
        requested_delta = float(delta)
        kwargs = {
            "event_type": event_type,
            "dimension": dimension,
            "delta": requested_delta,
            "reason": reason,
            "source_episode_id": source_episode_id,
            "source_memory_id": source_memory_id,
            "created_at": now,
        }
        if self.coordinator is not None:
            stored = self.coordinator.transaction_blocking(
                lambda connection: self.repository.record_relationship_event(
                    scope, connection=connection, **kwargs
                )
            )
        else:
            stored = self.repository.record_relationship_event(scope, **kwargs)

        if not stored or not isinstance(stored, dict):
            return RelationshipEventResult(
                event_id=0,
                bot_id=bot_id,
                group_id=group_id,
                user_id=user_id,
                dimension=dimension,
                requested_delta=requested_delta,
                applied_delta=0.0,
                before_affection=0,
                after_affection=0,
                reason=reason,
            )

        return RelationshipEventResult(
            event_id=int(stored["event_id"]),
            bot_id=bot_id,
            group_id=group_id,
            user_id=user_id,
            dimension=str(stored["dimension"]),
            requested_delta=float(stored["requested_delta"]),
            applied_delta=float(stored["applied_delta"]),
            before_affection=int(stored["before_affinity"]),
            after_affection=int(stored["after_affinity"]),
            reason=str(stored["reason"]),
        )

    def recent_events(self, bot_id: str, user_id: str, group_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id, event_type, dimension, delta, reason, created_at
               FROM relationship_events
               WHERE bot_id=? AND user_id=? AND group_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (bot_id, user_id, group_id, int(limit)),
        ).fetchall()
        return [
            {
                "id": r[0],
                "event_type": r[1],
                "dimension": r[2],
                "delta": r[3],
                "reason": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def _constrain_delta(
        self,
        *,
        bot_id: str,
        group_id: str,
        user_id: str,
        dimension: str,
        event_type: str,
        delta: float,
        now: float,
    ) -> float:
        cap = self.hostility_delta_cap if dimension == "hostility" and delta > 0 else self.single_delta_cap
        delta = _clamp(delta, -cap, cap)
        day_start = now - 86400
        row = self.conn.execute(
            """SELECT COALESCE(SUM(delta), 0) FROM relationship_events
               WHERE bot_id=? AND group_id=? AND user_id=? AND dimension=? AND created_at >= ?""",
            (bot_id, group_id, user_id, dimension, day_start),
        ).fetchone()
        daily_total = float(row[0] or 0) if row else 0.0
        if delta > 0:
            remaining = self.daily_delta_cap - max(daily_total, 0)
            delta = min(delta, max(0.0, remaining))
        elif delta < 0:
            remaining = self.daily_delta_cap - max(-daily_total, 0)
            delta = max(delta, -max(0.0, remaining))
        return round(delta, 2)

    def _load_dimensions(self, user_id: str, group_id: str, bot_id: str) -> tuple[dict[str, float], int, dict[str, Any]]:
        row = self.conn.execute(
            "SELECT affection, metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
            (user_id, group_id, bot_id),
        ).fetchone()
        meta: dict[str, Any] = {}
        before_affection = 0
        if row:
            before_affection = int(row[0] or 0)
            if row[1]:
                try:
                    meta = json.loads(row[1])
                except Exception:
                    meta = {"legacy_metadata_raw": row[1]}
        dims_raw = meta.get("dimensions") or {}
        dims = {k: float(dims_raw.get(k, DEFAULT_DIMS[k])) for k in DEFAULT_DIMS}
        return dims, before_affection, meta
