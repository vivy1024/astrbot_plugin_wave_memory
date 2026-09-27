"""Wave Memory 有向序位共现矩阵 — 语义增益调制 + 反向锚定 + 防抖修复"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from itertools import groupby
from typing import TYPE_CHECKING, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests without AstrBot
    import logging
    logger = logging.getLogger(__name__)

from .cooccurrence_budget import RowBudgetParams, build_kernel, compress_evidence, compute_anchor_gains
from .db.scoped_tag_projection import effective_tag_rows
from .semantic_gain import bell_gain, SemanticGainConfig

if TYPE_CHECKING:  # 子进程构建只导入本模块，不拖进整个数据库层
    from .database import WaveMemoryDB


# Canonical rebuild-frequency policy for the derived cooccurrence projection.
# A full rebuild materialises a second forward/backward graph before publishing,
# so frequent rebuilds are the dominant memory-churn source.  These defaults are
# the single source of truth: main.py and any compatibility caller share them so
# an independently constructed scheduler cannot fall back to the old 5%/300s rate.
DEFAULT_REBUILD_THRESHOLD_PCT = 0.20
DEFAULT_REBUILD_COOLDOWN_SEC = 1800.0

# Bound on retained directed neighbours per source tag.  The builder previously
# accumulated the dense per-memory tag cross-product and only pruned afterwards,
# so peak memory grew with the widest tag co-occurrence rather than the retained
# graph.  Keeping the strongest edges preserves routing behaviour under a bound.
DEFAULT_MAX_NEIGHBORS_PER_TAG = 64

# 共现传播核：
#   global_max  全库按最大边权归一化、砍掉 < 0.01 的边（旧做法）
#   row_budget  每个标签的出边按固定预算分配（移植自 VCP TagMemo V9.1，见 cooccurrence_budget.py）
KERNEL_VERSIONS = ("global_max", "row_budget")
# 早期命名，配置里写旧值时照常识别
KERNEL_ALIASES = {"legacy": "global_max", "v91": "row_budget"}


def normalize_kernel(name) -> str:
    value = str(name or "").strip().lower()
    value = KERNEL_ALIASES.get(value, value)
    return value if value in KERNEL_VERSIONS else "global_max"

# 常驻图落盘格式版本；只存裁剪后的正向图（线上几百个节点、几千条边），反向图加载时推导。
SNAPSHOT_VERSION = 1
# 子进程构建超时；超时或失败时退回进程内线程构建。
SUBPROCESS_TIMEOUT_SEC = 600.0


def ordinal_potential(position: int, max_position: int) -> float:
    """计算序位势能 Φ ∈ [0.5, 0.9]。"""
    if position <= 0:
        return 0.7
    if max_position <= 1:
        return 0.7
    return 0.9 - 0.4 * (position - 1) / (max_position - 1)


class DirectedCooccurrence:
    """有向序位共现矩阵 + 语义增益调制 + 反向锚定。

    与旧 CooccurrenceMatrix 接口兼容。
    """

    def __init__(
        self,
        db: WaveMemoryDB,
        pair_sim_service=None,
        residual_map: dict = None,
        semantic_gain_config: SemanticGainConfig = None,
        max_neighbors_per_tag: int = DEFAULT_MAX_NEIGHBORS_PER_TAG,
        *,
        kernel_version: str = "global_max",
        kernel_params: RowBudgetParams | None = None,
    ):
        self.db = db
        self.kernel_version = normalize_kernel(kernel_version)
        self.kernel_params = kernel_params or RowBudgetParams()
        # row_budget：虫洞边（传播时低衰减、不扣动量）与锚增益（由内生残差映射），与图同代发布
        self.wormhole_edges: frozenset[tuple[int, int]] = frozenset()
        self.anchor_gain: dict[int, float] = {}
        self.pair_sim_service = pair_sim_service
        self.residual_map = residual_map or {}
        self.semantic_gain_config = semantic_gain_config or SemanticGainConfig()
        try:
            bound = int(max_neighbors_per_tag)
        except (TypeError, ValueError):
            bound = DEFAULT_MAX_NEIGHBORS_PER_TAG
        # 0 or negative would mean "unbounded"; keep a positive resident bound.
        self.max_neighbors_per_tag = bound if bound > 0 else DEFAULT_MAX_NEIGHBORS_PER_TAG
        # {source_id: {target_id: directed_weight}}
        self.forward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        # 反向索引
        self.backward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self._tag_count = 0

    def rebuild(self):
        """从 memory_tags 构建有向共现矩阵，加入语义增益调制。"""
        new_forward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        new_backward: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))

        has_scoped = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_memory_tags'"
        ).fetchone()
        if has_scoped:
            effective_rows = effective_tag_rows(self.db.conn)
            rows = [
                (
                    row["bot_id"],
                    row["session_id"],
                    row["visibility"],
                    int(row["memory_id"]),
                    int(row["tag_id"]),
                    int(row["position"]),
                )
                for row in effective_rows
                if row["tag_id"] is not None
            ]
            rows.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[5], row[4]))
        else:
            rows = [
                (None, None, None, int(row[0]), int(row[1]), int(row[2]))
                for row in self.db.conn.execute(
                    "SELECT memory_id, tag_id, position FROM memory_tags ORDER BY memory_id, position"
                ).fetchall()
            ]

        if not rows:
            self.publish(new_forward, 0)
            logger.info("[WaveMemory] DirectedCooccurrence: no data")
            return

        if self.kernel_version == "row_budget":
            self._rebuild_row_budget(rows, bool(has_scoped))
            return

        # 按完整 Scope + memory_id 分组，避免不同 Scope 的同号对象混合。
        for memory_key, group in groupby(rows, key=lambda r: (r[0], r[1], r[2], r[3])):
            del memory_key
            tags = [(r[4], r[5]) for r in group]
            if len(tags) < 2:
                continue

            max_pos = max(p for _, p in tags) if tags else 1

            for i, (src_id, src_pos) in enumerate(tags):
                src_phi = ordinal_potential(src_pos, max_pos)
                for j, (tgt_id, tgt_pos) in enumerate(tags):
                    if i == j or src_id == tgt_id:
                        continue
                    tgt_phi = ordinal_potential(tgt_pos, max_pos)
                    weight = src_phi * tgt_phi

                    # 语义增益调制：用 pair_sim 调节边权重
                    if self.pair_sim_service:
                        sim = self.pair_sim_service.get_similarity(src_id, tgt_id)
                        gain = bell_gain(sim, self.semantic_gain_config)
                        weight *= gain

                    # 反向锚定：高残差节点作为 target 时加权
                    tgt_residual = self.residual_map.get(tgt_id, 0.5)
                    weight *= (0.7 + 0.6 * tgt_residual)

                    new_forward[src_id][tgt_id] += weight
                    new_backward[tgt_id][src_id] += weight

        # 归一化
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

        # 剪枝 + Top-K 邻居上限：先丢弱边，再对超宽节点只保留最强邻居。
        # 上限只作用于常驻图，不改变数据库中的 Tag 事实。
        bound = self.max_neighbors_per_tag
        for src in list(new_forward.keys()):
            neighbors = {tgt: w for tgt, w in new_forward[src].items() if w >= 0.01}
            if len(neighbors) > bound:
                neighbors = dict(
                    sorted(neighbors.items(), key=lambda item: (-item[1], item[0]))[:bound]
                )
            if neighbors:
                new_forward[src] = neighbors
            else:
                del new_forward[src]

        # 原子切换（backward 由裁剪后的 forward 推导）
        self.publish(new_forward, self._current_tag_count(bool(has_scoped)))

        logger.info(
            f"[WaveMemory] DirectedCooccurrence rebuilt: "
            f"{len(self.forward)} nodes, {sum(len(v) for v in self.forward.values())} directed edges"
        )

    def _current_tag_count(self, has_scoped: bool) -> int:
        if has_scoped:
            count_row = self.db.conn.execute("SELECT COUNT(*) FROM scoped_tags").fetchone()
            return int(count_row[0]) if count_row else 0
        return int(self.db.get_tag_count())

    @staticmethod
    def backward_from_forward(forward: dict[int, dict[int, float]], bound: int) -> dict[int, dict[int, float]]:
        """backward 必须与裁剪后的 forward 保持一致，否则反向锚定会读到已被丢弃的边，
        并让常驻内存重新按未裁剪的宽度增长。"""
        backward: dict[int, dict[int, float]] = {}
        for src, neighbors in forward.items():
            for tgt, weight in neighbors.items():
                backward.setdefault(tgt, {})[src] = weight
        for tgt in list(backward.keys()):
            inbound = backward[tgt]
            if len(inbound) > bound:
                backward[tgt] = dict(sorted(inbound.items(), key=lambda item: (-item[1], item[0]))[:bound])
        return backward

    def publish(
        self,
        forward: dict[int, dict[int, float]],
        tag_count: int,
        *,
        wormholes=None,
        anchor_gain: dict[int, float] | None = None,
    ) -> None:
        """换上一份已裁剪的正向图（重建、子进程构建、落盘加载共用）；虫洞与锚增益随图同代替换。"""
        self.forward = forward
        self.backward = self.backward_from_forward(forward, self.max_neighbors_per_tag)
        self._tag_count = int(tag_count)
        self.wormhole_edges = frozenset(wormholes or ())
        self.anchor_gain = dict(anchor_gain or {})

    def adopt(self, other: "DirectedCooccurrence") -> None:
        """接管另一份构建好的图（调度器发布替换图时用），各字段一次换齐。"""
        self.forward = other.forward
        self.backward = other.backward
        self._tag_count = other._tag_count
        self.wormhole_edges = getattr(other, "wormhole_edges", frozenset())
        self.anchor_gain = getattr(other, "anchor_gain", {})

    # ─── row_budget 构建（TagMemo V9.1）───

    def _rebuild_row_budget(self, rows: list[tuple], has_scoped: bool) -> None:
        params = self.kernel_params
        # 同一记忆内标签对的原始证据与支持度（出现在几条记忆里）。权重对称，按无序对存一份。
        pair_weight: dict[tuple[int, int], float] = {}
        pair_support: dict[tuple[int, int], int] = {}
        for memory_key, group in groupby(rows, key=lambda r: (r[0], r[1], r[2], r[3])):
            del memory_key
            tags = [(r[4], r[5]) for r in group]
            if len(tags) < 2:
                continue
            max_pos = max(p for _, p in tags) or 1
            seen: set[tuple[int, int]] = set()
            for i in range(len(tags)):
                a, pa = tags[i]
                phi_a = ordinal_potential(pa, max_pos)
                for j in range(i + 1, len(tags)):
                    b, pb = tags[j]
                    if a == b:
                        continue
                    key = (a, b) if a < b else (b, a)
                    weight = phi_a * ordinal_potential(pb, max_pos)
                    if self.pair_sim_service:
                        weight *= bell_gain(self.pair_sim_service.get_similarity(a, b), self.semantic_gain_config)
                    pair_weight[key] = pair_weight.get(key, 0.0) + weight
                    if key not in seen:
                        seen.add(key)
                        pair_support[key] = pair_support.get(key, 0) + 1

        min_support = max(1, int(params.min_support))
        raw: dict[int, dict[int, float]] = defaultdict(dict)
        for key, weight in pair_weight.items():
            if pair_support.get(key, 0) < min_support:
                continue
            a, b = key
            raw[a][b] = weight
            raw[b][a] = weight
        del pair_weight, pair_support

        evidence = compress_evidence(raw, params)
        vectors = self._load_tag_vectors(list(evidence), has_scoped)
        _, gains = compute_anchor_gains(evidence, vectors, params)
        kernel, wormholes = build_kernel(evidence, gains, params, self.max_neighbors_per_tag)
        self.publish(kernel, self._current_tag_count(has_scoped), wormholes=wormholes, anchor_gain=gains)
        logger.info(
            "[WaveMemory] DirectedCooccurrence(row_budget) rebuilt: %s nodes, %s edges, %s wormholes, "
            "anchor gains %s/%s (min_support=%s)",
            len(kernel), sum(len(v) for v in kernel.values()), len(wormholes), len(gains), len(evidence), min_support,
        )

    def _load_tag_vectors(self, tag_ids: list[int], has_scoped: bool) -> dict[int, "np.ndarray"]:
        """scoped 标签取其目录项向量；旧版库取 tags.vector。缺向量的标签不参与残差（锚增益按 1）。"""
        import numpy as np

        sql = (
            "SELECT s.id, t.embedding FROM scoped_tags s JOIN tag_catalog t ON t.id = s.catalog_id "
            "WHERE s.id IN ({}) AND t.embedding IS NOT NULL"
            if has_scoped
            else "SELECT id, vector FROM tags WHERE id IN ({}) AND vector IS NOT NULL"
        )
        vectors: dict[int, np.ndarray] = {}
        try:
            for start in range(0, len(tag_ids), 900):
                batch = tag_ids[start:start + 900]
                for tag_id, blob in self.db.conn.execute(sql.format(",".join("?" * len(batch))), batch).fetchall():
                    if blob:
                        vectors[int(tag_id)] = np.frombuffer(bytes(blob), dtype=np.float32)
        except Exception as error:
            logger.warning(f"[WaveMemory] 共现图读取标签向量失败，锚增益全部按 1: {error!r}")
            return {}
        return vectors

    # ─── 落盘 ───

    def save_snapshot(self, path: str, *, built_at: float | None = None) -> None:
        """原子写入常驻图，重启后直接加载，不必再全量重建。"""
        payload = {
            "version": SNAPSHOT_VERSION,
            "built_at": float(built_at if built_at is not None else time.time()),
            "tag_count": int(self._tag_count),
            "max_neighbors_per_tag": int(self.max_neighbors_per_tag),
            "kernel_version": self.kernel_version,
            "wormholes": [[int(src), int(tgt)] for src, tgt in sorted(self.wormhole_edges)],
            "anchor_gain": {str(tag): float(gain) for tag, gain in self.anchor_gain.items()},
            "forward": {
                str(src): {str(tgt): float(weight) for tgt, weight in neighbors.items()}
                for src, neighbors in self.forward.items()
            },
        }
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    def load_snapshot(self, path: str) -> float | None:
        """加载落盘的常驻图，返回其构建时间；文件缺失、损坏或为空时返回 None 且不改动当前图。"""
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if int(payload.get("version", 0)) != SNAPSHOT_VERSION:
                return None
            # 换了传播核（global_max ↔ row_budget）时旧快照不能用，重建一次；没写内核的旧快照是 global_max
            if normalize_kernel(payload.get("kernel_version")) != self.kernel_version:
                return None
            bound = self.max_neighbors_per_tag
            forward: dict[int, dict[int, float]] = {}
            for src, neighbors in dict(payload.get("forward") or {}).items():
                edges = {int(tgt): float(weight) for tgt, weight in dict(neighbors).items()}
                if len(edges) > bound:  # 上限调小后按新上限裁剪
                    edges = dict(sorted(edges.items(), key=lambda item: (-item[1], item[0]))[:bound])
                if edges:
                    forward[int(src)] = edges
            if not forward:
                return None
            wormholes = {
                (int(src), int(tgt)) for src, tgt in payload.get("wormholes") or []
                if int(src) in forward and int(tgt) in forward[int(src)]
            }
            gains = {int(tag): float(gain) for tag, gain in dict(payload.get("anchor_gain") or {}).items()}
            self.publish(forward, int(payload.get("tag_count", 0)), wormholes=wormholes, anchor_gain=gains)
            return float(payload.get("built_at", 0.0))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            if not isinstance(error, FileNotFoundError):
                logger.warning(f"[WaveMemory] 共现图快照无法加载，将重建: {error!r}")
            return None

    def get_neighbors(self, tag_id: int, max_neighbors: int = 20) -> list[tuple[int, float]]:
        """获取某个 Tag 的有向出边邻居，按权重降序。"""
        neighbors = self.forward.get(tag_id, {})
        sorted_n = sorted(neighbors.items(), key=lambda x: x[1], reverse=True)
        return sorted_n[:max_neighbors]

    def get_incoming(self, tag_id: int, max_neighbors: int = 20) -> list[tuple[int, float]]:
        """获取指向该 Tag 的入边邻居。"""
        neighbors = self.backward.get(tag_id, {})
        sorted_n = sorted(neighbors.items(), key=lambda x: x[1], reverse=True)
        return sorted_n[:max_neighbors]

    # ─── 社区检测 ───

    def detect_communities(self, min_community_size: int = 3) -> dict[int, list[int]]:
        adj: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for src, neighbors in self.forward.items():
            for tgt, w in neighbors.items():
                adj[src][tgt] += w
                adj[tgt][src] += w

        if not adj:
            return {}

        labels: dict[int, int] = {node: node for node in adj}
        nodes = list(adj.keys())

        import random
        for _ in range(20):
            random.shuffle(nodes)
            changed = False
            for node in nodes:
                if not adj[node]:
                    continue
                votes: dict[int, float] = defaultdict(float)
                for neighbor, weight in adj[node].items():
                    votes[labels[neighbor]] += weight
                if votes:
                    best_label = max(votes, key=lambda k: votes[k])
                    if labels[node] != best_label:
                        labels[node] = best_label
                        changed = True
            if not changed:
                break

        communities: dict[int, list[int]] = defaultdict(list)
        for node, label in labels.items():
            communities[label].append(node)

        result: dict[int, list[int]] = {}
        for i, (_, members) in enumerate(
            sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)
        ):
            if len(members) < min_community_size:
                continue
            result[i] = members

        return result

    def get_galaxy_data(self, max_nodes: int = 300, max_edges: int = 800) -> dict:
        """生成全局星图数据。"""
        communities = self.detect_communities(min_community_size=5)
        if not communities:
            return {"nodes": [], "edges": [], "communities": []}

        degree: dict[int, int] = defaultdict(int)
        for src, neighbors in self.forward.items():
            degree[src] += len(neighbors)
        for tgt, neighbors in self.backward.items():
            degree[tgt] += len(neighbors)

        selected_nodes: set[int] = set()
        community_meta: list[dict] = []
        nodes_per_community = max(3, max_nodes // max(len(communities), 1))

        for cid, members in communities.items():
            sorted_members = sorted(members, key=lambda n: degree.get(n, 0), reverse=True)
            top_members = sorted_members[:nodes_per_community]
            selected_nodes.update(top_members)
            community_meta.append({"id": cid, "size": len(members), "top_nodes": top_members[:3]})
            if len(selected_nodes) >= max_nodes:
                break

        edges: list[dict] = []
        for src in selected_nodes:
            if src not in self.forward:
                continue
            for tgt, weight in self.forward[src].items():
                if tgt in selected_nodes and weight >= 0.05:
                    edges.append({"source": src, "target": tgt, "weight": round(weight, 3)})
                    if len(edges) >= max_edges:
                        break
            if len(edges) >= max_edges:
                break

        node_info: dict[int, dict] = {}
        if selected_nodes:
            placeholders = ",".join("?" * len(selected_nodes))
            has_scoped = self.db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_tags'"
            ).fetchone()
            tag_table = "scoped_tags" if has_scoped else "tags"
            rows = self.db.conn.execute(
                f"SELECT id, name, tag_type FROM {tag_table} WHERE id IN ({placeholders})",
                list(selected_nodes),
            ).fetchall()
            for r in rows:
                node_info[r[0]] = {"id": r[0], "name": r[1], "type": r[2], "degree": degree.get(r[0], 0)}

        node_community: dict[int, int] = {}
        for cid, members in communities.items():
            for m in members:
                if m in selected_nodes:
                    node_community[m] = cid

        nodes = []
        for nid in selected_nodes:
            if nid in node_info:
                info = node_info[nid]
                info["community"] = node_community.get(nid, -1)
                nodes.append(info)

        return {"nodes": nodes, "edges": edges, "communities": community_meta}

    @property
    def node_count(self) -> int:
        return len(self.forward)

    @property
    def edge_count(self) -> int:
        return sum(len(v) for v in self.forward.values())

    def needs_rebuild(self, threshold_pct: float = 0.05) -> bool:
        """判断是否需要重建（阈值改为 0.05）。"""
        has_scoped = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_tags'"
        ).fetchone()
        if has_scoped:
            row = self.db.conn.execute("SELECT COUNT(*) FROM scoped_tags").fetchone()
            current_count = int(row[0]) if row else 0
        else:
            current_count = self.db.get_tag_count()
        if self._tag_count == 0:
            return current_count > 10
        change = abs(current_count - self._tag_count) / self._tag_count
        return change >= threshold_pct


class CooccurrenceScheduler:
    """合并已提交 Tag 变更，并在共享屏障内异步重建共现矩阵。

    达到阈值后会始终保留一个延迟任务：即使冷却期内不再有事件，冷却期
    结束时仍会执行重建。所有自动和强制重建都经过同一 ``rebuild_lock``，
    使维护任务可以复用这一屏障而不会并行交换矩阵。
    """

    def __init__(
        self,
        cooccurrence: DirectedCooccurrence,
        threshold_pct: float = DEFAULT_REBUILD_THRESHOLD_PCT,
        cooldown_sec: float = DEFAULT_REBUILD_COOLDOWN_SEC,
        on_rebuild_complete=None,
        rebuild_lock: asyncio.Lock | None = None,
        *,
        snapshot_path: str | None = None,
        build_in_subprocess: bool = False,
    ):
        self.cooccurrence = cooccurrence
        # 重建完成后落盘，重启时加载，不必再全量重建
        self.snapshot_path = snapshot_path
        # 全量构建放到子进程，不占插件进程的 GIL（不满足条件时退回线程）
        self.build_in_subprocess = bool(build_in_subprocess)
        self.threshold_pct = max(float(threshold_pct), 0.0)
        self.cooldown_sec = max(float(cooldown_sec), 0.0)
        self.on_rebuild_complete = on_rebuild_complete
        self._rebuild_lock = rebuild_lock or asyncio.Lock()
        self._accumulated_changes = 0
        self._change_generation = 0
        self._last_rebuild_ts: float = 0
        self._is_rebuilding = False
        self._scheduled_task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._force_requested = False
        self._force_waiters: list[asyncio.Future] = []
        self._pending_reasons: dict[str, int] = {}
        self._metrics: dict[str, object] = {
            "notifications_total": 0,
            "rebuild_started_total": 0,
            "rebuild_completed_total": 0,
            "rebuild_failed_total": 0,
            "force_requested_total": 0,
            "pending_changes": 0,
            "pending_reasons": {},
            "last_rebuild": None,
        }
        # Projection construction happens after scheduler construction in the
        # existing plugin.  This compatibility attachment lets the projection
        # discover and bind the already configured scheduler without main.py.
        try:
            setattr(cooccurrence, "_cooccurrence_scheduler", self)
        except Exception:
            pass

    def load_snapshot(self) -> float | None:
        """启动时加载落盘的常驻图；成功时把它当作刚完成的重建（冷却期从现在算起）。"""
        if not self.snapshot_path:
            return None
        built_at = self.cooccurrence.load_snapshot(self.snapshot_path)
        if built_at is None:
            return None
        self._last_rebuild_ts = time.time()
        self._metrics["last_rebuild"] = {"status": "loaded_snapshot", "built_at": built_at}
        logger.info(
            "[WaveMemory] 共现图从快照加载: %s 个节点、%s 条边（构建于 %s）",
            self.cooccurrence.node_count,
            self.cooccurrence.edge_count,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(built_at)),
        )
        return built_at

    def _subprocess_request(self) -> dict | None:
        """能交给子进程构建时返回请求体：需要真实数据库文件，语义增益须来自 tag_pair_similarity 表。"""
        if not self.build_in_subprocess:
            return None
        live = self.cooccurrence
        db_path = getattr(getattr(live, "db", None), "db_path", None)
        if not isinstance(db_path, str) or db_path == ":memory:" or not os.path.isfile(db_path):
            return None
        pair_sim = getattr(live, "pair_sim_service", None)
        if pair_sim is not None and not getattr(pair_sim, "table_backed", False):
            return None
        gain = getattr(live, "semantic_gain_config", None)
        kernel_params = getattr(live, "kernel_params", None)
        return {
            "db_path": db_path,
            "kernel_version": str(getattr(live, "kernel_version", "global_max")),
            "kernel_params": kernel_params.to_dict() if hasattr(kernel_params, "to_dict") else {},
            "max_neighbors_per_tag": int(getattr(live, "max_neighbors_per_tag", DEFAULT_MAX_NEIGHBORS_PER_TAG)),
            "residual_map": [[int(k), float(v)] for k, v in dict(getattr(live, "residual_map", None) or {}).items()],
            "semantic_gain": asdict(gain) if is_dataclass(gain) else {},
            "pair_similarity": pair_sim is not None,
        }

    async def _build_in_subprocess(self, request: dict, new_matrix) -> None:
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cooccurrence_worker.py")
        process = await asyncio.create_subprocess_exec(
            sys.executable, worker,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode("utf-8")), timeout=SUBPROCESS_TIMEOUT_SEC
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise RuntimeError(f"cooccurrence worker exit={process.returncode}: {' | '.join(tail)}")
        result = json.loads(stdout.decode("utf-8"))
        forward = {
            int(src): {int(tgt): float(weight) for tgt, weight in neighbors}
            for src, neighbors in result.get("forward") or []
        }
        new_matrix.publish(
            forward,
            int(result.get("tag_count") or 0),
            wormholes={(int(src), int(tgt)) for src, tgt in result.get("wormholes") or []},
            anchor_gain={int(tag): float(gain) for tag, gain in result.get("anchor_gain") or []},
        )
        logger.info(
            "[WaveMemory] 共现图在子进程构建完成（%s）: %s 个节点、%s 条边、%s 条虫洞，%.1fs",
            getattr(new_matrix, "kernel_version", "global_max"),
            new_matrix.node_count, new_matrix.edge_count, len(getattr(new_matrix, "wormhole_edges", ())),
            float(result.get("elapsed_sec") or 0.0),
        )

    def _kernel_kwargs(self) -> dict:
        """替换图沿用常驻图的传播核配置（测试替身没有这些字段时不传）。"""
        kwargs = {}
        if hasattr(self.cooccurrence, "kernel_version"):
            kwargs["kernel_version"] = self.cooccurrence.kernel_version
        if hasattr(self.cooccurrence, "kernel_params"):
            kwargs["kernel_params"] = self.cooccurrence.kernel_params
        return kwargs

    async def _build_replacement(self, new_matrix) -> None:
        request = self._subprocess_request()
        if request is not None:
            try:
                await self._build_in_subprocess(request, new_matrix)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[WaveMemory] 共现图子进程构建失败，改在线程里构建: {error!r}")
        await asyncio.to_thread(new_matrix.rebuild)

    async def _save_snapshot(self) -> None:
        if not self.snapshot_path:
            return
        try:
            await asyncio.to_thread(self.cooccurrence.save_snapshot, self.snapshot_path)
        except Exception as error:
            logger.warning(f"[WaveMemory] 共现图快照写入失败（不影响使用，下次启动会重建）: {error!r}")

    def set_rebuild_lock(self, rebuild_lock: asyncio.Lock) -> None:
        """Bind the projection/maintenance barrier before work is scheduled."""
        if self._scheduled_task is not None and not self._scheduled_task.done():
            raise RuntimeError("cannot replace cooccurrence rebuild lock while scheduled")
        self._rebuild_lock = rebuild_lock

    def metrics_snapshot(self) -> dict[str, object]:
        """Return a copy suitable for structured diagnostics and tests."""
        snapshot = dict(self._metrics)
        snapshot["pending_changes"] = self._accumulated_changes
        snapshot["pending_reasons"] = dict(self._pending_reasons)
        last_rebuild = snapshot.get("last_rebuild")
        if isinstance(last_rebuild, dict):
            snapshot["last_rebuild"] = dict(last_rebuild)
        return snapshot

    def notify_tag_change(self, count: int = 1, *, reason: str = "tag_change") -> None:
        """Mark the projection dirty and start/retain one cooldown driver task."""
        normalized_count = max(int(count), 0)
        if normalized_count <= 0:
            return
        normalized_reason = str(reason or "tag_change")
        self._accumulated_changes += normalized_count
        self._change_generation += normalized_count
        self._pending_reasons[normalized_reason] = (
            self._pending_reasons.get(normalized_reason, 0) + normalized_count
        )
        self._metrics["notifications_total"] = int(self._metrics["notifications_total"]) + normalized_count
        self._metrics["pending_changes"] = self._accumulated_changes
        self._metrics["pending_reasons"] = dict(self._pending_reasons)
        if self._threshold_reached():
            self._ensure_driver()
            self._wake_event.set()

    async def force_rebuild(self, *, reason: str = "force") -> dict[str, object]:
        """Run one rebuild through the same barrier, bypassing cooldown safely."""
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._force_waiters.append(waiter)
        self._force_requested = True
        normalized_reason = str(reason or "force")
        self._pending_reasons[normalized_reason] = self._pending_reasons.get(normalized_reason, 0) + 1
        self._metrics["force_requested_total"] = int(self._metrics["force_requested_total"]) + 1
        self._metrics["pending_reasons"] = dict(self._pending_reasons)
        self._ensure_driver()
        self._wake_event.set()
        await asyncio.shield(waiter)
        return self.metrics_snapshot()

    def _threshold_reached(self) -> bool:
        if self._accumulated_changes <= 0:
            return False
        total = self.cooccurrence.node_count or 1
        return self._accumulated_changes / total >= self.threshold_pct

    def _ensure_driver(self) -> None:
        """Create exactly one driver task; it may sleep, then rebuild serially."""
        if self._scheduled_task is not None and not self._scheduled_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._scheduled_task = loop.create_task(self._run_rebuild_driver())

    def _schedule_rebuild(self) -> None:
        """Backward-compatible alias for older callers/tests."""
        self._ensure_driver()

    async def _run_rebuild_driver(self) -> None:
        try:
            while self._force_requested or self._threshold_reached():
                if not self._force_requested:
                    delay = self.cooldown_sec - (time.time() - self._last_rebuild_ts)
                    if delay > 0:
                        self._wake_event.clear()
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                        continue
                rebuilt = await self._do_rebuild()
                if not rebuilt:
                    return
        finally:
            # A notification can land after the loop condition observes a clean
            # state but before this task relinquishes ownership. Re-check after
            # clearing the handle so that burst is never stranded without a
            # future event to wake it.
            self._scheduled_task = None
            if self._force_requested or self._threshold_reached():
                self._ensure_driver()

    async def _do_rebuild(self) -> bool:
        """Build off-loop, atomically publish the replacement, and retain new deltas."""
        snapshot_reasons: dict[str, int] = {}
        force_waiters: list[asyncio.Future] = []
        generation = 0
        forced = False
        try:
            async with self._rebuild_lock:
                # 防循环保护：如果无变更且非强制请求，跳过无意义的全量重建
                if not self._force_requested and self._accumulated_changes == 0 and self._last_rebuild_ts > 0:
                    elapsed = time.time() - self._last_rebuild_ts
                    if elapsed < self.cooldown_sec:
                        logger.debug(
                            "[WaveMemory] CooccurrenceScheduler: skipping redundant rebuild (no changes, %.0fs since last)",
                            elapsed,
                        )
                        return True
                generation = self._change_generation
                snapshot_reasons = self._pending_reasons
                self._pending_reasons = {}
                force_waiters = self._force_waiters
                self._force_waiters = []
                forced = self._force_requested
                self._force_requested = False
                self._is_rebuilding = True
                reason_names = sorted(snapshot_reasons) or (["force"] if forced else ["threshold"])
                self._metrics["rebuild_started_total"] = int(self._metrics["rebuild_started_total"]) + 1
                self._metrics["last_rebuild"] = {
                    "status": "running",
                    "reasons": reason_names,
                    "generation": generation,
                    "started_at": time.time(),
                }
                logger.info(
                    "[WaveMemory] CooccurrenceScheduler: starting rebuild generation=%s pending_changes=%s reasons=%s",
                    generation,
                    self._accumulated_changes,
                    reason_names,
                )
                new_matrix = DirectedCooccurrence(
                    self.cooccurrence.db,
                    pair_sim_service=self.cooccurrence.pair_sim_service,
                    residual_map=self.cooccurrence.residual_map,
                    semantic_gain_config=self.cooccurrence.semantic_gain_config,
                    # The replacement must keep the live resident bound; otherwise a
                    # rebuild would silently republish an unbounded neighbour graph.
                    max_neighbors_per_tag=getattr(
                        self.cooccurrence,
                        "max_neighbors_per_tag",
                        DEFAULT_MAX_NEIGHBORS_PER_TAG,
                    ),
                    **self._kernel_kwargs(),
                )
                await self._build_replacement(new_matrix)
                # Publish only a fully rebuilt matrix; readers never observe its
                # partially constructed local dictionaries.
                if hasattr(self.cooccurrence, "adopt"):
                    self.cooccurrence.adopt(new_matrix)
                else:  # 测试替身
                    self.cooccurrence.forward = new_matrix.forward
                    self.cooccurrence.backward = new_matrix.backward
                    self.cooccurrence._tag_count = new_matrix._tag_count
                self._accumulated_changes = max(0, self._change_generation - generation)
                self._last_rebuild_ts = time.time()
                self._metrics["rebuild_completed_total"] = int(self._metrics["rebuild_completed_total"]) + 1
                self._metrics["pending_changes"] = self._accumulated_changes
                self._metrics["pending_reasons"] = dict(self._pending_reasons)
                self._metrics["last_rebuild"] = {
                    "status": "completed",
                    "reasons": reason_names,
                    "generation": generation,
                    "completed_at": self._last_rebuild_ts,
                    "remaining_changes": self._accumulated_changes,
                }
                logger.info(
                    "[WaveMemory] CooccurrenceScheduler: rebuild complete generation=%s remaining_changes=%s reasons=%s",
                    generation,
                    self._accumulated_changes,
                    reason_names,
                )
                gc.collect()

            await self._save_snapshot()
            if self.on_rebuild_complete:
                try:
                    result = self.on_rebuild_complete()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.warning(
                        "[WaveMemory] CooccurrenceScheduler rebuild completion callback failed",
                        exc_info=True,
                    )
            for waiter in force_waiters:
                if not waiter.done():
                    waiter.set_result(None)
            return True
        except Exception as exc:
            self._pending_reasons = {
                **snapshot_reasons,
                **{
                    key: self._pending_reasons.get(key, 0) + value
                    for key, value in snapshot_reasons.items()
                },
            }
            self._metrics["rebuild_failed_total"] = int(self._metrics["rebuild_failed_total"]) + 1
            self._metrics["pending_reasons"] = dict(self._pending_reasons)
            self._metrics["last_rebuild"] = {
                "status": "failed",
                "reasons": sorted(snapshot_reasons),
                "generation": generation,
                "failed_at": time.time(),
                "error_type": type(exc).__name__,
            }
            logger.error(
                "[WaveMemory] CooccurrenceScheduler rebuild error type=%s error=%r",
                type(exc).__name__,
                exc,
            )
            for waiter in force_waiters:
                if not waiter.done():
                    waiter.set_exception(exc)
            return False
        finally:
            self._is_rebuilding = False
