"""Wave Memory Embedding — 复用 AstrBot 的 Provider 系统获取向量"""

from __future__ import annotations

from typing import Optional

import numpy as np

from astrbot.api import logger


class EmbeddingService:
    """通过 AstrBot 的 embedding provider 获取文本向量。"""

    def __init__(self, context, provider_id: str, dimension: int = 1024):
        self.context = context
        self.provider_id = provider_id
        self.dimension = dimension

    async def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """获取单条文本的 embedding 向量。"""
        result = await self.get_embeddings([text])
        return result[0] if result else None

    async def get_embeddings(self, texts: list[str]) -> list[Optional[np.ndarray]]:
        """批量获取 embedding 向量。"""
        if not texts:
            return []

        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                logger.warning(f"[WaveMemory] Embedding provider '{self.provider_id}' not found")
                return [None] * len(texts)

            # AstrBot 的 embedding provider 返回 list[list[float]]
            raw_result = await provider.get_embeddings(texts)

            results = []
            for vec in raw_result:
                if vec is not None and len(vec) > 0:
                    arr = np.array(vec, dtype=np.float32)
                    results.append(arr)
                else:
                    results.append(None)
            return results

        except Exception as e:
            logger.error(f"[WaveMemory] Embedding failed: {e}")
            return [None] * len(texts)
