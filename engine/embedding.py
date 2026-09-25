"""Wave Memory Embedding — 通过 AstrBot 的 Embedding Provider 获取向量"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Optional

import numpy as np

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests without AstrBot
    import logging
    logger = logging.getLogger(__name__)


class EmbeddingService:
    """通过 AstrBot 的 embedding provider 获取文本向量。

    AstrBot 的 embedding provider 和 chat provider 是分开的：
    - chat provider: context.get_provider_by_id(id) → text_chat()
    - embedding provider: context.get_all_embedding_providers() → get_embeddings()

    配置中的 embedding_provider_id 用于匹配 embedding provider 的 ID。
    """

    # 同一句话在一次注入里会被 memory、book_lore 两个通道各算一次，消息入库时再算一次；
    # 结果短期缓存，并发的同句请求共享一次 provider 调用。
    CACHE_SIZE = 512
    CACHE_TTL_SECONDS = 600.0

    def __init__(self, context, provider_id: str, dimension: int = 1024):
        self.context = context
        self.provider_id = provider_id
        self.dimension = dimension
        self._provider = None
        self._cache: OrderedDict[str, tuple[float, np.ndarray]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._inflight_loop = None
        self.stats = {"hits": 0, "misses": 0, "joined": 0, "provider_calls": 0}

    def cache_clear(self) -> None:
        self._cache.clear()

    def _cache_get(self, text: str) -> Optional[np.ndarray]:
        item = self._cache.get(text)
        if item is None:
            return None
        stored_at, vector = item
        if time.monotonic() - stored_at > self.CACHE_TTL_SECONDS:
            self._cache.pop(text, None)
            return None
        self._cache.move_to_end(text)
        return vector.copy()

    def _cache_put(self, text: str, vector: np.ndarray) -> None:
        self._cache[text] = (time.monotonic(), vector.copy())
        self._cache.move_to_end(text)
        while len(self._cache) > self.CACHE_SIZE:
            self._cache.popitem(last=False)

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

    async def is_available(self) -> bool:
        """检查 embedding provider 是否可用。"""
        return self._get_provider() is not None

    async def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """获取单条文本的 embedding 向量。"""
        result = await self.get_embeddings([text])
        return result[0] if result else None

    async def get_embeddings(self, texts: list[str]) -> list[Optional[np.ndarray]]:
        """批量获取 embedding 向量（先查缓存，并发的同句请求合并成一次调用）。"""
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        if self._inflight_loop is not loop:  # 热重载换了事件循环，旧 Future 不能再等
            self._inflight = {}
            self._inflight_loop = loop

        results: list[Optional[np.ndarray]] = [None] * len(texts)
        waits: list[tuple[int, asyncio.Future]] = []
        owned: dict[str, asyncio.Future] = {}
        fetch: list[str] = []
        for i, text in enumerate(texts):
            key = str(text)
            cached = self._cache_get(key)
            if cached is not None:
                self.stats["hits"] += 1
                results[i] = cached
                continue
            future = self._inflight.get(key) or owned.get(key)
            if future is not None:
                if key not in owned:
                    self.stats["joined"] += 1
                waits.append((i, future))
                continue
            future = loop.create_future()
            self._inflight[key] = future
            owned[key] = future
            fetch.append(key)
            waits.append((i, future))

        if fetch:
            self.stats["misses"] += len(fetch)
            self.stats["provider_calls"] += 1
            vectors: list[Optional[np.ndarray]] = [None] * len(fetch)
            try:
                vectors = await self._fetch(fetch)
            finally:
                # 失败或被取消也要唤醒等待者（拿到 None），不能让并发请求一直挂着
                for index, key in enumerate(fetch):
                    # provider 少返回时补 None，保证每个等待者都被唤醒
                    vector = vectors[index] if index < len(vectors) else None
                    if vector is not None:
                        self._cache_put(key, vector)
                    future = owned[key]
                    if not future.done():
                        future.set_result(vector)
                    if self._inflight.get(key) is future:
                        self._inflight.pop(key, None)

        for i, future in waits:
            vector = await asyncio.shield(future)
            results[i] = vector.copy() if vector is not None else None
        return results

    async def _fetch(self, texts: list[str]) -> list[Optional[np.ndarray]]:
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

        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                # Provider 的 event loop 已关闭（热重载/重启残留），清缓存重试一次
                logger.warning(f"[WaveMemory] Embedding provider event loop closed, refreshing...")
                self._provider = None
                self.cache_clear()
                provider = self._get_provider()
                if provider:
                    try:
                        raw_result = await provider.get_embeddings(texts)
                        return [np.array(v, dtype=np.float32) if v is not None and len(v) > 0 else None for v in raw_result]
                    except Exception as e2:
                        logger.error(f"[WaveMemory] Embedding retry failed: {e2}")
            else:
                logger.error(f"[WaveMemory] Embedding failed: {e}")
            return [None] * len(texts)

        except Exception as e:
            logger.error(f"[WaveMemory] Embedding failed: {e}")
            return [None] * len(texts)
