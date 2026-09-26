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

    改进：top-N(max_tags=3000) + 按需加载向量。
    """

    def __init__(self, db: WaveMemoryDB, cooccurrence: DirectedCooccurrence, svd_rank: int = 5, max_tags: int = 3000):
        self.db = db
        self.cooccurrence = cooccurrence
        self.svd_rank = svd_rank
        self.max_tags = max_tags

    def _select_top_tags(self) -> list[int]:
        """选择频率最高的 top-N 标签 ID。"""
        rows = self.db.conn.execute(
            "SELECT id FROM tags WHERE vector IS NOT NULL ORDER BY frequency DESC LIMIT ?",
            (self.max_tags,),
        ).fetchall()
        return [r[0] for r in rows]

    def _load_tag_vectors(self, tag_ids: list[int] = None) -> dict[int, np.ndarray]:
        """按需加载标签向量。"""
        if tag_ids:
            result = {}
            # 分批加载
            for i in range(0, len(tag_ids), 500):
                batch = tag_ids[i:i+500]
                ph = ",".join("?" * len(batch))
                rows = self.db.conn.execute(
                    f"SELECT id, vector FROM tags WHERE id IN ({ph}) AND vector IS NOT NULL",
                    batch,
                ).fetchall()
                for r in rows:
                    result[r[0]] = np.frombuffer(r[1], dtype=np.float32)
            return result
        else:
            return self.db.get_tag_vectors_by_ids(
                [r[0] for r in self.db.conn.execute(
                    "SELECT id FROM tags WHERE vector IS NOT NULL LIMIT ?", (self.max_tags,)
                ).fetchall()]
            )

    def compute_all(self) -> dict[int, float]:
        """计算 top-N Tag 的内生残差。返回 {tag_id: residual_energy}。"""
        start = time.time()

        # 选择 top-N 标签
        top_ids = self._select_top_tags()
        if not top_ids:
            logger.info("[WaveMemory] IntrinsicResidual: no tags to compute")
            return {}

        # 按需加载这些标签 + 它们邻居的向量
        needed_ids = set(top_ids)
        for tid in top_ids:
            neighbors = self.cooccurrence.get_neighbors(tid, max_neighbors=30)
            for nid, _ in neighbors:
                needed_ids.add(nid)

        tag_vectors = self._load_tag_vectors(list(needed_ids))
        if not tag_vectors:
            return {}

        residuals = {}
        for tag_id in top_ids:
            if tag_id not in tag_vectors:
                continue
            residual = self._compute_single(tag_id, tag_vectors[tag_id], tag_vectors)
            residuals[tag_id] = residual

        elapsed = time.time() - start
        if residuals:
            logger.info(
                f"[WaveMemory] IntrinsicResidual computed: {len(residuals)} tags, "
                f"mean={np.mean(list(residuals.values())):.4f}, "
                f"elapsed={elapsed:.2f}s"
            )
        return residuals

    def compute_incremental(self, tag_ids: list[int]) -> dict[int, float]:
        """增量计算指定 Tag + 直接邻居的残差。"""
        affected = set(tag_ids)
        for tid in tag_ids:
            neighbors = self.cooccurrence.get_neighbors(tid, max_neighbors=20)
            for nid, _ in neighbors:
                affected.add(nid)

        tag_vectors = self._load_tag_vectors(list(affected))

        residuals = {}
        for tag_id in affected:
            if tag_id not in tag_vectors:
                continue
            residual = self._compute_single(tag_id, tag_vectors[tag_id], tag_vectors)
            residuals[tag_id] = residual

        return residuals

    def _compute_single(self, tag_id: int, tag_vec: np.ndarray, all_vectors: dict[int, np.ndarray]) -> float:
        """计算单个 Tag 的残差能量。"""
        neighbors = self.cooccurrence.get_neighbors(tag_id, max_neighbors=30)
        if not neighbors:
            return 1.0

        neighbor_vecs = []
        for nid, weight in neighbors:
            if nid in all_vectors:
                neighbor_vecs.append(all_vectors[nid] * weight)

        if len(neighbor_vecs) < 2:
            return 0.8

        neighbor_matrix = np.vstack(neighbor_vecs)

        try:
            rank = min(self.svd_rank, len(neighbor_vecs), neighbor_matrix.shape[1])
            U, S, Vt = np.linalg.svd(neighbor_matrix, full_matrices=False)
            basis = Vt[:rank]

            tag_norm = tag_vec / (np.linalg.norm(tag_vec) + 1e-8)
            projection = basis.T @ (basis @ tag_norm)
            residual_vec = tag_norm - projection

            residual_energy = float(np.linalg.norm(residual_vec))
            return min(residual_energy, 1.0)

        except Exception:
            return 0.5

    def persist(self, residuals: dict[int, float]):
        """批量持久化残差到数据库。"""
        now = time.time()
        # 一次批量写入再提交：逐条 execute 时每条都要重新抢 GIL，后台有计算线程时写事务会被拖长到
        # 超过写入协调器的 busy_timeout，其他写入随之报 database is locked。
        self.db.conn.executemany(
            """INSERT OR REPLACE INTO tag_intrinsic_residuals (tag_id, residual_energy, computed_at)
               VALUES (?, ?, ?)""",
            [(tag_id, energy, now) for tag_id, energy in residuals.items()],
        )
        self.db.conn.commit()
        logger.info(f"[WaveMemory] IntrinsicResidual persisted: {len(residuals)} tags")

    def load(self) -> dict[int, float]:
        """从数据库加载残差。"""
        rows = self.db.conn.execute(
            "SELECT tag_id, residual_energy FROM tag_intrinsic_residuals"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
