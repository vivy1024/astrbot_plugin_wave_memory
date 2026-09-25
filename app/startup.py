"""启动与关停：initialize 里的后台服务、工具注册、WebUI、灵魂子系统（从 main.py 拆出的 WaveMemoryPlugin mixin）。"""

from __future__ import annotations

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
from ..domain.scope import RuntimeScope
from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex
from ..engine.db.outbox_repo import OutboxRepository
from ..engine.db.scoped_learning_projection_repo import CoordinatorScopedProjectionWriter
from ..engine.embedding import EmbeddingService
from ..engine.query_engine import QueryEngine, QueryOptions
from ..engine.directed_cooccurrence import (
    DEFAULT_MAX_NEIGHBORS_PER_TAG,
    DEFAULT_REBUILD_COOLDOWN_SEC,
    DEFAULT_REBUILD_THRESHOLD_PCT,
    DirectedCooccurrence,
    CooccurrenceScheduler,
)
from ..engine.spike_routing import SpikeRouter
from ..engine.residual_pyramid import ResidualPyramid
from ..engine.geodesic_rerank import GeodesicReranker
from ..engine.epa import EPAModule
from ..engine.intrinsic_residual import IntrinsicResidualCalculator
from ..engine.semantic_gain import SemanticGainConfig
from ..services.message_writer import MessageWriter
from ..services.tag_extractor import TagExtractor
from ..services.tag_worker import TagWorker
from ..services.system_convergence_runtime import ProductionWriteGateway
from ..services.derived_projections import (
    CooccurrenceProjection,
    MemoryIndexProjection,
    TagIndexProjection,
    RuntimeRefreshProjection,
)
from ..services.task_supervisor import TaskSupervisor
from ..services.durable_jobs import DurableJobRunner
from ..services.data_governance_jobs import DataGovernancePreviewJobs
from ..services.scope_recovery import build_scope_recovery_handlers
from ..services.quality_gate import QualityGate
from ..services.pair_similarity import PairSimilarityService
from ..services.hot_config import HotConfig
from ..services.memory_index_policy import memory_index_policy_from_settings, select_hot_memory_candidates
from ..services.maintenance_tokens import maintenance_repair_token
from ..services.platform_context import PlatformContextManager
from ..services.inbound_message_handler import InboundMessagePipeline, event_message_id
from ..services.backup_lifecycle import DatabaseBackupManager
from ..services.runtime_mode import effective_native_injection_enabled, effective_query_feature, resolve_runtime_mode, runtime_capability_enabled, should_self_heal_advanced_query
from ..services.compat import build_duplicate_memory_warnings, build_livingmemory_compat_surface, detect_memory_plugins
from ..services.impression_timeline import configure_social_limits, parse_impression_mark, persist_unsettled_trace
from ..services.lifecycle import LifecycleService
from ..tools.livingmemory_compat_tools import build_livingmemory_compat_tools
from ..engine.book_lore_index import BookLoreIndex
from ..services.meta_thinking import MetaThinking
from ..services.dream import DreamService
from ..services.self_reflect import SelfReflectService
from ..services.llm_fallback import LLMFallbackClient, build_provider_chain
from ..services.eviction import EvictionService
from ..services.concern_tracker import ConcernTracker
from ..services.mood_trajectory import MoodTrajectory
from ..services.subjective_time import SubjectiveTime
from ..services.desire_engine import DesireEngine
from ..services.belief_engine import BeliefEngine
from ..services.belief_emergence import BeliefEmergenceService
from ..services.belief_gating import snapshot_from_relationship
from ..services.proactive_audit import record_proactive_timeline
from ..services.jargon.service import JargonService
from ..services.few_shot.service import FewShotService
from ..services.reflection_trigger import ReflectionTriggerService
from ..services.relationship_events import RelationshipEventService
from ..domain.scope import CatalogScope, RuntimeScope, SessionRef
from ..services.identity_safety import (
    build_identity_safety_injection,
    filter_identity_contamination_memories,
    is_identity_contamination,
    prepend_identity_safety_system_prompt,
)
from ..domain.bot_profile import BotProfile, BotProfileError, profile_from_legacy_config
from ..services.bot_registry import BotRegistry, legacy_profiles_from_config
from .common import _record_err, _ObservationEvent, _stringify_config_value, _parse_csv_config_value, _parse_bool_config_value, _parse_int_config_value, _positive_float, _parse_bot_config, _build_bot_registry


