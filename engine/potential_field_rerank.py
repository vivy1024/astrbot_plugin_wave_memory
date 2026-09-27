"""势场重排（移植自 VCPToolBox TagMemo 的 geodesicRerank 读出层，参数默认值取自其 rag_params.json）。

旧测地重排直接用 (1-α)·向量分 + α·标签能量分 替换原分数：带高频人名、碎片标签的记忆会被大幅拉上来。
这里的做法：

1. 能量场可信度：正能量节点 < min_field_tags 或归一化熵 < min_field_entropy 时不重排；只保留占
   field_energy_mass_ratio 能量的前 max_field_nodes 个节点；
2. 枢纽特异性：1 - √(入流 / 最大入流)，下限 public_hub_floor —— 越常见的标签在重排里越轻；
3. 候选记忆按标签顺序逐个采样势能（场内精确值，或与场节点向量相似时插值），每个标签的质量 =
   闭合度（标签与记忆向量相似）× 锚增益 × 位置衰减 × 特异性，据此得到覆盖、连续、孤立与曲线分；
4. 证据分级封顶加分：直接证据（命中查询种子）≤ direct_bonus_cap，结构证据 ≤ structural_bonus_cap，
   主题晕轮 ≤ thematic_bonus_cap。最终分 = 向量分 + 加分，向量分始终占主导；
5. 全部候选证据都弱时整体不重排。

上游的几何辅助轨、身份锚（≤0.018 的补分）与稀疏跨段联想减免未移植。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class PotentialFieldParams:
    alpha: float = 0.6
    min_geo_samples: int = 4
    min_field_tags: int = 4
    min_field_entropy: float = 0.12
    max_field_nodes: int = 48
    field_energy_mass_ratio: float = 0.95
    field_similarity_threshold: float = 0.5
    weak_contact_threshold: float = 0.04
    strong_contact_threshold: float = 0.14
    max_field_neighbors: int = 4
    field_kernel_exponent: float = 2.0
    candidate_position_decay: float = 0.035
    public_hub_floor: float = 0.35
    min_closure_similarity: float = 0.2
    geo_reward_floor: float = 0.015
    geo_reward_saturation: float = 0.25
    direct_bonus_cap: float = 0.18
    structural_bonus_cap: float = 0.10
    thematic_bonus_cap: float = 0.035
    structural_continuity_min: float = 0.08
    thematic_min_potential: float = 0.08
    thematic_max_isolated_ratio: float = 0.65
    min_geo_coverage_ratio: float = 0.05
    min_max_geo_score: float = 0.01
    min_geo_score_spread: float = 0.03
    min_strong_evidence: float = 1.0


def _clamp01(value: float) -> float:
    return 0.0 if value <= 0 else 1.0 if value >= 1 else float(value)


def _unit(vec: np.ndarray | None) -> np.ndarray | None:
    if vec is None:
        return None
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 1e-10 else None


def hub_specificity_base(kernel: dict[int, dict[int, float]]) -> dict[int, float]:
    """按传播核的入流计算 1 - √(入流 / 最大入流)。"""
    inbound: dict[int, float] = {}
    for edges in kernel.values():
        for tgt, weight in edges.items():
            inbound[tgt] = inbound.get(tgt, 0.0) + max(0.0, float(weight))
    top = max(inbound.values(), default=0.0)
    if top <= 0:
        return {}
    return {tag: 1.0 - math.sqrt(value / top) for tag, value in inbound.items()}


def rerank(
    candidates: list[dict],
    energy_field: dict[int, float],
    *,
    seed_ids: frozenset | set,
    tag_chains: dict[int, list[tuple[int, int]]],
    tag_vectors: dict[int, np.ndarray],
    memory_vectors: dict[int, np.ndarray],
    specificity_base: dict[int, float],
    anchor_gain: dict[int, float],
    params: PotentialFieldParams,
) -> tuple[list[dict], dict[str, Any]]:
    """返回 (重排后的候选, 诊断)。不满足可信度条件时原样返回候选，诊断里写明原因。"""
    p = params
    field = {int(k): float(v) for k, v in energy_field.items() if float(v) > 0}
    if len(field) < p.min_field_tags:
        return candidates, {"applied": False, "reason": "field_too_small", "field_tags": len(field)}
    total = sum(field.values())
    top_energy = max(field.values())
    entropy = -sum((v / total) * math.log(v / total) for v in field.values())
    normalized_entropy = entropy / math.log(len(field)) if len(field) > 1 else 0.0
    if normalized_entropy < p.min_field_entropy:
        return candidates, {"applied": False, "reason": "field_entropy_low", "entropy": round(normalized_entropy, 4)}

    ranked_field = sorted(field.items(), key=lambda item: -item[1])
    retained: list[tuple[int, float]] = []
    mass = 0.0
    for tag_id, energy in ranked_field:
        if len(retained) >= p.max_field_nodes or (len(retained) >= p.min_field_tags and mass / total >= p.field_energy_mass_ratio):
            break
        retained.append((tag_id, energy))
        mass += energy
    field_nodes = [(tag_id, energy / top_energy, _unit(tag_vectors.get(tag_id))) for tag_id, energy in retained]
    field_nodes = [node for node in field_nodes if node[2] is not None]

    def specificity(tag_id: int) -> float:
        base = specificity_base.get(int(tag_id))
        return max(p.public_hub_floor, _clamp01(base)) if base is not None else 1.0

    potential_cache: dict[int, tuple[float, bool]] = {}

    def sample(tag_id: int, unit_vec: np.ndarray | None) -> tuple[float, bool]:
        if tag_id in potential_cache:
            return potential_cache[tag_id]
        exact = field.get(tag_id)
        exact_potential = _clamp01(exact / top_energy) if exact is not None else 0.0
        interpolated = 0.0
        if unit_vec is not None and field_nodes:
            contributions = []
            for node_id, normalized, node_vec in field_nodes:
                if node_id == tag_id:
                    continue
                similarity = float(unit_vec @ node_vec)
                if similarity < p.field_similarity_threshold:
                    continue
                local = (similarity - p.field_similarity_threshold) / max(1e-6, 1 - p.field_similarity_threshold)
                contributions.append(normalized * _clamp01(local) ** p.field_kernel_exponent * specificity(node_id))
            contributions.sort(reverse=True)
            chosen = contributions[: p.max_field_neighbors]
            if chosen:
                interpolated = sum(chosen) / math.sqrt(len(chosen))
        result = (_clamp01(max(exact_potential, interpolated)), exact is not None)
        potential_cache[tag_id] = result
        return result

    items = []
    for original_index, candidate in enumerate(candidates):
        memory_id = int(candidate["id"])
        chain = sorted(tag_chains.get(memory_id, ()), key=lambda item: item[1])
        memory_vec = _unit(memory_vectors.get(memory_id))
        item = {"candidate": candidate, "index": original_index, "geo": 0.0, "confidence": 0.0,
                "cls": "neutral", "eligible": False, "strong": 0, "exact": 0}
        if not chain or memory_vec is None:
            items.append(item)
            continue
        samples = []
        total_mass = contacted_mass = weighted_potential = 0.0
        closure_mass = 0.0
        strong = exact_hits = direct_exact = 0
        max_potential = 0.0
        for tag_id, position in chain:
            tag_vec = _unit(tag_vectors.get(tag_id))
            closure = 0.0
            if tag_vec is not None:
                closure = _clamp01((float(tag_vec @ memory_vec) - p.min_closure_similarity) / max(1e-6, 1 - p.min_closure_similarity))
            gain = max(0.5, min(2.0, float(anchor_gain.get(tag_id, 1.0))))
            positional = math.exp(-p.candidate_position_decay * max(0, position - 1))
            node_mass = max(0.02, closure * gain * positional * specificity(tag_id))
            potential, is_exact = sample(tag_id, tag_vec)
            contacted = potential >= p.weak_contact_threshold
            total_mass += node_mass
            closure_mass += closure * node_mass
            if contacted:
                contacted_mass += node_mass
                weighted_potential += node_mass * potential
            if potential >= p.strong_contact_threshold:
                strong += 1
            if is_exact:
                exact_hits += 1
                if tag_id in seed_ids:
                    direct_exact += 1
            max_potential = max(max_potential, potential)
            samples.append(contacted)
        if total_mass <= 0:
            items.append(item)
            continue
        contacted_count = sum(samples)
        coverage = contacted_mass / total_mass
        mean_potential = weighted_potential / contacted_mass if contacted_mass > 0 else 0.0
        adjacent = [samples[i] and samples[i + 1] for i in range(len(samples) - 1)]
        continuity = sum(adjacent) / len(adjacent) if adjacent else (1.0 if contacted_count else 0.0)
        isolated = sum(
            1 for i, hit in enumerate(samples)
            if hit and not ((i > 0 and samples[i - 1]) or (i + 1 < len(samples) and samples[i + 1]))
        )
        isolated_ratio = isolated / contacted_count if contacted_count and len(samples) > 1 else 0.0
        closure_quality = closure_mass / total_mass
        action_quality = _clamp01(max_potential * 0.5)
        effective = strong + exact_hits * 0.75
        target = max(1, min(p.min_geo_samples, math.ceil(math.sqrt(len(samples)))))
        evidence_confidence = _clamp01(effective / target)
        confidence = _clamp01(coverage * (0.55 + 0.45 * evidence_confidence) * (1 - 0.65 * isolated_ratio))
        geo = confidence * _clamp01(
            0.30 * mean_potential + 0.20 * max_potential + 0.20 * continuity + 0.15 * action_quality + 0.15 * closure_quality
        )
        if direct_exact > 0:
            cls = "direct"
        elif exact_hits > 0 or (strong >= 2 and continuity >= p.structural_continuity_min and isolated_ratio <= p.thematic_max_isolated_ratio):
            cls = "structural"
        else:
            cls = "thematic"
        eligible = cls != "thematic" or (max_potential >= p.thematic_min_potential and isolated_ratio <= p.thematic_max_isolated_ratio)
        item.update(geo=geo, confidence=confidence, cls=cls, eligible=eligible, strong=strong, exact=exact_hits)
        items.append(item)

    contributors = [item for item in items if item["geo"] > 0]
    if not contributors:
        return candidates, {"applied": False, "reason": "no_candidate_contact"}
    max_geo = max(item["geo"] for item in contributors)
    spread = max_geo - min(item["geo"] for item in contributors)
    coverage_ratio = len(contributors) / len(candidates)
    strong_evidence = sum(item["strong"] + item["exact"] * 0.75 for item in contributors)
    weak = max_geo < p.min_max_geo_score and strong_evidence < p.min_strong_evidence
    if weak and (coverage_ratio < p.min_geo_coverage_ratio or (len(contributors) > 1 and spread < p.min_geo_score_spread)):
        return candidates, {"applied": False, "reason": "candidate_evidence_low", "max_geo": round(max_geo, 4)}

    caps = {"direct": p.direct_bonus_cap, "structural": p.structural_bonus_cap, "thematic": p.thematic_bonus_cap}
    out = []
    for item in items:
        candidate = dict(item["candidate"])
        bonus = 0.0
        if item["eligible"] and item["geo"] > 0:
            strength = _clamp01((item["geo"] - p.geo_reward_floor) / (p.geo_reward_saturation - p.geo_reward_floor))
            bonus = min(caps.get(item["cls"], 0.0), max(0.0, p.alpha * item["confidence"] * strength))
        candidate["score"] = float(candidate.get("score", 0) or 0) + bonus
        candidate["geo_score"] = round(item["geo"], 4)
        candidate["geo_bonus"] = round(bonus, 4)
        candidate["geo_class"] = item["cls"]
        out.append((candidate, item["index"]))
    out.sort(key=lambda pair: (-pair[0]["score"], pair[1]))
    boosted = sum(1 for candidate, _ in out if candidate["geo_bonus"] > 0)
    return [candidate for candidate, _ in out], {
        "applied": True, "field_nodes": len(field_nodes), "entropy": round(normalized_entropy, 4),
        "boosted": boosted, "max_geo": round(max_geo, 4),
    }


__all__ = ["PotentialFieldParams", "hub_specificity_base", "rerank"]
