"""Wave Memory 异步写入服务 — 消息 → Tag → Embedding → 存储"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import numpy as np

from astrbot.api import logger

from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex
from ..engine.embedding import EmbeddingService
from .tag_extractor import TagExtractor


class MessageWriter:
    """异步消息写入服务，后台处理不阻塞回复。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service: EmbeddingService,
        tag_extractor: Optional[TagExtractor] = None,
        batch_size: int = 10,
        flush_interval: float = 30.0,
        on_tags_written=None,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.tag_extractor = tag_extractor
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.on_tags_written = on_tags_written  # callback(count: int)

        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._write_count = 0
        self._save_threshold = 100

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("[WaveMemory] MessageWriter started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def enqueue(self, message_data: dict):
        """将消息放入写入队列。"""
        await self._queue.put(message_data)

    async def _run(self):
        """主循环：批量处理队列中的消息。"""
        while self._running:
            try:
                batch = []

                # 收集一批消息
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=self.flush_interval
                    )
                    batch.append(item)
                except asyncio.TimeoutError:
                    continue

                # 尝试多取几条（非阻塞）
                while len(batch) < self.batch_size:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                # 处理这批消息
                await self._process_batch(batch)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WaveMemory] Writer error: {e}")
                await asyncio.sleep(2)

        # 关闭前保存索引
        self.memory_index.save()

    async def _process_batch(self, batch: list[dict]):
        """处理一批消息：embedding + tag 提取 + 存储。"""
        texts = [item["content"] for item in batch]

        # 批量 embedding
        vectors = await self.embedding.get_embeddings(texts)

        for item, vec in zip(batch, vectors):
            try:
                # 存入 DB
                memory_id = self.db.add_memory(
                    group_id=item["group_id"],
                    content=item["content"],
                    vector=vec,
                    sender_id=item.get("sender_id", ""),
                    sender_name=item.get("sender_name", ""),
                    timestamp=item.get("timestamp", time.time()),
                )

                # 存入向量索引
                if vec is not None:
                    self.memory_index.add([memory_id], vec.reshape(1, -1))

                # 异步提取 Tag（不等待完成）
                if self.tag_extractor:
                    asyncio.create_task(
                        self._extract_and_link_tags(memory_id, item["content"], item.get("sender_name", ""))
                    )

                self._write_count += 1

            except Exception as e:
                logger.debug(f"[WaveMemory] Single write failed: {e}")

        # 定期保存索引
        if self._write_count >= self._save_threshold:
            self.memory_index.save()
            self._write_count = 0
            logger.debug(f"[WaveMemory] Index saved, total: {self.memory_index.count}")

    async def _extract_and_link_tags(self, memory_id: int, content: str, sender_name: str = ""):
        """提取结构化 Tag 并关联到记忆。"""
        try:
            tags = await self.tag_extractor.extract_tags(content, sender=sender_name)
            if not tags:
                return

            # 获取 Tag 名称列表用于 embedding
            tag_names = [t["name"] for t in tags]
            tag_vecs = await self.embedding.get_embeddings(tag_names)

            tag_ids = []
            for tag_info, tag_vec in zip(tags, tag_vecs):
                tid = self.db.add_tag_extended(
                    name=tag_info["name"],
                    tag_type=tag_info.get("type", "keyword"),
                    vector=tag_vec,
                    confidence=tag_info.get("confidence", 0.8),
                )
                tag_ids.append(tid)

            # 关联（带 relevance = confidence）
            for pos, (tid, tag_info) in enumerate(zip(tag_ids, tags), 1):
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position, relevance) VALUES (?, ?, ?, ?)",
                    (memory_id, tid, pos, tag_info.get("confidence", 0.8)),
                )
            self.db.conn.commit()

            # 通知共现矩阵调度器
            if self.on_tags_written and tag_ids:
                self.on_tags_written(len(tag_ids))

        except Exception as e:
            logger.debug(f"[WaveMemory] Tag extraction failed for memory {memory_id}: {e}")
