"""Wave Memory EPA 模块 — 嵌入投影分析，判断查询聚焦度"""

from __future__ import annotations

import json
from typing import Optional

import numpy as np

from astrbot.api import logger

from .database import WaveMemoryDB


class EPAModule:
    """EPA (Embedding Projection Analysis)：分析查询向量的语义聚焦度。

    通过 PCA 将 Tag 向量空间降维，然后把查询向量投影到主成分上，
    根据能量分布判断查询是"聚焦"还是"发散"。
    """

    def __init__(self, db: WaveMemoryDB, max_basis_dim: int = 32, min_tags: int = 20):
        self.db = db
        self.max_basis_dim = max_basis_dim
        self.min_tags = min_tags

        self.basis: Optional[np.ndarray] = None  # (K, dim)
        self.mean_vector: Optional[np.ndarray] = None  # (dim,)
        self.energies: Optional[np.ndarray] = None  # (K,) 特征值
        self.initialized = False

    def initialize(self) -> bool:
        """从 Tag 向量构建 PCA 基底。"""
        # 尝试从缓存加载
        cached = self.db.get_kv("epa_basis")
        if cached:
            try:
                meta = json.loads(cached[0])
                if cached[1] is not None:
                    dim = meta["dim"]
                    k = meta["k"]
                    blob = cached[1]
                    # blob 包含: mean(dim) + basis(k*dim) + energies(k)
                    offset = 0
                    self.mean_vector = np.frombuffer(blob[offset:offset + dim * 4], dtype=np.float32).copy()
                    offset += dim * 4
                    self.basis = np.frombuffer(blob[offset:offset + k * dim * 4], dtype=np.float32).reshape(k, dim).copy()
                    offset += k * dim * 4
                    self.energies = np.frombuffer(blob[offset:offset + k * 4], dtype=np.float32).copy()
                    self.initialized = True
                    logger.debug(f"[WaveMemory] EPA loaded from cache: {k} components")
                    return True
            except Exception:
                pass

        # 从 Tag 向量计算
        tag_data = self.db.get_all_tag_vectors()
        if len(tag_data) < self.min_tags:
            logger.debug(f"[WaveMemory] EPA: not enough tags ({len(tag_data)} < {self.min_tags})")
            return False

        vectors = np.array([t[2] for t in tag_data], dtype=np.float32)
        dim = vectors.shape[1]

        # 去中心化
        self.mean_vector = vectors.mean(axis=0)
        centered = vectors - self.mean_vector

        # SVD (截断)
        try:
            from sklearn.decomposition import TruncatedSVD
            k = min(self.max_basis_dim, len(vectors) - 1, dim)
            svd = TruncatedSVD(n_components=k)
            svd.fit(centered)
            self.basis = svd.components_.astype(np.float32)  # (k, dim)
            self.energies = svd.singular_values_.astype(np.float32)
        except ImportError:
            # fallback: numpy SVD
            U, S, Vt = np.linalg.svd(centered, full_matrices=False)
            k = min(self.max_basis_dim, len(S))
            self.basis = Vt[:k].astype(np.float32)
            self.energies = S[:k].astype(np.float32)

        self.initialized = True

        # 缓存
        self._save_cache(dim, k)
        logger.info(f"[WaveMemory] EPA initialized: {k} components from {len(tag_data)} tags")
        return True

    def analyze(self, query_vector: np.ndarray) -> dict:
        """分析查询向量的语义特征。

        Returns:
            {
                "logic_depth": float (0~1, 高=聚焦),
                "entropy": float (0~1, 高=发散),
                "dominant_axis": int (最强主成分索引),
            }
        """
        if not self.initialized:
            return {"logic_depth": 0.5, "entropy": 0.5, "dominant_axis": 0}

        # 去中心化
        centered = query_vector - self.mean_vector

        # 投影到基底
        projections = self.basis @ centered  # (k,)
        proj_energy = projections ** 2

        # 归一化为概率分布
        total = proj_energy.sum()
        if total < 1e-12:
            return {"logic_depth": 0.5, "entropy": 0.5, "dominant_axis": 0}

        probs = proj_energy / total

        # 计算熵
        entropy = -float(np.sum(probs * np.log(probs + 1e-10))) / np.log(len(probs))
        logic_depth = 1.0 - entropy

        # 最强轴
        dominant_axis = int(np.argmax(proj_energy))

        return {
            "logic_depth": float(logic_depth),
            "entropy": float(entropy),
            "dominant_axis": dominant_axis,
        }

    def _save_cache(self, dim: int, k: int):
        """将基底缓存到 DB。"""
        meta = json.dumps({"dim": dim, "k": k})
        # 拼接 blob: mean + basis + energies
        blob = np.concatenate([
            self.mean_vector,
            self.basis.flatten(),
            self.energies,
        ]).astype(np.float32)
        self.db.put_kv("epa_basis", meta, blob)
