"""Wave Memory 异步写入服务 — 带 source 分层门控"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from astrbot.api import logger

from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex
from ..engine.embedding import EmbeddingService
from .identity_safety import is_identity_contamination
try:
    from ..domain.quality import QualityDecision, QualityProposal
    from ..domain.scope import RuntimeScope
    from .quality_gate import QualityGate, decode_quality_evidence
    from .storage_capacity_policy import (
        StorageCapacityPolicy,
        count_active_memories,
        decide_storage_admission,
    )
except ImportError:  # 兼容独立测试/外部调用 services
    from domain.quality import QualityDecision, QualityProposal
    from domain.scope import RuntimeScope
    from services.quality_gate import QualityGate, decode_quality_evidence
    from services.storage_capacity_policy import (
        StorageCapacityPolicy,
        count_active_memories,
        decide_storage_admission,
    )


# noise 判定：这些消息不入向量索引
_NOISE_MAX_LENGTH = 10


class MessageScopeError(ValueError):
    """Stable rejection for a scope-bearing MessageWriter payload."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class MemoryDedupPolicy:
    """普通记忆写入统一去重策略。

    覆盖 MessageWriter 队列入口，因此自动捕获、Agent remember 与
    LivingMemory-compatible facade 都共享同一内容级去重规则。
    """

    window_seconds: float = 300.0
    enabled: bool = True

    def filter_batch(self, db: Any, batch: list[dict]) -> tuple[list[dict], int]:
        if not self.enabled or not batch:
            return batch, 0

        unique_by_key: dict[tuple[str, str, str, str], dict] = {}
        duplicate_count = 0
        for item in batch:
            normalized = self.normalize_content(item.get("content", ""))
            if not normalized:
                continue
            candidate = dict(item)
            candidate["content"] = normalized
            candidate["_dedup_normalized_content"] = normalized
            scope = self._require_memory_scope(candidate)
            if self.find_recent_duplicate(db, candidate, normalized):
                duplicate_count += 1
                continue
            assert scope.session is not None
            key = (scope.bot_id, scope.session.id, scope.visibility, normalized)
            existing = unique_by_key.get(key)
            if existing is None:
                unique_by_key[key] = candidate
                continue
            duplicate_count += 1
            if self._importance(candidate) > self._importance(existing):
                unique_by_key[key] = candidate
        return list(unique_by_key.values()), duplicate_count

    @staticmethod
    def _require_memory_scope(item: Mapping[str, Any]) -> RuntimeScope:
        scope = item.get("scope")
        if not isinstance(scope, RuntimeScope):
            raise MessageScopeError("scope_required", "MessageWriter requires a RuntimeScope")
        if scope.visibility not in {"group", "private"} or scope.session is None:
            raise MessageScopeError(
                "legacy_writer_scope_visibility_unsupported",
                "MessageWriter only persists group/private RuntimeScope values",
            )
        return scope

    def find_recent_duplicate(self, db: Any, item: dict, normalized_content: str) -> Any:
        scope = self._require_memory_scope(item)
        assert scope.session is not None
        try:
            timestamp = float(item.get("timestamp") or time.time())
        except (TypeError, ValueError):
            timestamp = time.time()
        since_ts = timestamp - max(float(self.window_seconds or 0), 0.0)

        finder = getattr(db, "find_recent_duplicate_memory", None)
        if callable(finder):
            return finder(scope=scope, normalized_content=normalized_content, since_ts=since_ts)

        conn = getattr(db, "conn", None)
        if conn is None:
            return None
        try:
            rows = conn.execute(
                """SELECT id, content FROM memories
                   WHERE bot_id=? AND session_id=? AND visibility=?
                     AND resolution_state='resolved' AND quarantine=0
                     AND timestamp>=? AND memory_type='message'
                   ORDER BY timestamp DESC LIMIT 100""",
                (scope.bot_id, scope.session.id, scope.visibility, since_ts),
            ).fetchall()
            for row in rows:
                if self.normalize_content(row[1] if len(row) > 1 else "") == normalized_content:
                    return row[0]
        except Exception:
            return None
        return None

    @staticmethod
    def normalize_content(content: Any) -> str:
        text = str(content or "").strip()
        return re.sub(r"\s+", " ", text)

    @staticmethod
    def _importance(item: dict) -> float:
        try:
            return float(item.get("importance", 1.0))
        except (TypeError, ValueError):
            return 1.0


