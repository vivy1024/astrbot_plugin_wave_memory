"""Explicit authorization policy for QueryEngine memory recall.

This policy deliberately governs read selection only.  It does not alter RuntimeScope
or any write boundary: a resolved group Scope authorizes cross-group recall only when
the caller explicitly enables it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - plugin loaded as a top-level module
    from domain.scope import RuntimeScope


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in _TRUE_VALUES
    return bool(value)


@dataclass(frozen=True)
class RecallPolicy:
    """One query's authorized recall envelope and safe-touch selection.

    A resolved group Scope is always required.  With ``cross_group_enabled`` the
    repositories may expand *reads* to group-visible formal memories and fully
    unscoped legacy memories; mutations remain limited to returned rows from the
    current group so an exact-scope write command is never forged for another Scope.

    ``shared_grants_enabled`` is a *narrower* opt-in: only memory_ids with an
    active ``shared_memory_grants`` row for the consumer Scope may be read across
    groups.  It never authorizes writes or touch on foreign rows, and it is not
    physical fanout.

    ``private_subject_group_recall`` 只用于私聊：私聊对象本人在群里公开说过的话
    可以被读取（像真人一样记得对方在群里聊过什么），私聊内容本身仍不外流到任何群。
    """

    scope: RuntimeScope
    cross_group_enabled: bool = False
    shared_grants_enabled: bool = False
    granted_memory_ids: tuple[int, ...] = ()
    private_subject_group_recall: bool = False

    @classmethod
    def from_config(cls, scope: RuntimeScope, config: Mapping[str, Any]) -> "RecallPolicy":
        # A private session is an exact owner boundary, never a group-sharing
        # envelope. Do not let persisted/global switches reopen legacy, grant, or
        # cross-group lanes for a private query. 唯一例外是跨群开关开启时，
        # 读取私聊对象本人的公开群发言。
        if scope.visibility == "private":
            return cls(
                scope=scope,
                private_subject_group_recall=_enabled(config.get("cross_group_enabled", False)),
            )
        return cls(
            scope=scope,
            cross_group_enabled=_enabled(config.get("cross_group_enabled", False)),
            shared_grants_enabled=_enabled(config.get("shared_memory_grants_enabled", False)),
            granted_memory_ids=(),
        )

    def with_granted_memory_ids(self, memory_ids: list[int] | tuple[int, ...] | set[int]) -> "RecallPolicy":
        """Return a copy with an explicit grant allow-list (read path only)."""
        if self.scope.visibility == "private":
            return self
        cleaned: list[int] = []
        seen: set[int] = set()
        for raw in memory_ids or ():
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                continue
            if mid <= 0 or mid in seen:
                continue
            seen.add(mid)
            cleaned.append(mid)
            if len(cleaned) >= 5000:
                break
        return RecallPolicy(
            scope=self.scope,
            cross_group_enabled=self.cross_group_enabled,
            shared_grants_enabled=self.shared_grants_enabled,
            granted_memory_ids=tuple(cleaned),
        )

    @property
    def current_group_id(self) -> str:
        assert self.scope.session is not None
        return self.scope.session.conversation_id

    def is_cross_group(self, memory: Mapping[str, Any]) -> bool:
        """Classify presentation/touch eligibility solely from persisted group_id."""
        return str(memory.get("group_id") or "") != self.current_group_id

    def is_shared_grant(self, memory: Mapping[str, Any]) -> bool:
        if memory.get("_shared_grant") or memory.get("shared_grant"):
            return True
        try:
            mid = int(memory.get("id"))
        except (TypeError, ValueError):
            return False
        return mid in set(self.granted_memory_ids)

    def touchable_ids(self, memories: list[Mapping[str, Any]]) -> list[int]:
        """Return rows safe for the caller's exact-Scope access-count mutation.

        Production rows always carry a complete owner tuple after repository
        filtering, so a different Bot/session is excluded even when it happens to
        share the same group.  Focused legacy test doubles may omit ownership fields;
        for that compatibility contract, only a result explicitly marked as another
        group is withheld.  Real partial Scope rows never reach this method.

        Shared-grant and cross-group rows are never touchable.
        """
        assert self.scope.session is not None
        owner = (self.scope.bot_id, self.scope.session.id, self.scope.visibility)
        result: list[int] = []
        for memory in memories:
            if self.is_shared_grant(memory) or self.is_cross_group(memory):
                continue
            group_value = memory.get("group_id")
            if group_value is not None and str(group_value or "") != self.current_group_id:
                continue
            memory_owner = (
                str(memory.get("bot_id") or ""),
                str(memory.get("session_id") or ""),
                str(memory.get("visibility") or ""),
            )
            if any(memory_owner) and memory_owner != owner:
                continue
            try:
                memory_id = int(memory["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if memory_id > 0:
                result.append(memory_id)
        return result
