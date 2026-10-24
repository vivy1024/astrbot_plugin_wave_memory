"""BookLoreIndex 的笔记映射必须与 hnsw 的实际内容一致。

线上事故（2026-10-24）：工作台导入第 962–1023 章时 embedding 失败，``add_notes_batch``
先把 int id 写进 ``_notes_id_map`` 再调 ``notes_index.add_items``；写入抛错后映射留了下来，
于是这 52 条笔记被当成「已在索引里」，检索永远看不到、补齐索引也不会重试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("hnswlib")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.book_lore_index import BookLoreIndex  # noqa: E402


def _index(tmp_path: Path) -> BookLoreIndex:
    return BookLoreIndex(data_dir=str(tmp_path), dimension=8, notes_max_elements=16)


def _vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random(8).astype(np.float32)


def test_write_failure_does_not_leave_mapping_entries(tmp_path, monkeypatch):
    index = _index(tmp_path)

    class _Broken:
        def __init__(self, real):
            self._real = real

        def get_current_count(self):
            return self._real.get_current_count()

        def add_items(self, *args, **kwargs):
            raise RuntimeError("hnsw write failed")

        def __getattr__(self, name):
            return getattr(self._real, name)

    import engine.book_lore_index as module

    original = module.hnswlib.Index

    def _factory(*args, **kwargs):
        return _Broken(original(*args, **kwargs))

    monkeypatch.setattr(module.hnswlib, "Index", _factory)

    with pytest.raises(RuntimeError):
        index.add_notes_batch(["note_a", "note_b"], np.stack([_vector(1), _vector(2)]))

    # 关键：映射里不能出现 hnsw 并不存在的条目
    assert index.note_ids() == set()


def test_successful_write_registers_mapping(tmp_path):
    index = _index(tmp_path)

    index.add_notes_batch(["note_a", "note_b"], np.stack([_vector(1), _vector(2)]))

    assert index.note_ids() == {"note_a", "note_b"}
    assert index.notes_count == 2


def test_load_id_maps_drops_phantom_entries(tmp_path):
    """映射里登记了 int id、hnsw 里没有的条目，加载时清掉，补齐索引才会重试。"""

    index = _index(tmp_path)
    index.add_notes_batch(["note_real"], np.stack([_vector(1)]))
    index.save_notes()

    # 手工塞一条幽灵映射：int id 99 在 hnsw 里并不存在
    index._notes_id_map[99] = "note_ghost"
    index._save_id_maps()

    reloaded = _index(tmp_path)
    reloaded.load_id_maps()

    assert reloaded.note_ids() == {"note_real"}


def test_load_id_maps_keeps_mapping_when_index_unreadable(tmp_path, monkeypatch):
    """hnsw 读不出 id 列表时不要乱删映射（老版本没有 get_ids_list）。"""

    index = _index(tmp_path)
    index.add_notes_batch(["note_real"], np.stack([_vector(1)]))
    index.save_notes()

    import engine.book_lore_index as module

    reloaded = _index(tmp_path)

    def _no_ids():
        raise AttributeError("get_ids_list")

    monkeypatch.setattr(reloaded.notes_index, "get_ids_list", _no_ids, raising=False)

    reloaded.load_id_maps()

    assert reloaded.note_ids() == {"note_real"}
