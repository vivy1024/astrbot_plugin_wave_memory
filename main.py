"""
AstrBot Wave Memory 插件 — 基于 VCP TagMemo 浪潮算法的高性能记忆系统
查询路径零 LLM 调用，延迟 < 500ms
"""

import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .domain.scope import RuntimeScope
from .engine.database import WaveMemoryDB
from .engine.vector_index import VectorIndex
from .engine.db.outbox_repo import OutboxRepository
from .engine.db.scoped_learning_projection_repo import CoordinatorScopedProjectionWriter
from .engine.embedding import EmbeddingService
from .engine.query_engine import QueryEngine, QueryOptions
from .engine.directed_cooccurrence import (
    DEFAULT_MAX_NEIGHBORS_PER_TAG,
    DEFAULT_REBUILD_COOLDOWN_SEC,
    DEFAULT_REBUILD_THRESHOLD_PCT,
    DirectedCooccurrence,
    CooccurrenceScheduler,
)
from .engine.spike_routing import SpikeRouter
from .engine.residual_pyramid import ResidualPyramid
from .engine.geodesic_rerank import GeodesicReranker
from .engine.epa import EPAModule
from .engine.intrinsic_residual import IntrinsicResidualCalculator
from .engine.semantic_gain import SemanticGainConfig
from .services.message_writer import MessageWriter
from .services.tag_extractor import TagExtractor
from .services.tag_worker import TagWorker
from .services.system_convergence_runtime import ProductionWriteGateway
from .services.derived_projections import (
    CooccurrenceProjection,
    MemoryIndexProjection,
    TagIndexProjection,
    RuntimeRefreshProjection,
)
from .services.task_supervisor import TaskSupervisor
from .services.durable_jobs import DurableJobRunner
from .services.data_governance_jobs import DataGovernancePreviewJobs
from .services.scope_recovery import build_scope_recovery_handlers
from .services.quality_gate import QualityGate
from .services.pair_similarity import PairSimilarityService
from .services.hot_config import HotConfig
from .services.memory_index_policy import memory_index_policy_from_settings, select_hot_memory_candidates
from .services.maintenance_tokens import maintenance_repair_token
from .services.platform_context import PlatformContextManager
from .services.inbound_message_handler import InboundMessagePipeline, event_message_id
from .services.backup_lifecycle import DatabaseBackupManager
from .services.runtime_mode import effective_native_injection_enabled, effective_query_feature, resolve_runtime_mode, runtime_capability_enabled, should_self_heal_advanced_query
from .services.compat import build_duplicate_memory_warnings, build_livingmemory_compat_surface, detect_memory_plugins
from .services.impression_timeline import configure_social_limits, parse_impression_mark, persist_unsettled_trace
from .services.lifecycle import LifecycleService
from .tools.livingmemory_compat_tools import build_livingmemory_compat_tools
from .engine.book_lore_index import BookLoreIndex
from .services.meta_thinking import MetaThinking
from .services.dream import DreamService
from .services.self_reflect import SelfReflectService
from .services.llm_fallback import LLMFallbackClient, build_provider_chain
from .services.eviction import EvictionService
from .services.concern_tracker import ConcernTracker
from .services.mood_trajectory import MoodTrajectory
from .services.subjective_time import SubjectiveTime
from .services.desire_engine import DesireEngine
from .services.belief_engine import BeliefEngine
from .services.belief_emergence import BeliefEmergenceService
from .services.belief_gating import snapshot_from_relationship
from .services.proactive_audit import record_proactive_timeline
from .services.jargon.service import JargonService
from .services.few_shot.service import FewShotService
from .services.reflection_trigger import ReflectionTriggerService
from .services.relationship_events import RelationshipEventService
from .domain.scope import CatalogScope, RuntimeScope, SessionRef
from .services.identity_safety import (
    build_identity_safety_injection,
    filter_identity_contamination_memories,
    is_identity_contamination,
    prepend_identity_safety_system_prompt,
)
from .domain.bot_profile import BotProfile, BotProfileError, profile_from_legacy_config
from .services.bot_registry import BotRegistry, legacy_profiles_from_config
from .app.common import _record_err, _ObservationEvent, _stringify_config_value, _parse_csv_config_value, _parse_bool_config_value, _parse_int_config_value, _positive_float, _parse_bot_config, _build_bot_registry
from .app.bootstrap import BootstrapMixin
from .app.startup import StartupMixin
from .app.ingress import IngressMixin
from .app.injection import InjectionMixin
from .app.maintenance import MaintenanceMixin


@register(
    "astrbot_plugin_wave_memory",
    "vivy1024",
    "群聊长期记忆插件。日常检索只依赖向量模型，本地 SQLite 毫秒级召回，不装 Neo4j/ES。记忆注入和黑话、风格、信念、好感、时间线一起进回复，通道和预算可单独调。适合长期陪聊群 Bot；不是只塞最近几条的轻量摘要。",
    "5.0.0",
    "https://github.com/vivy1024/astrbot_plugin_wave_memory",
)
class WaveMemoryPlugin(BootstrapMixin, StartupMixin, IngressMixin, InjectionMixin, MaintenanceMixin, Star):
    """插件入口。组装与业务逻辑在 app/ 的 mixin 里；AstrBot 钩子必须定义在本模块（按 __module__ 绑定插件）。"""

    _reply_tracker: dict = {}

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._construct(context, config)

    async def initialize(self):
        """Initialize at most once, including concurrent framework callbacks."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            if self._terminated:
                raise RuntimeError("WaveMemoryPlugin is already terminated")
            await self._initialize_once()
            self._initialized = True

    async def terminate(self):
        """Serialize teardown with initialization and make repeated callbacks harmless."""
        async with self._initialize_lock:
            if self._terminated:
                return
            self._terminated = True
            await self._terminate_once()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """捕获所有消息，异步写入记忆。"""
        await self._handle_message(event)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在发送消息前提取 <<impression:...>> 标记并更新画像，彻底从消息链中剥离标记。"""
        await self._handle_decorating_result(event)

    @filter.after_message_sent()
    async def on_bot_sent(self, event: AstrMessageEvent):
        """捕获 bot 回复，写入记忆 + 异步更新好感度。"""
        await self._handle_bot_sent(event)

    @filter.on_llm_request(priority=1)
    async def meta_thinking_check(self, event: AstrMessageEvent, req=None):
        """v1.3.0: 纯规则判断 + 态度注入，不独立调 LLM。"""
        await self._handle_meta_thinking_check(event, req)

    @filter.on_llm_request(priority=6)
    async def filter_bot_tools(self, event: AstrMessageEvent, req=None):
        await self._handle_filter_bot_tools(event, req)

    @filter.on_llm_request(priority=5)
    async def inject_memory(self, event: AstrMessageEvent, req=None):
        """Inject only through the canonical scoped orchestrator; failures are fail-closed."""
        await self._handle_inject_memory(event, req)
