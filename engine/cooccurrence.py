"""Wave Memory 共现矩阵 — Tag 共现统计，供脉冲传播使用"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from astrbot.api import logger

from .database import WaveMemoryDB


class CooccurrenceMatrix:
    """Tag 共现矩阵：记录哪些 Tag 经常在同一条记忆中出现。"""

    def __init__(self, db: WaveMemoryDB):
        self.db = db
        # {tag_id: {neighbor_id: weight}}
        self.matrix: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self._tag_count = 0

    def rebuild(self):
        """从数据库重建共现矩阵。"""
        self.matrix.clear()

        cooc_data = self.db.get_cooccurrence_data()
        for tag_a, tag_b, count in cooc_data:
            weight = min(count / 5.0, 1.0)  # 归一化，5次共现 = 满权重
            self.matrix[tag_a][tag_b] = weight
            self.matrix[tag_b][tag_a] = weight

        self._tag_count = self.db.get_tag_count()
        logger.info(
            f"[WaveMemory] Cooccurrence matrix rebuilt: "
            f"{len(self.matrix)} nodes, {sum(len(v) for v in self.matrix.values())} edges"
        )

    def get_neighbors(self, tag_id: int, max_neighbors: int = 20) -> list[tuple[int, float]]:
        """获取某个 Tag 的共现邻居，按权重降序。"""
        neighbors = self.matrix.get(tag_id, {})
        sorted_neighbors = sorted(neighbors.items(), key=lambda x: x[1], reverse=True)
        return sorted_neighbors[:max_neighbors]

    @property
    def node_count(self) -> int:
        return len(self.matrix)

    def needs_rebuild(self, threshold_pct: float = 0.01) -> bool:
        """判断是否需要重建（Tag 数量变化超过阈值）。"""
        current_count = self.db.get_tag_count()
        if self._tag_count == 0:
            return current_count > 10
        change = abs(current_count - self._tag_count) / self._tag_count
        return change >= threshold_pct
