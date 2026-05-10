"""Wave Memory 查询引擎 — 串联向量检索 + 可选增强模块"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from astrbot.api import logger

from .database import WaveMemoryDB
from .vector_index import VectorIndex
from .embedding import EmbeddingService


class QueryEngine:
    """记忆查询管线：embedding → 向量检索 → (可选增强) → 返回结果。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service: EmbeddingService,
        config: dict,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.config = config

        # 可选模块（P2/P3 实现后注入）
        self.residual_pyramid = None
        self.spike_router = None
        self.epa = None
        self.geodesic_reranker = None

    async def query(
        self,
        text: str,
        group_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """执行记忆查询，返回最相关的记忆片段列表。"""
        start = time.time()

        # Step 1: Embedding（唯一的外部 API 调用）
        query_vec = await self.embedding.get_embedding(text)
        if query_vec is None:
            logger.warning("[WaveMemory] Query embedding failed, skipping memory retrieval")
            return []

        embed_ms = (time.time() - start) * 1000

        # Step 2: 向量检索
        # 多取一些候选，后续可能被增强模块重排
        candidates_k = top_k * 3
        results = self.memory_index.search(query_vec, k=candidates_k)

        if not results:
            return []

        # Step 3: 获取记忆内容
        memory_ids = [r[0] for r in results]
        distances = {r[0]: r[1] for r in results}
        memories = self.db.get_memories_by_ids(memory_ids)

        # 按 group_id 过滤（如果指定）
        if group_id:
            memories = [m for m in memories if m["group_id"] == group_id]

        # Step 4: 计算相关性分数（cosine distance → similarity）
        for mem in memories:
            dist = distances.get(mem["id"], 1.0)
            mem["similarity"] = 1.0 - dist  # cosine distance → similarity
            mem["score"] = mem["similarity"] * mem.get("importance", 1.0)

        # Step 5: 排序并截断
        memories.sort(key=lambda m: m["score"], reverse=True)
        memories = memories[:top_k]

        # Step 6: 更新访问记录
        if memories:
            self.db.touch_memories([m["id"] for m in memories])

        total_ms = (time.time() - start) * 1000
        logger.debug(
            f"[WaveMemory] Query done: {len(memories)} results, "
            f"embed={embed_ms:.0f}ms, total={total_ms:.0f}ms"
        )

        return memories

    def format_injection(self, memories: list[dict]) -> str:
        """将记忆列表格式化为注入文本。"""
        if not memories:
            return ""

        parts = ["<wave_memory>"]
        for mem in memories:
            sender = mem.get("sender_name") or mem.get("sender_id") or "unknown"
            ts = time.strftime("%m-%d %H:%M", time.localtime(mem["timestamp"]))
            score = mem.get("score", 0)
            parts.append(f"[{ts}] {sender}: {mem['content']} (relevance: {score:.2f})")
        parts.append("</wave_memory>")

        return "\n".join(parts)
