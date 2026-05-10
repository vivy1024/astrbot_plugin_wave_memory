"""Wave Memory 脉冲传播 — 模拟神经元联想，通过共现图发现间接关联"""

from __future__ import annotations

from collections import defaultdict

from .cooccurrence import CooccurrenceMatrix


class SpikeRouter:
    """脉冲传播引擎：从种子 Tag 出发，沿共现图扩散能量，发现间接关联。

    模拟大脑联想：想到 A → 自然想到和 A 经常一起出现的 B → 再想到 C。
    """

    def __init__(
        self,
        cooccurrence: CooccurrenceMatrix,
        max_hops: int = 4,
        base_momentum: float = 2.0,
        firing_threshold: float = 0.10,
        base_decay: float = 0.25,
        wormhole_decay: float = 0.70,
        tension_threshold: float = 1.0,
        max_emergent_nodes: int = 50,
        max_neighbors_per_node: int = 20,
    ):
        self.cooccurrence = cooccurrence
        self.max_hops = max_hops
        self.base_momentum = base_momentum
        self.firing_threshold = firing_threshold
        self.base_decay = base_decay
        self.wormhole_decay = wormhole_decay
        self.tension_threshold = tension_threshold
        self.max_emergent_nodes = max_emergent_nodes
        self.max_neighbors_per_node = max_neighbors_per_node

    def propagate(self, seed_tags: list[dict]) -> dict:
        """执行脉冲传播。

        Args:
            seed_tags: [{"tag_id": int, "weight": float}, ...]
                       种子节点及其初始能量

        Returns:
            {
                "activated_tags": [{"tag_id": int, "energy": float, "is_emergent": bool}, ...],
                "energy_field": {tag_id: accumulated_energy}  # 供测地线重排使用
            }
        """
        if not seed_tags or self.cooccurrence.node_count == 0:
            return {"activated_tags": seed_tags, "energy_field": {}}

        # 初始化能量场
        energy_field: dict[int, float] = {}
        seed_ids = set()

        # 注入种子能量
        active_nodes: list[dict] = []
        for seed in seed_tags:
            tid = seed["tag_id"]
            energy = seed["weight"]
            momentum = self.base_momentum
            energy_field[tid] = energy
            seed_ids.add(tid)
            active_nodes.append({
                "tag_id": tid,
                "energy": energy,
                "momentum": momentum,
            })

        # 迭代传播
        for hop in range(self.max_hops):
            next_active = []

            for node in active_nodes:
                if node["energy"] < self.firing_threshold:
                    continue
                if node["momentum"] <= 0:
                    continue

                # 获取邻居
                neighbors = self.cooccurrence.get_neighbors(
                    node["tag_id"], max_neighbors=self.max_neighbors_per_node
                )

                for neighbor_id, cooc_weight in neighbors:
                    # 计算张力
                    tension = cooc_weight * node["energy"]

                    # 决定衰减方式
                    if tension >= self.tension_threshold:
                        # 虫洞：高张力，低衰减，不扣动量
                        propagated_energy = node["energy"] * self.wormhole_decay * cooc_weight
                        new_momentum = node["momentum"]
                    else:
                        # 常规传播：高衰减，扣动量
                        propagated_energy = node["energy"] * self.base_decay * cooc_weight
                        new_momentum = node["momentum"] - 1.0

                    # 微电流过滤
                    if propagated_energy < 0.01:
                        continue

                    # 累积能量
                    energy_field[neighbor_id] = energy_field.get(neighbor_id, 0) + propagated_energy

                    # 加入下一轮活跃节点
                    if new_momentum > 0:
                        next_active.append({
                            "tag_id": neighbor_id,
                            "energy": propagated_energy,
                            "momentum": new_momentum,
                        })

            active_nodes = next_active
            if not active_nodes:
                break

        # 收集结果：种子 + 涌现节点
        activated = []

        # 种子节点保留
        for seed in seed_tags:
            activated.append({
                "tag_id": seed["tag_id"],
                "energy": energy_field.get(seed["tag_id"], seed["weight"]),
                "is_emergent": False,
            })

        # 涌现节点（非种子，按能量排序）
        emergent = []
        for tid, energy in energy_field.items():
            if tid not in seed_ids and energy >= self.firing_threshold:
                emergent.append({"tag_id": tid, "energy": energy, "is_emergent": True})

        emergent.sort(key=lambda x: x["energy"], reverse=True)
        activated.extend(emergent[: self.max_emergent_nodes])

        return {
            "activated_tags": activated,
            "energy_field": energy_field,
        }
