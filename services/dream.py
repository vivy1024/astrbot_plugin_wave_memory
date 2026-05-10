"""Wave Memory 做梦系统 — 后台记忆巩固与联想发现"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Optional

import numpy as np

from astrbot.api import logger

from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex


class DreamService:
    """模拟人脑睡眠时的记忆巩固：随机抽取种子记忆，向量联想，发现隐藏关联。

    三层时间线：
    - 近期涟漪 (0-7天)：抽 3 个种子，联想 k=5
    - 中期回音 (7-30天)：抽 2 个种子，联想 k=3
    - 深渊浪潮 (>30天)：用前两层的联想结果合成浪潮向量，在深远记忆中搜索
    """

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        dream_interval_hours: float = 6.0,
    ):
        self.db = db
        self.memory_index = memory_index
        self.dream_interval = dream_interval_hours * 3600
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._dream_loop())
        logger.info("[WaveMemory] Dream service started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _dream_loop(self):
        """定时做梦。"""
        while self._running:
            try:
                await asyncio.sleep(self.dream_interval)
                await self.dream_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WaveMemory] Dream error: {e}")
                await asyncio.sleep(60)

    async def dream_once(self) -> dict:
        """执行一次做梦，返回发现的关联。"""
        now = time.time()
        day = 86400

        # Phase 1: 近期涟漪 (0-7天)
        recent_seeds = self._sample_memories(now - 7 * day, now, count=3)
        recent_associations = []
        for seed in recent_seeds:
            if seed["vector"] is not None:
                results = self.memory_index.search(seed["vector"], k=5)
                recent_associations.extend([r[0] for r in results if r[0] != seed["id"]])

        # Phase 2: 中期回音 (7-30天)
        mid_seeds = self._sample_memories(now - 30 * day, now - 7 * day, count=2)
        mid_associations = []
        for seed in mid_seeds:
            if seed["vector"] is not None:
                results = self.memory_index.search(seed["vector"], k=3)
                mid_associations.extend([r[0] for r in results if r[0] != seed["id"]])

        # Phase 3: 深渊浪潮 (>30天)
        # 合成浪潮向量：所有联想结果的向量平均
        wave_ids = list(set(recent_associations + mid_associations))
        deep_associations = []

        if wave_ids:
            wave_memories = self.db.get_memories_by_ids(wave_ids[:20])
            wave_vectors = []
            for mem in wave_memories:
                full = self.db.get_memory_by_id(mem["id"])
                if full and full["vector"] is not None:
                    wave_vectors.append(full["vector"])

            if wave_vectors:
                wave_vector = np.mean(wave_vectors, axis=0).astype(np.float32)
                wave_vector /= np.linalg.norm(wave_vector) + 1e-10
                deep_results = self.memory_index.search(wave_vector, k=3)
                deep_associations = [r[0] for r in deep_results]

        # 找共振桥梁：被多个种子同时联想到的记忆
        all_associations = recent_associations + mid_associations
        from collections import Counter
        freq = Counter(all_associations)
        bridges = [mid for mid, cnt in freq.items() if cnt >= 2]

        # 强化被联想到的记忆（增加 importance）
        reinforced_ids = list(set(all_associations + deep_associations))
        if reinforced_ids:
            self.db.touch_memories(reinforced_ids[:50])

        result = {
            "recent_seeds": len(recent_seeds),
            "mid_seeds": len(mid_seeds),
            "associations": len(set(all_associations)),
            "bridges": len(bridges),
            "deep_discoveries": len(deep_associations),
            "reinforced": len(reinforced_ids),
        }

        logger.info(
            f"[WaveMemory] Dream complete: "
            f"seeds={len(recent_seeds)+len(mid_seeds)}, "
            f"associations={result['associations']}, "
            f"bridges={result['bridges']}, "
            f"deep={result['deep_discoveries']}"
        )

        return result

    def _sample_memories(self, start_time: float, end_time: float, count: int) -> list[dict]:
        """从时间范围内随机抽取记忆。"""
        rows = self.db.conn.execute(
            "SELECT id, content, vector, timestamp FROM memories WHERE timestamp BETWEEN ? AND ? AND vector IS NOT NULL",
            (start_time, end_time),
        ).fetchall()

        if not rows:
            return []

        sampled = random.sample(rows, min(count, len(rows)))
        return [
            {
                "id": r[0],
                "content": r[1],
                "vector": np.frombuffer(r[2], dtype=np.float32) if r[2] else None,
                "timestamp": r[3],
            }
            for r in sampled
        ]