class StartupMixin:
    async def _initialize_once(self):
        """AstrBot 完成 handler 绑定后执行一次实际初始化。"""
        # 从现有 Bot Registry 构造唯一 ScopeResolver；领域切片未落地时显式保持 fail closed。
        self._rebuild_scope_resolver()
        self.bot_registry.add_listener(self._on_bot_registry_changed)

        # 启动写入器
        self.writer.start(self.task_supervisor)

        # 启动 TagWorker
        if self.tag_worker:
            self.tag_worker.start(self.task_supervisor)

        # 长修复只通过 durable Maintenance jobs 执行；启动时仅排队，不直接重建。
        self.maintenance_job_runner.start(self.task_supervisor)
        if await self._has_pending_vector_backfill():
            await self._queue_vector_backfill(reason="startup_missing_vectors")
        if (
            self.memory_index.manifest_error
            or (self.memory_index.count == 0 and self.db.get_memory_count() > 0)
        ):
            await self._queue_maintenance_repair("memory_index", reason="startup_drift")

        try:
            catalog_tag_count = int(self.db.conn.execute(
                "SELECT COUNT(*) FROM tag_catalog WHERE embedding IS NOT NULL AND status='active'"
            ).fetchone()[0])
        except Exception:
            catalog_tag_count = 0
        if (
            self.tag_catalog_index.manifest_error
            or (self.tag_catalog_index.count == 0 and catalog_tag_count > 0)
        ):
            await self._queue_maintenance_repair("tag_index", reason="startup_drift")

        # PairSimilarity：通过 durable job 分批计算；请求路径只做懒加载读取。
        pair_count_row = self.db.conn.execute(
            "SELECT COUNT(*) FROM tag_pair_similarity"
        ).fetchone()
        if self.db.get_tag_count() > 1 and int(pair_count_row[0] if pair_count_row else 0) == 0:
            await self._queue_maintenance_repair("pair_similarity", reason="startup_empty")

        if self.enable_spike and self.db.get_tag_count() > 10 and not self.cooccurrence.forward:
            await self._queue_maintenance_repair("cooccurrence", reason="startup_empty")

        # 中文全文索引：没回填完就排一个可续跑的回填任务（查询方在回填完成前继续用旧索引）。
        try:
            from ..engine.db import fts_cjk

            if not fts_cjk.is_ready(self.db.conn) and self.db.get_memory_count() > 0:
                await self._queue_maintenance_repair("fts_cjk", reason="startup_backfill")
        except Exception as exc:
            logger.warning(f"[WaveMemory] 中文全文索引回填排队失败: {exc}")

        # 初始化 EPA
        if self.epa:
            self._spawn(self._init_epa())

        # 注册 LLM 工具：memory_only 保留纯记忆工具；compat_only 仅暴露 LivingMemory 风格别名（如已启用）。
        # 工具定义在 tools/builtin_registry.py（外部扩展放 <plugin_data>/extensions/），这里只按能力开关实例化。
        livingmemory_alias_tools = build_livingmemory_compat_tools(
            self.memory_engine,
            enabled=self.livingmemory_alias_tools_enabled,
        )
        self.livingmemory_alias_tools_registered = bool(livingmemory_alias_tools)

        from ..services.tool_registry import ToolRegistry
        from ..tools.builtin_registry import builtin_tool_specs

        self.tool_registry = ToolRegistry()
        self.tool_registry.register_many(builtin_tool_specs())
        self.tool_registry.load_extensions(
            os.path.join(self.data_dir, "extensions"),
            extra_registries={"channel_registry": self._channel_registry()},
        )
        llm_tools = [
            *livingmemory_alias_tools,
            *self.tool_registry.build(
                self,
                capability_enabled=lambda capability, default: runtime_capability_enabled(
                    self.runtime_mode, capability, default
                ),
            ),
        ]
        social_record = self.tool_registry.get("wave_memory_record_social_impression")
        self._bot_aware_tools = [social_record.instance] if social_record else []
        from ..webui.container import get_container

        get_container().tool_registry = self.tool_registry

        if llm_tools:
            self.context.add_llm_tools(*llm_tools)

        # 启动 WebUI
        if self.webui_enabled:
            await self._refresh_group_names_from_platforms()
            try:
                from ..webui import WaveMemoryWebUI
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
                    write_gateway=self.write_gateway,
                    durable_jobs=self.write_gateway.jobs,
                    data_governance_jobs=self.data_governance_jobs,
                    scope_recovery_jobs=self.scope_recovery_jobs,
                    task_supervisor=self.task_supervisor,
                    host=self.webui_host,
                    port=self.webui_port,
                    password=self.webui_password,
                    plugin_config=self.config,
                    injection_channel_config=self.injection_channel_config,
                    injection_channel_config_setter=self._set_injection_channel_config,
                    livingmemory_facade=self.memory_engine,
                    livingmemory_facade_enabled=self.livingmemory_compat_enabled,
                    livingmemory_alias_tools_registered=self.livingmemory_alias_tools_registered,
                    detected_memory_plugins=self.detected_memory_plugins,
                    bot_registry=self.bot_registry.by_db_id,
                    group_name_resolver=self._get_group_name,
                    bot_registry_service=self.bot_registry,
                )
                await self.webui.start()
                self._spawn(
                    self._warm_group_names_when_platforms_ready(),
                    name="wave-memory:webui-group-name-warmup",
                    owner="webui",
                )
            except Exception as e:
                logger.warning(f"[WaveMemory] WebUI failed to start: {e}")
                _record_err("WebUI", e)
                self.webui = None
        else:
            self.webui = None

        # legacy TagBackfillJob 写入 tags/memory_tags，已退出正式数据面。
        # scoped TagWorker 会持续扫描所有未完成的 resolved v2 memory，并通过统一写入口提交。
        self.tag_job = None

        # v2.0: Tag 质量检测——垃圾率 > 50% 时降级关闭脉冲传播
        try:
            catalog_exists = self.db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'"
            ).fetchone()
            if catalog_exists:
                total_kw = self.db.conn.execute("SELECT COUNT(*) FROM tag_catalog WHERE tag_type='keyword' AND status='active'").fetchone()[0]
                bad_kw = self.db.conn.execute("SELECT COUNT(*) FROM tag_catalog WHERE tag_type='keyword' AND status='active' AND LENGTH(display_name) > 5").fetchone()[0]
            else:
                total_kw = self.db.conn.execute("SELECT COUNT(*) FROM tags WHERE tag_type='keyword'").fetchone()[0]
                bad_kw = self.db.conn.execute("SELECT COUNT(*) FROM tags WHERE tag_type='keyword' AND LENGTH(name) > 5").fetchone()[0]
            if total_kw > 100 and bad_kw / total_kw > 0.5:
                logger.warning(f"[WaveMemory] Tag 质量差（keyword 垃圾率 {bad_kw}/{total_kw} = {bad_kw*100//total_kw}%），自动降级关闭脉冲传播")
                self.enable_spike = False
        except Exception:
            pass

        # 启动生命周期服务
        if self.enable_affinity:
            # 旧 API 仍保留首个 Bot 作为默认读取值；新消息按 RuntimeScope.bot_id 分发到独立 affinity engine。
            _first_bot = list(self._bot_registry.values())[0] if self._bot_registry else None
            _affinity_bot_identities = {
                profile.db_id: profile.qq_id
                for profile in self._bot_registry.values()
                if profile.db_id
            }
            self.lifecycle = LifecycleService(
                db=self.db,
                bot_qq_id=_first_bot.qq_id if _first_bot else "",
                bot_db_id=_first_bot.db_id if _first_bot else "",
                bot_identities=_affinity_bot_identities,
                mood_duration_hours=self.mood_duration_hours,
                mood_msg_threshold=self.mood_msg_threshold,
                positive_emotion_threshold=self.positive_emotion_threshold,
                negative_emotion_threshold=self.negative_emotion_threshold,
                relationship_service=self.relationship_service,
            )
            self.lifecycle.start(self.task_supervisor)
            self.consolidation = None
            logger.info("[WaveMemory] ConsolidationService removed; topic tags stay with TagWorker")
        else:
            self.consolidation = None

        # 记忆淘汰服务
        eviction_cfg = self.config.get("Eviction_Settings", {})
        if eviction_cfg.get("enabled", True):
            self.eviction_service = EvictionService(
                db=self.db,
                memory_index=self.memory_index,
                noise_ttl_days=int(eviction_cfg.get("noise_ttl_days", 7)),
                chat_stale_days=self.memory_index_policy.chat_hot_days,
                eviction_interval_hours=float(eviction_cfg.get("interval_hours", 6.0)),
                write_gateway=self.write_gateway,
            )
            self.eviction_service.start(self.task_supervisor)
        else:
            self.eviction_service = None

        # 黑话系统 (US-4.1~4.5)：memory_only/compat_only 强制关闭，避免黑话学习/注入越过纯记忆边界。
        jargon_cfg = self.config.get("Jargon_Settings", {})
        if runtime_capability_enabled(self.runtime_mode, "jargon", jargon_cfg.get("enabled", True)) and self.tag_llm_provider_id:
            try:
                jargon_llm = LLMFallbackClient(
                    context=self.context,
                    provider_ids=self._llm_chain(),
                    log_prefix="[Jargon]",
                )
                self.jargon_service = JargonService(
                    db=self.db, llm_client=jargon_llm, enabled=True,
                    config=jargon_cfg,
                )
                logger.info("[WaveMemory] Jargon system initialized")
                if getattr(self, "lifecycle", None):
                    self.lifecycle.jargon_service = self.jargon_service
                    for engine in getattr(self.lifecycle, "_affinities", {}).values():
                        engine.jargon_service = self.jargon_service
                if getattr(self, "webui", None):
                    from ..webui.container import get_container
                    get_container().jargon_service = self.jargon_service
            except Exception as e:
                logger.warning(f"[WaveMemory] Jargon init failed: {e}")
                _record_err("Jargon", e)
                self.jargon_service = None
        else:
            self.jargon_service = None

        # Few-Shot 风格学习 (US-5.1~5.4)：纯记忆模式不启动风格学习服务。
        fewshot_cfg = self.config.get("FewShot_Settings", {})
        if runtime_capability_enabled(self.runtime_mode, "fewshot", fewshot_cfg.get("enabled", True)) and self.tag_llm_provider_id:
            try:
                fewshot_llm = LLMFallbackClient(
                    context=self.context,
                    provider_ids=self._llm_chain(),
                    log_prefix="[FewShot]",
                )
                self.few_shot_service = FewShotService(
                    db=self.db, llm_client=fewshot_llm,
                    embedding_service=self.embedding_service, enabled=True,
                    config=fewshot_cfg,
                    repository=self.db.fewshot_repository,
                    writer=self.scoped_projection_writer,
                )
                logger.info("[WaveMemory] Few-Shot system initialized")
            except Exception as e:
                logger.warning(f"[WaveMemory] FewShot init failed: {e}")
                _record_err("FewShot", e)
                self.few_shot_service = None
        else:
            self.few_shot_service = None

        # MetaThinking（内心判断层）：memory_only/compat_only 禁止启动独立判断层。
        meta_cfg = self.config.get("MetaThinking_Settings", {})
        if runtime_capability_enabled(self.runtime_mode, "metathinking", meta_cfg.get("enabled", True)):
            try:
                # 从 bot registry 构建 prompt 映射（配置驱动）
                maps = self._meta_thinking_bot_maps()
                self.meta_thinking = MetaThinking(
                    db=self.db,
                    context=self.context,
                    bot_qq_id=self._bot_qq_ids[0] if self._bot_qq_ids else "",
                    bot_qq_ids=maps["bot_qq_ids"],
                    bot_prompts=maps["bot_prompts"],
                    bot_names=maps["bot_names"],
                    bot_db_ids=maps["bot_db_ids"],
                    admin_ids=self._get_admin_ids(),
                    config=meta_cfg,
                    global_fallback_ids=self.config.get("meta_thinking_fallback_ids", ""),
                    extra_interests=maps["extra_interests"],
                )
                self.meta_thinking._plugin_config = self.config  # 好感度约束需要顶层 config
            except Exception as e:
                logger.warning(f"[WaveMemory] MetaThinking init failed: {e}")
                _record_err("MetaThinking", e)
                self.meta_thinking = None
        else:
            self.meta_thinking = None

        # 启动做梦系统
        if self.enable_dream:
            self.dream_service = DreamService(
                db=self.db,
                memory_index=self.memory_index,
                dream_interval_hours=self.dream_interval_hours,
                recent_seeds=self.dream_recent_seeds,
                recent_k=self.dream_recent_k,
                mid_seeds=self.dream_mid_seeds,
                mid_k=self.dream_mid_k,
            )
            self.dream_service.start(self.task_supervisor)
        else:
            self.dream_service = None

        # 自主学习系统（对有经历通道的 bot 生效）
        # 找到没有 exclude_sources 的 bot（即经历所有者）
        _registry = getattr(self, '_bot_registry', {})
        experience_bot = next(
            (p for p in _registry.values() if p.experience_source),
            None,
        ) or next(
            (p for p in _registry.values() if not p.exclude_sources),
            None
        )
        study_cfg = self.config.get("Study_Settings", {}) or {}
        # 自省系统（检测纠正 → 记录，所有 bot 共用）：纯记忆模式关闭自省学习后台能力。
        reflect_bot = experience_bot or (list(_registry.values())[0] if _registry else None)
        if runtime_capability_enabled(self.runtime_mode, "self_reflect", study_cfg.get("self_reflect_enabled", True)) and self.tag_llm_provider_id and reflect_bot:
            try:
                reflect_llm = LLMFallbackClient(
                    context=self.context,
                    provider_ids=self._llm_chain(),
                    log_prefix="[SelfReflect]",
                )
                self.self_reflect = SelfReflectService(
                    db=self.db,
                    memory_index=self.memory_index,
                    embedding_service=self.embedding_service,
                    llm_client=reflect_llm,
                    book_lore_index=self.book_lore_index,  # 可为 None
                    lore_db_path=self.lore_db_path,
                    bot_name=reflect_bot.name,
                    bot_qq_id=reflect_bot.qq_id,
                    bot_aliases=reflect_bot.aliases,
                    bot_id=reflect_bot.db_id,
                    # v5 起没传 CatalogScope，自省检索书设一直是空的
                    catalog_scope=self.book_lore_catalog_scope if self.book_lore_index else None,
                    profile_lookup=lambda bot_id: self.bot_registry.get(bot_id),
                )
            except Exception as e:
                logger.warning(f"[WaveMemory] SelfReflectService init failed: {e}")
                _record_err("SelfReflect", e)
                self.self_reflect = None
        else:
            self.self_reflect = None

        # ─── BDI / 灵魂子系统实例化（修复 06-12 集体停摆：原代码仅有 hasattr 守卫调用，缺实例化）───
        soul_bot = experience_bot or reflect_bot
        soul_bot_id = soul_bot.db_id if soul_bot else ""
        # 信念引擎（提取在 consolidation 内触发，注入在 on_llm_request）
        try:
            if runtime_capability_enabled(self.runtime_mode, "belief", True) and self.tag_llm_provider_id and soul_bot_id:
                belief_llm = LLMFallbackClient(
                    context=self.context,
                    provider_ids=self._llm_chain(),
                    log_prefix="[BeliefEngine]",
                )
                self.belief_engine = BeliefEngine(
                    db=self.db,
                    llm_client=belief_llm,
                    bot_id=soul_bot_id,
                    soul_repository=self.db.soul_repository,
                )
            else:
                self.belief_engine = None
        except Exception as e:
            logger.warning(f"[WaveMemory] BeliefEngine init failed: {e}")
            _record_err("BeliefEngine", e)
            self.belief_engine = None
        try:
            self.belief_emergence = BeliefEmergenceService(db=self.db, bot_id=soul_bot_id) if runtime_capability_enabled(self.runtime_mode, "belief_emergence", True) and soul_bot_id else None
        except Exception as e:
            logger.warning(f"[WaveMemory] BeliefEmergence init failed: {e}")
            _record_err("BeliefEmergence", e)
            self.belief_emergence = None
        # 关切 / 情绪轨迹 / 时间锚点：memory_only/compat_only 下属于高级灵魂状态能力，默认不实例化。
        soul_repository = self.db.soul_repository
        soul_coordinator = self.write_gateway.coordinator
        try:
            self.concern_tracker = ConcernTracker(
                db=self.db,
                bot_id=soul_bot_id,
                repository=soul_repository,
                coordinator=soul_coordinator,
            ) if runtime_capability_enabled(self.runtime_mode, "concern", True) and soul_bot_id else None
        except Exception as e:
            logger.warning(f"[WaveMemory] ConcernTracker init failed: {e}")
            _record_err("ConcernTracker", e)
            self.concern_tracker = None
        try:
            self.mood_trajectory = MoodTrajectory(
                db=self.db,
                bot_id=soul_bot_id,
                repository=soul_repository,
                coordinator=soul_coordinator,
            ) if runtime_capability_enabled(self.runtime_mode, "mood_trajectory", True) and soul_bot_id else None
        except Exception as e:
            logger.warning(f"[WaveMemory] MoodTrajectory init failed: {e}")
            _record_err("MoodTrajectory", e)
            self.mood_trajectory = None
        try:
            self.subjective_time = SubjectiveTime(
                db=self.db,
                bot_id=soul_bot_id,
                repository=soul_repository,
                coordinator=soul_coordinator,
            ) if runtime_capability_enabled(self.runtime_mode, "subjective_time", True) and soul_bot_id else None
        except Exception as e:
            logger.warning(f"[WaveMemory] SubjectiveTime init failed: {e}")
            _record_err("SubjectiveTime", e)
            self.subjective_time = None
        # 欲望引擎（依赖信念引擎）
        try:
            self.desire_engine = DesireEngine(belief_engine=self.belief_engine, bot_id=soul_bot_id) if runtime_capability_enabled(self.runtime_mode, "desire", True) and soul_bot_id else None
        except Exception as e:
            logger.warning(f"[WaveMemory] DesireEngine init failed: {e}")
            _record_err("DesireEngine", e)
            self.desire_engine = None
        logger.info(
            f"[WaveMemory] 灵魂子系统就绪: belief={bool(self.belief_engine)} "
            f"concern={bool(self.concern_tracker)} mood_traj={bool(self.mood_trajectory)} "
            f"time_anchor={bool(self.subjective_time)} desire={bool(self.desire_engine)}"
        )

        # 新编排器影子链路：只写 trace，不改真实 ProviderRequest。
        self._setup_injection_shadow_pipeline()
        self._setup_runtime_context_preparer()
        self._setup_service_registry()

        # ─── 注册所有服务状态到健康面板（WebUI 可视化）───
        from ..utils.health_registry import register as _reg
        _reg("向量索引", "ok" if self.memory_index else "off", "" if self.memory_index else "memory_index 未初始化", dependency="Embedding Provider")
        _reg("Tag 索引", "ok" if self.tag_index else "off", "" if self.tag_index else "tag_index 未初始化", dependency="Embedding Provider")
        _reg("共现矩阵", "ok" if self.cooccurrence else "off", "" if self.cooccurrence else "cooccurrence 未加载", dependency="Tag 覆盖率 > 20%")
        _reg("脉冲传播", "ok" if self.spike_router else "off", "" if self.spike_router else "依赖共现矩阵", dependency="共现矩阵 + Tag 覆盖率 > 20%")
        _reg("残差金字塔", "ok" if self.residual_pyramid else "off", "" if self.residual_pyramid else "依赖共现矩阵", dependency="共现矩阵 + Embedding")
        _reg("测地线重排", "ok" if self.geodesic else "off", "" if self.geodesic else "依赖共现矩阵", dependency="共现矩阵节点 > 1000")
        _reg("Embedding", "ok" if self.embedding_service else "off", "" if self.embedding_service else "embedding_provider_id 未配置", dependency="AstrBot Provider 配置")
        _reg("Tag 提取", "ok" if self.tag_extractor else "off", "" if self.tag_extractor else "tag_llm_provider_id 未配置", dependency="Tag LLM Provider 配置")
        _reg("统一写协调器", "ok" if getattr(self, "write_gateway", None) else "degraded", "" if getattr(self, "write_gateway", None) else "WriteCoordinator 未接线", dependency="SQLite writer lease")
        _reg("EPA 基底", "ok" if (self.epa and self.epa.initialized) else "degraded", "" if (self.epa and self.epa.initialized) else f"需 ≥{self.epa.min_tags if self.epa else 20} 个 tag 向量", dependency="Tag 覆盖率 > 20%")
        _reg("MetaThinking", "ok" if getattr(self, 'meta_thinking', None) else "off", "" if getattr(self, 'meta_thinking', None) else "MetaThinking 配置缺失或初始化失败", dependency="MetaThinking_Settings.enabled + LLM Provider")
        _reg("做梦系统", "ok" if getattr(self, 'dream_service', None) else "off", "" if getattr(self, 'dream_service', None) else "enable_dream=false 或初始化失败", dependency="enable_dream=true")

        missing_bot_profile_reason = "未配置 Bot Profile（在 9876「Bot 管理」页添加 Bot）"
        self_reflect_off_reason = (
            "SelfReflect 未启用" if not runtime_capability_enabled(self.runtime_mode, "self_reflect", study_cfg.get("self_reflect_enabled", True))
            else "tag_llm_provider_id 未配置" if not self.tag_llm_provider_id
            else missing_bot_profile_reason if not reflect_bot
            else "SelfReflect 初始化失败或未启用"
        )
        soul_off_reason = (
            "tag_llm_provider_id 未配置" if not self.tag_llm_provider_id
            else missing_bot_profile_reason if not soul_bot_id
            else "belief_engine 初始化失败或未启用"
        )
        soul_state_off_reason = missing_bot_profile_reason if not soul_bot_id else "初始化失败或未启用"

        _reg("自省系统", "ok" if getattr(self, 'self_reflect', None) else "off", "" if getattr(self, 'self_reflect', None) else self_reflect_off_reason, dependency="LLM Provider + Bot Profile")
        _reg("记忆淘汰", "ok" if getattr(self, 'eviction_service', None) else "off", "" if getattr(self, 'eviction_service', None) else "Eviction 未启用", dependency="自动启用")
        _reg("信念引擎", "ok" if self.belief_engine else "off", "" if self.belief_engine else soul_off_reason, dependency="LLM Provider + Bot Profile")
        _reg("关切追踪", "ok" if self.concern_tracker else "off", "" if self.concern_tracker else f"concern_tracker {soul_state_off_reason}", dependency="Bot Profile")
        _reg("情绪轨迹", "ok" if self.mood_trajectory else "off", "" if self.mood_trajectory else f"mood_trajectory {soul_state_off_reason}", dependency="Bot Profile")
        _reg("黑话系统", "ok" if getattr(self, 'jargon_service', None) else "off", "" if getattr(self, 'jargon_service', None) else "Jargon 未启用", dependency="LLM Provider + 聊天记录积累")
        _reg("风格学习", "ok" if getattr(self, 'few_shot_service', None) else "off", "" if getattr(self, 'few_shot_service', None) else "FewShot 未启用", dependency="LLM Provider + bot 回复积累")

        # 启动 committed-outbox 派生泵；启动后会自动 replay 未完成 delivery。
        self._spawn(
            self.write_gateway.run_outbox_loop(),
            name="wave-memory:outbox-dispatcher",
            owner="outbox",
        )

        # 高频互动者缓存预热 (US-2.3) — 异步执行，不阻塞启动
        self._spawn(self._async_cache_warmup(), owner="cache")

        logger.info("[WaveMemory] Fully initialized")

    async def _terminate_once(self):
        """插件卸载时清理 — 各资源独立 try-except。"""
        try:
            if hasattr(self, "task_supervisor") and self.task_supervisor:
                await self.task_supervisor.close_accepting()
        except Exception as e:
            logger.debug(f"[WaveMemory] task supervisor ingress close error: {e}")

        try:
            if hasattr(self, "maintenance_job_runner") and self.maintenance_job_runner:
                self.maintenance_job_runner.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] maintenance job runner stop error: {e}")

        try:
            if hasattr(self, 'tag_worker') and self.tag_worker:
                self.tag_worker.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] tag_worker stop error: {e}")

        try:
            pending = getattr(self, "_pending_belief_tag_refresh", None)
            if isinstance(pending, dict):
                pending.clear()
            if hasattr(self, "task_supervisor") and self.task_supervisor:
                await self.task_supervisor.cancel(owner="belief", timeout=5.0)
        except Exception as e:
            logger.debug(f"[WaveMemory] belief tag refresh cancel error: {e}")

        try:
            if hasattr(self, 'dream_service') and self.dream_service:
                self.dream_service.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] dream_service stop error: {e}")

        try:
            if hasattr(self, 'eviction_service') and self.eviction_service:
                self.eviction_service.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] eviction_service stop error: {e}")

        try:
            if hasattr(self, 'lifecycle') and self.lifecycle:
                self.lifecycle.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] lifecycle stop error: {e}")

        try:
            if hasattr(self, 'tag_job') and self.tag_job:
                self.tag_job.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] tag_job stop error: {e}")

        try:
            if hasattr(self, 'webui') and self.webui:
                await self.webui.stop()
        except Exception as e:
            logger.debug(f"[WaveMemory] webui stop error: {e}")

        try:
            if hasattr(self, "writer") and self.writer:
                await self.writer.shutdown()
        except Exception as e:
            logger.debug(f"[WaveMemory] writer settle error: {e}")

        try:
            if hasattr(self, "task_supervisor") and self.task_supervisor:
                # Durable jobs may still be inside LLM/import handlers that use the
                # coordinator. Settle their cancellation before closing the gateway.
                await self.task_supervisor.settle(owner="durable-jobs", timeout=5.0)
        except Exception as e:
            logger.debug(f"[WaveMemory] durable job runner settle error: {e}")
            try:
                await self.task_supervisor.cancel(owner="durable-jobs", timeout=5.0)
            except Exception as cancel_error:
                logger.debug(f"[WaveMemory] durable job runner cancel error: {cancel_error}")

        try:
            if hasattr(self, "task_supervisor") and self.task_supervisor:
                # The outbox loop also dispatches through the coordinator; stop it
                # before the gateway closes its writer connection.
                await self.task_supervisor.cancel(owner="outbox", timeout=5.0)
        except Exception as e:
            logger.debug(f"[WaveMemory] outbox dispatcher settle error: {e}")

        try:
            if hasattr(self, "write_gateway") and self.write_gateway:
                # close_accepting → job/outbox settle → projection drain → lease release.
                await self.write_gateway.shutdown()
        except Exception as e:
            logger.debug(f"[WaveMemory] write_gateway shutdown error: {e}")

        try:
            if hasattr(self, "task_supervisor") and self.task_supervisor:
                await self.task_supervisor.settle(timeout=10.0)
        except Exception as e:
            logger.debug(f"[WaveMemory] task supervisor settle error: {e}")
            try:
                await self.task_supervisor.cancel(timeout=5.0)
            except Exception as cancel_error:
                logger.debug(f"[WaveMemory] task supervisor cancel error: {cancel_error}")

        try:
            self.db.close()
        except Exception as e:
            logger.debug(f"[WaveMemory] db close error: {e}")

        logger.info("[WaveMemory] Shutdown complete")

    async def _init_epa(self):
        """EPA 初始化（在线程池中执行，避免阻塞事件循环）。"""
        await asyncio.to_thread(self.epa.initialize)

    def _spawn(self, coro, *, name: str | None = None, owner: str = "plugin") -> asyncio.Task:
        """通过统一 supervisor 创建可观察、可等待的命名后台任务。"""
        self._task_sequence += 1
        task_name = name or f"wave-memory:{owner}:{self._task_sequence}"
        return self.task_supervisor.start(task_name, coro, owner=owner)

    def _llm_chain(self) -> list[str]:
        """构建统一的 LLM provider 回退链：Tag LLM 在前，配置的回退渠道在后。

        所有需要 LLM 的后台子系统共用同一条链，避免单渠道 503/402 让能力静默停产。
        """
        return build_provider_chain(
            self.tag_llm_provider_id,
            getattr(self, "llm_fallback_provider_ids", ""),
        )

    def _build_book_lore_catalog_scope(self) -> CatalogScope:
        """构造书设 CatalogScope；书设是独立知识库，不绑定群会话 RuntimeScope。"""
        study = self.config.get("Study_Settings", {}) or {}
        catalog_id = str(study.get("source_library_id") or "book-lore").strip() or "book-lore"
        corpus_id = str(study.get("source_corpus_id") or "default").strip() or "default"
        version = str(study.get("source_version") or "current").strip() or "current"
        return CatalogScope(catalog_id=catalog_id, corpus_id=corpus_id, version=version)

    async def _async_cache_warmup(self):
        """保留兼容任务入口，但不预热无 Scope 的 legacy persona。"""
        logger.debug("[WaveMemory] persona cache warmup withheld: scope_migration_required")

    async def _jargon_mine_task(self, runtime_scope: RuntimeScope) -> None:
        """手工/排障入口：不再由入站消息自动调度。"""
        if runtime_scope.visibility != "group" or runtime_scope.session is None:
            return
        try:
            results = await self.jargon_service.mine(runtime_scope)
            if results:
                logger.info(f"[WaveMemory] Jargon mined {len(results)} new in {runtime_scope.session.id}")
        except Exception as e:
            logger.debug(f"[WaveMemory] Jargon mine error: {e}")
            _record_err("JargonMine", e)

    async def _belief_emergence_task(self, runtime_scope: RuntimeScope | None = None) -> None:
        """手工/排障入口：不再由入站消息自动调度。"""
        try:
            if not getattr(self, "belief_emergence", None):
                return
            created = await self.belief_emergence.emerge_recent(days=14, limit=2, scope=runtime_scope)
            if created:
                logger.info(f"[WaveMemory] Belief emerged {len(created)} candidates")
        except Exception as e:
            logger.debug(f"[WaveMemory] Belief emergence error: {e}")
            _record_err("BeliefEmergence", e)
