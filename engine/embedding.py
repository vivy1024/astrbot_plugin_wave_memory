"""Wave Memory Embedding — 通过 AstrBot 的 Embedding Provider 获取向量"""

from __future__ import annotations

from typing import Optional

import numpy as np

from astrbot.api import logger


class EmbeddingService:
    """通过 AstrBot 的 embedding provider 获取文本向量。

    AstrBot 的 embedding provider 和 chat provider 是分开的：
    - chat provider: context.get_provider_by_id(id) → text_chat()
    - embedding provider: context.get_all_embedding_providers() → get_embeddings()

    配置中的 embedding_provider_id 用于匹配 embedding provider 的 ID。
    """

    def __init__(self, context, provider_id: str, dimension: int = 1024):
        self.context = context
        self.provider_id = provider_id
        self.dimension = dimension
        self._provider = None

    def _get_provider(self):
        """获取 embedding provider 实例。"""
        if self._provider is not None:
            return self._provider

        providers = self.context.get_all_embedding_providers()
        if not providers:
            logger.warning("[WaveMemory] No embedding providers available")
            return None

        # 按 ID 匹配
        if self.provider_id:
            for p in providers:
                if hasattr(p, 'meta') and p.meta().id == self.provider_id:
                    self._provider = p
                    return p
                # fallback: 直接比较
                pid = getattr(p, 'provider_id', '') or (p.meta().id if hasattr(p, 'meta') else '')
                if pid == self.provider_id:
                    self._provider = p
                    return p

        # 没匹配到就用第一个
        if providers:
            self._provider = providers[0]
            logger.info(f"[WaveMemory] Using first available embedding provider")
            return self._provider

        return None

    async def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """获取单条文本的 embedding 向量。"""
        result = await self.get_embeddings([text])
        return result[0] if result else None

    async def get_embeddings(self, texts: list[str]) -> list[Optional[np.ndarray]]:
        """批量获取 embedding 向量。"""
        if not texts:
            return []

        provider = self._get_provider()
        if not provider:
            return [None] * len(texts)

        try:
            # AstrBot embedding provider 的标准接口
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
