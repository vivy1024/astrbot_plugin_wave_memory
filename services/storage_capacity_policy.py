"""Canonical SQLite capacity policy for new memory writes.

This is deliberately *not* a hot-HNSW policy.  ``max_memories`` historically
looked like a database cap but only sized the index.  The policy below makes a
real, write-path decision:

- Durable sources always keep their vectors.
- When the live active row count is at/over the configured cap, ordinary chat
  writes stay in SQLite as text-only cold rows (vector=NULL) instead of growing
  the embedding store.
- Historical rows are never deleted here.
"""

from __future__ import annotations

try:
    from ..domain import bot_identity
except ImportError:  # top-level import in isolated tests
    from domain import bot_identity

from dataclasses import dataclass
from typing import Any, Mapping

# Sources that must not lose vectors under capacity pressure.
_DURABLE_SOURCES = frozenset({
    "core",
    "explicit",
    "book_lore",
    "oni_lore",
    "knowledge",
    "experience",
})

# Rows counted against the live soft cap.  Quarantine/noise/deleted are excluded.
_ACTIVE_COUNT_SQL = """
SELECT COUNT(*) FROM memories
 WHERE COALESCE(quarantine, 0)=0
   AND COALESCE(memory_type, 'message') NOT IN ('deleted', 'archived')
   AND COALESCE(source, '') NOT IN ('noise', 'identity_quarantine', 'persona_quarantine',
                                    'persona_context_quarantine')
"""


@dataclass(frozen=True, slots=True)
class StorageCapacityPolicy:
    """Soft canonical-store capacity for write-path admission."""

    max_active_memories: int = 100_000
    # When True and over cap, chat writes persist without embedding bytes.
    cold_chat_when_over_capacity: bool = True
    enabled: bool = True

    @classmethod
    def from_settings(cls, section: Mapping[str, Any] | None) -> "StorageCapacityPolicy":
        settings = section if isinstance(section, Mapping) else {}
        try:
            max_active = max(1, int(float(settings.get("max_memories", 100_000))))
        except (TypeError, ValueError):
            max_active = 100_000
        raw_enabled = settings.get("canonical_capacity_enabled")
        if raw_enabled is None:
            enabled = True
        elif isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().casefold() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(raw_enabled)
        raw_cold = settings.get("cold_chat_when_over_capacity")
        if raw_cold is None:
            cold = True
        elif isinstance(raw_cold, str):
            cold = raw_cold.strip().casefold() in {"1", "true", "yes", "on"}
        else:
            cold = bool(raw_cold)
        return cls(
            max_active_memories=max_active,
            cold_chat_when_over_capacity=cold,
            enabled=enabled,
        )


@dataclass(frozen=True, slots=True)
class StorageAdmissionDecision:
    """Write-path decision for one memory payload under capacity pressure."""

    keep_vector: bool
    over_capacity: bool
    active_count: int
    reason: str
    source: str

    @property
    def demoted_to_cold(self) -> bool:
        return self.over_capacity and not self.keep_vector


def count_active_memories(connection: Any) -> int:
    """Count live non-noise rows that should compete for the soft store cap."""
    try:
        row = connection.execute(_ACTIVE_COUNT_SQL).fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return 0


def decide_storage_admission(
    *,
    source: str,
    active_count: int,
    policy: StorageCapacityPolicy,
    has_vector: bool = True,
) -> StorageAdmissionDecision:
    """Decide whether a new write may carry an embedding under the soft cap."""
    normalized_source = str(source or "chat").strip().casefold() or "chat"
    if not policy.enabled:
        return StorageAdmissionDecision(
            keep_vector=has_vector,
            over_capacity=False,
            active_count=int(active_count),
            reason="capacity_policy_disabled",
            source=normalized_source,
        )

    over = int(active_count) >= int(policy.max_active_memories)
    if not over:
        return StorageAdmissionDecision(
            keep_vector=has_vector,
            over_capacity=False,
            active_count=int(active_count),
            reason="under_capacity",
            source=normalized_source,
        )

    if normalized_source in _DURABLE_SOURCES or bot_identity.is_experience_source(normalized_source):
        return StorageAdmissionDecision(
            keep_vector=has_vector,
            over_capacity=True,
            active_count=int(active_count),
            reason="durable_source_keeps_vector",
            source=normalized_source,
        )

    if normalized_source == "noise":
        return StorageAdmissionDecision(
            keep_vector=False,
            over_capacity=True,
            active_count=int(active_count),
            reason="noise_never_vectorized",
            source=normalized_source,
        )

    if policy.cold_chat_when_over_capacity and has_vector:
        return StorageAdmissionDecision(
            keep_vector=False,
            over_capacity=True,
            active_count=int(active_count),
            reason="over_capacity_chat_cold",
            source=normalized_source,
        )

    return StorageAdmissionDecision(
        keep_vector=has_vector,
        over_capacity=True,
        active_count=int(active_count),
        reason="over_capacity_keep_vector",
        source=normalized_source,
    )


__all__ = [
    "StorageAdmissionDecision",
    "StorageCapacityPolicy",
    "count_active_memories",
    "decide_storage_admission",
]