_SYSTEM_ERROR_MARKERS = (
    "llm 响应错误",
    "all chat models failed",
    "apiconnectionerror",
    "ratelimiterror",
    "apistatuserror",
    "apitimeouterror",
    "images 请求失败",
)


def is_system_error_message(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(marker in lowered for marker in _SYSTEM_ERROR_MARKERS)


def classify_source(
    message: str,
    sender_id: str,
    bot_keywords: set[str],
    is_at_bot: bool = False,
    noise_max_length: int = _NOISE_MAX_LENGTH,
) -> str:
    """规则引擎：零 LLM 调用判断 source 分类。

    Returns: "core" / "chat" / "noise"
    """
    # 0. 系统/模型报错文本一律视为噪音，绝不进入核心记忆或送去提标
    if is_system_error_message(message):
        return "noise"
    # 1. bot 自己发的 → core
    if sender_id == "bot":
        return "core"
    # 2. 消息含 @bot → core
    if is_at_bot:
        return "core"
    # 3. 消息含 bot 名字/别名 → core
    msg_lower = message.lower()
    if any(kw.lower() in msg_lower for kw in bot_keywords if kw):
        return "core"
    # 4. 过短 → noise
    if len(message.strip()) < noise_max_length:
        return "noise"
    # 5. 其余 → chat
    return "chat"


class MessageWriter:
    """异步消息写入服务。

    负责：批量 embedding → 按 source 分类 → 写入 memories。
    HNSW 准入由 committed outbox 的 Tag 驱动热索引投影决定；新 core/chat
    先保留在 canonical 冷层，只有获得有效 Scoped Tag 后才可能升温。
    """

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service: EmbeddingService,
        bot_keywords: set[str] = None,
        noise_max_length: int = _NOISE_MAX_LENGTH,
        batch_size: int = 10,
        flush_interval: float = 30.0,
        dedup_policy: MemoryDedupPolicy | None = None,
        quality_gate: QualityGate | None = None,
        write_gateway: Any | None = None,
        embedding_timeout: float = 8.0,
        embedding_retry_attempts: int = 2,
        embedding_retry_backoff_seconds: float = 0.5,
        on_vector_backfill_requested: Any | None = None,
        storage_capacity_policy: StorageCapacityPolicy | None = None,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.bot_keywords = bot_keywords or set()
        self.noise_max_length = noise_max_length
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.dedup_policy = dedup_policy or MemoryDedupPolicy()
        self.quality_gate = quality_gate or QualityGate()
        self.write_gateway = write_gateway
        self.embedding_timeout = max(float(embedding_timeout), 0.1)
        self.on_vector_backfill_requested = on_vector_backfill_requested
        self.storage_capacity_policy = storage_capacity_policy or StorageCapacityPolicy()
        try:
            self.embedding_retry_attempts = min(max(int(embedding_retry_attempts), 1), 5)
        except (TypeError, ValueError):
            self.embedding_retry_attempts = 2
        try:
            self.embedding_retry_backoff_seconds = min(
                max(float(embedding_retry_backoff_seconds), 0.0),
                10.0,
            )
        except (TypeError, ValueError):
            self.embedding_retry_backoff_seconds = 0.5

        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._accepting = True
        self._write_count = 0
        self._save_threshold = 100

        # 统计
        self._stats = {
            "core": 0,
            "chat": 0,
            "noise": 0,
            "writer_failures": 0,
            "consecutive_failures": 0,
            "embedding_timeouts": 0,
            "embedding_retries": 0,
            "embedding_recovered_after_retry": 0,
            "embedding_terminal_timeouts": 0,
            "cold_chat_writes": 0,
            "capacity_over_writes": 0,
            "last_success_at": None,
        }

    def start(self, supervisor=None):
        self._running = True
        if supervisor is None:
            self._task = asyncio.create_task(self._run())
        else:
            self._task = supervisor.start(
                "wave-memory:message-writer", self._run(), owner="message-writer"
            )
        logger.info("[WaveMemory] MessageWriter started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    @staticmethod
    def _normalize_scope_payload(message_data: dict[str, Any]) -> dict[str, Any]:
        """Require an event-resolved group/private Scope and project its compatibility key."""
        if not isinstance(message_data, dict):
            raise MessageScopeError("invalid_message_payload", "message payload must be a dict")
        item = dict(message_data)
        scope = item.get("scope")
        if scope is None:
            raise MessageScopeError("scope_required", "MessageWriter requires an event-resolved RuntimeScope")
        if not isinstance(scope, RuntimeScope):
            raise MessageScopeError("invalid_runtime_scope", "scope must be RuntimeScope")
        if scope.visibility not in {"group", "private"} or scope.session is None:
            raise MessageScopeError(
                "legacy_writer_scope_visibility_unsupported",
                "MessageWriter only projects group/private RuntimeScope values",
            )
        canonical_group_id = scope.session.conversation_id
        declared_group_id = str(item.get("group_id") or "").strip()
        if declared_group_id and declared_group_id != canonical_group_id:
            raise MessageScopeError(
                "scope_session_mismatch",
                "payload group_id does not match RuntimeScope session",
            )
        item["group_id"] = canonical_group_id
        return item

    @staticmethod
    def _provenance(item: Mapping[str, Any], *, quarantined: bool = False) -> dict[str, Any]:
        """Build a compact, versioned origin record without duplicating message text."""
        raw_metadata = item.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        event_id = metadata.get("event_id") or item.get("event_id")
        origin_kind = str(metadata.get("origin_kind") or f"message_writer:{item.get('source', 'live')}")
        try:
            captured_at = float(item.get("timestamp") or time.time())
        except (TypeError, ValueError):
            captured_at = time.time()
        provenance: dict[str, Any] = {
            "schema": "memory-origin/v1",
            "origin_kind": origin_kind,
            "captured_at": captured_at,
            "source": str(item.get("source") or "live"),
        }
        if event_id not in {None, ""}:
            provenance["event_id"] = str(event_id)
        quality_decision = item.get("_quality_decision")
        if isinstance(quality_decision, QualityDecision):
            provenance["quality"] = {
                "outcome": quality_decision.outcome,
                "reason_code": quality_decision.reason_code,
                "rule_version": quality_decision.rule_version,
            }
        if quarantined:
            provenance["quarantine_reason"] = "identity_contamination"
        return provenance

    async def _persist_memory(
        self,
        item: Mapping[str, Any],
        *,
        vector: np.ndarray | None,
        importance: float,
        source: str,
        quarantine: bool = False,
    ) -> int:
        """Persist through the coordinated production ingress when it is wired."""
        timestamp = float(item.get("timestamp") or time.time())
        provenance = self._provenance(item, quarantined=quarantine)
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        raw_idempotency_hint = (
            metadata.get("event_id")
            or item.get("event_id")
            or item.get("message_id")
        )
        idempotency_hint = None
        if raw_idempotency_hint not in {None, ""}:
            scope = item["scope"]
            session_id = scope.session.id if getattr(scope, "session", None) is not None else "no-session"
            idempotency_hint = (
                f"{scope.bot_id}:{scope.visibility}:{session_id}:{raw_idempotency_hint}"
            )
        if self.write_gateway is not None:
            return await self.write_gateway.append_memory(
                scope=item["scope"],
                group_id=str(item["group_id"]),
                content=str(item.get("content") or ""),
                vector=vector,
                sender_id=str(item.get("sender_id") or ""),
                sender_name=str(item.get("sender_name") or ""),
                timestamp=timestamp,
                importance=float(importance),
                source=source,
                provenance=provenance,
                origin_metadata=provenance,
                quarantine=quarantine,
                idempotency_hint=None if idempotency_hint in {None, ""} else str(idempotency_hint),
            )
        return self.db.add_memory(
            group_id=item["group_id"],
            content=item["content"],
            vector=vector,
            sender_id=item.get("sender_id", ""),
            sender_name=item.get("sender_name", ""),
            timestamp=timestamp,
            importance=float(importance),
            source=source,
            scope=item["scope"],
            provenance=provenance,
            origin_metadata=provenance,
            quarantine=quarantine,
        )

    async def enqueue(self, message_data: dict):
        """Validate scope-bearing payloads before placing them in the write queue."""
        if not self._accepting:
            raise RuntimeError("message_writer_ingress_closed")
        await self._queue.put(self._normalize_scope_payload(message_data))

    async def shutdown(self) -> None:
        """Fence ingress, settle accepted queue items, then stop the worker task."""
        self._accepting = False
        await self._queue.join()
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self):
        """主循环：批量处理队列中的消息。"""
        while self._running:
            try:
                batch = []
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=self.flush_interval
                    )
                    batch.append(item)
                except asyncio.TimeoutError:
                    continue

                while len(batch) < self.batch_size:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                try:
                    await self._process_batch(batch)
                finally:
                    for _ in batch:
                        self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["writer_failures"] = self._stats.get("writer_failures", 0) + 1
                self._stats["consecutive_failures"] = self._stats.get("consecutive_failures", 0) + 1
                logger.warning(
                    "[WaveMemory] Writer error type=%s error=%r queue_depth=%s consecutive=%s",
                    type(e).__name__,
                    e,
                    self._queue.qsize(),
                    self._stats["consecutive_failures"],
                )
                await asyncio.sleep(2)

        # HNSW persistence is owned by the committed-outbox projection barrier.

    async def _embed_with_limited_retry(self, texts: list[str]) -> list[Any] | None:
        """Request one embedding batch with bounded timeout retries.

        The caller persists the original memories even after terminal timeout; the
        durable vector-backfill maintenance job can later recover those vectors.
        """
        for attempt in range(self.embedding_retry_attempts):
            try:
                vectors = await asyncio.wait_for(
                    self.embedding.get_embeddings(texts),
                    timeout=self.embedding_timeout,
                )
            except asyncio.TimeoutError:
                self._stats["embedding_timeouts"] = self._stats.get("embedding_timeouts", 0) + 1
                if attempt + 1 >= self.embedding_retry_attempts:
                    self._stats["embedding_terminal_timeouts"] = (
                        self._stats.get("embedding_terminal_timeouts", 0) + 1
                    )
                    logger.warning(
                        "[WaveMemory] Embedding timed out after %s attempt(s), %.1fs each; "
                        "persisting %s memory record(s) without vectors for backfill",
                        self.embedding_retry_attempts,
                        self.embedding_timeout,
                        len(texts),
                    )
                    return None
                self._stats["embedding_retries"] = self._stats.get("embedding_retries", 0) + 1
                delay = self.embedding_retry_backoff_seconds * (2**attempt)
                logger.warning(
                    "[WaveMemory] Embedding timeout attempt=%s/%s batch_size=%s; retrying in %.2fs",
                    attempt + 1,
                    self.embedding_retry_attempts,
                    len(texts),
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
                continue
            if attempt:
                self._stats["embedding_recovered_after_retry"] = (
                    self._stats.get("embedding_recovered_after_retry", 0) + 1
                )
            return list(vectors or [])
        return None  # defensive: retry attempts are normalized to at least one

    async def _process_batch(self, batch: list[dict]):
        """处理一批消息：分类 + embedding + 存储。"""
        batch = [self._normalize_scope_payload(item) for item in batch]
        # 分类
        for item in batch:
            if "source" not in item:
                item["source"] = classify_source(
                    message=item["content"],
                    sender_id=item.get("sender_id", ""),
                    bot_keywords=self.bot_keywords,
                    is_at_bot=item.get("is_at_bot", False),
                    noise_max_length=self.noise_max_length,
                )

        # Apply quality gate per message
        quality_filtered_batch = []
        contaminated_items = []
        for item in batch:
            raw_scope = item["scope"]
            content_text = item.get("content", "")
            
            proposal = self.quality_gate.propose(
                operation="message.write",
                content=content_text,
                raw_artifact=self.quality_gate.make_raw_artifact(
                    kind="chat_message",
                    artifact_id=item.get("message_id") or hashlib.sha256(content_text.encode()).hexdigest()[:16],
                    content=content_text,
                    source_scope=raw_scope,
                ),
                target_scope=raw_scope,
            )
            # The synchronous quality decision remains a fast in-memory gate. Its
            # audit repository shares the production writer and must never be a
            # prerequisite for recording the raw scoped message.
            decision = self.quality_gate.evaluate(proposal, record=False)
            item["_quality_decision"] = decision

            if decision.outcome == "reject":
                continue
            elif decision.outcome == "quarantine" or is_identity_contamination(content_text):
                item["_quality_decision"] = decision
                item["content"] = decision.normalized_content
                contaminated_items.append(item)
            else:
                item["_quality_decision"] = decision
                item["content"] = decision.normalized_content
                quality_filtered_batch.append(item)

        normal_batch = quality_filtered_batch
        normal_batch, duplicate_count = self.dedup_policy.filter_batch(self.db, normal_batch)

        if duplicate_count:
            self._stats["duplicate"] = self._stats.get("duplicate", 0) + duplicate_count

        # noise 不需要 embedding（省 API 调用）；身份接管污染不入向量索引，写入后归档，仅保留审计。
        need_embed = [item for item in normal_batch if item["source"] != "noise"]
        noise_items = [item for item in normal_batch if item["source"] == "noise"]

        # embed 非 noise 消息。Provider 超时不能丢掉原始消息；无向量记录
        # 仍会正式落库，并由维护/补向量任务恢复派生索引。
        vector_backfill_required = False
        if need_embed:
            texts = [item["content"] for item in need_embed]
            vectors = await self._embed_with_limited_retry(texts)
            if vectors is None:
                vectors = [None] * len(need_embed)
                vector_backfill_required = True
            if len(vectors) < len(need_embed):
                vectors = [*vectors, *([None] * (len(need_embed) - len(vectors)))]
            vector_backfill_required = vector_backfill_required or any(vector is None for vector in vectors)
        else:
            vectors = []

        # Soft canonical capacity: over-cap ordinary chat may keep text only.
        # One COUNT per batch keeps the extra query off the hot per-message path.
        try:
            active_count = count_active_memories(self.db.conn)
        except Exception:
            active_count = 0

        # 写入有向量的消息（超额 chat 可降为冷落库）
        for item, vec in zip(need_embed, vectors):
            try:
                decision = decide_storage_admission(
                    source=str(item.get("source") or "chat"),
                    active_count=active_count,
                    policy=self.storage_capacity_policy,
                    has_vector=vec is not None,
                )
                write_vector = vec if decision.keep_vector else None
                if decision.over_capacity:
                    self._stats["capacity_over_writes"] = (
                        self._stats.get("capacity_over_writes", 0) + 1
                    )
                if decision.demoted_to_cold:
                    self._stats["cold_chat_writes"] = self._stats.get("cold_chat_writes", 0) + 1
                    provenance_extra = item.setdefault("metadata", {})
                    if isinstance(provenance_extra, dict):
                        provenance_extra["storage_admission"] = decision.reason
                memory_id = await self._persist_memory(
                    item,
                    vector=write_vector,
                    importance=MemoryDedupPolicy._importance(item),
                    source=item["source"],
                )
                del memory_id  # id is assigned by the coordinator; stats only need counts

                # The committed memory.created event drives HNSW projection.
                self._write_count += 1
                self._stats[item["source"]] = self._stats.get(item["source"], 0) + 1
                # Approximate subsequent inserts in this batch without re-counting.
                active_count += 1

            except Exception as e:
                logger.debug(f"[WaveMemory] Single write failed: {e}")

        # 写入 noise（无向量，不入索引）
        for item in noise_items:
            try:
                await self._persist_memory(
                    item,
                    vector=None,
                    importance=0.3,
                    source="noise",
                )
                self._stats["noise"] = self._stats.get("noise", 0) + 1
            except Exception:
                pass

        # 写入身份接管污染审计，但立即归档/降权，不入向量索引、不参与召回。
        for item in contaminated_items:
            try:
                await self._persist_memory(
                    item,
                    vector=None,
                    importance=0.01,
                    source="identity_quarantine",
                    quarantine=True,
                )
                self._stats["identity_quarantine"] = self._stats.get("identity_quarantine", 0) + 1
            except Exception:
                pass

        if self._write_count >= self._save_threshold:
            self._write_count = 0
            logger.debug("[WaveMemory] Index save deferred to outbox projection barrier")

        if vector_backfill_required and callable(self.on_vector_backfill_requested):
            try:
                result = self.on_vector_backfill_requested()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                # Backfill scheduling must never turn a successfully persisted
                # source memory into a writer failure; startup detection retries it.
                logger.warning("[WaveMemory] vector backfill queue request failed", exc_info=True)

        self._stats["consecutive_failures"] = 0
        self._stats["last_success_at"] = time.time()

    @property
    def stats(self) -> dict:
        return dict(self._stats)
