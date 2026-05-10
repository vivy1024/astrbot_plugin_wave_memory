"""Wave Memory 向量索引 — hnswlib 封装"""

from __future__ import annotations

import os
import threading
from typing import Optional

import numpy as np

try:
    import hnswlib
except ImportError:
    hnswlib = None


class VectorIndex:
    """基于 hnswlib 的 HNSW 向量索引，支持增量添加和持久化。"""

    def __init__(self, dimension: int, max_elements: int = 100000, index_path: Optional[str] = None):
        if hnswlib is None:
            raise ImportError("hnswlib is required: pip install hnswlib")

        self.dimension = dimension
        self.max_elements = max_elements
        self.index_path = index_path
        self._lock = threading.Lock()

        self.index = hnswlib.Index(space="cosine", dim=dimension)

        if index_path and os.path.exists(index_path):
            self.index.load_index(index_path, max_elements=max_elements)
        else:
            self.index.init_index(max_elements=max_elements, ef_construction=200, M=16)

        self.index.set_ef(50)

    def add(self, ids: list[int], vectors: np.ndarray):
        """添加向量到索引。vectors shape: (n, dim)"""
        with self._lock:
            current = self.index.get_current_count()
            needed = current + len(ids)
            if needed > self.max_elements:
                self.index.resize_index(needed + 10000)
                self.max_elements = needed + 10000
            self.index.add_items(vectors.astype(np.float32), np.array(ids, dtype=np.int64))

    def search(self, query: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
        """搜索最近邻。返回 [(id, distance), ...]，distance 越小越相似（cosine distance）。"""
        if self.index.get_current_count() == 0:
            return []
        k = min(k, self.index.get_current_count())
        with self._lock:
            labels, distances = self.index.knn_query(
                query.astype(np.float32).reshape(1, -1), k=k
            )
        return list(zip(labels[0].tolist(), distances[0].tolist()))

    def save(self):
        """持久化索引到磁盘。"""
        if self.index_path:
            with self._lock:
                os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
                self.index.save_index(self.index_path)

    @property
    def count(self) -> int:
        return self.index.get_current_count()
