"""BookLore 向量索引 — 书设知识的独立 HNSW 索引"""

from __future__ import annotations

import os
import threading
from typing import Optional, List, Tuple

import numpy as np

try:
    import hnswlib
except ImportError:
    hnswlib = None


# Initial allocation used only when no corpus exists yet.  These are starting
# sizes, not caps: BookLore grows with daily chapter ingestion.
DEFAULT_ENTITY_MAX_ELEMENTS = 4096
DEFAULT_COMMUNITY_MAX_ELEMENTS = 1024
DEFAULT_NOTES_MAX_ELEMENTS = 512

# BookLore grows incrementally: roughly one or two chapters are extracted per day.
# It must therefore never refuse a write, but it also must not preallocate in huge
# jumps.  hnswlib pays resident memory for ``max_elements`` regardless of the live
# row count, so the original ``needed + 10000`` step wasted memory for years of
# headroom that a daily trickle does not need.
#
# Instead: start fitted to the existing corpus and grow in small proportional
# steps.  A ~15% step keeps resize events rare (amortised) while keeping unused
# slack proportional to real content rather than a fixed large block.
GROWTH_MARGIN_RATIO = 0.15
MIN_GROWTH_MARGIN = 128


def _bounded(value: object, default: int) -> int:
    """Return a positive allocation size; malformed input keeps the default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def growth_step(current_capacity: int) -> int:
    """Return the next allocation increment for a growing index."""
    try:
        current = int(current_capacity)
    except (TypeError, ValueError):
        current = 0
    if current <= 0:
        return MIN_GROWTH_MARGIN
    return max(MIN_GROWTH_MARGIN, int(current * GROWTH_MARGIN_RATIO))


def grown_capacity(current_capacity: int, needed: int) -> int:
    """Smallest stepped capacity that fits ``needed``.

    Growth is unbounded by design: a hard count ceiling would eventually reject
    legitimate daily chapter ingestion.  Stepping keeps resize calls amortised
    instead of reallocating on every single insert.
    """
    try:
        target = max(0, int(current_capacity))
    except (TypeError, ValueError):
        target = 0
    try:
        required = max(0, int(needed))
    except (TypeError, ValueError):
        required = 0
    if required <= target:
        return max(1, target)
    while target < required:
        target += growth_step(target)
    return target


def fitted_capacity(existing_count: int, *, initial: int) -> int:
    """Size an index to its existing corpus plus one growth step.

    ``initial`` is only the starting allocation for an empty corpus; it is not a
    cap.  Fitting avoids paying resident memory for slots a slow-growing corpus
    will not reach for years.
    """
    try:
        count = int(existing_count)
    except (TypeError, ValueError):
        count = 0
    start = _bounded(initial, MIN_GROWTH_MARGIN)
    if count <= 0:
        return start
    return max(1, count + growth_step(count))


class BookLoreIndex:
    """书设实体和社区报告的独立向量索引。

    书设按每天一两章的节奏持续增长，因此这里不设条数硬上限：写入永不被拒绝。
    内存控制靠“贴合已有语料 + 小步扩容”实现，而不是预分配大块空槽。
    """

    def __init__(
        self,
        dimension: int,
        data_dir: str,
        max_elements: int = DEFAULT_ENTITY_MAX_ELEMENTS,
        *,
        community_max_elements: int = DEFAULT_COMMUNITY_MAX_ELEMENTS,
        notes_max_elements: int = DEFAULT_NOTES_MAX_ELEMENTS,
        fit_to_existing: bool = True,
    ):
        if hnswlib is None:
            raise ImportError("hnswlib is required: pip install hnswlib")

        self.dimension = dimension
        self.data_dir = data_dir
        self._lock = threading.Lock()

        entity_initial = _bounded(max_elements, DEFAULT_ENTITY_MAX_ELEMENTS)
        community_initial = _bounded(
            community_max_elements, DEFAULT_COMMUNITY_MAX_ELEMENTS
        )
        notes_initial = _bounded(notes_max_elements, DEFAULT_NOTES_MAX_ELEMENTS)

        # 书设按每天一两章持续增长：起始容量贴合已有语料，之后按需小步扩容。
        # 这里的入参只是“初始分配”，不是上限。
        if fit_to_existing:
            counts = self._existing_counts()
            self.max_elements = fitted_capacity(
                counts.get("entities", 0), initial=entity_initial
            )
            self.community_max_elements = fitted_capacity(
                counts.get("communities", 0), initial=community_initial
            )
            self.notes_max_elements = fitted_capacity(
                counts.get("notes", 0), initial=notes_initial
            )
        else:
            self.max_elements = entity_initial
            self.community_max_elements = community_initial
            self.notes_max_elements = notes_initial

        # 实体索引
        self.entity_index_path = os.path.join(data_dir, "book_entities.hnsw")
        self.entity_index = hnswlib.Index(space="cosine", dim=dimension)
        if os.path.exists(self.entity_index_path):
            self.entity_index.load_index(self.entity_index_path, max_elements=self.max_elements)
        else:
            self.entity_index.init_index(
                max_elements=self.max_elements, ef_construction=200, M=16
            )
        self.entity_index.set_ef(50)

        # 社区索引
        self.community_index_path = os.path.join(data_dir, "book_communities.hnsw")
        self.community_index = hnswlib.Index(space="cosine", dim=dimension)
        if os.path.exists(self.community_index_path):
            self.community_index.load_index(
                self.community_index_path, max_elements=self.community_max_elements
            )
        else:
            self.community_index.init_index(
                max_elements=self.community_max_elements, ef_construction=200, M=16
            )
        self.community_index.set_ef(50)

        # 笔记索引
        self.notes_index_path = os.path.join(data_dir, "book_notes.hnsw")
        self.notes_index = hnswlib.Index(space="cosine", dim=dimension)
        if os.path.exists(self.notes_index_path):
            self.notes_index.load_index(
                self.notes_index_path, max_elements=self.notes_max_elements
            )
        else:
            self.notes_index.init_index(
                max_elements=self.notes_max_elements, ef_construction=200, M=16
            )
        self.notes_index.set_ef(50)

        # ID 映射：hnsw 内部用 int id，需要映射到 entity text id
        self._entity_id_map: dict[int, str] = {}  # int_id → entity_id
        self._entity_int_counter = 0
        self._community_id_map: dict[int, str] = {}
        self._community_int_counter = 0
        self._notes_id_map: dict[int, str] = {}
        self._notes_int_counter = 0

    def _existing_counts(self) -> dict[str, int]:
        """Best-effort content sizes for capacity fitting.

        Reads only cheap metadata that is already on disk (the persisted id maps,
        or the npz bootstrap headers).  Any failure returns 0 for that tier, which
        makes ``fitted_capacity`` fall back to the configured ceiling instead of
        silently starving a first-time import.
        """
        import json

        counts = {"entities": 0, "communities": 0, "notes": 0}
        map_path = os.path.join(self.data_dir, "book_lore_id_maps.json")
        if os.path.exists(map_path):
            try:
                with open(map_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                counts["entities"] = len(data.get("entity_map", {}) or {})
                counts["communities"] = len(data.get("community_map", {}) or {})
                counts["notes"] = len(data.get("notes_map", {}) or {})
                if any(counts.values()):
                    return counts
            except (OSError, ValueError, TypeError):
                return {"entities": 0, "communities": 0, "notes": 0}

        # No id maps yet: fall back to npz headers so a fresh import still fits.
        for key, filename in (
            ("entities", "book_lore_entity_vectors.npz"),
            ("communities", "book_lore_community_vectors.npz"),
            ("notes", "book_lore_notes_vectors.npz"),
        ):
            path = os.path.join(self.data_dir, filename)
            if not os.path.exists(path):
                continue
            try:
                with np.load(path, allow_pickle=True) as data:
                    counts[key] = int(len(data["ids"]))
            except (OSError, ValueError, KeyError, TypeError):
                counts[key] = 0
        return counts

    # ─── 容量增长（调用方已持锁）─────────────────────────────────────────────

    def _ensure_entity_capacity(self, needed: int) -> None:
        """Grow the entity index in small steps; never refuse a write."""
        if needed <= self.max_elements:
            return
        target = grown_capacity(self.max_elements, needed)
        self.entity_index.resize_index(target)
        self.max_elements = target

    def _ensure_community_capacity(self, needed: int) -> None:
        if needed <= self.community_max_elements:
            return
        target = grown_capacity(self.community_max_elements, needed)
        self.community_index.resize_index(target)
        self.community_max_elements = target

    def _ensure_notes_capacity(self, needed: int) -> None:
        if needed <= self.notes_max_elements:
            return
        target = grown_capacity(self.notes_max_elements, needed)
        self.notes_index.resize_index(target)
        self.notes_max_elements = target

    # ─── 实体索引操作 ─────────────────────────────────────────────────────────

    def add_entity(self, entity_id: str, vector: np.ndarray) -> int:
        """添加单个实体向量，返回内部 int id。容量不足时按小步扩容。"""
        with self._lock:
            current = self.entity_index.get_current_count()
            self._ensure_entity_capacity(current + 1)
            int_id = self._entity_int_counter
            self._entity_int_counter += 1
            self._entity_id_map[int_id] = entity_id
            self.entity_index.add_items(
                vector.astype(np.float32).reshape(1, -1),
                np.array([int_id], dtype=np.int64),
            )
        return int_id

    def add_entities_batch(self, entity_ids: List[str], vectors: np.ndarray):
        """批量添加实体向量。vectors shape: (n, dim)

        容量不足时按小步扩容；书设写入不会被拒绝。
        """
        n = len(entity_ids)
        if n == 0:
            return
        with self._lock:
            current = self.entity_index.get_current_count()
            self._ensure_entity_capacity(current + n)
            int_ids = list(range(self._entity_int_counter, self._entity_int_counter + n))
            self._entity_int_counter += n
            for int_id, eid in zip(int_ids, entity_ids):
                self._entity_id_map[int_id] = eid
            self.entity_index.add_items(
                vectors.astype(np.float32),
                np.array(int_ids, dtype=np.int64),
            )

    def search_entities(self, query_vector: np.ndarray, k: int = 10) -> List[Tuple[str, float]]:
        """搜索最相似的实体。返回 [(entity_id, similarity_score), ...]"""
        if self.entity_index.get_current_count() == 0:
            return []
        k = min(k, self.entity_index.get_current_count())
        with self._lock:
            labels, distances = self.entity_index.knn_query(
                query_vector.astype(np.float32).reshape(1, -1), k=k
            )
        results = []
        for int_id, dist in zip(labels[0].tolist(), distances[0].tolist()):
            entity_id = self._entity_id_map.get(int_id, "")
            if entity_id:
                # cosine distance → similarity: 1 - dist
                results.append((entity_id, 1.0 - dist))
        return results

    # ─── 社区索引操作 ─────────────────────────────────────────────────────────

    def add_community(self, community_id: str, vector: np.ndarray) -> int:
        """添加社区报告向量。容量不足时按小步扩容。"""
        with self._lock:
            current = self.community_index.get_current_count()
            self._ensure_community_capacity(current + 1)
            int_id = self._community_int_counter
            self._community_int_counter += 1
            self._community_id_map[int_id] = community_id
            self.community_index.add_items(
                vector.astype(np.float32).reshape(1, -1),
                np.array([int_id], dtype=np.int64),
            )
        return int_id

    def add_communities_batch(self, community_ids: List[str], vectors: np.ndarray):
        """批量添加社区向量。容量不足时按小步扩容。"""
        n = len(community_ids)
        if n == 0:
            return
        with self._lock:
            current = self.community_index.get_current_count()
            self._ensure_community_capacity(current + n)
            int_ids = list(range(self._community_int_counter, self._community_int_counter + n))
            self._community_int_counter += n
            for int_id, cid in zip(int_ids, community_ids):
                self._community_id_map[int_id] = cid
            self.community_index.add_items(
                vectors.astype(np.float32),
                np.array(int_ids, dtype=np.int64),
            )

    def search_communities(self, query_vector: np.ndarray, k: int = 5) -> List[Tuple[str, float]]:
        """搜索最相关的社区报告。"""
        if self.community_index.get_current_count() == 0:
            return []
        k = min(k, self.community_index.get_current_count())
        with self._lock:
            labels, distances = self.community_index.knn_query(
                query_vector.astype(np.float32).reshape(1, -1), k=k
            )
        results = []
        for int_id, dist in zip(labels[0].tolist(), distances[0].tolist()):
            cid = self._community_id_map.get(int_id, "")
            if cid:
                results.append((cid, 1.0 - dist))
        return results

    # ─── 笔记索引操作 ─────────────────────────────────────────────────────────

    def add_notes_batch(self, note_ids: List[str], vectors: np.ndarray):
        """批量添加笔记向量。容量不足时按小步扩容。"""
        n = len(note_ids)
        if n == 0:
            return
        with self._lock:
            current = self.notes_index.get_current_count()
            self._ensure_notes_capacity(current + n)
            int_ids = list(range(self._notes_int_counter, self._notes_int_counter + n))
            self._notes_int_counter += n
            for int_id, nid in zip(int_ids, note_ids):
                self._notes_id_map[int_id] = nid
            self.notes_index.add_items(
                vectors.astype(np.float32),
                np.array(int_ids, dtype=np.int64),
            )

    def search_notes(self, query_vector: np.ndarray, k: int = 5) -> List[Tuple[str, float]]:
        """搜索最相关的笔记。"""
        # get_current_count() 含已标记删除的元素；工作台删改过笔记后，k 超过存活数量
        # hnswlib 会报 "Cannot return the results in a contiguous 2D array"。
        live = len(self._notes_id_map)
        if live == 0:
            return []
        k = min(k, live)
        query = query_vector.astype(np.float32).reshape(1, -1)
        with self._lock:
            while True:
                try:
                    labels, distances = self.notes_index.knn_query(query, k=k)
                    break
                except RuntimeError:
                    if k <= 1:
                        return []
                    k = max(1, k // 2)
        results = []
        for int_id, dist in zip(labels[0].tolist(), distances[0].tolist()):
            nid = self._notes_id_map.get(int_id, "")
            if nid:
                results.append((nid, 1.0 - dist))
        return results

    @property
    def notes_count(self) -> int:
        return self.notes_index.get_current_count()

    # ─── 书设工作台：笔记的增量更新 ─────────────────────────────────────────

    def note_ids(self) -> set[str]:
        """已在笔记索引里（未删除）的笔记 id。"""
        with self._lock:
            return set(self._notes_id_map.values())

    def remove_notes(self, note_ids: List[str]) -> int:
        """从笔记索引里删掉这些笔记（hnswlib 标记删除，检索不再返回）。"""
        targets = set(note_ids)
        removed = 0
        with self._lock:
            for int_id, nid in list(self._notes_id_map.items()):
                if nid in targets:
                    try:
                        self.notes_index.mark_deleted(int_id)
                    except RuntimeError:
                        pass  # 已经标记过
                    del self._notes_id_map[int_id]
                    removed += 1
        return removed

    def upsert_notes(self, note_ids: List[str], vectors: np.ndarray) -> None:
        """新增或替换笔记向量：同 id 的旧向量先标记删除，再写入新向量。"""
        if not note_ids:
            return
        self.remove_notes(list(note_ids))
        self.add_notes_batch(list(note_ids), np.asarray(vectors, dtype=np.float32))

    def save_notes(self) -> None:
        """只保存笔记索引与 id 映射（实体、社区索引几十 MB，工作台每次改动不必重写）。"""
        with self._lock:
            if self.notes_index.get_current_count() > 0:
                self.notes_index.save_index(self.notes_index_path)
        self._save_id_maps()

    def _save_id_maps(self) -> None:
        import json

        map_path = os.path.join(self.data_dir, "book_lore_id_maps.json")
        with self._lock:
            payload = {
                "entity_map": {str(k): v for k, v in self._entity_id_map.items()},
                "entity_counter": self._entity_int_counter,
                "community_map": {str(k): v for k, v in self._community_id_map.items()},
                "community_counter": self._community_int_counter,
                "notes_map": {str(k): v for k, v in self._notes_id_map.items()},
                "notes_counter": self._notes_int_counter,
            }
        partial = map_path + ".partial"
        with open(partial, "w") as f:
            json.dump(payload, f)
        os.replace(partial, map_path)  # 写一半断电也不会留下坏的映射文件

    # ─── 持久化 ───────────────────────────────────────────────────────────────

    def save(self):
        """保存索引到磁盘。"""
        import json
        with self._lock:
            if self.entity_index.get_current_count() > 0:
                self.entity_index.save_index(self.entity_index_path)
            if self.community_index.get_current_count() > 0:
                self.community_index.save_index(self.community_index_path)
            if self.notes_index.get_current_count() > 0:
                self.notes_index.save_index(self.notes_index_path)

        # 保存 ID 映射
        map_path = os.path.join(self.data_dir, "book_lore_id_maps.json")
        with open(map_path, "w") as f:
            json.dump({
                "entity_map": {str(k): v for k, v in self._entity_id_map.items()},
                "entity_counter": self._entity_int_counter,
                "community_map": {str(k): v for k, v in self._community_id_map.items()},
                "community_counter": self._community_int_counter,
                "notes_map": {str(k): v for k, v in self._notes_id_map.items()},
                "notes_counter": self._notes_int_counter,
            }, f)

    def load_id_maps(self):
        """从磁盘加载 ID 映射。如果有 npz 向量文件但没有 hnsw 索引，自动构建。"""
        import json
        map_path = os.path.join(self.data_dir, "book_lore_id_maps.json")
        if os.path.exists(map_path):
            with open(map_path, "r") as f:
                data = json.load(f)
            self._entity_id_map = {int(k): v for k, v in data.get("entity_map", {}).items()}
            self._entity_int_counter = data.get("entity_counter", 0)
            self._community_id_map = {int(k): v for k, v in data.get("community_map", {}).items()}
            self._community_int_counter = data.get("community_counter", 0)
            self._notes_id_map = {int(k): v for k, v in data.get("notes_map", {}).items()}
            self._notes_int_counter = data.get("notes_counter", 0)
            return

        # 如果没有 id_maps 但有 npz 文件，从 npz 构建索引。
        # 全量载入：书设内容不能被容量截断，容量会按需扩容以容纳整份语料。
        entity_npz = os.path.join(self.data_dir, "book_lore_entity_vectors.npz")
        if os.path.exists(entity_npz) and self.entity_index.get_current_count() == 0:
            try:
                data = np.load(entity_npz, allow_pickle=True)
                self.add_entities_batch(data["ids"].tolist(), data["vectors"])
            except Exception:
                pass

        community_npz = os.path.join(self.data_dir, "book_lore_community_vectors.npz")
        if os.path.exists(community_npz) and self.community_index.get_current_count() == 0:
            try:
                data = np.load(community_npz, allow_pickle=True)
                self.add_communities_batch(data["ids"].tolist(), data["vectors"])
            except Exception:
                pass

        notes_npz = os.path.join(self.data_dir, "book_lore_notes_vectors.npz")
        if os.path.exists(notes_npz) and self.notes_index.get_current_count() == 0:
            try:
                data = np.load(notes_npz, allow_pickle=True)
                self.add_notes_batch(data["ids"].tolist(), data["vectors"])
            except Exception:
                pass

        # 保存构建好的索引
        if self.entity_index.get_current_count() > 0 or self.community_index.get_current_count() > 0 or self.notes_index.get_current_count() > 0:
            self.save()

    @property
    def entity_count(self) -> int:
        return self.entity_index.get_current_count()

    @property
    def community_count(self) -> int:
        return self.community_index.get_current_count()
