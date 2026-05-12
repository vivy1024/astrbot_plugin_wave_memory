"""Wave Memory 后台 Tag 补全 Job — 断点续跑，自动补全无 Tag 记忆"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from astrbot.api import logger


class TagBackfillJob:
    """后台 Tag 补全任务。

    特性：
    - 水位线持久化（断点续跑）
    - 失败重试队列（3 次后跳过）
    - 不阻塞在线查询
    - 可通过 stop() 优雅停止
    """

    def __init__(self, db, tag_extractor, embedding_service, tag_index, config: dict):
        self.db = db
        self.extractor = tag_extractor
        self.embedding = embedding_service
        self.tag_index = tag_index
        self.batch_size = int(config.get("tag_backfill_batch_size", 50))
        self.max_retries = 3
        self.sleep_between_batches = float(config.get("tag_backfill_sleep", 2.0))
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """启动后台任务。"""
        if self._running:
            return
        self._task = asyncio.create_task(self._run())

    def stop(self):
        """优雅停止。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self):
        """主循环。"""
        self._running = True

        # 等待 embedding 服务就绪（最多 60 秒）
        for _ in range(12):
            if self.embedding and await self.embedding.is_available():
                break
            await asyncio.sleep(5)
        else:
            logger.warning("[WaveMemory] Tag backfill: embedding not available after 60s, starting anyway")

        logger.info("[WaveMemory] Tag backfill job started")

        processed = 0
        failed = 0

        try:
            while self._running:
                batch = self._fetch_untagged_batch()
                if not batch:
                    logger.info(
                        f"[WaveMemory] Tag backfill complete: {processed} processed, {failed} failed"
                    )
                    break

                batch_processed, batch_failed = await self._process_batch(batch)
                processed += batch_processed
                failed += batch_failed

                if processed % 200 == 0:
                    logger.info(
                        f"[WaveMemory] Tag backfill progress: {processed} processed, {failed} failed"
                    )

                # 让出事件循环，避免阻塞在线查询
                await asyncio.sleep(self.sleep_between_batches)

        except asyncio.CancelledError:
            logger.info(f"[WaveMemory] Tag backfill cancelled: {processed} processed")
        except Exception as e:
            logger.error(f"[WaveMemory] Tag backfill error: {e}")
        finally:
            self._running = False

    def _fetch_untagged_batch(self) -> list[tuple]:
        """获取下一批无 Tag 且未失败的记忆。"""
        return self.db.conn.execute("""
            SELECT m.id, m.content, m.sender_name
            FROM memories m
            WHERE m.id NOT IN (
                SELECT DISTINCT memory_id FROM memory_tags
            )
            AND m.id NOT IN (
                SELECT memory_id FROM tag_extraction_status
                WHERE status IN ('done', 'failed', 'skipped')
            )
            AND LENGTH(m.content) >= 10
            ORDER BY m.id ASC
            LIMIT ?
        """, (self.batch_size,)).fetchall()

    async def _process_batch(self, batch: list[tuple]) -> tuple[int, int]:
        """处理一批记忆，返回 (成功数, 失败数)。"""
        messages = []
        for mem_id, content, sender_name in batch:
            messages.append({
                "id": mem_id,
                "content": content,
                "sender": sender_name or "unknown",
            })

        try:
            # 批量 LLM Tag 提取
            results = await self.extractor.extract_tags_batch(messages)

            success_count = 0
            fail_count = 0

            # 统计本批有多少条 LLM 返回了 tag，用于判断 LLM 是否正常工作
            has_tag_count = sum(1 for r in results if r)
            # 只要有至少 1 条消息有 tag，说明 LLM 正常返回了结果
            llm_working = has_tag_count >= 1

            for i, (mem_id, content, sender_name) in enumerate(batch):
                tags = results[i] if i < len(results) else []

                if tags:
                    # 写入 Tag
                    await self._save_tags(mem_id, tags)
                    self._mark_status(mem_id, "done")
                    success_count += 1
                elif llm_working:
                    # LLM 正常工作但判断该消息无需 tag，标记 skipped
                    self._mark_status(mem_id, "skipped")
                    success_count += 1
                else:
                    # LLM 可能截断或异常，重试
                    if self._increment_attempts(mem_id):
                        fail_count += 1
                    else:
                        success_count += 1

            return success_count, fail_count

        except Exception as e:
            logger.warning(f"[WaveMemory] Tag backfill batch error: {e}")
            # 整批失败，逐条标记重试
            fail_count = 0
            for mem_id, _, _ in batch:
                if self._increment_attempts(mem_id):
                    fail_count += 1
            return 0, fail_count

    async def _save_tags(self, memory_id: int, tags: list[dict]):
        """保存 Tag 到数据库和向量索引。"""
        tag_ids = []
        for tag_info in tags:
            name = tag_info.get("name", "")
            tag_type = tag_info.get("type", "keyword")
            confidence = tag_info.get("confidence", 0.8)

            if not name:
                continue

            # 获取或创建 Tag 向量
            tag_vec = None
            try:
                tag_vec = await self.embedding.get_embedding(name)
            except Exception:
                pass

            tag_id = self.db.add_tag_extended(
                name=name,
                tag_type=tag_type,
                vector=tag_vec,
                confidence=confidence,
            )
            tag_ids.append(tag_id)

            # 加入 Tag 向量索引
            if tag_vec is not None and self.tag_index:
                try:
                    self.tag_index.add(tag_id, tag_vec)
                except Exception:
                    pass

        if tag_ids:
            self.db.link_memory_tags(memory_id, tag_ids)

    def _mark_status(self, memory_id: int, status: str, error: str = None):
        """更新 tag_extraction_status。"""
        self.db.conn.execute("""
            INSERT OR REPLACE INTO tag_extraction_status (memory_id, status, attempts, last_error, updated_at)
            VALUES (?, ?, COALESCE((SELECT attempts FROM tag_extraction_status WHERE memory_id = ?), 0), ?, ?)
        """, (memory_id, status, memory_id, error, time.time()))
        self.db.conn.commit()

    def _increment_attempts(self, memory_id: int) -> bool:
        """增加重试次数，超过 max_retries 标记为 failed。返回是否标记为 failed。"""
        row = self.db.conn.execute(
            "SELECT attempts FROM tag_extraction_status WHERE memory_id = ?",
            (memory_id,)
        ).fetchone()

        attempts = (row[0] if row else 0) + 1

        if attempts >= self.max_retries:
            self._mark_status(memory_id, "failed", f"exceeded {self.max_retries} retries")
            return True
        else:
            self.db.conn.execute("""
                INSERT OR REPLACE INTO tag_extraction_status (memory_id, status, attempts, updated_at)
                VALUES (?, 'pending', ?, ?)
            """, (memory_id, attempts, time.time()))
            self.db.conn.commit()
            return False

    def get_coverage(self) -> float:
        """获取当前 Tag 覆盖率。"""
        total = self.db.conn.execute("SELECT COUNT(*) FROM memories WHERE LENGTH(content) >= 10").fetchone()[0]
        if total == 0:
            return 1.0
        with_tags = self.db.conn.execute("""
            SELECT COUNT(DISTINCT memory_id) FROM memory_tags
        """).fetchone()[0]
        return with_tags / total

    def get_stats(self) -> dict:
        """获取补全统计。"""
        total = self.db.conn.execute("SELECT COUNT(*) FROM memories WHERE LENGTH(content) >= 10").fetchone()[0]
        with_tags = self.db.conn.execute("SELECT COUNT(DISTINCT memory_id) FROM memory_tags").fetchone()[0]
        failed = self.db.conn.execute(
            "SELECT COUNT(*) FROM tag_extraction_status WHERE status = 'failed'"
        ).fetchone()[0]
        pending = total - with_tags - failed

        return {
            "total": total,
            "with_tags": with_tags,
            "failed": failed,
            "pending": max(0, pending),
            "coverage": with_tags / total if total > 0 else 1.0,
            "is_running": self._running,
        }
