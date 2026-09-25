"""Recoverable outbox consumers for derived WaveMemory state."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

try:
    from ..engine.db import fts_cjk
    from ..engine.db.outbox_repo import OutboxEvent
    from ..engine.vector_index import IndexCapacityError
    from .memory_index_policy import MemoryIndexPolicy, evaluate_memory_eligibility
except ImportError:  # pragma: no cover - repository tests import top-level packages
    from engine.db import fts_cjk
    from engine.db.outbox_repo import OutboxEvent
    from engine.vector_index import IndexCapacityError
    from services.memory_index_policy import MemoryIndexPolicy, evaluate_memory_eligibility


_INACTIVE_MEMORY_TYPES = {"archived", "evicted", "deleted"}


def _readonly_uri(database_path: str) -> str:
    return f"{Path(database_path).resolve().as_uri()}?mode=ro"


def _positive_ints(values: Any) -> list[int]:
    """Decode only positive numeric IDs; correction payloads may carry names."""
    result: list[int] = []
    for value in values or ():
        if isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.append(parsed)
    return result


def _decode_vector(value: Any, dimension: int) -> np.ndarray | None:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        vector = np.frombuffer(value, dtype=np.float32)
    elif isinstance(value, str):
        try:
            vector = np.asarray(json.loads(value), dtype=np.float32)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
        try:
            vector = np.asarray(value, dtype=np.float32)
        except (TypeError, ValueError):
            return None
    vector = vector.reshape(-1)
    if vector.size != int(dimension):
        return None
    return vector


class MemoryIndexProjection:
    """Project policy-admitted canonical memories into the bounded hot HNSW tier."""

    consumer_name = "memory_index"

    def __init__(
        self,
        database_path: str,
        index: Any,
        *,
        policy: MemoryIndexPolicy | None = None,
    ) -> None:
        self.database_path = str(database_path)
        self.index = index
        self.policy = policy or MemoryIndexPolicy()
        self._dirty = False
        self._lock = asyncio.Lock()
        # Keep lane and quota metadata separate: legacy group rows have a stable
        # group compatibility key but deliberately no fabricated formal Scope.
        self._hot_memory_lanes: dict[int, str] = {}
        self._hot_memory_scopes: dict[int, tuple[str, str, str, str]] = {}
        self._scope_counts: dict[tuple[str, str, str, str], int] = {}
        self._membership_loaded = False
        self._membership_safe = True

    @staticmethod
    def _canonical_scope_key(row: tuple[Any, ...]) -> tuple[str, str, str, str] | None:
        bot_id, session_id, visibility, group_id = (str(value or "").strip() for value in row)
        if not bot_id or not session_id or not group_id or visibility not in {"group", "private"}:
            return None
        parts = session_id.split(":", 2)
        if len(parts) != 3 or not parts[0] or parts[1] != visibility or parts[2] != group_id:
            return None
        return bot_id, session_id, visibility, group_id

    @staticmethod
    def _legacy_group_member(row: tuple[Any, ...]) -> bool:
        bot_id, session_id, visibility, group_id = (str(value or "").strip() for value in row)
        return bool(group_id) and not group_id.casefold().startswith("private:") and not any((bot_id, session_id, visibility))

    def _decrement_member(self, memory_id: int) -> None:
        memory_id = int(memory_id)
        self._hot_memory_lanes.pop(memory_id, None)
        scope_key = self._hot_memory_scopes.pop(memory_id, None)
        if scope_key is None:
            return
        remaining = self._scope_counts.get(scope_key, 0) - 1
        if remaining > 0:
            self._scope_counts[scope_key] = remaining
        else:
            self._scope_counts.pop(scope_key, None)

    def _set_member(
        self,
        memory_id: int,
        *,
        lane: str,
        scope_key: tuple[str, str, str, str] | None,
    ) -> None:
        memory_id = int(memory_id)
        previous_lane = self._hot_memory_lanes.get(memory_id)
        previous_scope = self._hot_memory_scopes.get(memory_id)
        if previous_lane == lane and previous_scope == scope_key:
            return
        if previous_lane is not None:
            self._decrement_member(memory_id)
        self._hot_memory_lanes[memory_id] = lane
        if scope_key is not None:
            self._hot_memory_scopes[memory_id] = scope_key
            self._scope_counts[scope_key] = self._scope_counts.get(scope_key, 0) + 1

    def _ensure_membership_cache(self) -> None:
        """Hydrate existing HNSW labels once so per-Scope quotas survive restart."""
        if self._membership_loaded:
            return
        self._membership_loaded = True
        get_ids = getattr(getattr(self.index, "index", None), "get_ids_list", None)
        if not callable(get_ids):
            return
        try:
            ids = [int(value) for value in get_ids() if int(value) > 0]
        except Exception:
            self._membership_safe = False
            return
        if not ids:
            return
        connection = None
        try:
            connection = sqlite3.connect(_readonly_uri(self.database_path), uri=True)
            connection.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
            if not {"id", "group_id"} <= columns:
                self._membership_safe = False
                return
            bot_id = "bot_id" if "bot_id" in columns else "''"
            session_id = "session_id" if "session_id" in columns else "''"
            visibility = "visibility" if "visibility" in columns else "''"
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""SELECT id, {bot_id}, {session_id}, {visibility}, group_id
                          FROM memories WHERE id IN ({placeholders})""",
                    chunk,
                ).fetchall()
                for row in rows:
                    values = tuple(row[1:])
                    scope_key = self._canonical_scope_key(values)
                    if scope_key is not None:
                        self._set_member(int(row[0]), lane="scoped", scope_key=scope_key)
                    elif self._legacy_group_member(values):
                        self._set_member(int(row[0]), lane="legacy_group", scope_key=None)
        except Exception:
            self._membership_safe = False
        finally:
            if connection is not None:
                connection.close()

    def set_hot_membership(self, candidates: Any) -> None:
        """Replace lane/quota membership after an authoritative rebuild."""
        self._hot_memory_lanes.clear()
        self._hot_memory_scopes.clear()
        self._scope_counts.clear()
        for candidate in candidates or ():
            try:
                raw_scope = getattr(candidate, "scope_key", None)
                scope_key = tuple(raw_scope) if raw_scope is not None else None
                if scope_key is not None and len(scope_key) != 4:
                    continue
                self._set_member(
                    int(candidate.memory_id),
                    lane=str(getattr(candidate, "recall_visibility", "scoped")),
                    scope_key=scope_key,  # type: ignore[arg-type]
                )
            except (AttributeError, TypeError, ValueError):
                continue
        self._membership_loaded = True
        self._membership_safe = True

    def _read_memory_admission(self, memory_id: int):
        connection = sqlite3.connect(_readonly_uri(self.database_path), uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
            version = "COALESCE(version, 1)" if "version" in columns else "1"
            row = connection.execute(
                f"SELECT {version} FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None, None
            candidate = evaluate_memory_eligibility(
                connection,
                memory_id,
                self.policy,
                int(self.index.dimension),
            )
            return int(row[0] or 1), candidate
        finally:
            connection.close()

    async def __call__(self, event: OutboxEvent) -> None:
        if event.aggregate_kind != "memory":
            return
        memory_id = int(event.aggregate_id)
        async with self._lock:
            canonical_version, candidate = await asyncio.to_thread(
                self._read_memory_admission,
                memory_id,
            )
            self._ensure_membership_cache()
            if canonical_version is None:
                self._decrement_member(memory_id)
                await asyncio.to_thread(self.index.mark_deleted, [memory_id])
                self._dirty = True
                return
            if int(canonical_version) > int(event.aggregate_version):
                return
            if candidate is None:
                # A creation without effective Tags, a demotion, or an inactive
                # lifecycle state must remove any stale hot label.  The canonical
                # row remains available to the bounded cold-retrieval path.
                self._decrement_member(memory_id)
                await asyncio.to_thread(self.index.mark_deleted, [memory_id])
                self._dirty = True
                return
            scope_key = candidate.scope_key
            if candidate.vector is None:
                self._decrement_member(memory_id)
                await asyncio.to_thread(self.index.mark_deleted, [memory_id])
                self._dirty = True
                return
            # v4.2.1 semantics: a full index resizes inline.  Capacity is not a
            # fault condition, so admission never queues a durable rebuild.
            await asyncio.to_thread(
                self.index.add,
                [memory_id],
                candidate.vector.reshape(1, -1),
            )
            self._set_member(
                memory_id,
                lane=str(getattr(candidate, "recall_visibility", "scoped")),
                scope_key=scope_key,
            )
            self._dirty = True

    async def save_barrier(self, *, db_watermark: int = 0) -> None:
        async with self._lock:
            if not self._dirty:
                return
            save = getattr(self.index, "save", None)
            if callable(save):
                try:
                    await asyncio.to_thread(save, db_watermark=db_watermark)
                except TypeError:
                    await asyncio.to_thread(save)
            self._dirty = False


class TagIndexProjection:
    """Consume Tag events without guessing vectors from legacy unscoped tables.

    Current scoped Tag writes emit memory.tags_applied with IDs but no canonical Tag
    vector/version. Those events are checkpointed and counted as withheld. Future
    scoped_tag vector events can use the explicit payload path below.
    """

    consumer_name = "tag_index"

    def __init__(self, index: Any, database_path: str | None = None) -> None:
        self.index = index
        self.database_path = str(database_path or "")
        self._dirty = False
        self._lock = asyncio.Lock()
        self.withheld_count = 0

    def _read_catalog_vector(self, catalog_id: int) -> np.ndarray | None:
        if not self.database_path:
            return None
        connection = sqlite3.connect(_readonly_uri(self.database_path), uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT embedding, status FROM tag_catalog WHERE id=?",
                (int(catalog_id),),
            ).fetchone()
            if row is None or str(row[1] or "active") != "active":
                return None
            return _decode_vector(row[0], int(self.index.dimension))
        except Exception:
            return None
        finally:
            connection.close()

    def _read_catalog_ids_for_scoped_tags(self, tag_ids: list[int]) -> list[int]:
        if not self.database_path or not tag_ids:
            return []
        connection = sqlite3.connect(_readonly_uri(self.database_path), uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            placeholders = ",".join("?" for _ in tag_ids)
            rows = connection.execute(
                f"SELECT DISTINCT catalog_id FROM scoped_tags WHERE id IN ({placeholders}) AND catalog_id IS NOT NULL",
                [int(value) for value in tag_ids],
            ).fetchall()
            return [int(row[0]) for row in rows if row[0] is not None]
        except Exception:
            return []
        finally:
            connection.close()

    async def __call__(self, event: OutboxEvent) -> None:
        if event.event_type in {
            "memory.tags_applied",
            "memory.tags_corrected",
            "memory.tags_correction_undone",
        }:
            raw_tag_ids = _positive_ints(
                event.payload.get("tag_ids") or event.payload.get("after_tags") or ()
            )
            raw_catalog_ids = _positive_ints(event.payload.get("catalog_ids") or ())
            if not raw_catalog_ids:
                raw_catalog_ids = self._read_catalog_ids_for_scoped_tags(raw_tag_ids)
            if not self.database_path or not raw_catalog_ids:
                self.withheld_count += len(raw_tag_ids)
                return
            added_ids: list[int] = []
            async with self._lock:
                for catalog_id in dict.fromkeys(raw_catalog_ids):
                    vector = await asyncio.to_thread(self._read_catalog_vector, catalog_id)
                    if vector is None:
                        self.withheld_count += 1
                        continue
                    await asyncio.to_thread(self.index.add, [catalog_id], vector.reshape(1, -1))
                    added_ids.append(catalog_id)
                if added_ids:
                    self._dirty = True
            if not added_ids:
                self.withheld_count += len(raw_tag_ids)
            return
        governance_event = (
            event.aggregate_kind == "tag_audit_suggestion"
            and event.event_type in {
                "tag.merge",
                "tag.deactivate",
                "tag.governance.applied",
                "tag.governance.compensated",
            }
        )
        if event.aggregate_kind not in {"tag", "scoped_tag"} and not governance_event:
            return
        if governance_event:
            impact = event.payload.get("impact") if isinstance(event.payload, dict) else {}
            removed = impact.get("removed_tag_ids") if isinstance(impact, dict) else ()
            if removed:
                async with self._lock:
                    await asyncio.to_thread(self.index.mark_deleted, [int(value) for value in removed])
                    self._dirty = True
            else:
                self.withheld_count += len(impact.get("related_tag_ids") or ()) if isinstance(impact, dict) else 1
            return
        tag_id = int(event.payload.get("tag_id") or event.aggregate_id)
        async with self._lock:
            if event.event_type in {"tag.deleted", "tag.merged", "scoped_tag.deleted"}:
                await asyncio.to_thread(self.index.mark_deleted, [tag_id])
                self._dirty = True
                return
            vector = _decode_vector(event.payload.get("vector"), int(self.index.dimension))
            if vector is None:
                self.withheld_count += 1
                return
            await asyncio.to_thread(self.index.add, [tag_id], vector.reshape(1, -1))
            self._dirty = True

    async def save_barrier(self, *, db_watermark: int = 0) -> None:
        async with self._lock:
            if not self._dirty:
                return
            save = getattr(self.index, "save", None)
            if callable(save):
                try:
                    await asyncio.to_thread(save, db_watermark=db_watermark)
                except TypeError:
                    await asyncio.to_thread(save)
            self._dirty = False


class CooccurrenceProjection:
    """将已提交的 Tag 变更快速通知给共现防抖调度器。"""

    consumer_name = "cooccurrence"
    _EVENT_TYPES = {
        "memory.tags_applied",
        "memory.tags_corrected",
        "memory.tags_correction_undone",
        "memory.deleted",
        "memory.archived",
        "memory.evicted",
        "tag.deleted",
        "tag.merged",
        "tag.merge",
        "tag.deactivate",
        "tag.governance.applied",
        "tag.governance.compensated",
    }

    def __init__(
        self,
        cooccurrence: Any,
        *,
        scheduler: Any | None = None,
        notify: Any | None = None,
    ) -> None:
        self.cooccurrence = cooccurrence
        self._lock = asyncio.Lock()
        self._dirty = False
        self._metrics: dict[str, object] = {
            "notifications_total": 0,
            "ignored_events_total": 0,
            "last_reason": None,
        }
        # Existing construction order creates the scheduler first.  Discover it
        # from the matrix so this wiring remains compatible without main.py.
        self.scheduler = scheduler or getattr(cooccurrence, "_cooccurrence_scheduler", None)
        if self.scheduler is None and notify is None:
            try:
                from ..engine.directed_cooccurrence import CooccurrenceScheduler
            except ImportError:  # pragma: no cover - top-level repository tests
                from engine.directed_cooccurrence import CooccurrenceScheduler
            self.scheduler = CooccurrenceScheduler(cooccurrence)
        if self.scheduler is not None:
            bind_lock = getattr(self.scheduler, "set_rebuild_lock", None)
            if callable(bind_lock):
                bind_lock(self._lock)
            notify = notify or getattr(self.scheduler, "notify_tag_change", None)
        if not callable(notify):
            raise TypeError("CooccurrenceProjection requires a scheduler or notify callback")
        self._notify = notify

    def metrics_snapshot(self) -> dict[str, object]:
        return dict(self._metrics)

    async def __call__(self, event: OutboxEvent) -> None:
        if event.event_type not in self._EVENT_TYPES:
            self._metrics["ignored_events_total"] = int(self._metrics["ignored_events_total"]) + 1
            return
        self._dirty = True
        self._metrics["notifications_total"] = int(self._metrics["notifications_total"]) + 1
        self._metrics["last_reason"] = event.event_type
        result = self._notify(count=1, reason=event.event_type)
        if hasattr(result, "__await__"):
            await result

    async def force_rebuild(self, *, reason: str = "maintenance") -> dict[str, object]:
        """Use the scheduler's shared barrier for maintenance-triggered rebuilds."""
        force_rebuild = getattr(self.scheduler, "force_rebuild", None)
        if not callable(force_rebuild):
            raise RuntimeError("cooccurrence scheduler does not support force_rebuild")
        result = force_rebuild(reason=reason)
        if hasattr(result, "__await__"):
            result = await result
        self._dirty = False
        return result if isinstance(result, dict) else self.metrics_snapshot()

    async def save_barrier(self, *, db_watermark: int = 0) -> None:
        self._dirty = False


class RuntimeRefreshProjection:
    """Invoke exact-scope runtime/cache refresh callbacks after committed changes."""

    consumer_name = "runtime_refresh"

    def __init__(self, callbacks: dict[str, Any] | None = None) -> None:
        self.callbacks = dict(callbacks or {})
        self._epochs: dict[str, int] = {}

    @staticmethod
    def _epoch_key(event: OutboxEvent) -> str:
        scope = event.payload.get("scope") if isinstance(event.payload, dict) else None
        scope_key = json.dumps(scope or {}, sort_keys=True, separators=(",", ":"))
        return f"{event.aggregate_kind}:{scope_key}"

    def epoch(self, aggregate_kind: str, scope: dict[str, Any] | None = None) -> int:
        scope_key = json.dumps(scope or {}, sort_keys=True, separators=(",", ":"))
        return self._epochs.get(f"{aggregate_kind}:{scope_key}", 0)

    async def __call__(self, event: OutboxEvent) -> None:
        key = self._epoch_key(event)
        self._epochs[key] = self._epochs.get(key, 0) + 1
        callback = self.callbacks.get(event.aggregate_kind)
        if callback is None:
            return
        result = callback(event)
        if hasattr(result, "__await__"):
            await result

    async def save_barrier(self, *, db_watermark: int = 0) -> None:
        return


class FtsCjkProjection:
    """跟随记忆增删改维护中文全文索引 fts_memories_cjk（见 engine/db/fts_cjk.py）。"""

    consumer_name = "fts_cjk"

    def __init__(self, database_path: str) -> None:
        self.database_path = str(database_path)
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None
        self.metrics = {"indexed": 0, "removed": 0, "errors": 0}

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(self.database_path, timeout=30.0, check_same_thread=False)
            connection.execute("PRAGMA busy_timeout=10000")
            fts_cjk.ensure_schema_committed(connection)
            self._connection = connection
        return self._connection

    def _sync(self, memory_id: int) -> str:
        return fts_cjk.sync_memory_committed(self._conn(), memory_id)

    async def __call__(self, event: OutboxEvent) -> None:
        if event.aggregate_kind != "memory":
            return
        async with self._lock:
            outcome = await asyncio.to_thread(self._sync, int(event.aggregate_id))
        self.metrics[outcome] = self.metrics.get(outcome, 0) + 1

    async def save_barrier(self, *, db_watermark: int = 0) -> None:
        return None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
