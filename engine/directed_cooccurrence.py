"""Wave Memory 有向序位共现矩阵 — 基于 Tag 在记忆中的位置构建有向拓扑"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from itertools import groupby
from typing import Optional

from astrbot.api import logger

from .database import WaveMemoryDB


def ordinal_potential(position: int, max_position: int) -> float:
    """计算序位势能 Φ ∈ [0.5, 0.9]。

    position=1 的 Tag 势能最高（0.9），越靠后越低。
    position=0 表示旧数据无序位信息，退化为 0.7。
    """
    if position <= 0:
        return 0.7
    if max_position <= 1:
        return 0.7
    return 0.9 - 0.4 * (position - 1) / (max_position - 1)


class DirectedCooccurrence:
    """有向序位共现矩阵。

    与旧 CooccurrenceMatrix 接口兼容（get_neighbors / node_count / edge_count），
    但内部维护有向权重。
    """

    def __init__(self, db: WaveMemoryDB):
        self.db = db
        # {source_id: {target_id: directed_weight}}
        self.forward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        # 反向索引（用于查询"谁指向我"）
        self.backward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self._tag_count = 0

    def rebuild(self):
        """从 memory_tags 构建有向共现矩阵。"""
        new_forward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        new_backward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))

        rows = self.db.conn.execute("""
            SELECT memory_id, tag_id, position FROM memory_tags
            ORDER BY memory_id, position
        """).fetchall()

        if not rows:
            self.forward = new_forward
            self.backward = new_backward
            self._tag_count = 0
            logger.info("[WaveMemory] DirectedCooccurrence: no data")
            return

        # 按 memory_id 分组
        for mem_id, group in groupby(rows, key=lambda r: r[0]):
            tags = [(r[1], r[2]) for r in group]
            if len(tags) < 2:
                continue

            max_pos = max(p for _, p in tags) if tags else 1

            # 对每对 (source, target) 计算有向权重
            for i, (src_id, src_pos) in enumerate(tags):
                src_phi = ordinal_potential(src_pos, max_pos)
                for j, (tgt_id, tgt_pos) in enumerate(tags):
                    if i == j or src_id == tgt_id:
                        continue
                    tgt_phi = ordinal_potential(tgt_pos, max_pos)
                    weight = src_phi * tgt_phi
                    new_forward[src_id][tgt_id] += weight
                    new_backward[tgt_id][src_id] += weight

        # 归一化：除以最大值使权重 ∈ (0, 1]
        max_w = 0.0
        for neighbors in new_forward.values():
            for w in neighbors.values():
                if w > max_w:
                    max_w = w

        if max_w > 0:
            for src in new_forward:
                for tgt in new_forward[src]:
                    new_forward[src][tgt] /= max_w
            for tgt in new_backward:
                for src in new_backward[tgt]:
                    new_backward[tgt][src] /= max_w

        # 剪枝：权重 < 0.01 的边移除（节省内存）
        for src in list(new_forward.keys()):
            new_forward[src] = {
                tgt: w for tgt, w in new_forward[src].items() if w >= 0.01
            }
            if not new_forward[src]:
                del new_forward[src]

        for tgt in list(new_backward.keys()):
            new_backward[tgt] = {
                src: w for src, w in new_backward[tgt].items() if w >= 0.01
            }
            if not new_backward[tgt]:
                del new_backward[tgt]

        # 原子切换
        self.forward = new_forward
        self.backward = new_backward
        self._tag_count = self.db.get_tag_count()

        logger.info(
            f"[WaveMemory] DirectedCooccurrence rebuilt: "
            f"{len(self.forward)} nodes, {sum(len(v) for v in self.forward.values())} directed edges"
        )

    def get_neighbors(self, tag_id: int, max_neighbors: int = 20) -> list[tuple[int, float]]:
        """获取某个 Tag 的有向出边邻居，按权重降序。接口兼容旧 CooccurrenceMatrix。"""
        neighbors = self.forward.get(tag_id, {})
        sorted_n = sorted(neighbors.items(), key=lambda x: x[1], reverse=True)
        return sorted_n[:max_neighbors]

    def get_incoming(self, tag_id: int, max_neighbors: int = 20) -> list[tuple[int, float]]:
        """获取指向该 Tag 的入边邻居。"""
        neighbors = self.backward.get(tag_id, {})
        sorted_n = sorted(neighbors.items(), key=lambda x: x[1], reverse=True)
        return sorted_n[:max_neighbors]

    @property
    def node_count(self) -> int:
        return len(self.forward)

    @property
    def edge_count(self) -> int:
        return sum(len(v) for v in self.forward.values())

    def needs_rebuild(self, threshold_pct: float = 0.01) -> bool:
        """判断是否需要重建。"""
        current_count = self.db.get_tag_count()
        if self._tag_count == 0:
            return current_count > 10
        change = abs(current_count - self._tag_count) / self._tag_count
        return change >= threshold_pct


class CooccurrenceScheduler:
    """防抖重建调度器。

    累积 Tag 变更超过阈值后，等待冷却期再触发重建。
    双缓冲：构建新矩阵后原子切换，不阻塞查询。
    """

    def __init__(
        self,
        cooccurrence: DirectedCooccurrence,
        threshold_pct: float = 0.01,
        cooldown_sec: float = 300,
        on_rebuild_complete=None,
    ):
        self.cooccurrence = cooccurrence
        self.threshold_pct = threshold_pct
        self.cooldown_sec = cooldown_sec
        self.on_rebuild_complete = on_rebuild_complete  # 回调：重建完成后触发（如残差重算）
        self._accumulated_changes = 0
        self._timer_handle = None
        self._is_rebuilding = False

    def notify_tag_change(self, count: int = 1):
        """通知有 Tag 变更。"""
        self._accumulated_changes += count
        total = self.cooccurrence.node_count or 1
        if self._accumulated_changes / total >= self.threshold_pct:
            self._schedule_rebuild()

    def _schedule_rebuild(self):
        """防抖调度：取消旧定时器，设置新的。"""
        if self._timer_handle:
            self._timer_handle.cancel()
        try:
            loop = asyncio.get_event_loop()
            self._timer_handle = loop.call_later(
                self.cooldown_sec,
                lambda: asyncio.create_task(self._do_rebuild()),
            )
        except RuntimeError:
            # 没有事件循环时直接跳过
            pass

    async def _do_rebuild(self):
        """执行重建。"""
        if self._is_rebuilding:
            return
        self._is_rebuilding = True
        try:
            logger.info("[WaveMemory] CooccurrenceScheduler: starting rebuild...")
            # 双缓冲：在新对象上构建，然后原子切换
            new_matrix = DirectedCooccurrence(self.cooccurrence.db)
            new_matrix.rebuild()
            self.cooccurrence.forward = new_matrix.forward
            self.cooccurrence.backward = new_matrix.backward
            self.cooccurrence._tag_count = new_matrix._tag_count
            self._accumulated_changes = 0
            logger.info("[WaveMemory] CooccurrenceScheduler: rebuild complete")

            # 触发后续回调（如残差重算）
            if self.on_rebuild_complete:
                await self.on_rebuild_complete()

        except Exception as e:
            logger.error(f"[WaveMemory] CooccurrenceScheduler rebuild error: {e}")
        finally:
            self._is_rebuilding = False
