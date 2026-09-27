"""Wave Memory 测地线重排 — L0/L1/L2 三级降级 + try/catch 兜底"""

from __future__ import annotations

from typing import Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests without AstrBot
    import logging
    logger = logging.getLogger(__name__)
from .database import WaveMemoryDB


class GeodesicReranker:
    """测地线重排：用共现图的"路径距离"修正纯向量的"直线距离"。

    三级降级策略：
    - L0: 完整测地线重排（Tag 命中 >= min_geo_samples）
    - L1: 简化版（仅能量场加成，不要求最小样本数）
    - L2: 跳过（直接返回原始排序）
    """

    def __init__(self, db: WaveMemoryDB, alpha: float = 0.3, min_geo_samples: int = 4):
        self.db = db
        self.alpha = alpha
        self.min_geo_samples = min_geo_samples
        # 能量场按 scoped_tags 的 id 计；旧逻辑读 legacy memory_tags，新数据上得分恒为 0。
        # 随共现传播核 v91 一起开启（legacy 模式保持原行为，便于对照）。
        self.use_scoped_tags = False

    def rerank(
        self,
        candidates: list[dict],
        energy_field: dict[int, float],
    ) -> list[dict]:
        """对候选记忆进行测地线重排，带三级降级。

        Args:
            candidates: [{"id": int, "score": float, ...}, ...]
            energy_field: {tag_id: accumulated_energy} 来自脉冲传播

        Returns:
            重排后的候选列表（不截断）
        """
        # L2 降级：无能量场或无候选
        if not energy_field or not candidates:
            return candidates

        try:
            return self._rerank_internal(candidates, energy_field)
        except Exception as e:
            # L2 兜底：异常时直接返回原列表
            logger.debug(f"[WaveMemory] GeodesicRerank fallback to L2: {e}")
            return candidates

    def _rerank_internal(self, candidates: list[dict], energy_field: dict[int, float]) -> list[dict]:
        """内部重排逻辑。"""
        # 获取每条记忆关联的 Tag
        memory_ids = [c["id"] for c in candidates]
        memory_tag_map = self._get_memory_tags(memory_ids)

        # 计算测地线分数
        max_geo = 0.0
        geo_scores = {}
        l0_count = 0

        for mem_id in memory_ids:
            tag_ids = memory_tag_map.get(mem_id, [])

            # 累积该记忆所有 Tag 的能量
            total_energy = sum(energy_field.get(tid, 0) for tid in tag_ids)
            hit_count = sum(1 for tid in tag_ids if tid in energy_field)

            if hit_count >= self.min_geo_samples:
                # L0: 完整测地线
                geo_scores[mem_id] = total_energy / hit_count
                l0_count += 1
            elif hit_count > 0:
                # L1: 简化版（能量总和 / 标签数）
                geo_scores[mem_id] = total_energy / max(len(tag_ids), 1) * 0.5
            else:
                geo_scores[mem_id] = 0.0

            max_geo = max(max_geo, geo_scores[mem_id])

        # 如果没有任何 L0 命中，降级为 L1（减小 alpha）
        effective_alpha = self.alpha if l0_count > 0 else self.alpha * 0.5

        # 归一化并混合
        if max_geo > 0:
            for candidate in candidates:
                mem_id = candidate["id"]
                knn_score = candidate.get("score", 0)
                normalized_geo = geo_scores.get(mem_id, 0) / max_geo
                candidate["score"] = (1 - effective_alpha) * knn_score + effective_alpha * normalized_geo
                candidate["geo_score"] = normalized_geo

        # 重排
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    def _get_memory_tags(self, memory_ids: list[int]) -> dict[int, list[int]]:
        """获取记忆关联的 Tag ID 列表。"""
        if not memory_ids:
            return {}

        placeholders = ",".join("?" * len(memory_ids))
        table = "scoped_memory_tags" if self.use_scoped_tags else "memory_tags"
        rows = self.db.conn.execute(
            f"SELECT memory_id, tag_id FROM {table} WHERE memory_id IN ({placeholders})",
            memory_ids,
        ).fetchall()

        result: dict[int, list[int]] = {}
        for mem_id, tag_id in rows:
            result.setdefault(mem_id, []).append(tag_id)
        return result
