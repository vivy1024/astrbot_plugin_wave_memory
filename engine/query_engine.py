"""Wave Memory 查询引擎 V2 — tag 向量按需加载（删全量缓存）"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

import numpy as np

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # 兼容插件作为顶级模块加载
    from domain.scope import RuntimeScope

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests without AstrBot
    import logging
    logger = logging.getLogger(__name__)

from .database import WaveMemoryDB
from .vector_index import VectorIndex
from .embedding import EmbeddingService
from .directed_cooccurrence import DirectedCooccurrence
from .context_segmenter import ContextSegmenter
from .spike_routing import SpikeRouter
from .residual_pyramid import ResidualPyramid
from .epa import EPAModule
from .geodesic_rerank import GeodesicReranker
from .recall_policy import RecallPolicy


_QUERY_STAGE_NAMES = ("epa", "pyramid", "spike", "geodesic")
# Inject path warns after 1.5s, but still waits for the embedding result.
_INJECT_EMBEDDING_TIMEOUT_SEC = 1.5
# Upper bound for source-filtered knn fan-out. Without it a large top_k turned the
# hot search into the dominant cost of the bounded memory channel.
_MAX_FILTERED_CANDIDATES = 200
_QUERY_PARAM_LIMITS = {
    "pyramid_max_levels": (int, 1, 10),
    "pyramid_top_k": (int, 1, 50),
    "spike_max_hops": (int, 0, 16),
    "spike_firing_threshold": (float, 0.0, 1.0),
    "geodesic_alpha": (float, 0.0, 1.0),
}
_DEBUG_SENSITIVE_KEY = re.compile(
    r"^(?:authorization|cookie|secret|password|passwd|token|api[_-]?key|credential|"
    r"embedding|vector|final_residual|path)$",
    re.IGNORECASE,
)
_DEBUG_SENSITIVE_PATH = re.compile(
    r"(?:^|\.)(?:scope|session|subject_principal_id|content|text|raw)(?:\.|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryOptions:
    """Per-call query controls; never mutate shared stage objects."""

    touch: bool = True
    stages: Mapping[str, bool] = field(default_factory=dict)
    params: Mapping[str, int | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_stages = {
            name: bool(value)
            for name, value in dict(self.stages or {}).items()
            if name in _QUERY_STAGE_NAMES
        }
        normalized_params: dict[str, int | float] = {}
        for name, value in dict(self.params or {}).items():
            limits = _QUERY_PARAM_LIMITS.get(name)
            if limits is None or isinstance(value, bool):
                continue
            caster, minimum, maximum = limits
            try:
                cast_value = caster(value)
            except (TypeError, ValueError):
                continue
            normalized_params[name] = max(minimum, min(maximum, cast_value))
        object.__setattr__(self, "touch", bool(self.touch))
        object.__setattr__(self, "stages", MappingProxyType(normalized_stages))
        object.__setattr__(self, "params", MappingProxyType(normalized_params))


class QueryDebugCollector:
    """Bounded, recursively redacted per-call query trace collector."""

    def __init__(
        self,
        *,
        max_items_per_partition: int = 50,
        max_total_bytes: int = 48_000,
        max_depth: int = 6,
        max_string_length: int = 512,
    ) -> None:
        self.max_items_per_partition = max(1, min(int(max_items_per_partition), 100))
        self.max_total_bytes = max(2_048, min(int(max_total_bytes), 256_000))
        self.max_depth = max(2, min(int(max_depth), 10))
        self.max_string_length = max(64, min(int(max_string_length), 2_048))
        self._partitions: dict[str, dict[str, Any]] = {}
        self._warnings: list[dict[str, Any]] = []
        self._truncated = False

    @staticmethod
    def _reason_code(value: Any, fallback: str = "query_debug_stage_failed") -> str:
        raw = str(value or fallback).strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        return normalized[:80] or fallback

    def _redact(self, value: Any, *, path: str = "", depth: int = 0) -> Any:
        if depth >= self.max_depth:
            return "[TRUNCATED]"
        if isinstance(value, np.ndarray):
            return "[REDACTED_VECTOR]"
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, Mapping):
            result = {}
            for raw_key, item in list(value.items())[: self.max_items_per_partition]:
                key = str(raw_key)
                item_path = f"{path}.{key}" if path else key
                if _DEBUG_SENSITIVE_KEY.search(key) or _DEBUG_SENSITIVE_PATH.search(item_path):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = self._redact(item, path=item_path, depth=depth + 1)
            if len(value) > self.max_items_per_partition:
                result["_truncated_items"] = len(value) - self.max_items_per_partition
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            result = [
                self._redact(item, path=f"{path}[]", depth=depth + 1)
                for item in items[: self.max_items_per_partition]
            ]
            if len(items) > self.max_items_per_partition:
                result.append({"_truncated_items": len(items) - self.max_items_per_partition})
            return result
        if isinstance(value, bytes):
            return f"[REDACTED_BYTES:{len(value)}]"
        if isinstance(value, str):
            return value[: self.max_string_length] + ("…" if len(value) > self.max_string_length else "")
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[: self.max_string_length]

    def _fits(self, payload: Mapping[str, Any]) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return len(encoded) <= self.max_total_bytes

    def record(self, partition: str, payload: Mapping[str, Any]) -> None:
        name = self._reason_code(partition, "query")
        sanitized = self._redact(dict(payload), path=name)
        previous = self._partitions.get(name)
        if isinstance(previous, dict) and isinstance(sanitized, dict):
            sanitized = {**previous, **sanitized}
        proposed = {**self._partitions, name: sanitized}
        if self._fits({"partitions": proposed, "warnings": self._warnings}):
            self._partitions[name] = sanitized
        else:
            self._truncated = True

    def warn(self, stage: str, reason_code: str, message: str | None = None) -> None:
        warning = {
            "stage": self._reason_code(stage, "query"),
            "reason_code": self._reason_code(reason_code),
            "reason": str(message or reason_code)[: self.max_string_length],
        }
        proposed = [*self._warnings, warning]
        if len(proposed) <= self.max_items_per_partition and self._fits(
            {"partitions": self._partitions, "warnings": proposed}
        ):
            self._warnings = proposed
        else:
            self._truncated = True

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({
            **self._partitions,
            "warnings": self._warnings,
            "trace_meta": {
                "readonly": True,
                "touch": False,
                "truncated": self._truncated,
                "max_items_per_partition": self.max_items_per_partition,
                "max_total_bytes": self.max_total_bytes,
            },
        })


class QueryEngine:
    """记忆查询管线 V2：EPA → 残差金字塔 → 脉冲传播 → 向量融合 → 检索 → 测地线重排。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service: EmbeddingService,
        config: dict,
        tag_index: Optional[VectorIndex] = None,
        cooccurrence: Optional[DirectedCooccurrence] = None,
        spike_router: Optional[SpikeRouter] = None,
        residual_pyramid: Optional[ResidualPyramid] = None,
        epa: Optional[EPAModule] = None,
        geodesic: Optional[GeodesicReranker] = None,
        write_gateway: Any | None = None,
        *,
        tag_catalog_index: Optional[VectorIndex] = None,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.config = config
        # ``tag_index`` remains the public compatibility argument and now means
        # the formal Catalog index.
        self.tag_catalog_index = tag_catalog_index or tag_index
        self.tag_index = self.tag_catalog_index
        self.cooccurrence = cooccurrence
        self.spike_router = spike_router
        self.residual_pyramid = residual_pyramid
        self.epa = epa
        self.geodesic = geodesic
        self.write_gateway = write_gateway

        # 配置参数（对齐默认值）
        self.min_similarity = float(config.get("min_similarity", "0.35"))
        self.enable_spike = config.get("enable_spike_routing", True)
        self.enable_pyramid = config.get("enable_residual_pyramid", True)
        self.enable_epa = config.get("enable_epa", True)
        self.enable_geodesic = config.get("enable_geodesic_rerank", True)

    @staticmethod
    def _memory_scope(scope: Any) -> RuntimeScope | None:
        """Return a resolved canonical group/private memory Scope only."""
        if not isinstance(scope, RuntimeScope) or scope.visibility not in {"group", "private"}:
            return None
        if (
            scope.session is None
            or scope.session.kind != scope.visibility
            or not scope.bot_id
            or not scope.session.id
            or not scope.session.conversation_id
        ):
            return None
        return scope

    def _resolve_recall_policy(self, scope: RuntimeScope) -> RecallPolicy:
        """Build recall policy and optionally load active shared-memory grant ids."""
        policy = RecallPolicy.from_config(scope, self.config)
        if not policy.shared_grants_enabled or policy.cross_group_enabled:
            # Full cross-group already covers granted rows; grants are a narrow path.
            return policy
        grant_repo = getattr(self.db, "shared_memory_grants", None)
        if grant_repo is None:
            return policy
        lister = getattr(grant_repo, "active_memory_ids_for_consumer", None)
        if not callable(lister):
            return policy
        assert scope.session is not None
        consumer = {
            "bot_id": scope.bot_id,
            "session_id": scope.session.id,
            "visibility": scope.visibility,
            "group_id": scope.session.conversation_id,
        }
        try:
            ids = lister(consumer_scope=consumer) or []
        except Exception:
            logger.warning("[WaveMemory] shared_memory_grants load failed", exc_info=True)
            return policy
        return policy.with_granted_memory_ids(ids)

    def _get_scoped_memories_by_ids(self, ids: list[Any], policy: RecallPolicy) -> list[dict]:
        """Post-filter HNSW candidates through the explicit recall policy.

        The vector index has no Scope dimension.  Repository defaults stay exact
        Scope; only this policy-bearing QueryEngine call may opt into the approved
        group-visible cross-Scope read envelope or a narrow shared-grant allow-list.
        """
        if not ids:
            return []
        getter = getattr(self.db, "get_memories_by_ids", None)
        if not callable(getter):
            return []
        grant_ids = list(policy.granted_memory_ids) if policy.shared_grants_enabled else None
        try:
            # Prefer full signature; fall back for focused doubles that only know
            # cross-group flag or exact Scope.
            try:
                memories = getter(
                    ids,
                    scope=policy.scope,
                    allow_cross_group_recall=policy.cross_group_enabled,
                    shared_grant_memory_ids=grant_ids,
                )
            except TypeError:
                try:
                    memories = getter(
                        ids,
                        scope=policy.scope,
                        allow_cross_group_recall=policy.cross_group_enabled,
                    )
                except TypeError:
                    memories = getter(ids, scope=policy.scope)
        except Exception:
            logger.warning("[WaveMemory] Scoped memory candidate filter failed", exc_info=True)
            return []
        out: list[dict] = []
        grant_set = set(policy.granted_memory_ids) if policy.shared_grants_enabled else set()
        for memory in memories or []:
            if not isinstance(memory, dict):
                continue
            item = dict(memory)
            if not self._candidate_matches_memory_policy(item, policy):
                continue
            try:
                mid = int(item.get("id"))
            except (TypeError, ValueError):
                mid = 0
            if mid in grant_set and policy.is_cross_group(item):
                item["_shared_grant"] = True
            out.append(item)
        return out

    @staticmethod
    def _candidate_matches_memory_policy(memory: Mapping[str, Any], policy: RecallPolicy) -> bool:
        """Treat every HNSW/adapter DTO as untrusted before recall rendering.

        Production repositories already enforce these predicates in SQL.  This
        second check protects private sessions from an incomplete custom adapter
        or future retrieval path, while preserving group-focused test doubles
        that intentionally omit formal owner fields.
        """
        scope = policy.scope
        assert scope.session is not None
        visibility = str(memory.get("visibility") or "")
        group_id = str(memory.get("group_id") or "")
        if scope.visibility == "private":
            return (
                visibility == "private"
                and str(memory.get("bot_id") or "") == scope.bot_id
                and str(memory.get("session_id") or "") == scope.session.id
                and group_id == scope.session.conversation_id
            )
        return visibility != "private" and not group_id.casefold().startswith("private:")

    def _map_catalog_hits_to_scope(
        self,
        scope: RuntimeScope | None,
        catalog_ids: list[Any],
        *,
        allow_cross_group_recall: bool = False,
    ) -> dict[int, list[int]]:
        """Map Catalog ids to eligible scoped tag ids without mixing tag spaces."""
        if not isinstance(scope, RuntimeScope) or not catalog_ids:
            return {}
        mapper = getattr(self.db, "list_scoped_catalog_links", None)
        if not callable(mapper):
            return {}
        try:
            links = mapper(
                scope,
                [int(value) for value in catalog_ids],
                allow_cross_group_recall=allow_cross_group_recall,
            ) or []
        except TypeError:
            # Compatibility facades stay exact-Scope rather than accidentally
            # broadening their reads.
            try:
                links = mapper(scope, [int(value) for value in catalog_ids]) or []
            except Exception:
                return {}
        except Exception:
            return {}
        result: dict[int, list[int]] = {}
        for link in links:
            if not isinstance(link, Mapping):
                continue
            try:
                catalog_id = int(link["catalog_id"])
                scoped_tag_id = int(link["scoped_tag_id"])
            except (KeyError, TypeError, ValueError):
                continue
            result.setdefault(catalog_id, []).append(scoped_tag_id)
        return result

    def _search_scoped_tags(
        self,
        query_vec: np.ndarray,
        scope: RuntimeScope | None,
        *,
        k: int = 10,
        allow_cross_group_recall: bool = False,
    ) -> list[tuple[int, float]]:
        """Search the Catalog index, then return only current-Scope tag ids."""
        catalog_index = self.tag_catalog_index or self.tag_index
        if not catalog_index:
            return []
        raw = catalog_index.search(query_vec, k=max(int(k) * 8, 32))
        if not isinstance(scope, RuntimeScope):
            return [(int(tag_id), 1.0 - float(distance)) for tag_id, distance in raw[:k]]
        mapper = getattr(self.db, "list_scoped_catalog_links", None)
        if not callable(mapper):
            # Cross-group Catalog reads must never pass global Catalog IDs into
            # a scoped-tag API. Legacy compatibility remains exact-only.
            if allow_cross_group_recall:
                return []
            # Test/extension doubles without the formal mapper retain their old
            # contract; the production WaveMemoryDB always exposes the mapper.
            return [(int(tag_id), 1.0 - float(distance)) for tag_id, distance in raw[:k]]
        catalog_to_scoped = self._map_catalog_hits_to_scope(
            scope,
            [item[0] for item in raw],
            allow_cross_group_recall=allow_cross_group_recall,
        )
        scoped: list[tuple[int, float]] = []
        mapped_catalog_count = 0
        # In shared mode one semantic Catalog entry can map to many independent
        # group/Bot scoped-tag IDs. Bound by Catalog hits rather than the first N
        # scoped IDs, otherwise low numeric tag IDs could starve every other group.
        for catalog_id, distance in raw:
            scoped_ids = catalog_to_scoped.get(int(catalog_id), ())
            if not scoped_ids:
                continue
            mapped_catalog_count += 1
            scoped.extend((scoped_id, 1.0 - float(distance)) for scoped_id in scoped_ids)
            if mapped_catalog_count >= k:
                break
        return scoped

    def _load_tag_vectors(self, scope: RuntimeScope | None, tag_ids: list[Any]) -> dict[Any, Any]:
        scoped_getter = getattr(self.db, "get_scoped_tag_vectors_by_ids", None)
        if callable(scoped_getter) and isinstance(scope, RuntimeScope):
            try:
                return dict(scoped_getter(scope, [int(value) for value in tag_ids]) or {})
            except Exception:
                return {}
        getter = getattr(self.db, "get_tag_vectors_by_ids", None)
        if not callable(getter):
            return {}
        try:
            return dict(getter([int(value) for value in tag_ids]) or {})
        except Exception:
            return {}

    def _cold_recall_enabled(self) -> bool:
        value = self.config.get("cold_recall_enabled", True)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cold_candidate_limit(self) -> int:
        try:
            return min(512, max(1, int(self.config.get("cold_candidate_limit", 128))))
        except (TypeError, ValueError):
            return 128

    def _search_scoped_cold_memories(
        self,
        *,
        tag_query_vec: np.ndarray,
        score_query_vec: np.ndarray,
        policy: RecallPolicy,
    ) -> tuple[list[tuple[dict[str, Any], float]], dict[str, Any]]:
        """Cold-recall from the formal Catalog lane.

        Catalog hits remain strictly mapped through formal Scope. The legacy tag
        lane was removed together with the legacy ``tags``/``memory_tags`` data.
        """
        details: dict[str, Any] = {
            "enabled": self._cold_recall_enabled(),
            "available": False,
            "tag_count": 0,
            "candidate_count": 0,
            "accepted_count": 0,
            "catalog": {"available": False, "tag_count": 0, "candidate_count": 0, "accepted_count": 0},
        }
        if not details["enabled"]:
            details["reason_code"] = "disabled_by_config"
            return [], details
        query = np.asarray(score_query_vec, dtype=np.float32).reshape(-1)
        query_norm = float(np.linalg.norm(query))
        if not np.isfinite(query_norm) or query_norm <= 1e-10:
            details["reason_code"] = "invalid_query_vector"
            return [], details

        results_by_id: dict[int, tuple[dict[str, Any], float]] = {}

        def rerank(rows: Any, lane: str) -> int:
            accepted = 0
            for raw in rows or ():
                if not isinstance(raw, Mapping):
                    continue
                try:
                    memory_id = int(raw["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                vector_raw = raw.get("vector")
                if isinstance(vector_raw, memoryview):
                    vector_raw = vector_raw.tobytes()
                try:
                    vector = (
                        np.frombuffer(vector_raw, dtype=np.float32)
                        if isinstance(vector_raw, bytes)
                        else np.asarray(vector_raw, dtype=np.float32)
                    ).reshape(-1)
                except (TypeError, ValueError):
                    continue
                if vector.size != query.size or not np.isfinite(vector).all():
                    continue
                denominator = query_norm * float(np.linalg.norm(vector))
                if not np.isfinite(denominator) or denominator <= 1e-10:
                    continue
                similarity = float(np.dot(query, vector) / denominator)
                candidate = dict(raw)
                candidate.pop("vector", None)
                candidate["id"] = memory_id
                candidate["_retrieval_tier"] = "cold"
                candidate["_tag_lane"] = lane
                distance = 1.0 - similarity
                existing = results_by_id.get(memory_id)
                if existing is None or distance < existing[1]:
                    results_by_id[memory_id] = (candidate, distance)
                accepted += 1
            return accepted

        # Formal Catalog lane.
        catalog_getter = getattr(self.db, "list_scoped_cold_memory_candidates", None)
        catalog_index = self.tag_catalog_index or self.tag_index
        if catalog_index and callable(catalog_getter):
            catalog = details["catalog"]
            try:
                catalog_hits = self._search_scoped_tags(
                    tag_query_vec,
                    policy.scope,
                    k=12,
                    allow_cross_group_recall=policy.cross_group_enabled,
                )
                catalog_ids = [int(tag_id) for tag_id, similarity in catalog_hits if float(similarity) > 0.1]
                catalog["tag_count"] = len(catalog_ids)
                if catalog_ids:
                    try:
                        catalog_rows = catalog_getter(
                            policy.scope,
                            catalog_ids,
                            limit=self._cold_candidate_limit(),
                            allow_cross_group_recall=policy.cross_group_enabled,
                        ) or []
                    except TypeError:
                        catalog_rows = catalog_getter(policy.scope, catalog_ids, limit=self._cold_candidate_limit()) or []
                    catalog["candidate_count"] = len(catalog_rows)
                    catalog["accepted_count"] = rerank(catalog_rows, "catalog")
                    catalog["available"] = True
                else:
                    catalog["reason_code"] = "no_scoped_semantic_tags"
            except Exception as exc:
                catalog["reason_code"] = "catalog_cold_query_failed"
                catalog["error"] = str(exc)
        else:
            details["catalog"]["reason_code"] = "catalog_lane_unavailable"

        details["tag_count"] = int(details["catalog"]["tag_count"])
        details["candidate_count"] = int(details["catalog"]["candidate_count"])
        details["available"] = bool(details["catalog"]["available"])
        paired = sorted(results_by_id.values(), key=lambda item: (item[1], int(item[0]["id"])))
        # Collapse multi-group fanout clones before cold candidates enter the main
        # hot/cold merge; otherwise one semantic event can monopolize the budget.
        collapse_input: list[dict[str, Any]] = []
        for memory, distance in paired:
            item = dict(memory)
            item["score"] = 1.0 - float(distance)
            item["similarity"] = 1.0 - float(distance)
            item["_is_cross_group"] = policy.is_cross_group(item)
            collapse_input.append(item)
        try:
            from .memory_collapse import collapse_memories
        except ImportError:  # pragma: no cover
            from engine.memory_collapse import collapse_memories
        collapsed = collapse_memories(
            collapse_input,
            current_group_id=policy.current_group_id,
        )
        result = [
            (memory, 1.0 - float(memory.get("score") or 0.0))
            for memory in collapsed
        ]
        details["accepted_count"] = len(result)
        details["collapsed_from"] = len(paired)
        if not result and not details["available"]:
            details["reason_code"] = "cold_lanes_unavailable"
        elif not result:
            details["reason_code"] = "no_usable_cold_vectors"
        return result, details

    @staticmethod
    def _trace_record(collector: QueryDebugCollector | None, partition: str, payload: Mapping[str, Any]) -> None:
        if collector is None:
            return
        try:
            collector.record(partition, payload)
        except Exception:
            logger.debug("[WaveMemory] Query debug collector record failed", exc_info=True)

    @staticmethod
    def _trace_warning(
        collector: QueryDebugCollector | None,
        stage: str,
        reason_code: str,
        message: str,
    ) -> None:
        if collector is None:
            return
        try:
            collector.warn(stage, reason_code, message)
        except Exception:
            logger.debug("[WaveMemory] Query debug collector warning failed", exc_info=True)

    @staticmethod
    def _stage_copy(stage: Any, updates: Mapping[str, Any]) -> Any:
        """Copy a stage before applying per-call parameters; shared stages stay immutable."""
        if stage is None:
            return None
        isolated = copy.copy(stage)
        for attr, value in updates.items():
            if value is not None and hasattr(isolated, attr):
                setattr(isolated, attr, value)
        return isolated

    def _stage_enabled(self, options: QueryOptions, name: str, default: bool) -> bool:
        return bool(options.stages.get(name, default))

    async def query(
        self,
        text: str,
        group_id: Optional[str] = None,
        top_k: int = 5,
        exclude_sources: Optional[list[str]] = None,
        source_filter: Optional[str | list[str]] = None,
        *,
        scope: RuntimeScope | None = None,
        options: QueryOptions | None = None,
        collector: QueryDebugCollector | None = None,
    ) -> list[dict]:
        """执行完整查询；options/collector 均为请求级对象，不写共享 stage。"""
        call_options = options if isinstance(options, QueryOptions) else QueryOptions()
        resolved_scope = self._memory_scope(scope)
        if resolved_scope is None:
            logger.warning("[WaveMemory] Query rejected: resolved group/private RuntimeScope required")
            self._trace_warning(collector, "scope", "canonical_memory_scope_required", "Canonical group/private RuntimeScope is required")
            self._trace_record(collector, "final", {"result_count": 0, "reason_code": "canonical_memory_scope_required"})
            return []

        recall_policy = self._resolve_recall_policy(resolved_scope)
        is_private = resolved_scope.visibility == "private"
        start = time.perf_counter()
        stage_flags = {
            "epa": False if is_private else self._stage_enabled(call_options, "epa", self.enable_epa),
            "pyramid": False if is_private else self._stage_enabled(call_options, "pyramid", self.enable_pyramid),
            "spike": False if is_private else self._stage_enabled(call_options, "spike", self.enable_spike),
            "geodesic": False if is_private else self._stage_enabled(call_options, "geodesic", self.enable_geodesic),
        }
        self._trace_record(collector, "query", {
            "text": text,
            "text_length": len(text),
            "top_k": top_k,
            "source_filter": source_filter,
            "exclude_sources": list(exclude_sources or []),
            "stages": stage_flags,
            "params": dict(call_options.params),
            "readonly": not call_options.touch,
            "touch": call_options.touch,
        })

        embed_start = time.perf_counter()
        try:
            embedding_task = asyncio.create_task(self.embedding.get_embedding(text))
            try:
                query_vec = await asyncio.wait_for(
                    asyncio.shield(embedding_task),
                    timeout=_INJECT_EMBEDDING_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[WaveMemory] embedding exceeded %.1fs; waiting for result without cancelling",
                    _INJECT_EMBEDDING_TIMEOUT_SEC,
                )
                query_vec = await embedding_task
        except Exception as exc:
            if collector is None:
                raise
            self._trace_warning(collector, "embedding", "embedding_failed", str(exc))
            self._trace_record(collector, "embedding", {"enabled": True, "available": False, "reason_code": "embedding_failed"})
            return []
        embed_ms = (time.perf_counter() - embed_start) * 1000
        if query_vec is None:
            self._trace_warning(collector, "embedding", "embedding_unavailable", "Embedding service returned no vector")
            self._trace_record(collector, "embedding", {"enabled": True, "available": False, "reason_code": "embedding_unavailable", "latency_ms": embed_ms})
            return []
        query_vec = np.asarray(query_vec, dtype=np.float32)
        self._trace_record(collector, "embedding", {
            "enabled": True,
            "available": True,
            "dimension": int(query_vec.size),
            "latency_ms": round(embed_ms, 1),
        })

        if is_private:
            # Private recall deliberately bypasses all tag-derived stages and uses
            # the raw embedding; HNSW IDs are then exact-Scope filtered below.
            search_vec, energy_field = query_vec, {}
        else:
            search_vec, energy_field = self._wave_boost(
                query_vec,
                scope=resolved_scope,
                options=call_options,
                collector=collector,
                stage_flags=stage_flags,
            )
        # A source filter needs headroom because most knn hits get dropped, but an
        # unbounded 20x fan-out made the hot search dominate the channel budget.
        candidates_k = min(top_k * 20, _MAX_FILTERED_CANDIDATES) if source_filter else top_k * 3
        search_start = time.perf_counter()

        def _hot_and_cold_sync() -> tuple[list, list[tuple[dict[str, Any], float]], dict[str, Any], list[dict]]:
            try:
                hot = self.memory_index.search(search_vec, k=candidates_k)
            except Exception as exc:
                if collector is None:
                    raise
                self._trace_warning(collector, "vector_search", "vector_search_failed", str(exc))
                hot = []
            if is_private:
                cold, cold_meta = [], {
                    "enabled": False,
                    "available": False,
                    "reason_code": "private_raw_vector_only",
                }
            else:
                cold, cold_meta = self._search_scoped_cold_memories(
                    tag_query_vec=query_vec,
                    score_query_vec=search_vec,
                    policy=recall_policy,
                )
            hot_ids = [item[0] for item in hot]
            hot_memories = self._get_scoped_memories_by_ids(hot_ids, recall_policy)
            return hot, cold, cold_meta, hot_memories

        try:
            (
                raw_candidates,
                cold_candidates,
                cold_details,
                memories,
            ) = await asyncio.to_thread(_hot_and_cold_sync)
        except Exception as exc:
            if collector is None:
                raise
            self._trace_warning(collector, "vector_search", "vector_search_failed", str(exc))
            raw_candidates = []
            cold_candidates, cold_details = [], {"enabled": False, "available": False, "reason_code": "sync_search_failed"}
            memories = []
        search_ms = (time.perf_counter() - search_start) * 1000

        memory_ids = [item[0] for item in raw_candidates]
        distances = {int(item[0]): float(item[1]) for item in raw_candidates}
        for memory in memories:
            memory["_retrieval_tier"] = "hot"
        scoped_ids = {int(memory.get("id")) for memory in memories if memory.get("id") is not None}
        scoped_candidates = [item for item in raw_candidates if int(item[0]) in scoped_ids]
        known_ids = set(scoped_ids)
        for cold_memory, cold_distance in cold_candidates:
            memory_id = int(cold_memory["id"])
            if memory_id in known_ids:
                distances[memory_id] = min(distances.get(memory_id, float(cold_distance)), float(cold_distance))
                continue
            known_ids.add(memory_id)
            distances[memory_id] = float(cold_distance)
            memories.append(cold_memory)

        self._trace_record(collector, "vector_search", {
            "enabled": True,
            "available": True,
            "candidate_count": len(raw_candidates),
            "scoped_candidate_count": len(scoped_candidates),
            "filtered_out_count": len(raw_candidates) - len(scoped_candidates),
            "cold": cold_details,
            "k": candidates_k,
            "used_vector": "wave_boosted" if energy_field else "raw",
            "top_candidates": [
                {"rank": rank, "memory_id": item[0], "distance": round(float(item[1]), 4), "similarity": round(1.0 - float(item[1]), 4)}
                for rank, item in enumerate(scoped_candidates, 1)
            ],
            "latency_ms": round(search_ms, 1),
        })
        if not memories:
            self._trace_record(collector, "final", {"result_count": 0, "ids": [], "cold": cold_details})
            return []

        normalized_source_filter = [source_filter] if isinstance(source_filter, str) else list(source_filter or [])
        if normalized_source_filter:
            memories = [memory for memory in memories if memory.get("source", "live") in normalized_source_filter]
        elif exclude_sources:
            excluded = set(exclude_sources)
            memories = [memory for memory in memories if memory.get("source", "live") not in excluded]

        score_breakdown: dict[Any, dict[str, Any]] = {}
        now = time.time()
        import math
        for memory in memories:
            memory["_is_cross_group"] = recall_policy.is_cross_group(memory)
            similarity = 1.0 - distances.get(memory["id"], 1.0)
            memory["similarity"] = similarity
            timestamp = memory.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    from datetime import datetime
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    timestamp = now
            elif timestamp is None:
                timestamp = now
            time_decay = 0.997 ** max(0, (now - timestamp) / 86400.0)
            access_boost = 1.0 + math.log2(1 + (memory.get("access_count", 0) or 0)) * 0.15
            importance = memory.get("importance", 1.0)
            memory["score"] = similarity * importance * time_decay * access_boost
            score_breakdown[memory["id"]] = {
                "memory_id": memory["id"],
                "similarity": round(similarity, 4),
                "importance": round(float(importance), 4),
                "time_decay": round(time_decay, 4),
                "access_boost": round(access_boost, 4),
                "score_before_geodesic": round(memory["score"], 4),
            }

        geodesic_enabled = stage_flags["geodesic"]
        geodesic_details: list[dict[str, Any]] = []
        if geodesic_enabled and self.geodesic and energy_field and memories:
            geo = self._stage_copy(self.geodesic, {"alpha": call_options.params.get("geodesic_alpha")})
            # The energy graph carries scoped-tag IDs. Never feed legacy-tag
            # memories into it, because the legacy tag ID space is independent.
            candidates_for_rerank = [
                {"id": memory["id"], "score": memory["score"]}
                for memory in memories
                if memory.get("_tag_lane") != "legacy"
            ]
            before_rank = {item["id"]: rank for rank, item in enumerate(candidates_for_rerank, 1)}
            if not candidates_for_rerank:
                self._trace_record(collector, "geodesic", {"enabled": True, "available": False, "reason_code": "legacy_tag_lane_only"})
            try:
                reranked = geo.rerank(candidates_for_rerank, energy_field)
                rerank_scores = {item["id"]: item["score"] for item in reranked}
                for rank, item in enumerate(reranked, 1):
                    memory_id = item["id"]
                    geodesic_details.append({
                        "memory_id": memory_id,
                        "rank_before": before_rank.get(memory_id),
                        "rank_after": rank,
                        "score_after": round(float(item.get("score", 0)), 4),
                        "geo_score": round(float(item.get("geo_score", 0) or 0), 4),
                    })
                    if memory_id in score_breakdown:
                        score_breakdown[memory_id].update(geodesic_details[-1])
                for memory in memories:
                    if memory["id"] in rerank_scores:
                        memory["score"] = rerank_scores[memory["id"]]
                self._trace_record(collector, "geodesic", {
                    "enabled": True, "available": True,
                    "params": {"alpha": getattr(geo, "alpha", None)},
                    "before_ids": list(before_rank), "after_ids": [item["id"] for item in reranked],
                    "reranked": geodesic_details,
                })
            except Exception as exc:
                self._trace_warning(collector, "geodesic", "geodesic_rerank_failed", str(exc))
                self._trace_record(collector, "geodesic", {"enabled": True, "available": False, "reason_code": "geodesic_rerank_failed"})
        elif geodesic_enabled:
            reason_code = "geodesic_unavailable" if self.geodesic is None else "spike_energy_unavailable"
            self._trace_record(collector, "geodesic", {"enabled": True, "available": False, "reason_code": reason_code})
            self._trace_warning(collector, "geodesic", reason_code, reason_code)
        else:
            self._trace_record(collector, "geodesic", {"enabled": False, "available": False, "reason_code": "disabled_by_request"})

        before_threshold = len(memories)
        memories = [memory for memory in memories if memory["similarity"] >= self.min_similarity]
        # Collapse historical fanout clones before top_k, otherwise duplicate
        # projections can fill the entire result window and starve unique rows.
        current_group_id = ""
        if resolved_scope is not None and resolved_scope.session is not None:
            current_group_id = resolved_scope.session.conversation_id
        memories = self._prefer_current_group_and_dedupe(
            memories,
            current_group_id=current_group_id,
        )
        memories = memories[:top_k]
        final_ids = [memory["id"] for memory in memories]
        final_breakdown = []
        for rank, memory_id in enumerate(final_ids, 1):
            item = dict(score_breakdown.get(memory_id, {"memory_id": memory_id}))
            item["rank_after"] = rank
            item["score_after"] = round(float(next(memory["score"] for memory in memories if memory["id"] == memory_id)), 4)
            final_breakdown.append(item)
        self._trace_record(collector, "scoring", {
            "enabled": True, "available": True,
            "before_filter_count": before_threshold,
            "after_filter_count": len(memories),
            "min_similarity": self.min_similarity,
            "score_breakdown": final_breakdown,
        })

        if memories and call_options.touch:
            # Cross-group recall is intentionally read-only: the current Scope
            # cannot be used to manufacture an exact-scope mutation for another
            # group's memory. Touch failures are best-effort and never fail recall.
            touch_ids = recall_policy.touchable_ids(memories)
            if touch_ids:
                try:
                    if self.write_gateway is not None:
                        await self.write_gateway.touch_memories(scope=resolved_scope, memory_ids=touch_ids)
                    else:
                        self.db.touch_memories(touch_ids)
                except Exception:
                    logger.warning("[WaveMemory] Safe memory touch failed", exc_info=True)
        self._trace_record(collector, "final", {
            "result_count": len(memories), "ids": final_ids, "score_breakdown": final_breakdown,
            "readonly": not call_options.touch, "touch": call_options.touch,
        })
        self._trace_record(collector, "highlights", {
            "geodesic_memory_ids": [item["memory_id"] for item in geodesic_details],
            "final_memory_ids": final_ids,
        })

        total_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            f"[WaveMemory] Query done: {len(memories)} results, "
            f"embed={embed_ms:.0f}ms, total={total_ms:.0f}ms"
        )
        self._trace_record(collector, "timing", {"embedding_ms": round(embed_ms, 1), "total_ms": round(total_ms, 1)})
        return memories

    def _wave_boost(
        self,
        query_vec: np.ndarray,
        *,
        scope: RuntimeScope | None = None,
        options: QueryOptions | None = None,
        collector: QueryDebugCollector | None = None,
        stage_flags: Mapping[str, bool] | None = None,
    ) -> tuple[np.ndarray, dict]:
        """VCP TagMemo 浪潮增强；所有调试参数只作用于 stage 浅副本。"""
        call_options = options if isinstance(options, QueryOptions) else QueryOptions()
        flags = dict(stage_flags or {
            "epa": self._stage_enabled(call_options, "epa", self.enable_epa),
            "pyramid": self._stage_enabled(call_options, "pyramid", self.enable_pyramid),
            "spike": self._stage_enabled(call_options, "spike", self.enable_spike),
            "geodesic": self._stage_enabled(call_options, "geodesic", self.enable_geodesic),
        })
        energy_field: dict[Any, float] = {}
        highlights = {"pyramid_tags": [], "seed_tags": [], "emergent_tags": []}

        if not self.tag_index or getattr(self.tag_index, "count", 10) < 10:
            reason_code = "tag_index_unavailable"
            for stage_name in ("epa", "pyramid", "spike"):
                self._trace_record(collector, stage_name, {
                    "enabled": flags[stage_name], "available": False, "reason_code": reason_code,
                })
                if flags[stage_name]:
                    self._trace_warning(collector, stage_name, reason_code, "Tag index is unavailable")
            return query_vec, energy_field

        logic_depth = 0.5
        entropy = 0.5
        epa_result: dict[str, Any] | None = None
        if flags["epa"]:
            if self.epa and getattr(self.epa, "initialized", False):
                try:
                    epa_result = self.epa.analyze(query_vec)
                    logic_depth = float(epa_result.get("logic_depth", 0.5))
                    entropy = float(epa_result.get("entropy", 0.5))
                    self._trace_record(collector, "epa", {
                        "enabled": True, "available": True,
                        "logic_depth": round(logic_depth, 4), "entropy": round(entropy, 4),
                        "dominant_axis": epa_result.get("dominant_axis"),
                        "interpretation": "focused" if logic_depth >= 0.66 else "diffuse" if logic_depth <= 0.33 else "mixed",
                    })
                except Exception as exc:
                    self._trace_record(collector, "epa", {"enabled": True, "available": False, "reason_code": "epa_analysis_failed"})
                    self._trace_warning(collector, "epa", "epa_analysis_failed", str(exc))
            else:
                self._trace_record(collector, "epa", {"enabled": True, "available": False, "reason_code": "epa_unavailable"})
                self._trace_warning(collector, "epa", "epa_unavailable", "EPA module is unavailable or uninitialized")
        else:
            self._trace_record(collector, "epa", {"enabled": False, "available": False, "reason_code": "disabled_by_request"})

        matched_tags: list[tuple[Any, float]] = []
        if flags["pyramid"]:
            if self.residual_pyramid:
                pyramid = self._stage_copy(self.residual_pyramid, {
                    "max_levels": call_options.params.get("pyramid_max_levels"),
                    "top_k": call_options.params.get("pyramid_top_k"),
                })
                try:
                    try:
                        pyramid_result = pyramid.analyze(query_vec, scope=scope)
                    except TypeError:
                        # Compatibility for focused-test/custom stages with a one-arg contract.
                        pyramid_result = pyramid.analyze(query_vec)
                    compact_levels = []
                    for level_tags in pyramid_result.get("levels", []):
                        compact_level = []
                        for tag_info in level_tags:
                            raw_tag_id = tag_info.get("tag_id")
                            similarity = float(tag_info.get("similarity", 0))
                            mapped_ids = self._map_catalog_hits_to_scope(scope, [raw_tag_id]) if raw_tag_id else {}
                            mapper_available = callable(getattr(self.db, "list_scoped_catalog_links", None)) and isinstance(scope, RuntimeScope)
                            mapped_tag_ids = mapped_ids.get(int(raw_tag_id), ()) if mapper_available and raw_tag_id else ()
                            tag_id = mapped_tag_ids[0] if mapped_tag_ids else raw_tag_id
                            compact = {
                                "tag_id": tag_id,
                                "similarity": round(similarity, 4),
                                "level": tag_info.get("level"),
                            }
                            compact_level.append(compact)
                            if tag_id and similarity > 0.1:
                                matched_tags.append((tag_id, similarity))
                                highlights["pyramid_tags"].append(compact)
                        compact_levels.append(compact_level)
                    self._trace_record(collector, "pyramid", {
                        "enabled": True, "available": True,
                        "params": {"max_levels": getattr(pyramid, "max_levels", None), "top_k": getattr(pyramid, "top_k", None)},
                        "level_count": len(compact_levels), "levels": compact_levels,
                        "coverage": round(float(pyramid_result.get("coverage", 0)), 4),
                        "tag_count": len(pyramid_result.get("all_tag_ids", [])),
                    })
                except Exception as exc:
                    self._trace_record(collector, "pyramid", {"enabled": True, "available": False, "reason_code": "pyramid_analysis_failed"})
                    self._trace_warning(collector, "pyramid", "pyramid_analysis_failed", str(exc))
            else:
                self._trace_record(collector, "pyramid", {"enabled": True, "available": False, "reason_code": "pyramid_unavailable"})
                self._trace_warning(collector, "pyramid", "pyramid_unavailable", "Residual pyramid module is unavailable")
        else:
            self._trace_record(collector, "pyramid", {"enabled": False, "available": False, "reason_code": "disabled_by_request"})

        if not matched_tags:
            try:
                tag_results = self._search_scoped_tags(query_vec, scope, k=10)
                matched_tags.extend((tag_id, float(similarity)) for tag_id, similarity in tag_results if float(similarity) > 0.2)
            except Exception as exc:
                self._trace_warning(collector, "tag_search", "tag_search_failed", str(exc))

        if flags["spike"]:
            if self.spike_router and self.cooccurrence and self.cooccurrence.node_count > 0 and matched_tags:
                spike = self._stage_copy(self.spike_router, {
                    "max_hops": call_options.params.get("spike_max_hops"),
                    "firing_threshold": call_options.params.get("spike_firing_threshold"),
                })
                seed_tags = [{"tag_id": tag_id, "weight": weight} for tag_id, weight in matched_tags[:10]]
                highlights["seed_tags"] = [dict(item) for item in seed_tags]
                try:
                    spike_result = spike.propagate(seed_tags, epa_result={"logic_depth": logic_depth, "entropy": entropy})
                    energy_field = dict(spike_result.get("energy_field", {}))
                    activated = []
                    for item in spike_result.get("activated_tags", []):
                        compact = {
                            "tag_id": item.get("tag_id"),
                            "energy": round(float(item.get("energy", 0)), 4),
                            "is_emergent": bool(item.get("is_emergent")),
                        }
                        activated.append(compact)
                        if compact["is_emergent"] and compact["energy"] > 0.1:
                            matched_tags.append((compact["tag_id"], compact["energy"] * 0.5))
                            highlights["emergent_tags"].append(compact)
                    self._trace_record(collector, "spike", {
                        "enabled": True, "available": True,
                        "params": {"max_hops": getattr(spike, "max_hops", None), "firing_threshold": getattr(spike, "firing_threshold", None)},
                        "seed_count": len(seed_tags), "seed_tags": seed_tags,
                        "activated_count": len(activated), "activated_tags": activated,
                        "energy_field_size": len(energy_field),
                        "energy_field_top": [
                            {"tag_id": tag_id, "energy": round(float(energy), 4)}
                            for tag_id, energy in sorted(energy_field.items(), key=lambda item: float(item[1]), reverse=True)
                        ],
                    })
                except Exception as exc:
                    self._trace_record(collector, "spike", {"enabled": True, "available": False, "reason_code": "spike_routing_failed"})
                    self._trace_warning(collector, "spike", "spike_routing_failed", str(exc))
            else:
                reason_code = "spike_unavailable" if self.spike_router is None else "spike_seed_unavailable"
                self._trace_record(collector, "spike", {"enabled": True, "available": False, "reason_code": reason_code})
                self._trace_warning(collector, "spike", reason_code, reason_code)
        else:
            self._trace_record(collector, "spike", {"enabled": False, "available": False, "reason_code": "disabled_by_request"})

        self._trace_record(collector, "highlights", highlights)
        if not matched_tags:
            return query_vec, energy_field

        base_boost = 0.3
        dynamic_factor = logic_depth * (1.0 / (1.0 + entropy * 0.5))
        alpha = min(0.6, base_boost * max(0.5, min(2.0, dynamic_factor)))
        tag_weights: dict[Any, float] = {}
        for tag_id, weight in matched_tags:
            tag_weights[tag_id] = max(weight, tag_weights.get(tag_id, 0))
        try:
            tag_vecs = self._load_tag_vectors(scope, list(tag_weights))
        except Exception as exc:
            self._trace_warning(collector, "wave_boost", "tag_vector_load_failed", str(exc))
            return query_vec, energy_field

        context_vec = np.zeros_like(query_vec)
        total_weight = 0.0
        for tag_id, weight in tag_weights.items():
            if tag_id in tag_vecs:
                context_vec += tag_vecs[tag_id] * weight
                total_weight += weight
        if total_weight <= 0:
            return query_vec, energy_field
        context_vec /= total_weight
        norm = np.linalg.norm(context_vec)
        if norm > 1e-10:
            context_vec /= norm
        fused = (1 - alpha) * query_vec + alpha * context_vec
        fused_norm = np.linalg.norm(fused)
        if fused_norm > 1e-10:
            fused /= fused_norm
        return fused.astype(np.float32), energy_field

    async def shotgun_query(
        self,
        text: str,
        context_messages: list[str] = None,
        group_id: Optional[str] = None,
        top_k: int = 5,
        *,
        scope: RuntimeScope | None = None,
    ) -> list[dict]:
        """多路霰弹枪检索。"""
        resolved_scope = self._memory_scope(scope)
        if resolved_scope is None:
            logger.warning("[WaveMemory] Shotgun query rejected: resolved group/private RuntimeScope required")
            return []

        recall_policy = self._resolve_recall_policy(resolved_scope)
        is_private = resolved_scope.visibility == "private"
        start = time.time()

        query_vec = await self.embedding.get_embedding(text)
        if query_vec is None:
            return []

        if is_private:
            search_vec, energy_field = query_vec, {}
        else:
            search_vec, energy_field = self._wave_boost(query_vec, scope=resolved_scope)
        main_results = self.memory_index.search(search_vec, k=top_k * 3)

        segment_results = []
        if context_messages and not is_private:
            segmenter = ContextSegmenter(
                similarity_threshold=float(self.config.get("shotgun_similarity_threshold", 0.70)),
                max_segments=int(self.config.get("shotgun_max_segments", 3)),
            )
            ctx_vecs = await self.embedding.get_embeddings(context_messages)
            if ctx_vecs:
                segment_vecs = segmenter.segment(ctx_vecs)
                for seg_vec in segment_vecs:
                    seg_results = self.memory_index.search(seg_vec, k=top_k * 2)
                    segment_results.extend(seg_results)

        all_candidates = {}
        for mem_id, dist in main_results:
            all_candidates[int(mem_id)] = min(all_candidates.get(int(mem_id), 999), float(dist))
        for mem_id, dist in segment_results:
            all_candidates[int(mem_id)] = min(all_candidates.get(int(mem_id), 999), float(dist))
        if is_private:
            cold_candidates = []
        else:
            cold_candidates, _cold_details = self._search_scoped_cold_memories(
                tag_query_vec=np.asarray(query_vec, dtype=np.float32),
                score_query_vec=np.asarray(search_vec, dtype=np.float32),
                policy=recall_policy,
            )

        memory_ids = list(all_candidates)
        memories = self._get_scoped_memories_by_ids(memory_ids, recall_policy)
        known_ids = {int(memory["id"]) for memory in memories if memory.get("id") is not None}
        for mem in memories:
            mem["_retrieval_tier"] = "hot"
        for cold_memory, cold_distance in cold_candidates:
            memory_id = int(cold_memory["id"])
            if memory_id in known_ids:
                all_candidates[memory_id] = min(all_candidates.get(memory_id, float(cold_distance)), float(cold_distance))
                continue
            known_ids.add(memory_id)
            all_candidates[memory_id] = float(cold_distance)
            memories.append(cold_memory)

        if not memories:
            return []

        for mem in memories:
            mem["_is_cross_group"] = recall_policy.is_cross_group(mem)

        for mem in memories:
            dist = all_candidates.get(mem["id"], 1.0)
            mem["similarity"] = 1.0 - dist
            mem["score"] = mem["similarity"] * mem.get("importance", 1.0)

        if self.enable_geodesic and self.geodesic and energy_field:
            candidates_for_rerank = [
                {"id": m["id"], "score": m["score"]}
                for m in memories
                if m.get("_tag_lane") != "legacy"
            ]
            reranked = self.geodesic.rerank(candidates_for_rerank, energy_field)
            rerank_scores = {c["id"]: c["score"] for c in reranked}
            for mem in memories:
                if mem["id"] in rerank_scores:
                    mem["score"] = rerank_scores[mem["id"]]

        memories = [m for m in memories if m["similarity"] >= self.min_similarity]
        current_group_id = ""
        if resolved_scope is not None and resolved_scope.session is not None:
            current_group_id = resolved_scope.session.conversation_id
        memories = self._prefer_current_group_and_dedupe(
            memories,
            current_group_id=current_group_id,
        )
        if len(memories) > top_k:
            memories = self._svd_dedup(memories, query_vec, top_k)
        else:
            memories.sort(key=lambda m: m["score"], reverse=True)
            memories = memories[:top_k]

        if memories:
            touch_ids = recall_policy.touchable_ids(memories)
            if touch_ids:
                try:
                    if self.write_gateway is not None:
                        await self.write_gateway.touch_memories(
                            scope=resolved_scope,
                            memory_ids=touch_ids,
                        )
                    else:
                        self.db.touch_memories(touch_ids)
                except Exception:
                    logger.warning("[WaveMemory] Safe memory touch failed", exc_info=True)

        total_ms = (time.time() - start) * 1000
        logger.debug(
            f"[WaveMemory] Shotgun query done: {len(memories)} results, "
            f"candidates={len(all_candidates)}, total={total_ms:.0f}ms"
        )
        return memories

    def _svd_dedup(self, memories: list[dict], query_vec: np.ndarray, top_k: int) -> list[dict]:
        """SVD 主题去重。"""
        mem_ids = [m["id"] for m in memories]
        mem_vectors = self.db.get_memory_vectors(mem_ids)

        if not mem_vectors:
            memories.sort(key=lambda m: m["score"], reverse=True)
            return memories[:top_k]

        memories.sort(key=lambda m: m["score"], reverse=True)
        selected = []
        selected_vecs = []

        for mem in memories:
            if len(selected) >= top_k:
                break
            vec = mem_vectors.get(mem["id"])
            if vec is None:
                selected.append(mem)
                continue
            if selected_vecs:
                basis = np.vstack(selected_vecs)
                proj_coeffs = basis @ vec
                projection = proj_coeffs @ basis
                residual = vec - projection
                residual_norm = np.linalg.norm(residual)
                if residual_norm < 0.3:
                    continue
            selected.append(mem)
            norm = np.linalg.norm(vec)
            if norm > 1e-8:
                selected_vecs.append(vec / norm)

        return selected

    async def query_by_person(self, qq_id: str, topic: str = None, group_id: str = None, top_k: int = 8) -> list[dict]:
        """按人查询记忆。"""
        if topic:
            memories = self.db.get_memories_by_person(qq_id, limit=top_k * 5)
            if not memories:
                return []
            topic_vec = await self.embedding.get_embedding(topic)
            if topic_vec is None:
                return memories[:top_k]
            search_vec, _ = self._wave_boost(topic_vec)
            memory_ids = [m["id"] for m in memories]
            mem_vectors = self.db.get_memory_vectors(memory_ids)
            for mem in memories:
                vec = mem_vectors.get(mem["id"])
                if vec is not None:
                    sim = float(np.dot(search_vec, vec) / (np.linalg.norm(search_vec) * np.linalg.norm(vec) + 1e-10))
                    mem["score"] = max(0, sim) * mem.get("importance", 1.0)
                else:
                    mem["score"] = 0.0
            memories.sort(key=lambda m: m["score"], reverse=True)
            return [m for m in memories[:top_k] if m["score"] > 0.1]
        else:
            memories = self.db.get_memories_by_person(qq_id, role="sender", limit=top_k)
            for m in memories:
                m["score"] = m.get("importance", 1.0)
            return memories

    def format_injection(
        self,
        memories: list[dict],
        template: str = "",
        current_group_id: str = "",
        speaker_id: str = "",
        bot_ids: "set[str] | frozenset[str] | tuple[str, ...] | list[str] | None" = None,
    ) -> str:
        """将记忆列表格式化为注入文本，按 source 类型分段。

        ``speaker_id`` / ``bot_ids`` 给出后，群聊记忆会标注归属（对话者本人 /
        其他群友 / bot 自己）。不标注归属时，``[记忆] 某人(时间): ...`` 无法让模型
        分辨这句话是不是当前对话者说的，模型会把别人的历史当成对方说过的话。
        """
        if not memories:
            return ""
        if not template:
            template = "[记忆] {sender}({time}): {content}"

        # Prefer the active group and collapse fanout duplicates before rendering.
        ordered = self._prefer_current_group_and_dedupe(memories, current_group_id=current_group_id)

        # 按 source 分组
        your_memories = []  # bzz_experience — 白真真第一人称经历
        world_knowledge = []  # book_lore — 书设常识
        chat_memories = []  # live 及其他 — 群聊记忆

        for mem in ordered:
            source = mem.get("source", "live")
            if source == "bzz_experience":
                your_memories.append(mem)
            elif source == "book_lore":
                world_knowledge.append(mem)
            else:
                chat_memories.append(mem)

        sections = []

        # 第一人称经历：简洁格式，不加 sender 前缀
        if your_memories:
            lines = ["<your_memory>"]
            for mem in your_memories:
                content = mem.get("content", "")
                lines.append(content)
            lines.append("</your_memory>")
            sections.append("\n".join(lines))

        # 群聊记忆：标准格式
        if chat_memories:
            speaker = str(speaker_id or "").strip()
            bot_id_set = {str(b).strip() for b in (bot_ids or ()) if str(b or "").strip()}
            annotate = bool(speaker or bot_id_set)
            lines = ["<wave_memory>"]
            for mem in chat_memories:
                sender = mem.get("sender_name") or mem.get("sender_id") or "unknown"
                ts = time.strftime("%m-%d %H:%M", time.localtime(mem["timestamp"]))
                content = mem.get("content", "")
                score = mem.get("score", 0)
                group_id = mem.get("group_id", "")
                group_tag = ""
                if mem.get("_is_cross_group") and group_id:
                    group_tag = f"[群{group_id}] "
                owner_tag = ""
                if annotate:
                    owner_tag = self._attribution_tag(
                        mem, speaker_id=speaker, bot_id_set=bot_id_set
                    )
                line = template.replace("{sender}", sender).replace("{time}", ts).replace("{content}", content)
                lines.append(f"{group_tag}{owner_tag}{line} (relevance: {score:.2f})")
            lines.append("</wave_memory>")
            sections.append("\n".join(lines))

        # 书设常识：简洁格式
        if world_knowledge:
            lines = ["<world_knowledge>"]
            for mem in world_knowledge:
                content = mem.get("content", "")
                lines.append(content)
            lines.append("</world_knowledge>")
            sections.append("\n".join(lines))

        return "\n\n".join(sections)

    @staticmethod
    def _attribution_tag(
        memory: dict,
        *,
        speaker_id: str,
        bot_id_set: set[str],
    ) -> str:
        """Return an ownership prefix so the model cannot misattribute a memory.

        Without this, a recalled line from another member reads exactly like the
        current speaker's own history, which is how "you said that" mistakes are
        produced on the first reply.
        """
        sender_id = str(memory.get("sender_id") or "").strip()
        if sender_id and sender_id in bot_id_set:
            return "[你的历史回复] "
        if str(memory.get("source") or "") == "bot" or sender_id == "bot":
            return "[你的历史回复] "
        if speaker_id and sender_id == speaker_id:
            return "[对话者本人历史] "
        if sender_id:
            return "[其他群友历史] "
        return ""

    @staticmethod
    def _prefer_current_group_and_dedupe(
        memories: list[dict],
        *,
        current_group_id: str = "",
    ) -> list[dict]:
        """Prefer current-group rows and collapse identical fanout copies."""
        try:
            from .memory_collapse import collapse_memories
        except ImportError:  # pragma: no cover - top-level plugin import path
            from engine.memory_collapse import collapse_memories
        return collapse_memories(memories, current_group_id=current_group_id)
