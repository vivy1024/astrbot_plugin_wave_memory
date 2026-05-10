"""Wave Memory 测地线重排 — 基于共现拓扑修正向量距离偏差"""

from __future__ import annotations

from typing import Optional

from .database import WaveMemoryDB


class GeodesicReranker:
    """测地线重排：用共现图的"路径距离"修正纯向量的"直线距离"。

    向量相似度是高维空间的直线距离，但语义空间不是平的。
    两个记忆向量距离近，不代表它们在知识图谱上真的相关。
    测地线重排用 Tag 共现能量场来修正这个偏差。
    """

    def __init__(self, db: WaveMemoryDB, alpha: float = 0.3, min_geo_samples: int = 4):
        self.db = db
        self.alpha = alpha  # 测地线混合权重 (0=纯KNN, 1=纯测地线)
        self.min_geo_samples = min_geo_samples

    def rerank(
        self,
        candidates: list[dict],
        energy_field: dict[int, float],
    ) -> list[dict]:
        """对候选记忆进行测地线重排。

        Args:
            candidates: [{"id": int, "score": float, ...}, ...]
            energy_field: {tag_id: accumulated_energy} 来自脉冲传播

        Returns:
            重排后的候选列表（不截断）
        """
        if not energy_field or not candidates:
            return candidates

        # 获取每条记忆关联的 Tag
        memory_ids = [c["id"] for c in candidates]
        memory_tag_map = self._get_memory_tags(memory_ids)

        # 计算测地线分数
        max_geo = 0.0
        geo_scores = {}

        for mem_id in memory_ids:
            tag_ids = memory_tag_map.get(mem_id, [])
            if len(tag_ids) < self.min_geo_samples:
                geo_scores[mem_id] = 0.0
                continue

            # 累积该记忆所有 Tag 的能量
            total_energy = sum(energy_field.get(tid, 0) for tid in tag_ids)
            hit_count = sum(1 for tid in tag_ids if tid in energy_field)

            if hit_count >= self.min_geo_samples:
                geo_scores[mem_id] = total_energy / hit_count
            else:
                geo_scores[mem_id] = 0.0

            max_geo = max(max_geo, geo_scores[mem_id])

        # 归一化并混合
        if max_geo > 0:
            for candidate in candidates:
                mem_id = candidate["id"]
                knn_score = candidate.get("score", 0)
                normalized_geo = geo_scores.get(mem_id, 0) / max_geo
                candidate["score"] = (1 - self.alpha) * knn_score + self.alpha * normalized_geo
                candidate["geo_score"] = normalized_geo

        # 重排
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    def _get_memory_tags(self, memory_ids: list[int]) -> dict[int, list[int]]:
        """获取记忆关联的 Tag ID 列表。"""
        if not memory_ids:
            return {}

        placeholders = ",".join("?" * len(memory_ids))
        rows = self.db.conn.execute(
            f"SELECT memory_id, tag_id FROM memory_tags WHERE memory_id IN ({placeholders})",
            memory_ids,
        ).fetchall()

        result: dict[int, list[int]] = {}
        for mem_id, tag_id in rows:
            result.setdefault(mem_id, []).append(tag_id)
        return result
