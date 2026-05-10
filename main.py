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
from .engine.directed_cooccurrence import DirectedCooccurrence, CooccurrenceScheduler
from .engine.spike_routing import SpikeRouter
from .engine.residual_pyramid import ResidualPyramid
from .engine.geodesic_rerank import GeodesicReranker
from .engine.epa import EPAModule
from .engine.intrinsic_residual import IntrinsicResidualCalculator
from .services.message_writer import MessageWriter
from .services.tag_extractor import TagExtractor
from .services.tag_job import TagBackfillJob
from .services.hot_config import HotConfig
from .tools.memory_search import WaveMemorySearchTool, WaveMemoryRememberTool
from .tools.deep_search import WaveMemoryDeepSearchTool


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

        # 解析配置（顶层字段 + 嵌套 object）
        query_cfg = self.config.get("Query_Settings", {})
        tag_cfg = self.config.get("Tag_Settings", {})
        storage_cfg = self.config.get("Storage_Settings", {})
        webui_cfg = self.config.get("WebUI_Settings", {})
        filter_cfg = self.config.get("Message_Filter", {})
        perf_cfg = self.config.get("Performance_Settings", {})

        self.embedding_provider_id = self.config.get("embedding_provider_id", "")
        self.dimension = int(self.config.get("embedding_dimension", 1024))
        self.tag_llm_provider_id = self.config.get("tag_llm_provider_id", "")
        self.tag_extraction_enabled = tag_cfg.get("tag_extraction_enabled", True)
        self.max_tags = int(tag_cfg.get("max_tags_per_message", 10))
        self.enable_auto_inject = query_cfg.get("enable_auto_inject", True)
        self.inject_top_k = int(query_cfg.get("inject_top_k", 5))
        self.min_similarity = float(query_cfg.get("min_similarity", "0.35"))
        self.injection_format = query_cfg.get("injection_format", "[记忆] {sender}({time}): {content}")
        self.enable_spike = query_cfg.get("enable_spike_routing", True)
        self.enable_pyramid = query_cfg.get("enable_residual_pyramid", True)
        self.enable_epa = query_cfg.get("enable_epa", False)
        self.enable_geodesic = query_cfg.get("enable_geodesic_rerank", False)
        self.enable_shotgun = query_cfg.get("enable_shotgun", False)
        self.max_memories = int(storage_cfg.get("max_memories", 100000))

        # WebUI 配置
        self.webui_enabled = webui_cfg.get("webui_enabled", True)
        self.webui_host = webui_cfg.get("webui_host", "0.0.0.0")
        self.webui_port = int(webui_cfg.get("webui_port", 7890))
        self.webui_password = webui_cfg.get("webui_password", "")

        # 消息过滤配置
        self.min_message_length = int(filter_cfg.get("min_message_length", 4))
        self.max_message_length = int(filter_cfg.get("max_message_length", 2000))
        self.ignore_bot_messages = filter_cfg.get("ignore_bot_messages", False)
        self.group_whitelist = [g.strip() for g in filter_cfg.get("group_whitelist", "").split(",") if g.strip()]
        self.group_blacklist = [g.strip() for g in filter_cfg.get("group_blacklist", "").split(",") if g.strip()]

        # 性能配置
        self.embedding_batch_size = int(perf_cfg.get("embedding_batch_size", 10))
        self.write_flush_interval = int(perf_cfg.get("write_flush_interval", 30))

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

        # 共现矩阵（有向序位）
        self.cooccurrence = DirectedCooccurrence(self.db)
        self.intrinsic_residual = IntrinsicResidualCalculator(
            db=self.db, cooccurrence=self.cooccurrence
        )
        self.cooccurrence_scheduler = CooccurrenceScheduler(
            cooccurrence=self.cooccurrence,
            threshold_pct=0.01,
            cooldown_sec=300,
            on_rebuild_complete=self._on_cooccurrence_rebuilt,
        )

        # 脉冲传播
        self.spike_router = SpikeRouter(
            self.cooccurrence,
            residual_map=self.intrinsic_residual.load(),
        ) if self.enable_spike else None

        # 残差金字塔
        self.residual_pyramid = ResidualPyramid(self.tag_index) if self.enable_pyramid else None

        # EPA
        self.epa = EPAModule(self.db) if self.enable_epa else None

        # 测地线重排
        self.geodesic = GeodesicReranker(self.db) if self.enable_geodesic else None

        # 热配置
        self.hot_config = HotConfig(initial_config={
            "spike": {"firing_threshold": 0.10, "base_decay": 0.25, "wormhole_decay": 0.70,
                      "tension_threshold": 1.0, "max_hops": 4},
            "query": {"min_similarity": self.min_similarity, "boost_alpha_base": 0.3},
            "geodesic": {"energy_weight": 0.3},
            "residual": {"boost_range": 0.6},
        })
        if self.spike_router:
            self.hot_config.on_change(self.spike_router.on_config_change)

        # 查询引擎（注入所有模块）
        self.query_engine = QueryEngine(
            db=self.db,
            memory_index=self.memory_index,
            embedding_service=self.embedding_service,
            config=query_cfg,
            tag_index=self.tag_index,
            cooccurrence=self.cooccurrence,
            spike_router=self.spike_router,
            residual_pyramid=self.residual_pyramid,
            epa=self.epa,
            geodesic=self.geodesic,
        )

        # Tag 提取器
        self.tag_extractor = None
        if self.tag_extraction_enabled and self.tag_llm_provider_id:
            self.tag_extractor = TagExtractor(
                context=context,
                provider_id=self.tag_llm_provider_id,
                max_tags=self.max_tags,
                blacklist=tag_cfg.get("tag_blacklist", ""),
            )

        # 异步写入器
        self.writer = MessageWriter(
            db=self.db,
            memory_index=self.memory_index,
            embedding_service=self.embedding_service,
            tag_extractor=self.tag_extractor,
            on_tags_written=self.cooccurrence_scheduler.notify_tag_change,
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
        deep_search_tool = WaveMemoryDeepSearchTool(db=self.db)
        self.context.add_llm_tools(search_tool, remember_tool, deep_search_tool)

        # 启动 WebUI
        if self.webui_enabled:
            try:
                from .webui import WaveMemoryWebUI
                self.webui = WaveMemoryWebUI(
                    db=self.db,
                    query_engine=self.query_engine,
                    embedding_service=self.embedding_service,
                    memory_index=self.memory_index,
                    tag_index=self.tag_index,
                    cooccurrence=self.cooccurrence,
                    spike_router=self.spike_router,
                    residual_pyramid=self.residual_pyramid,
                    epa=self.epa,
                    geodesic=self.geodesic,
                    tag_extractor=self.tag_extractor,
                    writer=self.writer,
                    host=self.webui_host,
                    port=self.webui_port,
                    password=self.webui_password,
                    plugin_config=self.config,
                )
                await self.webui.start()
            except Exception as e:
                logger.warning(f"[WaveMemory] WebUI failed to start: {e}")
                self.webui = None
        else:
            self.webui = None

        # 启动后台 Tag 补全（覆盖率 < 90% 时自动触发）
        self.tag_job = TagBackfillJob(
            db=self.db,
            tag_extractor=self.tag_extractor,
            embedding_service=self.embedding_service,
            tag_index=self.tag_index,
            config=tag_cfg,
        )
        tag_coverage = self.tag_job.get_coverage()
        if tag_coverage < 0.90:
            logger.info(
                f"[WaveMemory] Tag coverage {tag_coverage:.1%} < 90%, starting backfill job"
            )
            self.tag_job.start()
        else:
            logger.info(f"[WaveMemory] Tag coverage {tag_coverage:.1%}, backfill not needed")

        logger.info("[WaveMemory] Fully initialized")

    async def terminate(self):
        """插件卸载时清理。"""
        if hasattr(self, 'tag_job') and self.tag_job:
            self.tag_job.stop()
        if hasattr(self, 'webui') and self.webui:
            self.webui.stop()
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
            # 霰弹枪模式：多路检索
            if self.enable_shotgun:
                # 获取最近上下文消息
                context_messages = self._get_recent_messages(event, max_messages=8)
                memories = await self.query_engine.shotgun_query(
                    text=message,
                    context_messages=context_messages,
                    group_id=group_id,
                    top_k=self.inject_top_k,
                )
            else:
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
        if not message or len(message.strip()) < self.min_message_length:
            return

        group_id = event.get_group_id() or f"private:{event.get_sender_id()}"

        # 群组白名单/黑名单过滤
        if self.group_whitelist and group_id not in self.group_whitelist:
            return
        if self.group_blacklist and group_id in self.group_blacklist:
            return

        # 截断过长消息
        if len(message) > self.max_message_length:
            message = message[:self.max_message_length]

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

    def _get_recent_messages(self, event, max_messages: int = 8) -> list[str]:
        """从事件上下文中获取最近的消息文本。"""
        try:
            # 从数据库获取最近消息
            group_id = event.get_group_id()
            rows = self.db.conn.execute(
                """SELECT content FROM memories
                   WHERE group_id = ? AND content IS NOT NULL
                   ORDER BY created_at DESC LIMIT ?""",
                (group_id, max_messages),
            ).fetchall()
            return [r[0] for r in reversed(rows)] if rows else []
        except Exception:
            return []

    async def _rebuild_cooccurrence(self):
        self.cooccurrence.rebuild()

    async def _on_cooccurrence_rebuilt(self):
        """共现矩阵重建完成后，重算内生残差。"""
        try:
            residuals = self.intrinsic_residual.compute_all()
            if residuals:
                self.intrinsic_residual.persist(residuals)
                # 更新 SpikeRouter 的残差 map
                if self.spike_router:
                    self.spike_router.residual_map = residuals
        except Exception as e:
            logger.warning(f"[WaveMemory] Intrinsic residual computation failed: {e}")

    async def _init_epa(self):
        self.epa.initialize()
