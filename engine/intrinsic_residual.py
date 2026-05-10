"""Wave Memory 内生残差计算 — 基于 SVD 投影计算 Tag 的不可预测性"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np
from astrbot.api import logger

from .database import WaveMemoryDB
from .directed_cooccurrence import DirectedCooccurrence


class IntrinsicResidualCalculator:
    """内生残差计算器。

    对每个 Tag，取其有向邻居的向量集合做 SVD 投影，
    计算该 Tag 向量在邻居子空间中的残差能量。
    残差越高 = 该 Tag 越不可被邻居预测 = 越有独特信息价值。
    """

    def __init__(self, db: WaveMemoryDB, cooccurrence: DirectedCooccurrence, svd_rank: int = 5):
        self.db = db
        self.cooccurrence = cooccurrence
        self.svd_rank = svd_rank

    def compute_all(self) -> dict[int, float]:
        """计算所有 Tag 的内生残差。返回 {tag_id: residual_energy}。"""
        start = time.time()

        # 加载所有 Tag 向量
        tag_vectors = {}
        for tag_id, name, vec in self.db.get_all_tag_vectors():
            tag_vectors[tag_id] = vec

        if not tag_vectors:
            logger.info("[WaveMemory] IntrinsicResidual: no tag vectors")
            return {}

        residuals = {}
        for tag_id, tag_vec in tag_vectors.items():
            residual = self._compute_single(tag_id, tag_vec, tag_vectors)
            residuals[tag_id] = residual

        elapsed = time.time() - start
        logger.info(
            f"[WaveMemory] IntrinsicResidual computed: {len(residuals)} tags, "
            f"mean={np.mean(list(residuals.values())):.4f}, "
            f"elapsed={elapsed:.2f}s"
        )
        return residuals

    def compute_incremental(self, tag_ids: list[int]) -> dict[int, float]:
        """增量计算指定 Tag + 直接邻居的残差。"""
        # 收集需要重算的 Tag 集合
        affected = set(tag_ids)
        for tid in tag_ids:
            neighbors = self.cooccurrence.get_neighbors(tid, max_neighbors=20)
            for nid, _ in neighbors:
                affected.add(nid)

        # 加载向量
        tag_vectors = {}
        for tag_id, name, vec in self.db.get_all_tag_vectors():
            tag_vectors[tag_id] = vec

        residuals = {}
        for tag_id in affected:
            if tag_id not in tag_vectors:
                continue
            residual = self._compute_single(tag_id, tag_vectors[tag_id], tag_vectors)
            residuals[tag_id] = residual

        return residuals

    def _compute_single(self, tag_id: int, tag_vec: np.ndarray, all_vectors: dict[int, np.ndarray]) -> float:
        """计算单个 Tag 的残差能量。"""
        # 获取有向邻居
        neighbors = self.cooccurrence.get_neighbors(tag_id, max_neighbors=30)
        if not neighbors:
            return 1.0  # 无邻居 = 完全不可预测

        # 收集邻居向量
        neighbor_vecs = []
        for nid, weight in neighbors:
            if nid in all_vectors:
                neighbor_vecs.append(all_vectors[nid] * weight)

        if len(neighbor_vecs) < 2:
            return 0.8  # 邻居太少，给较高残差

        # 构建邻居矩阵并做 SVD
        neighbor_matrix = np.vstack(neighbor_vecs)  # shape: (n_neighbors, dim)

        try:
            # 截断 SVD
            rank = min(self.svd_rank, len(neighbor_vecs), neighbor_matrix.shape[1])
            U, S, Vt = np.linalg.svd(neighbor_matrix, full_matrices=False)
            # 取前 rank 个主成分
            basis = Vt[:rank]  # shape: (rank, dim)

            # 投影 tag_vec 到邻居子空间
            tag_norm = tag_vec / (np.linalg.norm(tag_vec) + 1e-8)
            projection = basis.T @ (basis @ tag_norm)  # 投影向量
            residual_vec = tag_norm - projection

            # 残差能量 = 残差向量的 L2 范数
            residual_energy = float(np.linalg.norm(residual_vec))
            # 归一化到 [0, 1]
            return min(residual_energy, 1.0)

        except Exception:
            return 0.5  # SVD 失败，给中间值

    def persist(self, residuals: dict[int, float]):
        """批量持久化残差到数据库。"""
        now = time.time()
        for tag_id, energy in residuals.items():
            self.db.conn.execute("""
                INSERT OR REPLACE INTO tag_intrinsic_residuals (tag_id, residual_energy, computed_at)
                VALUES (?, ?, ?)
            """, (tag_id, energy, now))
        self.db.conn.commit()
        logger.info(f"[WaveMemory] IntrinsicResidual persisted: {len(residuals)} tags")

    def load(self) -> dict[int, float]:
        """从数据库加载残差。"""
        rows = self.db.conn.execute(
            "SELECT tag_id, residual_energy FROM tag_intrinsic_residuals"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
