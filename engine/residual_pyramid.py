"""Wave Memory 残差金字塔 — 多层语义分解，提升复杂问题的召回精度"""

from __future__ import annotations

import numpy as np

from .vector_index import VectorIndex


class ResidualPyramid:
    """基于 Gram-Schmidt 正交化的残差金字塔分析。

    将查询向量逐层分解：每层找到最匹配的 Tag，
    然后从查询中"减去"已解释的语义，用残差继续搜索下一层。
    """

    def __init__(self, tag_index: VectorIndex, max_levels: int = 3, top_k: int = 10, min_energy_ratio: float = 0.1):
        self.tag_index = tag_index
        self.max_levels = max_levels
        self.top_k = top_k
        self.min_energy_ratio = min_energy_ratio

    def analyze(self, query_vector: np.ndarray, tag_vectors_by_id: dict[int, np.ndarray]) -> dict:
        """执行残差金字塔分析。

        Args:
            query_vector: 原始查询向量 (float32)
            tag_vectors_by_id: {tag_id: vector} 映射，用于投影计算

        Returns:
            {
                "levels": [{tag_id, similarity, contribution}, ...],
                "all_tag_ids": [所有层命中的 tag_id],
                "coverage": 被解释的能量比 (0~1),
                "final_residual": 最终残差向量
            }
        """
        query = query_vector.astype(np.float32)
        original_energy = float(np.dot(query, query))

        if original_energy < 1e-12:
            return {"levels": [], "all_tag_ids": [], "coverage": 0.0, "final_residual": query}

        current_residual = query.copy()
        levels = []
        all_tag_ids = []

        for level in range(self.max_levels):
            # 搜索当前残差的最近 Tag
            results = self.tag_index.search(current_residual, k=self.top_k)
            if not results:
                break

            level_tags = []
            projection_vectors = []

            for tag_id, distance in results:
                if tag_id not in tag_vectors_by_id:
                    continue
                tag_vec = tag_vectors_by_id[tag_id]
                similarity = 1.0 - distance

                if similarity < 0.05:
                    continue

                level_tags.append({
                    "tag_id": tag_id,
                    "similarity": similarity,
                    "level": level,
                })
                projection_vectors.append(tag_vec)
                all_tag_ids.append(tag_id)

            if not projection_vectors:
                break

            levels.append(level_tags)

            # Gram-Schmidt: 从残差中减去已解释的分量
            for pvec in projection_vectors:
                pvec_norm = pvec / (np.linalg.norm(pvec) + 1e-10)
                proj = np.dot(current_residual, pvec_norm) * pvec_norm
                current_residual = current_residual - proj * 0.5  # 保守减去，避免过度剥离

            # 检查残差能量
            residual_energy = float(np.dot(current_residual, current_residual))
            if residual_energy / original_energy < self.min_energy_ratio:
                break

        # 计算覆盖率
        final_energy = float(np.dot(current_residual, current_residual))
        coverage = 1.0 - (final_energy / original_energy)

        return {
            "levels": levels,
            "all_tag_ids": list(set(all_tag_ids)),
            "coverage": coverage,
            "final_residual": current_residual,
        }
