"""维护任务与派生投影：索引重建、回填、审计、导入、信念刷新（从 main.py 拆出的 WaveMemoryPlugin mixin）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from ..domain.scope import RuntimeScope
from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex
from ..engine.db.outbox_repo import OutboxRepository
from ..engine.db.scoped_learning_projection_repo import CoordinatorScopedProjectionWriter
from ..engine.embedding import EmbeddingService
from ..engine.query_engine import QueryEngine, QueryOptions
from ..engine.directed_cooccurrence import (
    DEFAULT_MAX_NEIGHBORS_PER_TAG,
    DEFAULT_REBUILD_COOLDOWN_SEC,
    DEFAULT_REBUILD_THRESHOLD_PCT,
    DirectedCooccurrence,
    CooccurrenceScheduler,
)
from ..engine.spike_routing import SpikeRouter
from ..engine.residual_pyramid import ResidualPyramid
from ..engine.geodesic_rerank import GeodesicReranker
from ..engine.epa import EPAModule
from ..engine.intrinsic_residual import IntrinsicResidualCalculator
from ..engine.semantic_gain import SemanticGainConfig
from ..services.message_writer import MessageWriter
from ..services.tag_extractor import TagExtractor
from ..services.tag_worker import TagWorker
from ..services.system_convergence_runtime import ProductionWriteGateway
from ..services.derived_projections import (
    CooccurrenceProjection,
    MemoryIndexProjection,
    TagIndexProjection,
    RuntimeRefreshProjection,
)
from ..services.task_supervisor import TaskSupervisor
from ..services.durable_jobs import DurableJobRunner
from ..services.data_governance_jobs import DataGovernancePreviewJobs
from ..services.scope_recovery import build_scope_recovery_handlers
from ..services.quality_gate import QualityGate
from ..services.pair_similarity import PairSimilarityService
from ..services.hot_config import HotConfig
from ..services.memory_index_policy import memory_index_policy_from_settings, select_hot_memory_candidates
from ..services.maintenance_tokens import maintenance_repair_token
from ..services.platform_context import PlatformContextManager
from ..services.inbound_message_handler import InboundMessagePipeline, event_message_id
from ..services.backup_lifecycle import DatabaseBackupManager
from ..services.runtime_mode import effective_native_injection_enabled, effective_query_feature, resolve_runtime_mode, runtime_capability_enabled, should_self_heal_advanced_query
from ..services.compat import build_duplicate_memory_warnings, build_livingmemory_compat_surface, detect_memory_plugins
from ..services.impression_timeline import configure_social_limits, parse_impression_mark, persist_unsettled_trace
from ..services.lifecycle import LifecycleService
from ..tools.livingmemory_compat_tools import build_livingmemory_compat_tools
from ..engine.book_lore_index import BookLoreIndex
from ..services.meta_thinking import MetaThinking
from ..services.dream import DreamService
from ..services.self_reflect import SelfReflectService
from ..services.llm_fallback import LLMFallbackClient, build_provider_chain
from ..services.eviction import EvictionService
from ..services.concern_tracker import ConcernTracker
from ..services.mood_trajectory import MoodTrajectory
from ..services.subjective_time import SubjectiveTime
from ..services.desire_engine import DesireEngine
from ..services.belief_engine import BeliefEngine
from ..services.belief_emergence import BeliefEmergenceService
from ..services.belief_gating import snapshot_from_relationship
from ..services.proactive_audit import record_proactive_timeline
from ..services.jargon.service import JargonService
from ..services.few_shot.service import FewShotService
from ..services.reflection_trigger import ReflectionTriggerService
from ..services.relationship_events import RelationshipEventService
from ..domain.scope import CatalogScope, RuntimeScope, SessionRef
from ..services.identity_safety import (
    build_identity_safety_injection,
    filter_identity_contamination_memories,
    is_identity_contamination,
    prepend_identity_safety_system_prompt,
)
from ..domain.bot_profile import BotProfile, BotProfileError, profile_from_legacy_config
from ..services.bot_registry import BotRegistry, legacy_profiles_from_config
from .common import _record_err, _ObservationEvent, _stringify_config_value, _parse_csv_config_value, _parse_bool_config_value, _parse_int_config_value, _positive_float, _parse_bot_config, _build_bot_registry


class MaintenanceMixin:
    async def _maintenance_run_vector_backfill(self, run, request, runner):
        """Recover one bounded batch of missing embeddings through scoped commands."""
        import numpy as np

        payload = request.payload if isinstance(request.payload, dict) else {}
        try:
            batch_size = max(1, min(int(payload.get("batch_size", 16)), 32))
        except (TypeError, ValueError):
            batch_size = 16
        cursor = run.cursor if isinstance(run.cursor, dict) else {}
        try:
            after_id = max(0, int(cursor.get("after_id", 0)))
            timeout_batches = max(0, int(cursor.get("timeout_batches", 0)))
        except (TypeError, ValueError):
            after_id, timeout_batches = 0, 0
        where, params = self._vector_backfill_predicate(after_id=after_id)

        def _snapshot(connection):
            return connection.execute(
                f"""SELECT m.id, m.content, m.bot_id, m.session_id, m.visibility, m.group_id
                      FROM memories m
                     WHERE {where}
                     ORDER BY m.id ASC
                     LIMIT ?""",
                (*params, batch_size),
            ).fetchall()

        rows = await self.write_gateway.coordinator.read(_snapshot)
        if not rows:
            return {
                "kind": "memory_vector_backfill",
                "status": "completed",
                "processed": 0,
                "updated": 0,
                "after_id": after_id,
            }

        selected: list[tuple[int, str, RuntimeScope]] = []
        skipped_scope = 0
        for memory_id, content, bot_id, session_id, visibility, group_id in rows:
            try:
                raw_session_id = str(session_id)
                platform_id, kind, conversation_id = raw_session_id.split(":", 2)
                canonical_visibility = str(visibility)
                if (
                    canonical_visibility not in {"group", "private"}
                    or kind != canonical_visibility
                    or conversation_id != str(group_id)
                ):
                    raise ValueError("noncanonical_session_id")
                scope = RuntimeScope(
                    bot_id=str(bot_id),
                    visibility=canonical_visibility,
                    session=SessionRef(
                        id=raw_session_id,
                        platform_id=platform_id,
                        kind=kind,
                        conversation_id=conversation_id,
                    ),
                )
                text = str(content or "").strip()
                if not text:
                    raise ValueError("empty_content")
            except (TypeError, ValueError):
                skipped_scope += 1
                continue
            selected.append((int(memory_id), text, scope))

        next_after_id = int(rows[-1][0])
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress={
                "phase": "embed",
                "selected": len(selected),
                "skipped_scope": skipped_scope,
                "after_id": next_after_id,
            },
            cursor={"phase": "embed", "after_id": after_id, "timeout_batches": timeout_batches},
        )

        vectors = await self.writer._embed_with_limited_retry([item[1] for item in selected]) if selected else []
        if vectors is None:
            # A terminal writer retry records a temporary failure. Requeue this
            # exact cursor only a small, explicit number of times; vector=NULL
            # remains the durable recoverable state after that limit.
            timeout_batches += 1
            retry_queued = False
            if timeout_batches < 3:
                retry = await self.write_gateway.jobs.schedule_run(
                    request_id=request.request_id,
                    schedule_slot=run.schedule_slot,
                    cursor_generation=run.cursor_generation + 1,
                    cursor={
                        "phase": "retry",
                        "after_id": after_id,
                        "timeout_batches": timeout_batches,
                    },
                )
                retry_queued = retry.run_id != run.run_id
            return {
                "kind": "memory_vector_backfill",
                "status": "retry_queued" if retry_queued else "deferred",
                "processed": len(selected),
                "updated": 0,
                "after_id": after_id,
                "timeout_batches": timeout_batches,
                "retry_queued": retry_queued,
            }

        vectors = list(vectors)
        if len(vectors) < len(selected):
            vectors.extend([None] * (len(selected) - len(vectors)))
        updated = 0
        skipped_vector = 0
        write_failures = 0
        for (memory_id, _text, scope), vector in zip(selected, vectors):
            if await self.write_gateway.jobs.cancellation_requested(run.run_id):
                return {
                    "kind": "memory_vector_backfill",
                    "status": "cancel_requested",
                    "processed": updated + skipped_vector + write_failures,
                    "updated": updated,
                    "after_id": after_id,
                }
            try:
                normalized = np.asarray(vector, dtype=np.float32)
                if normalized.ndim != 1 or normalized.size != self.dimension or not np.isfinite(normalized).all():
                    raise ValueError("embedding_dimension_or_finiteness_invalid")
                changed = await self.write_gateway.backfill_memory_vector(
                    scope=scope,
                    memory_id=memory_id,
                    vector=normalized,
                    idempotency_hint=f"{memory_id}:{hashlib.sha256(normalized.tobytes()).hexdigest()}",
                )
                updated += int(changed)
            except (TypeError, ValueError):
                skipped_vector += 1
            except Exception:
                write_failures += 1
                logger.warning(
                    "[WaveMemory] vector backfill write skipped memory_id=%s",
                    memory_id,
                    exc_info=True,
                )

        remaining_where, remaining_params = self._vector_backfill_predicate(after_id=next_after_id)
        has_remaining = await self.write_gateway.coordinator.read(
            lambda connection: connection.execute(
                f"SELECT 1 FROM memories m WHERE {remaining_where} LIMIT 1",
                remaining_params,
            ).fetchone() is not None
        )
        next_run_id = None
        if has_remaining:
            next_run = await self.write_gateway.jobs.schedule_run(
                request_id=request.request_id,
                schedule_slot=run.schedule_slot,
                cursor_generation=max(run.cursor_generation + 1, next_after_id),
                cursor={"phase": "queued", "after_id": next_after_id, "timeout_batches": 0},
            )
            next_run_id = next_run.run_id
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={
                "phase": "write",
                "processed": len(selected),
                "updated": updated,
                "skipped_scope": skipped_scope,
                "skipped_vector": skipped_vector,
                "write_failures": write_failures,
            },
            cursor={"phase": "write", "after_id": next_after_id, "timeout_batches": 0},
        )
        return {
            "kind": "memory_vector_backfill",
            "status": "continued" if next_run_id else "completed",
            "processed": len(selected),
            "updated": updated,
            "skipped_scope": skipped_scope,
            "skipped_vector": skipped_vector,
            "write_failures": write_failures,
            "after_id": next_after_id,
            "next_run_id": next_run_id,
        }

    async def _maintenance_rebuild_memory_index(self, run, request, runner):
        """Build one bounded, Tag-admitted hot HNSW generation from canonical state."""
        import numpy as np

        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={"phase": "snapshot"},
            cursor={"phase": "snapshot"},
        )

        # Hold the projection lock before the writer-serialized snapshot and keep
        # it through swap/save.  Committed events after the snapshot then project
        # in order against the new generation instead of being overwritten by it.
        async with self.memory_index_projection._lock:
            def _snapshot(connection):
                candidates = select_hot_memory_candidates(
                    connection,
                    self.memory_index_policy,
                    self.dimension,
                )
                rows = [
                    (candidate.memory_id, np.asarray(candidate.vector, dtype=np.float32).copy())
                    for candidate in candidates
                    if candidate.vector is not None
                ]
                return candidates, rows, OutboxRepository.committed_watermark(connection)

            candidates, rows, watermark = await self.write_gateway.coordinator.read(_snapshot)

            def _build_fresh_index() -> VectorIndex:
                fresh_index = VectorIndex(
                    dimension=self.dimension,
                    max_elements=self.memory_index_policy.max_vectors,
                    index_path=None,
                    kind="memory",
                    allow_resize=True,
                )
                if rows:
                    fresh_index.add(
                        [int(row[0]) for row in rows],
                        np.asarray([row[1] for row in rows], dtype=np.float32),
                    )
                return fresh_index

            # HNSW construction is CPU- and allocation-heavy; never block the
            # AstrBot event loop while publishing a replacement generation.
            fresh = await asyncio.to_thread(_build_fresh_index)
            with self.memory_index._lock:
                self.memory_index.index = fresh.index
                self.memory_index.max_elements = fresh.max_elements
                self.memory_index.allow_resize = True
            manifest = await asyncio.to_thread(
                self.memory_index.save,
                db_watermark=int(watermark),
            )
            self.memory_index_projection._dirty = False
            self.memory_index_projection.set_hot_membership(candidates)
        return {
            "kind": "memory_index",
            "count": len(rows),
            "capacity": self.memory_index_policy.max_vectors,
            "per_scope_capacity": self.memory_index_policy.per_scope_max_vectors,
            "generation": None if manifest is None else manifest.generation,
            "db_watermark": int(watermark),
            "verified": manifest is not None and manifest.count == len(rows),
        }

    async def _maintenance_rebuild_tag_index(self, run, request, runner):
        """Rebuild the canonical semantic Tag Catalog HNSW under a hard capacity."""
        import numpy as np

        from ..services.tag_index_capacity import hard_capacity, select_bounded_tag_vectors

        capacity = hard_capacity(self.tag_index_max_vectors, default=1)
        expected = self.dimension * np.dtype(np.float32).itemsize

        def _snapshot(connection):
            catalog_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'"
            ).fetchone()
            if not catalog_exists:
                return [], OutboxRepository.committed_watermark(connection)
            # Prefer frequently used active Catalog rows; never auto-expand past capacity.
            rows = connection.execute(
                """
                SELECT id, embedding
                FROM tag_catalog
                WHERE embedding IS NOT NULL AND status='active'
                ORDER BY COALESCE(updated_at, created_at, 0) DESC, id ASC
                LIMIT ?
                """,
                (capacity,),
            ).fetchall()
            return rows, OutboxRepository.committed_watermark(connection)

        rows, watermark = await self.write_gateway.coordinator.read(_snapshot)
        selected = select_bounded_tag_vectors(
            rows,
            capacity=capacity,
            dimension=self.dimension,
            vector_bytes_expected=expected,
        )
        valid_rows = [
            (tag_id, np.frombuffer(blob, dtype=np.float32).copy())
            for tag_id, blob in selected
        ]
        fresh = VectorIndex(
            dimension=self.dimension,
            max_elements=capacity,
            index_path=None,
            kind="tag_catalog",
            allow_resize=True,
        )
        if valid_rows:
            fresh.add(
                [row[0] for row in valid_rows],
                np.asarray([row[1] for row in valid_rows], dtype=np.float32),
            )
        with self.tag_catalog_index._lock:
            self.tag_catalog_index.index = fresh.index
            self.tag_catalog_index.max_elements = capacity
            self.tag_catalog_index.allow_resize = True
        manifest = await asyncio.to_thread(
            self.tag_catalog_index.save,
            db_watermark=int(watermark),
        )
        return {
            "kind": "tag_index",
            "source": "tag_catalog",
            "count": len(valid_rows),
            "capacity": capacity,
            "truncated": True,
            "generation": None if manifest is None else manifest.generation,
            "db_watermark": int(watermark),
            "verified": manifest is not None and manifest.count == len(valid_rows),
        }

    async def _maintenance_run_import(self, run, request, runner):
        """Execute serializable Import requests under a durable lease and fail closed on unresolved scope."""
        mode = str(request.payload.get("mode") or "legacy")
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress={"phase": "preflight", "mode": mode},
            cursor={"phase": "preflight"},
        )

        if mode == "legacy":
            from ..webui.importer import WaveMemoryImporter

            source = str(request.payload.get("source") or "")
            importer = WaveMemoryImporter(
                self.db,
                self.embedding_service,
                self.tag_extractor,
                memory_index=None,
                writer=None,
            )
            last = {}
            async for raw_event in importer.run(
                source=source,
                re_embed=bool(request.payload.get("re_embed", True)),
                extract_tags=bool(request.payload.get("extract_tags", True)),
                batch_size=max(1, min(int(request.payload.get("batch_size", 20)), 100)),
            ):
                try:
                    last = json.loads(raw_event)
                except (TypeError, ValueError, json.JSONDecodeError):
                    last = {"message": str(raw_event)}
                await self.write_gateway.jobs.update_progress(
                    run.run_id,
                    lease_owner=runner.lease_owner,
                    lease_seconds=120.0,
                    progress=last,
                    cursor={"phase": last.get("status", "running")},
                )
            return last

        if mode == "discovered_source":
            from ..webui.source_discovery import SourceDiscovery

            source_id = str(request.payload.get("source_id") or "")
            source = next(
                (item for item in SourceDiscovery().discover_all() if item.get("id") == source_id),
                None,
            )
            if source is None:
                raise RuntimeError("import_source_not_found")
            target = str((source.get("adapter") or {}).get("target", "memories"))
            if target == "memories" or source.get("type") != "known":
                return {
                    "status": "blocked",
                    "reason_code": "unresolved_import_not_supported",
                    "source_id": source_id,
                    "message": "Import source has no verified RuntimeScope binding.",
                }
            return {
                "status": "blocked",
                "reason_code": "domain_import_gateway_required",
                "source_id": source_id,
                "target": target,
                "message": "Non-memory imports require a target-specific coordinator command.",
            }

        raise RuntimeError("import_mode_invalid")

    async def _maintenance_run_tag_backfill(self, run, request, runner):
        """Extract one bounded scoped Tag batch under a durable lease."""
        from ..webui.tag_execution import tag_memory_batch

        batch_size = max(1, min(int(request.payload.get("batch_size", 20)), 50))
        min_length = max(0, int(request.payload.get("skip_short_min_length", 10)))

        def _snapshot(connection):
            return connection.execute(
                """SELECT m.id, m.content, m.sender_name, o.payload_json
                   FROM memories m
                   JOIN domain_outbox o
                     ON o.aggregate_kind='memory'
                    AND o.aggregate_id=CAST(m.id AS TEXT)
                    AND o.event_type='memory.created'
                   WHERE NOT EXISTS (
                       SELECT 1 FROM scoped_memory_tags smt WHERE smt.memory_id=m.id
                   )
                     AND m.resolution_state='resolved'
                     AND COALESCE(m.quarantine, 0)=0
                     AND LENGTH(COALESCE(m.content, '')) >= ?
                   ORDER BY m.id ASC LIMIT ?""",
                (min_length, batch_size),
            ).fetchall()

        rows = await self.write_gateway.coordinator.read(_snapshot)
        messages = []
        for memory_id, content, sender_name, payload_json in rows:
            try:
                scope = json.loads(payload_json).get("scope")
            except (TypeError, ValueError, json.JSONDecodeError):
                scope = None
            if isinstance(scope, dict):
                messages.append({
                    "id": int(memory_id),
                    "content": str(content or "")[:800],
                    "sender": str(sender_name or ""),
                    "scope": scope,
                })

        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress={"phase": "extract", "selected": len(messages)},
            cursor={"phase": "extract", "after_id": messages[-1]["id"] if messages else 0},
        )
        result = await asyncio.wait_for(
            tag_memory_batch(
                self.db,
                self.embedding_service,
                self.tag_extractor,
                messages,
                tag_batch_size=batch_size,
                tag_write_policy="missing_only",
                skip_short_min_length=min_length,
                write_gateway=self.write_gateway,
            ),
            timeout=110.0,
        )
        return {**result, "bounded": True}

    async def _maintenance_run_tag_audit(self, run, request, runner):
        """Run LLM Tag audit as a resumable durable job, never in an SSE request."""
        from ..services.tag_auditor import TagAuditor

        provider_id = str(request.payload.get("provider_id") or self.tag_llm_provider_id or "")
        if not provider_id:
            raise RuntimeError("tag_audit_provider_not_configured")
        strategy = str(request.payload.get("strategy", "mixed"))
        if strategy not in {"mixed", "low_quality", "high_freq"}:
            raise RuntimeError("tag_audit_strategy_invalid")
        total_count = max(10, min(int(request.payload.get("total_count", 500)), 2000))
        batch_size = max(1, min(int(request.payload.get("batch_size", 50)), 100))
        auditor = TagAuditor(
            db=self.db,
            context=self.context,
            provider_id=provider_id,
        )

        async def _publish(suggestion):
            action = suggestion.get("action")
            if action == "merge":
                tag_ids = json.dumps(suggestion.get("source_ids", []))
                target_name = suggestion.get("target_name", "")
                target_type = suggestion.get("target_type", "")
            elif action == "retype":
                tag_ids = json.dumps([suggestion.get("tag_id")])
                target_name = None
                target_type = suggestion.get("new_type", "")
            elif action == "delete":
                tag_ids = json.dumps([suggestion.get("tag_id")])
                target_name = None
                target_type = None
            else:
                return

            def _insert(connection):
                connection.execute(
                    """
                    INSERT INTO tag_audit_suggestions(
                        action, tag_ids, target_name, target_type,
                        reason, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        action,
                        tag_ids,
                        target_name,
                        target_type,
                        suggestion.get("reason", ""),
                        time.time(),
                    ),
                )

            await self.write_gateway.coordinator.transaction(
                _insert,
                actor="maintenance.tag_audit",
            )

        last_event = {"progress": 0, "processed": 0, "total_suggestions": 0}
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress=last_event,
            cursor={"processed": 0, "strategy": strategy},
        )
        iterator = auditor.run_audit(
            batch_size=batch_size,
            strategy=strategy,
            total_count=total_count,
            save_suggestion=_publish,
        ).__aiter__()
        while True:
            if await self.write_gateway.jobs.cancellation_requested(run.run_id):
                return {**last_event, "cancelled": True}
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=110.0)
            except StopAsyncIteration:
                break
            last_event = dict(event)
            await self.write_gateway.jobs.update_progress(
                run.run_id,
                lease_owner=runner.lease_owner,
                lease_seconds=120.0,
                progress=last_event,
                cursor={
                    "processed": int(last_event.get("processed", 0)),
                    "strategy": strategy,
                },
            )
        return last_event

    async def _maintenance_rebuild_pair_similarity(self, run, request, runner):
        """Compute a sparse Top-K PairSimilarity projection off the event loop.

        Payload knobs (all optional, hard-capped):
        - ``max_tags``: highest-frequency tags admitted to the rebuild set.
        - ``top_k``: neighbors retained per tag.
        - ``min_similarity``: floor similarity for a retained edge.
        """
        from ..services.pair_similarity_projection import (
            ABSOLUTE_MAX_TAGS,
            ABSOLUTE_TOP_K,
            DEFAULT_MAX_TAGS,
            DEFAULT_MIN_SIMILARITY,
            DEFAULT_TOP_K,
        )

        payload = request.payload if isinstance(request.payload, dict) else {}
        try:
            max_tags = max(2, min(int(payload.get("max_tags", DEFAULT_MAX_TAGS)), ABSOLUTE_MAX_TAGS))
        except (TypeError, ValueError):
            max_tags = DEFAULT_MAX_TAGS
        try:
            top_k = max(1, min(int(payload.get("top_k", DEFAULT_TOP_K)), ABSOLUTE_TOP_K))
        except (TypeError, ValueError):
            top_k = DEFAULT_TOP_K
        try:
            min_similarity = float(payload.get("min_similarity", DEFAULT_MIN_SIMILARITY))
        except (TypeError, ValueError):
            min_similarity = DEFAULT_MIN_SIMILARITY

        def _snapshot(connection):
            return connection.execute(
                """
                SELECT id, vector
                FROM tags
                WHERE vector IS NOT NULL
                ORDER BY frequency DESC, id ASC
                LIMIT ?
                """,
                (max_tags,),
            ).fetchall()

        rows = await self.write_gateway.coordinator.read(_snapshot)
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={
                "phase": "compute",
                "tags": len(rows),
                "top_k": top_k,
                "min_similarity": min_similarity,
            },
            cursor={"phase": "compute", "tags": len(rows)},
        )

        params, cache = await asyncio.to_thread(
            self.pair_sim_service.compute_projection,
            rows,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        def _publish(connection):
            connection.execute("DELETE FROM tag_pair_similarity")
            if params:
                connection.executemany(
                    """
                    INSERT INTO tag_pair_similarity(
                        tag_id_a, tag_id_b, similarity, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    params,
                )

        await self.write_gateway.coordinator.transaction(_publish)
        self.pair_sim_service.install_projection(cache)
        return {
            "tags": len(rows),
            "pairs": len(params),
            "max_tags": max_tags,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "mode": "sparse_top_k",
        }

    async def _maintenance_rebuild_cooccurrence(self, run, request, runner):
        """Force one serialized scheduler rebuild for an operator maintenance request."""
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={"phase": "rebuild"},
            cursor={"phase": "rebuild"},
        )
        scheduler_metrics = await self.cooccurrence_projection.force_rebuild(
            reason="maintenance.cooccurrence.rebuild"
        )
        watermark = await self.write_gateway.coordinator.committed_watermark()
        return {
            "kind": "cooccurrence",
            "nodes": self.cooccurrence.node_count,
            "edges": self.cooccurrence.edge_count,
            "db_watermark": int(watermark),
            "scheduler": scheduler_metrics,
            "verified": True,
        }

    async def _maintenance_rebuild_fts_cjk(self, run, request, runner):
        """分批回填中文全文索引；可中断续跑（游标存在索引自己的状态表里）。"""
        import sqlite3

        from ..engine.db import fts_cjk

        def _open():
            # 每步都经 asyncio.to_thread，线程池不保证同一线程；各步顺序执行、不并发，可以跨线程用
            connection = sqlite3.connect(self.db.db_path, timeout=30.0, check_same_thread=False)
            connection.execute("PRAGMA busy_timeout=10000")
            fts_cjk.ensure_schema(connection)
            connection.commit()
            return connection

        connection = await asyncio.to_thread(_open)
        indexed = 0
        try:
            if bool((request.payload or {}).get("reset")):
                await asyncio.to_thread(lambda: (fts_cjk.reset(connection), connection.commit()))
            cursor = await asyncio.to_thread(fts_cjk.backfill_cursor, connection)
            while True:
                def _batch(after=cursor):
                    result = fts_cjk.backfill_batch(connection, after_id=after, limit=2000)
                    connection.commit()
                    return result

                last, count = await asyncio.to_thread(_batch)
                if last == cursor:
                    break
                cursor, indexed = last, indexed + count
                await self.write_gateway.jobs.update_progress(
                    run.run_id,
                    lease_owner=runner.lease_owner,
                    lease_seconds=120.0,
                    progress={"phase": "backfill", "indexed": indexed, "cursor": cursor},
                    cursor={"phase": "backfill", "after_id": cursor},
                )
                await asyncio.sleep(0.05)  # 让出写锁给消息写入
            await asyncio.to_thread(lambda: (fts_cjk.mark_ready(connection), connection.commit()))
            state = await asyncio.to_thread(fts_cjk.status, connection)
        finally:
            connection.close()
        logger.info(f"[WaveMemory] 中文全文索引回填完成: 本次 {indexed} 条, 共 {state.get('rows')} 条")
        return {"kind": "fts_cjk", "indexed": indexed, **state, "verified": bool(state.get("ready"))}

    async def _queue_maintenance_repair(self, kind: str, *, reason: str) -> str:
        """Idempotently queue a recoverable repair instead of mutating derived state inline."""
        manifest = None
        try:
            if kind == "memory_index":
                manifest = self.memory_index.read_manifest(verify_checksum=False)
            elif kind == "tag_index":
                manifest = self.tag_catalog_index.read_manifest(verify_checksum=False)
        except Exception:
            manifest = None
        generation = 0 if manifest is None else int(manifest.generation)
        watermark = await self.write_gateway.coordinator.committed_watermark()
        # A full hot index must not receive one rebuild request per incoming
        # event while its physical generation is already capacity-bound.
        # The generation changes only after a successful rebuild, naturally
        # opening the next coalescing window when capacity is reached again.
        token = maintenance_repair_token(
            kind,
            reason,
            watermark=watermark,
            generation=generation,
        )
        request = await self.write_gateway.jobs.create_request(
            idempotency_key=f"maintenance:{token}",
            kind=f"maintenance.{kind}.rebuild",
            scope={"kind": "system_maintenance"},
            payload={"kind": kind, "reason": reason, "preflight_token": token},
        )
        run = await self.write_gateway.jobs.schedule_run(
            request_id=request.request_id,
            schedule_slot=token,
            cursor_generation=generation + 1,
            cursor={"phase": "queued", "watermark": watermark},
            # If the same drift token already terminated without removing drift,
            # atomically advance the slot generation instead of replaying terminal state.
            reschedule_terminal=True,
        )
        return run.run_id

    async def _queue_vector_backfill(self, *, reason: str) -> str:
        """Queue one bounded, resumable recovery chain for missing embeddings."""
        token = "memory-vector-backfill"
        request = await self.write_gateway.jobs.create_request(
            idempotency_key="maintenance:memory-vector-backfill:v1",
            kind="maintenance.vector_backfill.run",
            scope={"kind": "system_maintenance"},
            payload={"kind": "memory_vector_backfill", "reason": str(reason)},
        )
        run = await self.write_gateway.jobs.schedule_run(
            request_id=request.request_id,
            schedule_slot=token,
            cursor_generation=0,
            cursor={"phase": "queued", "after_id": 0, "reason": str(reason)},
            # An already-completed pass may be safely restarted after a later
            # timeout creates a new vectorless memory; an active pass is reused.
            reschedule_terminal=True,
        )
        return run.run_id

    async def _has_pending_vector_backfill(self) -> bool:
        """Check for eligible vectorless canonical memories without touching writes."""
        where, params = self._vector_backfill_predicate()

        def _read(connection):
            return connection.execute(
                f"SELECT 1 FROM memories m WHERE {where} LIMIT 1", params
            ).fetchone() is not None

        return bool(await self.write_gateway.coordinator.read(_read))

    @staticmethod
    def _vector_backfill_predicate(*, after_id: int = 0) -> tuple[str, tuple[object, ...]]:
        """Return the exact canonical predicate for recoverable missing vectors."""
        return (
            """m.id>? AND m.vector IS NULL
                 AND m.resolution_state='resolved'
                 AND COALESCE(m.quarantine, 0)=0
                 AND m.visibility IN ('group', 'private')
                 AND COALESCE(m.bot_id, '')<>''
                 AND COALESCE(m.session_id, '')<>''
                 AND COALESCE(m.source, '') NOT IN ('noise', 'identity_quarantine')""",
            (int(after_id),),
        )

    async def _on_vector_backfill_requested(self) -> None:
        """Coalesce post-timeout recovery only after source rows were committed."""
        if await self._has_pending_vector_backfill():
            await self._queue_vector_backfill(reason="embedding_terminal_timeout")

    async def _on_memory_projection_refresh(self, event) -> None:
        """Invalidate dependent reads and coalesce pending-belief evidence refresh.

        Capacity is handled inline by index resize (v4.2.1 semantics); a full
        index is a steady state, not a fault, so it no longer queues a rebuild.

        PairSimilarity is no longer rebuilt on every tag-change bucket.  That path
        previously materialised an O(n^2) upper triangle (~2M pairs for 2k tags)
        every five minutes and was the dominant CPU/WAL amplifier.  Tag changes only
        invalidate the small read cache; a sparse rebuild runs at startup when the
        projection table is empty, or via an explicit maintenance request.

        After TagWorker commits tags, pending beliefs that already cited the
        memory must recompute evidence-v1.  This does not extract new beliefs
        and never auto-approves quarantine / non-direct gates.
        """
        if event.event_type not in {
            "memory.tags_applied",
            "memory.tags_corrected",
            "memory.tags_correction_undone",
            "tag.merge",
            "tag.deactivate",
            "tag.governance.applied",
            "tag.governance.compensated",
        }:
            return
        # Drop stale O(1) lookups only.  Do not schedule a full pair rebuild here.
        self.pair_sim_service.clear_cache()
        if event.event_type in {
            "memory.tags_applied",
            "memory.tags_corrected",
            "memory.tags_correction_undone",
        }:
            self._schedule_belief_tag_refresh(event)

    def _schedule_belief_tag_refresh(self, event) -> None:
        """Debounce Tag-driven evidence refresh onto one pending belief at a time."""
        engine = getattr(self, "belief_engine", None)
        if engine is None:
            return
        payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
        try:
            memory_id = int(payload.get("memory_id") or event.aggregate_id)
        except (TypeError, ValueError):
            return
        if memory_id <= 0:
            return
        try:
            scope = RuntimeScope.from_dict(payload.get("scope") or {})
        except Exception:
            return
        if scope.visibility != "group" or scope.session is None:
            return
        try:
            belief_ids = self.db.list_scoped_belief_ids_citing_memory(scope, memory_id)
        except Exception as exc:
            logger.debug("[WaveMemory] belief tag refresh lookup failed: %s", exc)
            return
        delay = float(getattr(self, "_belief_tag_refresh_delay_seconds", 0.4) or 0.4)
        pending = getattr(self, "_pending_belief_tag_refresh", None)
        if pending is None:
            pending = {}
            self._pending_belief_tag_refresh = pending
        for belief_id in belief_ids:
            key = (scope.bot_id, scope.session.id, scope.visibility, int(belief_id))
            previous = pending.get(key)
            if previous is not None and not previous.done():
                previous.cancel()
            try:
                pending[key] = self._spawn(
                    self._run_belief_tag_refresh(scope, int(belief_id), delay, key),
                    owner="belief",
                )
            except Exception as exc:
                logger.debug("[WaveMemory] belief tag refresh schedule failed: %s", exc)
                pending.pop(key, None)

    async def _run_belief_tag_refresh(
        self,
        scope: RuntimeScope,
        belief_id: int,
        delay: float,
        key: tuple[str, str, str, int],
    ) -> None:
        pending = getattr(self, "_pending_belief_tag_refresh", {})
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            engine = getattr(self, "belief_engine", None)
            if engine is None:
                return
            await asyncio.to_thread(engine.refresh_evidence_after_tags, scope, belief_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[WaveMemory] belief tag refresh failed: %s", exc)
        finally:
            current = pending.get(key)
            task = asyncio.current_task()
            if current is task:
                pending.pop(key, None)

    async def _on_cooccurrence_rebuilt(self):
        """共现矩阵重建完成后，重算内生残差（30分钟最小间隔）。"""
        # row_budget 的锚增益在构建共现图时一并算好、随图同代发布，这里的旧版残差（按 legacy tags 表）不再需要
        if getattr(self.cooccurrence, "kernel_version", "global_max") == "row_budget":
            return
        # 最小间隔保护
        now = time.time()
        last_ts = getattr(self, '_last_residual_compute_ts', 0)
        if now - last_ts < 1800:  # 30 分钟
            return
        self._last_residual_compute_ts = now

        try:
            residuals = await asyncio.to_thread(self.intrinsic_residual.compute_all)
            if residuals:
                self.intrinsic_residual.persist(residuals)
                if self.spike_router:
                    self.spike_router.residual_map = residuals
                self.cooccurrence.residual_map = residuals
        except Exception as e:
            logger.warning(f"[WaveMemory] Intrinsic residual computation failed: {e}")
            _record_err("IntrinsicResidual", e)
