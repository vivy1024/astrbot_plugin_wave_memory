"""
AstrBot Wave Memory 插件 — 基于 VCP TagMemo 浪潮算法的高性能记忆系统
查询路径零 LLM 调用，延迟 < 500ms
"""

import asyncio
import os
import time
from typing import Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .engine.database import WaveMemoryDB
from .engine.vector_index import VectorIndex
from .engine.embedding import EmbeddingService
from .engine.query_engine import QueryEngine
from .engine.cooccurrence import CooccurrenceMatrix
from .engine.spike_routing import SpikeRouter
from .engine.residual_pyramid import ResidualPyramid
from .engine.geodesic_rerank import GeodesicReranker
from .engine.epa import EPAModule
from .services.message_writer import MessageWriter
from .services.tag_extractor import TagExtractor
from .tools.memory_search import WaveMemorySearchTool, WaveMemoryRememberTool


@register(
    "astrbot_plugin_wave_memory",
    "vivy1024",
    "基于 VCP TagMemo 浪潮算法的高性能记忆插件",
    "0.1.0",
    "https://github.com/vivy1024/astrbot_plugin_wave_memory",
)
class WaveMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # 解析配置
        embed_cfg = self.config.get("Embedding_Settings", {})
        tag_cfg = self.config.get("Tag_Settings", {})
        query_cfg = self.config.get("Query_Settings", {})
        storage_cfg = self.config.get("Storage_Settings", {})

        self.embedding_provider_id = embed_cfg.get("embedding_provider_id", "")
        self.dimension = int(embed_cfg.get("dimension", 1024))
        self.tag_llm_provider_id = tag_cfg.get("tag_llm_provider_id", "")
        self.tag_extraction_enabled = tag_cfg.get("tag_extraction_enabled", True)
        self.max_tags = int(tag_cfg.get("max_tags_per_message", 10))
        self.enable_auto_inject = query_cfg.get("enable_auto_inject", True)
        self.inject_top_k = int(query_cfg.get("inject_top_k", 5))
        self.enable_spike = query_cfg.get("enable_spike_routing", True)
        self.enable_pyramid = query_cfg.get("enable_residual_pyramid", True)
        self.enable_epa = query_cfg.get("enable_epa", False)
        self.enable_geodesic = query_cfg.get("enable_geodesic_rerank", False)
        self.max_memories = int(storage_cfg.get("max_memories", 100000))

        # 初始化数据目录
        data_path = get_astrbot_data_path() or os.path.dirname(__file__)
        self.data_dir = os.path.join(data_path, "plugin_data", "astrbot_plugin_wave_memory")
        os.makedirs(self.data_dir, exist_ok=True)

        # 初始化核心组件
        db_path = os.path.join(self.data_dir, "wave_memory.db")
        index_path = os.path.join(self.data_dir, "memory.hnsw")
        tag_index_path = os.path.join(self.data_dir, "tags.hnsw")

        self.db = WaveMemoryDB(db_path, dimension=self.dimension)

        self.memory_index = VectorIndex(
            dimension=self.dimension,
            max_elements=self.max_memories,
            index_path=index_path,
        )

        self.tag_index = VectorIndex(
            dimension=self.dimension,
            max_elements=50000,
            index_path=tag_index_path,
        )

        self.embedding_service = EmbeddingService(
            context=context,
            provider_id=self.embedding_provider_id,
            dimension=self.dimension,
        )

        # 共现矩阵
        self.cooccurrence = CooccurrenceMatrix(self.db)

        # 脉冲传播
        self.spike_router = SpikeRouter(self.cooccurrence) if self.enable_spike else None

        # 残差金字塔
        self.residual_pyramid = ResidualPyramid(self.tag_index) if self.enable_pyramid else None

        # EPA
        self.epa = EPAModule(self.db) if self.enable_epa else None

        # 测地线重排
        self.geodesic = GeodesicReranker(self.db) if self.enable_geodesic else None

        # 查询引擎
        self.query_engine = QueryEngine(
            db=self.db,
            memory_index=self.memory_index,
            embedding_service=self.embedding_service,
            config=query_cfg,
        )

        # Tag 提取器
        self.tag_extractor = None
        if self.tag_extraction_enabled and self.tag_llm_provider_id:
            self.tag_extractor = TagExtractor(
                context=context,
                provider_id=self.tag_llm_provider_id,
                max_tags=self.max_tags,
            )

        # 异步写入器
        self.writer = MessageWriter(
            db=self.db,
            memory_index=self.memory_index,
            embedding_service=self.embedding_service,
            tag_extractor=self.tag_extractor,
        )

        logger.info(
            f"[WaveMemory] Init: {self.db.get_memory_count()} memories, "
            f"{self.db.get_tag_count()} tags, "
            f"dim={self.dimension}, "
            f"spike={self.enable_spike}, pyramid={self.enable_pyramid}, "
            f"epa={self.enable_epa}, geodesic={self.enable_geodesic}"
        )

    async def initialize(self):
        """AstrBot 完成 handler 绑定后调用。"""
        # 启动写入器
        self.writer.start()

        # 重建索引（如果需要）
        if self.memory_index.count == 0 and self.db.get_memory_count() > 0:
            asyncio.create_task(self._rebuild_memory_index())

        if self.tag_index.count == 0 and self.db.get_tag_count() > 0:
            asyncio.create_task(self._rebuild_tag_index())

        # 构建共现矩阵
        if self.enable_spike and self.db.get_tag_count() > 10:
            asyncio.create_task(self._rebuild_cooccurrence())

        # 初始化 EPA
        if self.epa:
            asyncio.create_task(self._init_epa())

        # 注册 LLM 工具
        search_tool = WaveMemorySearchTool(query_engine=self.query_engine)
        remember_tool = WaveMemoryRememberTool(writer=self.writer)
        self.context.add_llm_tools(search_tool, remember_tool)

        logger.info("[WaveMemory] Fully initialized")

    async def terminate(self):
        """插件卸载时清理。"""
        self.writer.stop()
        self.memory_index.save()
        self.tag_index.save()
        self.db.close()
        logger.info("[WaveMemory] Shutdown complete")

    # ─── Hook: 自动注入记忆 ───

    @filter.on_llm_request(priority=5)
    async def inject_memory(self, event: AstrMessageEvent, req=None):
        """在 LLM 请求前注入相关记忆。"""
        if not self.enable_auto_inject or not req:
            return
        if not self.embedding_provider_id:
            return

        message = event.get_message_str()
        if not message or len(message.strip()) < 4:
            return

        group_id = event.get_group_id()

        try:
            memories = await self.query_engine.query(
                text=message,
                group_id=group_id,
                top_k=self.inject_top_k,
            )

            if memories:
                injection = self.query_engine.format_injection(memories)
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(TextPart(text=injection))

        except Exception as e:
            logger.warning(f"[WaveMemory] Injection failed: {e}")

    # ─── Hook: 捕获消息 ───

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """捕获所有消息，异步写入记忆。"""
        message = event.get_message_str()
        if not message or len(message.strip()) < 4:
            return

        group_id = event.get_group_id() or f"private:{event.get_sender_id()}"
        sender_id = event.get_sender_id()
        sender_name = ""
        if event.message_obj and event.message_obj.sender:
            sender_name = event.message_obj.sender.nickname or ""

        await self.writer.enqueue({
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": message,
            "timestamp": time.time(),
        })

    @filter.after_message_sent()
    async def on_bot_sent(self, event: AstrMessageEvent):
        """捕获 bot 回复，写入记忆。"""
        result = event.get_result()
        if not result or not result.chain:
            return

        from astrbot.core.message.components import Plain
        text_parts = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
        bot_text = "".join(text_parts).strip()
        if not bot_text or len(bot_text) < 4:
            return

        group_id = event.get_group_id() or f"private:{event.get_sender_id()}"

        await self.writer.enqueue({
            "group_id": group_id,
            "sender_id": "bot",
            "sender_name": "羽书",
            "content": bot_text,
            "timestamp": time.time(),
        })

    # ─── 后台任务 ───

    async def _rebuild_memory_index(self):
        logger.info("[WaveMemory] Rebuilding memory index...")
        import numpy as np
        all_vecs = self.db.get_all_memory_vectors()
        if all_vecs:
            ids = [v[0] for v in all_vecs]
            vectors = np.array([v[1] for v in all_vecs], dtype=np.float32)
            self.memory_index.add(ids, vectors)
            self.memory_index.save()
            logger.info(f"[WaveMemory] Memory index rebuilt: {len(ids)} vectors")

    async def _rebuild_tag_index(self):
        logger.info("[WaveMemory] Rebuilding tag index...")
        import numpy as np
        tag_data = self.db.get_all_tag_vectors()
        if tag_data:
            ids = [t[0] for t in tag_data]
            vectors = np.array([t[2] for t in tag_data], dtype=np.float32)
            self.tag_index.add(ids, vectors)
            self.tag_index.save()
            logger.info(f"[WaveMemory] Tag index rebuilt: {len(ids)} vectors")

    async def _rebuild_cooccurrence(self):
        self.cooccurrence.rebuild()

    async def _init_epa(self):
        self.epa.initialize()
