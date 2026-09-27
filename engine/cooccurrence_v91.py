"""共现传播核 V9.1（移植自 VCPToolBox TagMemo V9.1，参数默认值取自其 rag_params.json）。

旧构建把全库边权按最大值归一化再砍掉 < 0.01 的边：线上 20 万个标签只剩 384 个节点，
查询种子几乎都不在图里，脉冲传播与测地重排实际不起作用。V9.1 的做法：

1. 证据压缩  C = log(1 + λ·W)；
2. 锚增益    g(r) = clip(g0 + k·r^γ, gmin, gmax)，r 为内生残差（标签相对邻域基底不可解释的比例）；
3. 虫洞      C·g(目标) ≥ tension 的边导通 × wormhole_gain；
4. 入流枢纽校正：按目标全图入流相对中位数施加 (相对入流)^-β，夹在 [floor, ceiling]；
5. 固定出流预算：每行总质量 m_out，其中 reserve 专给虫洞边。

本模块只做纯计算，数据读取与发布在 DirectedCooccurrence / 子进程里。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class V91Params:
    # 传播核
    outbound_mass: float = 0.95
    association_reserve_mass: float = 0.05
    evidence_compression: float = 1.0
    wormhole_gain: float = 1.35
    tension_threshold: float = 1.0
    hub_penalty_exponent: float = 0.3
    hub_penalty_floor: float = 0.55
    hub_penalty_ceiling: float = 1.8
    hub_smoothing_ratio: float = 0.1
    # 只保留至少在这么多条记忆里共现过的标签对（线上 85% 的标签对只共现 1 次，多为长尾噪声）
    min_support: int = 2
    # 内生残差 → 锚增益
    residual_max_neighbors: int = 48
    residual_max_basis: int = 4
    residual_min_neighbors: int = 3
    anchor_base: float = 0.75
    anchor_scale: float = 1.25
    anchor_gamma: float = 1.0
    anchor_min: float = 0.5
    anchor_max: float = 2.0
    # 传播
    return_flow_factor: float = 0.15
    fir_gamma: float = 0.6
    max_propagation_states: int = 2000

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, values: dict | None) -> "V91Params":
        params = cls()
        for key, value in dict(values or {}).items():
            if hasattr(params, key):
                setattr(params, key, type(getattr(params, key))(value))
        return params


def anchor_gain(residual: float, params: V91Params) -> float:
    value = params.anchor_base + params.anchor_scale * max(0.0, min(1.0, float(residual))) ** params.anchor_gamma
    return max(params.anchor_min, min(params.anchor_max, value))


def intrinsic_residual(tag_vec: np.ndarray, neighbor_vecs: list[np.ndarray], weights: list[float], max_basis: int) -> float:
    """单位化标签向量减去它在（按共现证据加权的）邻域主成分基底上的投影后剩余的长度，∈[0, 1]。"""
    norm = float(np.linalg.norm(tag_vec))
    if norm <= 1e-10:
        return 1.0
    unit = tag_vec / norm
    rows = []
    for vec, weight in zip(neighbor_vecs, weights):
        n = float(np.linalg.norm(vec))
        if n > 1e-10 and weight > 0:
            rows.append(vec / n * weight)
    if not rows:
        return 1.0
    matrix = np.vstack(rows)
    rank = min(max_basis, matrix.shape[0], matrix.shape[1])
    _, _, vt = np.linalg.svd(matrix, full_matrices=False)
    basis = vt[:rank]
    residual = unit - basis.T @ (basis @ unit)
    return float(min(1.0, max(0.0, np.linalg.norm(residual))))


def compute_anchor_gains(
    evidence: dict[int, dict[int, float]],
    vectors: dict[int, np.ndarray],
    params: V91Params,
) -> tuple[dict[int, float], dict[int, float]]:
    """按压缩后的共现证据取每个节点最强的邻居作为邻域，返回 (残差, 锚增益)。邻居不足或缺向量的节点不给值（按 1 处理）。"""
    residuals: dict[int, float] = {}
    gains: dict[int, float] = {}
    for tag_id, edges in evidence.items():
        vec = vectors.get(tag_id)
        if vec is None:
            continue
        ranked = sorted(edges.items(), key=lambda item: (-item[1], item[0]))
        neighbors = [(nid, w) for nid, w in ranked if nid in vectors][: params.residual_max_neighbors]
        if len(neighbors) < params.residual_min_neighbors:
            continue
        r = intrinsic_residual(vec, [vectors[nid] for nid, _ in neighbors], [w for _, w in neighbors], params.residual_max_basis)
        residuals[tag_id] = r
        gains[tag_id] = anchor_gain(r, params)
    return residuals, gains


def compress_evidence(raw: dict[int, dict[int, float]], params: V91Params) -> dict[int, dict[int, float]]:
    lam = max(0.01, params.evidence_compression)
    compressed: dict[int, dict[int, float]] = {}
    for src, edges in raw.items():
        row = {tgt: math.log1p(max(0.0, w) * lam) for tgt, w in edges.items() if w > 0}
        if row:
            compressed[src] = row
    return compressed


def build_kernel(
    evidence: dict[int, dict[int, float]],
    gains: dict[int, float],
    params: V91Params,
    max_neighbors: int,
) -> tuple[dict[int, dict[int, float]], set[tuple[int, int]]]:
    """由压缩证据构建固定出流预算的传播核，返回 (kernel, 虫洞边)。每行最多保留 max_neighbors 条边。"""
    m_out = max(0.01, min(1.0, params.outbound_mass))
    reserve_mass = min(max(0.0, params.association_reserve_mass), m_out)

    raw_rows: dict[int, list[tuple[int, float, bool]]] = {}
    inflow: dict[int, float] = {}
    for src, edges in evidence.items():
        row = []
        for tgt, c in edges.items():
            is_wormhole = c * gains.get(tgt, 1.0) >= params.tension_threshold
            conductance = c * (params.wormhole_gain if is_wormhole else 1.0)
            if conductance <= 0 or not math.isfinite(conductance):
                continue
            row.append((tgt, conductance, is_wormhole))
            inflow[tgt] = inflow.get(tgt, 0.0) + conductance
        if row:
            raw_rows[src] = row

    positive = sorted(v for v in inflow.values() if v > 0 and math.isfinite(v))
    median = positive[len(positive) // 2] if positive else 1.0
    smoothing = max(1e-9, median * params.hub_smoothing_ratio)

    kernel: dict[int, dict[int, float]] = {}
    wormholes: set[tuple[int, int]] = set()
    for src, row in raw_rows.items():
        adjusted = []
        for tgt, conductance, is_wormhole in row:
            relative = inflow.get(tgt, 0.0) / (median + smoothing)
            penalty = relative ** -params.hub_penalty_exponent if params.hub_penalty_exponent > 0 else 1.0
            penalty = max(params.hub_penalty_floor, min(params.hub_penalty_ceiling, penalty))
            adjusted.append((tgt, conductance * penalty, is_wormhole))
        # 先按调整后导通裁到 max_neighbors，再归一化，使保留下来的边正好分完行预算
        adjusted.sort(key=lambda item: (-item[1], item[0]))
        adjusted = adjusted[:max_neighbors]
        total = sum(item[1] for item in adjusted)
        if total <= 0:
            continue
        wormhole_total = sum(item[1] for item in adjusted if item[2])
        reserve = reserve_mass if wormhole_total > 0 else 0.0
        main_mass = m_out - reserve
        edges = {}
        for tgt, conductance, is_wormhole in adjusted:
            weight = main_mass * conductance / total
            if is_wormhole and wormhole_total > 0:
                weight += reserve * conductance / wormhole_total
                wormholes.add((src, tgt))
            edges[tgt] = weight
        kernel[src] = edges
    return kernel, wormholes


def fir_weights(gamma: float, max_hops: int) -> list[float]:
    """归一化几何有限脉冲响应 w_t = γ^t / Σγ^r。"""
    gamma = max(0.05, min(0.95, float(gamma)))
    raw = [gamma ** hop for hop in range(max_hops + 1)]
    total = sum(raw)
    return [value / total for value in raw]


__all__ = [
    "V91Params",
    "anchor_gain",
    "build_kernel",
    "compress_evidence",
    "compute_anchor_gains",
    "fir_weights",
    "intrinsic_residual",
]
