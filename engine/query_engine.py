"""Wave Memory 查询引擎 V2 — 移植 VCP TagMemo 浪潮算法的完整查询管线"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from astrbot.api import logger

from .database import WaveMemoryDB
from .vector_index import VectorIndex
from .embedding import EmbeddingService
from .cooccurrence import CooccurrenceMatrix
from .context_segmenter import ContextSegmenter
from .spike_routing import SpikeRouter
from .residual_pyramid import ResidualPyramid
from .epa import EPAModule
from .geodesic_rerank import GeodesicReranker


class QueryEngine:
    """记忆查询管线 V2：EPA → 残差金字塔 → 脉冲传播 → 向量融合 → 检索 → 测地线重排。

    对标 VCP TagMemoEngine.applyTagBoost() 的完整流程。
    """

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service: EmbeddingService,
        config: dict,
        tag_index: Optional[VectorIndex] = None,
        cooccurrence: Optional[CooccurrenceMatrix] = None,
        spike_router: Optional[SpikeRouter] = None,
        residual_pyramid: Optional[ResidualPyramid] = None,
        epa: Optional[EPAModule] = None,
        geodesic: Optional[GeodesicReranker] = None,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.config = config
        self.tag_index = tag_index
        self.cooccurrence = cooccurrence
        self.spike_router = spike_router
        self.residual_pyramid = residual_pyramid
        self.epa = epa
        self.geodesic = geodesic

        # 配置参数
        self.min_similarity = float(config.get("min_similarity", "0.35"))
        self.enable_spike = config.get("enable_spike_routing", True)
        self.enable_pyramid = config.get("enable_residual_pyramid", True)
        self.enable_epa = config.get("enable_epa", False)
        self.enable_geodesic = config.get("enable_geodesic_rerank", False)

        # Tag 向量缓存
        self._tag_vec_cache: Optional[dict] = None
        self._tag_vec_cache_ts: float = 0

    async def query(
        self,
        text: str,
        group_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """执行完整的浪潮查询管线。"""
        start = time.time()

        # Step 1: Embedding
        query_vec = await self.embedding.get_embedding(text)
        if query_vec is None:
            return []

        embed_ms = (time.time() - start) * 1000

        # Step 2: 浪潮增强（EPA + 残差金字塔 + 脉冲传播 + 向量融合）
        search_vec, energy_field = self._wave_boost(query_vec)

        # Step 3: 向量检索（用增强后的向量）
        candidates_k = top_k * 3
        results = self.memory_index.search(search_vec, k=candidates_k)

        if not results:
            return []

        # Step 4: 获取记忆内容
        memory_ids = [r[0] for r in results]
        distances = {r[0]: r[1] for r in results}
        memories = self.db.get_memories_by_ids(memory_ids)

        # 按 group_id 过滤
        if group_id:
            memories = [m for m in memories if m["group_id"] == group_id]

        # Step 5: 计算分数
        for mem in memories:
            dist = distances.get(mem["id"], 1.0)
            mem["similarity"] = 1.0 - dist
            mem["score"] = mem["similarity"] * mem.get("importance", 1.0)

        # Step 6: 测地线重排
        if self.enable_geodesic and self.geodesic and energy_field:
            candidates_for_rerank = [{"id": m["id"], "score": m["score"]} for m in memories]
            reranked = self.geodesic.rerank(candidates_for_rerank, energy_field)
            # 更新分数
            rerank_scores = {c["id"]: c["score"] for c in reranked}
            for mem in memories:
                if mem["id"] in rerank_scores:
                    mem["score"] = rerank_scores[mem["id"]]

        # Step 7: 过滤低分 + 排序 + 截断
        memories = [m for m in memories if m["score"] >= self.min_similarity]
        memories.sort(key=lambda m: m["score"], reverse=True)
        memories = memories[:top_k]

        # Step 8: 更新访问记录
        if memories:
            self.db.touch_memories([m["id"] for m in memories])

        total_ms = (time.time() - start) * 1000
        logger.debug(
            f"[WaveMemory] Query done: {len(memories)} results, "
            f"embed={embed_ms:.0f}ms, total={total_ms:.0f}ms"
        )

        return memories

    def _wave_boost(self, query_vec: np.ndarray) -> tuple[np.ndarray, dict]:
        """VCP TagMemo 浪潮增强：EPA → 残差金字塔 → 脉冲传播 → 向量融合。

        Returns:
            (enhanced_vector, energy_field)
        """
        energy_field = {}

        # 如果没有 tag_index 或 tag 数据太少，直接返回原始向量
        if not self.tag_index or self.tag_index.count < 10:
            return query_vec, energy_field

        # ─── EPA 分析 ───
        logic_depth = 0.5
        entropy = 0.5
        if self.enable_epa and self.epa and self.epa.initialized:
            epa_result = self.epa.analyze(query_vec)
            logic_depth = epa_result.get("logic_depth", 0.5)
            entropy = epa_result.get("entropy", 0.5)

        # ─── 残差金字塔：找到与查询相关的 Tag ───
        matched_tags = []  # [(tag_id, weight), ...]

        if self.enable_pyramid and self.residual_pyramid:
            tag_vecs = self._get_tag_vectors_cache()
            if tag_vecs:
                pyramid_result = self.residual_pyramid.analyze(query_vec, tag_vecs)
                # 从金字塔各层收集 Tag
                for level_tags in pyramid_result.get("levels", []):
                    for tag_info in level_tags:
                        tid = tag_info.get("tag_id")
                        sim = tag_info.get("similarity", 0)
                        if tid and sim > 0.1:
                            matched_tags.append((tid, sim))
        else:
            # Fallback: 直接用 tag_index 搜索
            tag_results = self.tag_index.search(query_vec, k=10)
            for tid, dist in tag_results:
                sim = 1.0 - dist
                if sim > 0.2:
                    matched_tags.append((tid, sim))

        if not matched_tags:
            return query_vec, energy_field

        # ─── 脉冲传播：沿共现图扩散 ───
        if self.enable_spike and self.spike_router and self.cooccurrence and self.cooccurrence.node_count > 0:
            seed_tags = [{"tag_id": tid, "weight": w} for tid, w in matched_tags[:10]]
            epa_for_spike = {"logic_depth": logic_depth, "entropy": entropy}
            spike_result = self.spike_router.propagate(seed_tags, epa_result=epa_for_spike)
            energy_field = spike_result.get("energy_field", {})

            # 收集涌现节点
            for activated in spike_result.get("activated_tags", []):
                tid = activated["tag_id"]
                energy = activated["energy"]
                if activated.get("is_emergent") and energy > 0.1:
                    matched_tags.append((tid, energy * 0.5))  # 涌现节点降权

        # ─── 向量融合 ───
        # 动态计算 alpha（对标 VCP 的 dynamicBoostFactor）
        base_boost = 0.3  # 基础增强因子
        dynamic_factor = logic_depth * (1.0 / (1.0 + entropy * 0.5))
        alpha = min(0.6, base_boost * max(0.5, min(2.0, dynamic_factor)))

        # 构建上下文向量
        tag_vecs = self._get_tag_vectors_cache()
        context_vec = np.zeros_like(query_vec)
        total_weight = 0.0

        # 语义去重（简化版：按 tag_id 去重，保留最高权重）
        tag_weights = {}
        for tid, w in matched_tags:
            if tid not in tag_weights or w > tag_weights[tid]:
                tag_weights[tid] = w

        for tid, weight in tag_weights.items():
            if tid in tag_vecs:
                context_vec += tag_vecs[tid] * weight
                total_weight += weight

        if total_weight > 0:
            context_vec /= total_weight
            # 归一化
            norm = np.linalg.norm(context_vec)
            if norm > 1e-10:
                context_vec /= norm

            # 融合: fused = (1-α)·query + α·context
            fused = (1 - alpha) * query_vec + alpha * context_vec
            # 归一化融合向量
            fused_norm = np.linalg.norm(fused)
            if fused_norm > 1e-10:
                fused /= fused_norm
            return fused.astype(np.float32), energy_field

        return query_vec, energy_field

    def _get_tag_vectors_cache(self) -> dict:
        """获取 tag 向量缓存（60s TTL）。"""
        now = time.time()
        if self._tag_vec_cache is None or now - self._tag_vec_cache_ts > 60:
            tag_data = self.db.get_all_tag_vectors()
            self._tag_vec_cache = {t[0]: t[2] for t in tag_data}
            self._tag_vec_cache_ts = now
        return self._tag_vec_cache

    async def shotgun_query(
        self,
        text: str,
        context_messages: list[str] = None,
        group_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """多路霰弹枪检索：主查询 + 上下文段查询，SVD 去重。

        Args:
            text: 当前查询文本
            context_messages: 最近 N 轮消息文本列表
            group_id: 群组 ID 过滤
            top_k: 最终返回数量
        """
        start = time.time()

        # 主查询向量
        query_vec = await self.embedding.get_embedding(text)
        if query_vec is None:
            return []

        # 主路：浪潮增强检索
        search_vec, energy_field = self._wave_boost(query_vec)
        main_results = self.memory_index.search(search_vec, k=top_k * 3)

        # 段路：上下文分段检索
        segment_results = []
        if context_messages:
            segmenter = ContextSegmenter(
                similarity_threshold=float(self.config.get("shotgun_similarity_threshold", 0.70)),
                max_segments=int(self.config.get("shotgun_max_segments", 3)),
            )
            # 获取上下文消息的向量
            ctx_vecs = await self.embedding.get_embeddings(context_messages)
            if ctx_vecs:
                segment_vecs = segmenter.segment(ctx_vecs)
                for seg_vec in segment_vecs:
                    seg_results = self.memory_index.search(seg_vec, k=top_k * 2)
                    segment_results.extend(seg_results)

        # 合并去重（按 memory_id）
        all_candidates = {}
        for mem_id, dist in main_results:
            all_candidates[mem_id] = min(all_candidates.get(mem_id, 999), dist)
        for mem_id, dist in segment_results:
            all_candidates[mem_id] = min(all_candidates.get(mem_id, 999), dist)

        if not all_candidates:
            return []

        # 获取记忆内容
        memory_ids = list(all_candidates.keys())
        memories = self.db.get_memories_by_ids(memory_ids)

        if group_id:
            memories = [m for m in memories if m["group_id"] == group_id]

        # 计算分数
        for mem in memories:
            dist = all_candidates.get(mem["id"], 1.0)
            mem["similarity"] = 1.0 - dist
            mem["score"] = mem["similarity"] * mem.get("importance", 1.0)

        # 测地线重排
        if self.enable_geodesic and self.geodesic and energy_field:
            candidates_for_rerank = [{"id": m["id"], "score": m["score"]} for m in memories]
            reranked = self.geodesic.rerank(candidates_for_rerank, energy_field)
            rerank_scores = {c["id"]: c["score"] for c in reranked}
            for mem in memories:
                if mem["id"] in rerank_scores:
                    mem["score"] = rerank_scores[mem["id"]]

        # SVD 主题去重
        memories = [m for m in memories if m["score"] >= self.min_similarity]
        if len(memories) > top_k:
            memories = self._svd_dedup(memories, query_vec, top_k)
        else:
            memories.sort(key=lambda m: m["score"], reverse=True)
            memories = memories[:top_k]

        # 更新访问记录
        if memories:
            self.db.touch_memories([m["id"] for m in memories])

        total_ms = (time.time() - start) * 1000
        logger.debug(
            f"[WaveMemory] Shotgun query done: {len(memories)} results, "
            f"candidates={len(all_candidates)}, total={total_ms:.0f}ms"
        )
        return memories

    def _svd_dedup(self, memories: list[dict], query_vec: np.ndarray, top_k: int) -> list[dict]:
        """SVD 主题去重：Gram-Schmidt 残差选择，确保结果多样性。"""
        # 获取记忆向量
        mem_ids = [m["id"] for m in memories]
        mem_vectors = self.db.get_memory_vectors(mem_ids)

        if not mem_vectors:
            memories.sort(key=lambda m: m["score"], reverse=True)
            return memories[:top_k]

        # 按 score 排序
        memories.sort(key=lambda m: m["score"], reverse=True)

        # Gram-Schmidt 残差选择
        selected = []
        selected_vecs = []

        for mem in memories:
            if len(selected) >= top_k:
                break

            vec = mem_vectors.get(mem["id"])
            if vec is None:
                selected.append(mem)
                continue

            # 计算该向量在已选向量子空间中的残差
            if selected_vecs:
                basis = np.vstack(selected_vecs)
                # 投影
                proj_coeffs = basis @ vec
                projection = proj_coeffs @ basis
                residual = vec - projection
                residual_norm = np.linalg.norm(residual)

                # 如果残差太小（与已选内容太相似），跳过
                if residual_norm < 0.3:
                    continue

            # 选中
            selected.append(mem)
            norm = np.linalg.norm(vec)
            if norm > 1e-8:
                selected_vecs.append(vec / norm)

        return selected

    def format_injection(self, memories: list[dict], template: str = "") -> str:
        """将记忆列表格式化为注入文本。"""
        if not memories:
            return ""

        if not template:
            template = "[记忆] {sender}({time}): {content}"

        parts = ["<wave_memory>"]
        for mem in memories:
            sender = mem.get("sender_name") or mem.get("sender_id") or "unknown"
            ts = time.strftime("%m-%d %H:%M", time.localtime(mem["timestamp"]))
            content = mem.get("content", "")
            score = mem.get("score", 0)

            line = template.replace("{sender}", sender).replace("{time}", ts).replace("{content}", content)
            parts.append(f"{line} (relevance: {score:.2f})")

        parts.append("</wave_memory>")
        return "\n".join(parts)
