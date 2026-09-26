"""PairSimilarityService — 标签对相似度预计算 + O(1) Map 查表"""

from __future__ import annotations

import time
from typing import Optional

from astrbot.api import logger

from .pair_similarity_projection import compute_pair_similarity_projection


class PairSimilarityService:
    """标签对语义相似度预计算服务。

    定期从 tag 向量计算 pair similarity 并缓存到内存 map + DB。
    优化：内存使用受限大小的懒加载缓存，防200万条大表撑爆内存。
    """

    # 相似度只来自 tag_pair_similarity 表（缺失即 0）：共现图子进程构建可以直接读同一张表
    table_backed = True

    def __init__(self, db, refresh_interval: float = 1800.0):
        """
        Args:
            db: WaveMemoryDB facade
            refresh_interval: 刷新间隔（秒），默认 30 分钟
        """
        self.db = db
        self.refresh_interval = refresh_interval
        self._cache: dict[tuple[int, int], float] = {}
        self._last_refresh: float = 0
        self._max_cache_size = 20000 # 限制内存字典最大容量，防止大数据下撑爆内存

    def get_similarity(self, tag_a: int, tag_b: int) -> float:
        """查表获取标签对相似度。未命中从 DB 兜底。"""
        if tag_a == tag_b:
            return 1.0
        key = (min(tag_a, tag_b), max(tag_a, tag_b))
        
        # 1. 尝试从内存缓存获取
        sim = self._cache.get(key, None)
        if sim is not None:
            return sim
            
        # 2. 缓存未命中，去 DB 查
        try:
            row = self.db.conn.execute(
                "SELECT similarity FROM tag_pair_similarity WHERE tag_id_a=? AND tag_id_b=?",
                (key[0], key[1])
            ).fetchone()
            sim = row[0] if row else 0.0
        except Exception:
            sim = 0.0
            
        # 3. 写入内存缓存并维护容量上限
        if len(self._cache) >= self._max_cache_size:
            pop_key = next(iter(self._cache))
            self._cache.pop(pop_key, None)
        self._cache[key] = sim
        return sim

    def refresh_if_needed(self):
        """按需刷新缓存。"""
        now = time.time()
        if now - self._last_refresh < self.refresh_interval:
            return
        self._refresh()

    def _refresh(self):
        """从 DB 加载或重算相似度。"""
        start = time.time()

        # 优化：不把 200 万行数据全拉到 Python 内存，只做轻量行数检测
        try:
            row = self.db.conn.execute("SELECT COUNT(*) FROM tag_pair_similarity").fetchone()
            if row and row[0] > 0:
                self._cache.clear() # 依靠 get_similarity 中的懒加载，清空本地缓存以释放内存
                self._last_refresh = time.time()
                logger.info(f"[WaveMemory] PairSimilarity database checked (rows: {row[0]}), memory lazy loading enabled.")
                return
        except Exception:
            pass

        # DB 无数据时也不得在读取/请求线程直接写入投影。生产运行时由
        # maintenance.pair_similarity.rebuild durable job 负责计算与发布。
        self._cache.clear()
        self._last_refresh = time.time()
        logger.info("[WaveMemory] PairSimilarity projection empty; waiting for durable rebuild job.")

    @staticmethod
    def compute_projection(rows, **kwargs):
        """Delegate to the pure sparse projection builder (accepts Top-K kwargs)."""
        return compute_pair_similarity_projection(rows, **kwargs)

    def clear_cache(self) -> None:
        """Invalidate the read cache after a committed Tag projection change."""
        self._cache.clear()
        self._last_refresh = 0

    def install_projection(self, cache: dict[tuple[int, int], float]) -> None:
        """Install a verified durable-job result into the bounded read cache.

        Sparse Top-K projections already keep the edge set small; if an older
        oversized payload arrives, keep the highest-similarity edges first.
        """
        if len(cache) > self._max_cache_size:
            ordered = sorted(cache.items(), key=lambda item: item[1], reverse=True)
            cache = dict(ordered[: self._max_cache_size])
        self._cache = dict(cache)
        self._last_refresh = time.time()

    def force_refresh(self):
        """Refresh the read cache only; projection writes require a durable job."""
        self._last_refresh = 0
        self._refresh()
