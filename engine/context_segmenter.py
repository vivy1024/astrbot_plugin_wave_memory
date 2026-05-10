"""Wave Memory 上下文分段器 — 按话题切割对话上下文"""

from __future__ import annotations

from typing import Optional

import numpy as np
from astrbot.api import logger


class ContextSegmenter:
    """上下文分段器：将最近 N 轮消息按向量相似度切割为语义段。

    每段返回加权平均向量，越近的消息权重越高。
    """

    def __init__(
        self,
        similarity_threshold: float = 0.70,
        max_segments: int = 4,
        min_segment_size: int = 2,
    ):
        self.similarity_threshold = similarity_threshold
        self.max_segments = max_segments
        self.min_segment_size = min_segment_size

    def segment(self, message_vectors: list[np.ndarray]) -> list[np.ndarray]:
        """将消息向量序列切割为段，返回每段的加权平均向量。

        Args:
            message_vectors: 按时间顺序排列的消息向量列表

        Returns:
            段向量列表（最多 max_segments 个）
        """
        if not message_vectors:
            return []

        if len(message_vectors) == 1:
            return [message_vectors[0]]

        # 计算相邻消息的余弦相似度
        similarities = []
        for i in range(len(message_vectors) - 1):
            sim = self._cosine_similarity(message_vectors[i], message_vectors[i + 1])
            similarities.append(sim)

        # 找到切割点（相似度低于阈值的位置）
        cut_points = [0]  # 第一段起始
        for i, sim in enumerate(similarities):
            if sim < self.similarity_threshold:
                cut_points.append(i + 1)
        cut_points.append(len(message_vectors))  # 最后一段结束

        # 合并过短的段
        merged_cuts = [cut_points[0]]
        for i in range(1, len(cut_points) - 1):
            segment_size = cut_points[i] - merged_cuts[-1]
            if segment_size >= self.min_segment_size:
                merged_cuts.append(cut_points[i])
        merged_cuts.append(cut_points[-1])

        # 限制段数（保留最近的段）
        if len(merged_cuts) - 1 > self.max_segments:
            merged_cuts = merged_cuts[-(self.max_segments + 1):]

        # 计算每段的加权平均向量
        segment_vectors = []
        for i in range(len(merged_cuts) - 1):
            start = merged_cuts[i]
            end = merged_cuts[i + 1]
            segment_vecs = message_vectors[start:end]

            # 时间衰减权重：越近的消息权重越高
            weights = np.array([0.5 + 0.5 * (j / max(len(segment_vecs) - 1, 1))
                               for j in range(len(segment_vecs))])
            weights /= weights.sum()

            # 加权平均
            avg_vec = np.zeros_like(segment_vecs[0])
            for w, v in zip(weights, segment_vecs):
                avg_vec += w * v

            # 归一化
            norm = np.linalg.norm(avg_vec)
            if norm > 1e-8:
                avg_vec /= norm

            segment_vectors.append(avg_vec)

        return segment_vectors

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """计算余弦相似度。"""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a < 1e-8 or norm_b < 1e-8:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))
