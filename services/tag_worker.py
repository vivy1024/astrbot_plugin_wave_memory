"""TagWorker — 匀速后台标签提取 + source 升级判断"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from astrbot.api import logger

try:
    from ..domain.scope import RuntimeScope, ScopeValidationError, SessionRef
except ImportError:  # pragma: no cover - repository tests import top-level packages
    from domain.scope import RuntimeScope, ScopeValidationError, SessionRef


@dataclass(frozen=True)
class TagWorkItem:
    """Tag work for either a formal Scope or an explicitly unscoped legacy group."""

    memory_id: int
    content: str
    sender_name: str | None
    scope: RuntimeScope | None
    legacy_group_id: str = ""


class TagWorker:
    """匀速标签提取工作线程。

    每 interval_seconds 秒醒一次，取无标签记忆（< 2个标签），
    一次 batch LLM 调用打完，写回。
    打完标签后检查是否应将 chat → core（bot 相关标签升级）。
    """

    def __init__(
        self,
        db,
        tag_extractor,
        embedding_service,
        tag_index,
        config: dict = None,
        bot_keywords: set = None,
        write_gateway=None,
    ):
        self.db = db
        self.extractor = tag_extractor
        # Tag vectors are persisted on the canonical tag_catalog via the write gateway;
        # this compatibility dependency remains for embedding enrichment and tests.
        self.embedding = embedding_service
        self.tag_index = tag_index
        cfg = config or {}
        self.wake_interval = int(cfg.get("interval_seconds", 300))
        self.batch_size = int(cfg.get("max_batch_per_cycle", cfg.get("tag_worker_batch_size", 100)))
        self.bot_keywords = bot_keywords or set()
        self.include_recovered_backfill = bool(cfg.get("include_recovered_backfill", False))
        self.write_gateway = write_gateway
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.on_tags_written = None  # callback(count)

    def start(self, supervisor=None):
        if self._running:
            return
        self._running = True
        if supervisor is None:
            self._task = asyncio.create_task(self._loop())
        else:
            self._task = supervisor.start(
                "wave-memory:tag-worker", self._loop(), owner="tag-worker"
            )
        logger.info("[WaveMemory] TagWorker started")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        # 首次等 60s 让系统稳定
        await asyncio.sleep(60)
        while self._running:
            try:
                batch = self._fetch_untagged_batch()
                if batch:
                    await self._process_batch(batch)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WaveMemory] TagWorker error: {e}")
            # 固定间隔休眠
            try:
                await asyncio.sleep(self.wake_interval)
            except asyncio.CancelledError:
                break
        logger.info("[WaveMemory] TagWorker stopped")

    def _fetch_untagged_batch(self) -> list[TagWorkItem]:
        """Fetch missing-tag work from formal and legacy-group lanes.

        Existing legacy ``memory_tags`` are valid semantic evidence and therefore
        suppress re-extraction. Only no-tag rows (or rows with a retryable failed
        status) are selected; legacy rows retain their original group_id rather
        than receiving invented bot/session/visibility values.
        """
        conn = self.db.conn
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"id", "content", "group_id"} <= columns:
            return []
        select = lambda name, fallback: f"m.{name}" if name in columns else fallback
        bot_id = select("bot_id", "''")
        session_id = select("session_id", "''")
        visibility = select("visibility", "''")
        sender_name = select("sender_name", "''")
        active = []
        if "resolution_state" in columns:
            active.append("COALESCE(m.resolution_state, '') IN ('', 'resolved')")
        if "quarantine" in columns:
            active.append("COALESCE(m.quarantine, 0)=0")
        if "source" in columns:
            active.append("COALESCE(m.source, '') != 'noise'")
        if "memory_type" in columns:
            active.append("COALESCE(m.memory_type, 'message') != 'noise'")
        if "provenance" in columns and not self.include_recovered_backfill:
            active.append("COALESCE(m.provenance, '') NOT LIKE '%classified_legacy_recovery%'")
        status_filter = "1=1"
        if "tag_extraction_status" in tables:
            # done/skipped are terminal.  failed stays retryable only while
            # attempts stay under the poison budget; beyond that the row is
            # treated as skipped so TagWorker does not hot-loop forever.
            status_filter = (
                "NOT EXISTS ("
                "SELECT 1 FROM tag_extraction_status tes WHERE tes.memory_id=m.id AND ("
                "tes.status IN ('done', 'skipped') "
                "OR (tes.status='failed' AND COALESCE(tes.attempts, 0) >= 5)"
                "))"
            )
        legacy_link_missing = "1=1"
        if "memory_tags" in tables:
            legacy_link_missing = "NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id=m.id)"
        scoped_link_missing = "1=1"
        if "scoped_memory_tags" in tables and {"bot_id", "session_id", "visibility"} <= columns:
            scoped_link_missing = (
                "NOT EXISTS (SELECT 1 FROM scoped_memory_tags smt WHERE smt.memory_id=m.id "
                "AND smt.bot_id=m.bot_id AND smt.session_id=m.session_id AND smt.visibility=m.visibility)"
            )
        formal_memory_type = (
            "COALESCE(m.memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')"
            if "memory_type" in columns else "1=1"
        )
        legacy_memory_type = (
            "COALESCE(m.memory_type, 'message') NOT IN ('deleted', 'noise')"
            if "memory_type" in columns else "1=1"
        )
        formal = (
            f"COALESCE({bot_id}, '') != '' AND COALESCE({session_id}, '') != '' "
            f"AND COALESCE({visibility}, '')='group' AND ({formal_memory_type}) "
            f"AND {scoped_link_missing} AND {legacy_link_missing}"
        )
        legacy = (
            f"m.group_id IS NOT NULL AND m.group_id != '' AND COALESCE({bot_id}, '')='' "
            f"AND COALESCE({session_id}, '')='' AND COALESCE({visibility}, '')='' "
            f"AND ({legacy_memory_type}) AND {legacy_link_missing}"
        )
        rows = conn.execute(
            f"""SELECT m.id, m.content, {sender_name} AS sender_name, m.group_id,
                       {bot_id} AS bot_id, {session_id} AS session_id, {visibility} AS visibility,
                       CASE WHEN ({formal}) THEN 'scoped' ELSE 'legacy_group' END AS lane
                  FROM memories m
                 WHERE LENGTH(m.content) >= 10
                   AND ({' AND '.join(active) if active else '1=1'})
                   AND ({status_filter})
                   AND (({formal}) OR ({legacy}))
                 ORDER BY m.id DESC
                 LIMIT ?""",
            (self.batch_size,),
        ).fetchall()

        batch: list[TagWorkItem] = []
        rejected_ids: list[int] = []
        for memory_id, content, raw_sender_name, group_id, raw_bot_id, raw_session_id, raw_visibility, lane in rows:
            if lane == "legacy_group":
                batch.append(TagWorkItem(int(memory_id), str(content), raw_sender_name, None, str(group_id)))
                continue
            try:
                scope = self._scope_for_memory(
                    group_id=group_id,
                    bot_id=raw_bot_id,
                    session_id=raw_session_id,
                    visibility=raw_visibility,
                )
            except (ScopeValidationError, TypeError, ValueError) as error:
                rejected_ids.append(int(memory_id))
                logger.warning(
                    "[WaveMemory] TagWorker skipped memory %s: invalid RuntimeScope (%s)",
                    memory_id,
                    error,
                )
                continue
            batch.append(TagWorkItem(int(memory_id), str(content), raw_sender_name, scope))

        if rejected_ids and "tag_extraction_status" in tables:
            now = time.time()
            conn.executemany(
                """INSERT INTO tag_extraction_status (
                       memory_id, status, attempts, last_error, last_run_at, updated_at
                   ) VALUES (?, 'skipped', 0, NULL, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                       status=excluded.status,
                       last_error=excluded.last_error,
                       last_run_at=excluded.last_run_at,
                       updated_at=excluded.updated_at""",
                [(memory_id, now, now) for memory_id in rejected_ids],
            )
            conn.commit()
        return batch

    @staticmethod
    def _scope_for_memory(*, group_id, bot_id, session_id, visibility) -> RuntimeScope:
        """从已持久化的 v2 字段重建 scope；不接受任何 legacy 推断。"""
        if visibility != "group":
            raise ValueError("memory visibility is not group")
        if not isinstance(group_id, str) or not group_id or group_id != group_id.strip():
            raise ValueError("memory group_id is incomplete")
        if not isinstance(session_id, str):
            raise ValueError("memory session_id is incomplete")
        parts = session_id.split(":", 2)
        if len(parts) != 3:
            raise ValueError("memory session_id is not canonical")
        platform_id, session_kind, conversation_id = parts
        if session_kind != "group" or conversation_id != group_id:
            raise ValueError("memory session does not canonically identify its group")
        return RuntimeScope(
            bot_id=bot_id,
            visibility="group",
            session=SessionRef(
                id=session_id,
                platform_id=platform_id,
                kind=session_kind,
                conversation_id=conversation_id,
            ),
        )

    def _is_current_work_item(self, item: TagWorkItem) -> bool:
        """确认 LLM await 后目标仍存在，且没有跨入另一条 Scope/legacy lane。"""
        conn = self.db.conn
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        if not {"id", "group_id"} <= columns:
            return False

        clauses = ["id=?"]
        params: list[object] = [item.memory_id]
        if item.scope is None:
            clauses.append("group_id=?")
            params.append(item.legacy_group_id)
            for column in ("bot_id", "session_id", "visibility"):
                if column in columns:
                    clauses.append("COALESCE(%s, '')=''" % column)
            if "memory_type" in columns:
                clauses.append("COALESCE(memory_type, 'message') NOT IN ('deleted', 'noise')")
        else:
            scope = item.scope
            if scope.session is None or not {"bot_id", "session_id", "visibility"} <= columns:
                return False
            clauses.extend(("group_id=?", "bot_id=?", "session_id=?", "visibility='group'"))
            params.extend((scope.session.conversation_id, scope.bot_id, scope.session.id))
            if "resolution_state" in columns:
                clauses.append("COALESCE(resolution_state, '') IN ('', 'resolved')")
            if "quarantine" in columns:
                clauses.append("COALESCE(quarantine, 0)=0")

        if "source" in columns:
            clauses.append("COALESCE(source, '') != 'noise'")
        if "memory_type" in columns:
            clauses.append("COALESCE(memory_type, 'message') != 'noise'")
        if "provenance" in columns and not self.include_recovered_backfill:
            clauses.append("COALESCE(provenance, '') NOT LIKE '%classified_legacy_recovery%'")
        return conn.execute(
            f"SELECT 1 FROM memories WHERE {' AND '.join(clauses)}", params
        ).fetchone() is not None

    def _record_status(
        self,
        memory_id: int,
        status: str,
        now: float,
        *,
        error: str | None = None,
        bump_attempts: bool = False,
    ) -> None:
        """Persist terminal/retryable extraction state for one memory.

        Failed rows keep ``status='failed'`` and increment ``attempts`` so the
        fetcher can stop hot-looping poison items after a bounded budget.
        """
        err = None if error is None else str(error)[:2000]
        if bump_attempts:
            self.db.conn.execute(
                """INSERT INTO tag_extraction_status (
                       memory_id, status, attempts, last_error, last_run_at, updated_at
                   ) VALUES (?, ?, 1, ?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                       status=excluded.status,
                       attempts=COALESCE(tag_extraction_status.attempts, 0) + 1,
                       last_error=excluded.last_error,
                       last_run_at=excluded.last_run_at,
                       updated_at=excluded.updated_at""",
                (memory_id, status, err, now, now),
            )
            return
        self.db.conn.execute(
            """INSERT INTO tag_extraction_status (
                   memory_id, status, attempts, last_error, last_run_at, updated_at
               ) VALUES (?, ?, 0, ?, ?, ?)
               ON CONFLICT(memory_id) DO UPDATE SET
                   status=excluded.status,
                   last_error=excluded.last_error,
                   last_run_at=excluded.last_run_at,
                   updated_at=excluded.updated_at""",
            (memory_id, status, err, now, now),
        )

    async def _process_batch(self, batch: list[TagWorkItem]):
        """Process formal scoped work and explicitly unscoped legacy-group work."""
        messages = [
            {"id": item.memory_id, "content": item.content, "sender": item.sender_name or "unknown"}
            for item in batch
        ]

        try:
            # Formal batches retain Scope-isolated reference vocabularies. Legacy
            # group batches never masquerade as a RuntimeScope and use no scoped
            # prompt reference; their output is written back to legacy links.
            grouped: dict[tuple[str, ...], list[tuple[int, TagWorkItem, dict]]] = {}
            for message, item in zip(messages, batch):
                if item.scope is not None and item.scope.session is not None:
                    key = ("scoped", item.scope.bot_id, item.scope.session.id, item.scope.visibility)
                else:
                    key = ("legacy_group", item.legacy_group_id)
                grouped.setdefault(key, []).append((item.memory_id, item, message))
            result_by_memory: dict[int, list[dict]] = {}
            for grouped_items in grouped.values():
                group_messages = [entry[2] for entry in grouped_items]
                group_scope = grouped_items[0][1].scope
                try:
                    if group_scope is None:
                        group_results = await self.extractor.extract_tags_batch(group_messages)
                    else:
                        group_results = await self.extractor.extract_tags_batch(group_messages, scope=group_scope)
                except TypeError:
                    # 兼容旧的测试/扩展 extractor；其结果仍只写回本组 memory。
                    group_results = await self.extractor.extract_tags_batch(group_messages)
                for index, (memory_id, _item, _message) in enumerate(grouped_items):
                    result_by_memory[memory_id] = group_results[index] if index < len(group_results) else []
        except Exception as error:
            logger.warning(f"[WaveMemory] TagWorker batch LLM error: {error}")
            return

        tag_count = 0
        for item in batch:
            tags = await self._attach_tag_vectors(result_by_memory.get(item.memory_id, []))
            # LLM/embedding await 期间 memory 可能被删除或迁移；只允许写回
            # 到仍属于原 lane 的行，避免 legacy/scoped 之间的越界写入。
            if not self._is_current_work_item(item):
                logger.debug(f"[WaveMemory] TagWorker skipped stale memory {item.memory_id}")
                continue

            try:
                now = time.time()
                if self.write_gateway is not None and item.scope is not None:
                    saved_count = await self.write_gateway.apply_tag_extraction(
                        scope=item.scope,
                        memory_id=item.memory_id,
                        tags=tags,
                        status="done" if tags else "skipped",
                        upgrade_source=self._should_upgrade_source(tags),
                    )
                    tag_count += saved_count
                    continue

                if tags:
                    saved_count = await self._save_tags(item, tags)
                    tag_count += saved_count
                    self._record_status(item.memory_id, "done", now)
                    # 未注入协调入口的兼容测试路径仍保持原有事务语义。
                    if item.scope is not None:
                        self._maybe_upgrade_source(item, tags)
                else:
                    self._record_status(item.memory_id, "skipped", now)
                # 单条提交保证删除竞态或 FK 冲突不会回滚同批其它有效记忆。
                self.db.conn.commit()
            except sqlite3.IntegrityError as error:
                try:
                    self.db.conn.rollback()
                except Exception as rollback_error:
                    logger.warning(f"[WaveMemory] TagWorker rollback failed: {rollback_error}")
                try:
                    self._record_status(
                        item.memory_id,
                        "failed",
                        time.time(),
                        error=f"IntegrityError: {error}",
                        bump_attempts=True,
                    )
                    self.db.conn.commit()
                except Exception as status_error:
                    logger.warning(
                        "[WaveMemory] TagWorker failed-status write skipped memory=%s error=%r",
                        item.memory_id,
                        status_error,
                    )
                logger.warning(
                    f"[WaveMemory] TagWorker skipped memory {item.memory_id} after FK integrity error: {error}"
                )
            except ValueError as error:
                # ProductionWriteGateway 会在提交时再次校验 Scope；若在两次
                # 校验之间发生迁移/删除，视为该条 stale，而非整批失败。
                if self.write_gateway is not None and item.scope is not None:
                    logger.warning(
                        f"[WaveMemory] TagWorker skipped stale scoped memory {item.memory_id}: {error}"
                    )
                    try:
                        self._record_status(
                            item.memory_id,
                            "skipped",
                            time.time(),
                            error=f"ValueError: {error}",
                            bump_attempts=False,
                        )
                        self.db.conn.commit()
                    except Exception:
                        pass
                    continue
                try:
                    self.db.conn.rollback()
                except Exception as rollback_error:
                    logger.warning(f"[WaveMemory] TagWorker rollback failed: {rollback_error}")
                try:
                    self._record_status(
                        item.memory_id,
                        "failed",
                        time.time(),
                        error=f"ValueError: {error}",
                        bump_attempts=True,
                    )
                    self.db.conn.commit()
                except Exception:
                    pass
                logger.warning(f"[WaveMemory] TagWorker batch error: {error}")
                return
            except Exception as error:
                try:
                    self.db.conn.rollback()
                except Exception as rollback_error:
                    logger.warning(f"[WaveMemory] TagWorker rollback failed: {rollback_error}")
                try:
                    self._record_status(
                        item.memory_id,
                        "failed",
                        time.time(),
                        error=f"{type(error).__name__}: {error}",
                        bump_attempts=True,
                    )
                    self.db.conn.commit()
                except Exception:
                    pass
                logger.warning(f"[WaveMemory] TagWorker batch error: {error}")
                return

        if tag_count > 0 and self.on_tags_written:
            self.on_tags_written(tag_count)

        logger.debug(f"[WaveMemory] TagWorker batch done: {len(batch)} memories, {tag_count} tags")

    async def _attach_tag_vectors(self, tags: list) -> list[dict]:
        """Attach explicit Catalog vectors when an embedding provider is available.

        The vector is carried through the writer command so the outbox projection can
        update the canonical Tag index after commit.  A missing provider only leaves
        the semantic index degraded; scoped tag links still persist.
        """
        normalized: list[dict] = []
        for raw in tags or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            name = str(item.get("name") or "").strip()
            if name and "embedding" not in item and self.embedding is not None:
                try:
                    vector = await self.embedding.get_embedding(name)
                    if vector is not None:
                        item["embedding"] = [float(value) for value in vector]
                except Exception:
                    pass
            normalized.append(item)
        return normalized

    async def _save_tags(self, item: TagWorkItem, tags: list) -> int:
        """Write formal tags to scoped projections or legacy tags to legacy links.

        Scoped work never expands the legacy ``tags`` pool.  Both lanes run the
        shared admission filter so stop-words and low-confidence phrases cannot
        become long-lived vectors.
        """
        try:
            from .tag_admission import admit_tag_batch, quality_reject_reason
        except ImportError:  # pragma: no cover
            from services.tag_admission import admit_tag_batch, quality_reject_reason

        admitted, _decisions = admit_tag_batch(tags if isinstance(tags, list) else [])
        saved_count = 0
        for position, tag_info in enumerate(admitted, 1):
            name = str(tag_info.get("name") or "").strip()
            if not name:
                continue
            tag_type = tag_info.get("type", "keyword")
            confidence = tag_info.get("confidence", 0.8)
            if not isinstance(tag_type, str):
                tag_type = "keyword"

            if item.scope is None:
                # Legacy lane only: exact-name upsert into historical tags/memory_tags.
                # Quality admission already ran; still refuse non-lexical leftovers.
                if quality_reject_reason(name, tag_type, float(confidence)) is not None:
                    continue
                vector = tag_info.get("embedding")
                try:
                    import numpy as np
                    vector = np.asarray(vector, dtype=np.float32) if vector is not None else None
                except (TypeError, ValueError):
                    vector = None
                tag_id = self.db.add_tag_extended(
                    name,
                    tag_type=tag_type,
                    vector=vector,
                    confidence=float(confidence),
                    metadata={"producer": "tag_worker", "memory_id": item.memory_id, "lane": "legacy_group"},
                )
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position, relevance) VALUES (?, ?, ?, 1.0)",
                    (item.memory_id, tag_id, position),
                )
                saved_count += 1
                continue

            # Formal Scope path: Catalog + scoped_tags only. Never touch legacy tags.
            tag_id = self.db.upsert_scoped_tag(
                item.scope,
                name=name,
                tag_type=tag_type,
                confidence=confidence,
                metadata={
                    "producer": "tag_worker",
                    "memory_id": item.memory_id,
                    "admission": tag_info.get("admission"),
                    "admission_reason": tag_info.get("admission_reason"),
                },
            )
            catalog_id = getattr(self.db, "get_scoped_tag_catalog_id", lambda *_: None)(item.scope, tag_id)
            vector = tag_info.get("embedding")
            if catalog_id is not None and vector is not None:
                getattr(self.db, "update_tag_catalog_embedding", lambda *_args, **_kwargs: False)(
                    catalog_id,
                    vector,
                    embedding_dim=len(vector) if hasattr(vector, "__len__") else None,
                )
            self.db.link_scoped_memory_tag(
                item.scope,
                memory_id=item.memory_id,
                tag_id=tag_id,
                position=position,
            )
            saved_count += 1
        return saved_count

    def _should_upgrade_source(self, tags: list[dict]) -> bool:
        if not self.bot_keywords:
            return False
        tag_names = {t.get("name", "").lower() for t in tags if isinstance(t, dict)}
        bot_kw_lower = {kw.lower() for kw in self.bot_keywords if kw}
        return bool(tag_names & bot_kw_lower)

    def _maybe_upgrade_source(self, item: TagWorkItem, tags: list[dict]):
        """如果标签中包含 bot 相关词，将同 Scope 的 chat 记忆升级为 core。"""
        if not self._should_upgrade_source(tags):
            return
        scope = item.scope
        assert scope.session is not None
        cursor = self.db.conn.execute(
            """UPDATE memories SET source='core'
                 WHERE id=? AND group_id=? AND bot_id=? AND session_id=? AND visibility='group'
                   AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0 AND source='chat'""",
            (item.memory_id, scope.session.conversation_id, scope.bot_id, scope.session.id),
        )
        if cursor.rowcount:
            logger.debug(f"[TagWorker] Upgraded memory {item.memory_id} to core (bot-related tags)")
