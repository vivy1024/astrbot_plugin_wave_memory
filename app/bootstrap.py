"""构造期组装：存储、检索、写入、注入通道、Bot 注册表与各注册表（从 main.py 拆出的 WaveMemoryPlugin mixin）。"""

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


class BootstrapMixin:
    def _construct(self, context, config) -> None:
        self.context = context
        self.config = config or {}
        self._terminated = False
        self._bot_qq_ids: list[str] = []
        self._group_names: dict[tuple[str, str], str] = {}

        # Bot identity 只能来自显式配置；缺失配置时 Scope 解析与相关能力失败关闭。
        # 构造阶段数据库还没打开，先用静态配置的旧槽位；数据库就绪后 attach() 切到 bot_profiles 表。
        self.bot_registry = BotRegistry(self.config)
        # 入站消息接收/被拒统计：拒绝过多时 /api/health 与 9876 首页报警（换号导致记忆静默丢失的教训）
        from ..services.ingress_health import IngressHealth

        self.ingress_health = IngressHealth()
        self._bot_registry = self.bot_registry.profiles
        self._bot_qq_ids = list(self._bot_registry)

        # Scope API 允许与主插件分切片落地：模块缺失时启动不崩溃，消息入口严格 fail closed。
        self.scope_resolver = None
        self._scope_resolution_failed_total: dict[str, int] = {}
        self._scope_resolution_last_warning: dict[str, float] = {}

        # 解析配置（顶层字段 + 嵌套 object）
        query_cfg = self.config.get("Query_Settings", {})
        self.tag_cfg = tag_cfg = self.config.get("Tag_Settings", {})
        storage_cfg = self.config.get("Storage_Settings", {})
        memory_index_cfg = self.config.get("Memory_Index_Settings", {}) or {}
        memory_budget_cfg = self.config.get("Memory_Budget_Settings", {}) or {}
        webui_cfg = self.config.get("WebUI_Settings", {})
        runtime_cfg = self.config.get("Runtime_Settings", {})
        social_cfg = self.config.get("Social_Settings", {})
        inject_cfg = self.config.get("Inject_Settings", {})
        filter_cfg = self.config.get("Message_Filter", {})
        perf_cfg = self.config.get("Performance_Settings", {})
        lifecycle_cfg = self.config.get("Lifecycle_Settings", {})
        cross_group_cfg = self.config.get("Cross_Group_Settings", {})
        affinity_cfg = self.config.get("Affinity_Settings", {})
        compat_cfg = self.config.get("Compatibility_Settings", {})
        trace_cfg = self.config.get("Trace_Settings", {}) or {}

        # 运行模式：旧配置缺失 Runtime_Settings 时默认 full，保持历史完整行为。
        self.runtime_mode = resolve_runtime_mode(self.config)
        self.runtime_mode_name = self.runtime_mode.mode
        self.runtime_cfg = runtime_cfg

        self.embedding_provider_id = self.config.get("embedding_provider_id", "")
        self.dimension = int(self.config.get("embedding_dimension", 1024))
        self.tag_llm_provider_id = self.config.get("tag_llm_provider_id", "")
        # 所有 LLM 子系统共用的回退链；单渠道 503/402 不应让后台能力整体停产。
        self.llm_fallback_provider_ids = self.config.get("llm_fallback_provider_ids", "")
        self.tag_extraction_enabled = tag_cfg.get("tag_extraction_enabled", True)
        self.max_tags = int(tag_cfg.get("max_tags_per_message", 10))
        self.enable_auto_inject = effective_native_injection_enabled(
            query_cfg,
            self.runtime_mode,
            compat_cfg=compat_cfg,
        )
        self.inject_top_k = int(query_cfg.get("inject_top_k", 5))
        self.min_similarity = float(query_cfg.get("min_similarity", "0.35"))
        self.injection_format = query_cfg.get("injection_format", "[记忆] {sender}({time}): {content}")
        # v2.0: inject 控制参数
        self.skip_recent_minutes = int(inject_cfg.get("skip_recent_minutes", 30))
        self.facts_max = int(inject_cfg.get("facts_max", 5))
        self.enable_spike = effective_query_feature(query_cfg, "enable_spike_routing", self.runtime_mode)
        self.enable_pyramid = effective_query_feature(query_cfg, "enable_residual_pyramid", self.runtime_mode)
        self.enable_epa = effective_query_feature(query_cfg, "enable_epa", self.runtime_mode)
        self.enable_geodesic = effective_query_feature(query_cfg, "enable_geodesic_rerank", self.runtime_mode)
        self.enable_shotgun = query_cfg.get("enable_shotgun", False)

        def _bounded_index_int(key: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(float(memory_index_cfg.get(key, default))))
            except (TypeError, ValueError):
                return default

        # Resident memory budget: one operator-facing profile derives hard caps for
        # every resident vector tier.  hnswlib preallocates for max_elements, so
        # rebuild cadence alone cannot bound the steady resident set.
        from ..services.memory_budget_policy import MemoryBudgetPolicy

        self.memory_budget_policy = MemoryBudgetPolicy.from_settings(memory_budget_cfg)

        # Scope remains an access boundary; its hot-tier quota is opt-in.
        self.memory_index_policy = memory_index_policy_from_settings(memory_index_cfg)
        if self.memory_budget_policy.enabled:
            budgeted_hot = self.memory_budget_policy.bounded_capacity(
                "hot_memory",
                self.memory_index_policy.max_vectors,
                self.dimension,
            )
            if budgeted_hot != self.memory_index_policy.max_vectors:
                from dataclasses import replace as _dataclass_replace

                logger.info(
                    "[WaveMemory] memory budget %s clamps hot_max_vectors %s -> %s",
                    self.memory_budget_policy.profile,
                    self.memory_index_policy.max_vectors,
                    budgeted_hot,
                )
                self.memory_index_policy = _dataclass_replace(
                    self.memory_index_policy,
                    max_vectors=budgeted_hot,
                    scoped_reserved_vectors=min(
                        self.memory_index_policy.scoped_reserved_vectors, budgeted_hot
                    ),
                )
        raw_cold_recall = memory_index_cfg.get("cold_recall_enabled")
        if raw_cold_recall is None:
            self.cold_recall_enabled = True
        elif isinstance(raw_cold_recall, str):
            self.cold_recall_enabled = raw_cold_recall.strip().casefold() in {"1", "true", "yes", "on"}
        else:
            self.cold_recall_enabled = bool(raw_cold_recall)
        self.tag_index_max_vectors = self.memory_budget_policy.bounded_capacity(
            "tag_catalog",
            _bounded_index_int("tag_index_max_vectors", 40_000, 1),
            self.dimension,
        )

        def _index_bool(key: str, default: bool) -> bool:
            """Explicit false must survive; a missing key keeps the default."""
            if key not in memory_index_cfg:
                return default
            value = memory_index_cfg.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().casefold()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off", ""}:
                    return False
                return default
            if isinstance(value, (int, float)):
                return bool(value)
            return default

        # Immutable HNSW generations retain the active manifest target.
        # Set to 1 by default to release old generation files promptly and reduce memory churn.
        self.index_generation_retention = _bounded_index_int("generation_retention", 1, 1)
        self.injection_orchestrator_active_enabled = inject_cfg.get("orchestrator_active_enabled", True)
        self.injection_shadow_enabled = inject_cfg.get("orchestrator_shadow_enabled", not self.injection_orchestrator_active_enabled)
        self.livingmemory_alias_tools_enabled = bool(compat_cfg.get("livingmemory_alias_tools_enabled", False))

        def _trace_int(key: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(float(trace_cfg.get(key, default))))
            except (TypeError, ValueError):
                return default

        self.injection_trace_retention_days = _trace_int("retention_days", 14, 1)
        self.injection_trace_max_rows = _trace_int("max_rows", 5000, 100)
        self.injection_trace_max_preview_chars = _trace_int("max_preview_chars", 1200, 120)
        try:
            from ..services.config.channel_config import build_channel_config_from_plugin_config
            self.injection_channel_config = build_channel_config_from_plugin_config(self.config)
        except Exception as e:
            logger.warning(f"[WaveMemory] injection channel config init failed: {e}")
            self.injection_channel_config = None

        # ─── 配置自愈：核心开关被关则强制恢复 ───
        # 根因：AstrBot 配置页保存是全量覆盖，未渲染的 bool 字段写 False。
        # compat_only 默认不主动注入；full/memory_only 则保留“纯记忆可用”的自愈行为。
        if not self.enable_auto_inject:
            if self.runtime_mode.native_injection_default:
                logger.warning("[WaveMemory] 🔧 enable_auto_inject=False，强制恢复（AstrBot 配置覆盖 bug）")
                self.enable_auto_inject = True
            else:
                logger.info("[WaveMemory] 运行模式 compat_only：原生自动注入保持关闭")
        # 高级检索全关在 full 中视为损坏；memory_only/compat_only 中是合法默认状态。
        if not any([self.enable_spike, self.enable_pyramid, self.enable_epa, self.enable_geodesic]):
            if should_self_heal_advanced_query(self.runtime_mode):
                logger.warning("[WaveMemory] 🔧 高级检索全部关闭，强制恢复")
                self.enable_spike = True
                self.enable_pyramid = True
                self.enable_epa = True
                self.enable_geodesic = True
            else:
                logger.info(f"[WaveMemory] 运行模式 {self.runtime_mode.mode}：高级检索默认关闭")
        # 持久化修复到 config.json
        _need_fix = False
        try:
            import json as _json
            config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_wave_memory_config.json")
            if os.path.isfile(config_path):
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    raw_cfg = _json.load(f)
                qs = raw_cfg.get("Query_Settings", {})
                if qs.get("enable_auto_inject") is False and self.runtime_mode.native_injection_default:
                    qs["enable_auto_inject"] = True
                    _need_fix = True
                if should_self_heal_advanced_query(self.runtime_mode):
                    for _k in ["enable_spike_routing", "enable_residual_pyramid", "enable_epa", "enable_geodesic_rerank"]:
                        if qs.get(_k) is False:
                            qs[_k] = True
                            _need_fix = True
                if _need_fix:
                    raw_cfg["Query_Settings"] = qs
                    with open(config_path, "w", encoding="utf-8") as f:
                        _json.dump(raw_cfg, f, ensure_ascii=False, indent=2)
                    logger.info("[WaveMemory] ✅ 配置自愈完成，已写回 config.json")
        except Exception as e:
            logger.debug(f"[WaveMemory] 配置自愈写回跳过: {e}")

        disabled_caps = ", ".join(self.runtime_mode.disabled_capabilities) if self.runtime_mode.disabled_capabilities else "无"
        logger.info(
            f"[WaveMemory] 运行模式: {self.runtime_mode.mode} ({self.runtime_mode.label})；"
            f"禁用高级能力: {disabled_caps}"
        )
        # Storage_Settings.max_memories is the canonical SQLite soft cap (new writes).
        # Hot HNSW capacity remains owned by Memory_Index_Settings.hot_max_vectors.
        from ..services.storage_capacity_policy import StorageCapacityPolicy

        self.storage_capacity_policy = StorageCapacityPolicy.from_settings(storage_cfg)
        self.legacy_max_memories = int(self.storage_capacity_policy.max_active_memories)
        self.max_memories = self.memory_index_policy.max_vectors

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

        # 跨群记忆配置：显式 False 必须覆盖缺失项的兼容默认 True。
        self.cross_group_enabled = _parse_bool_config_value(
            cross_group_cfg.get("cross_group_enabled"),
            True,
        )
        # 共享只读 grant：默认 False；缺失与显式 False 均保持关闭（不沿用跨群默认 True）。
        self.shared_memory_grants_enabled = _parse_bool_config_value(
            cross_group_cfg.get("shared_memory_grants_enabled"),
            False,
        )
        # 好感度引擎配置
        self.affinity_cfg = affinity_cfg

        # 生命周期配置：memory_only/compat_only 强制关闭高级社交/人格/情绪能力，避免旧 default=true 穿透模式边界。
        self.enable_affinity = runtime_capability_enabled(self.runtime_mode, "affinity", lifecycle_cfg.get("enable_affinity", True))
        self.enable_mood = runtime_capability_enabled(self.runtime_mode, "mood", lifecycle_cfg.get("enable_mood", True))
        self.mood_duration_hours = float(lifecycle_cfg.get("mood_duration_hours", "2.0"))
        self.mood_msg_threshold = int(lifecycle_cfg.get("mood_msg_threshold", 30))
        self.positive_emotion_threshold = float(lifecycle_cfg.get("positive_emotion_threshold", "0.6"))
        self.negative_emotion_threshold = float(lifecycle_cfg.get("negative_emotion_threshold", "0.4"))
        self.enable_dream = runtime_capability_enabled(self.runtime_mode, "dream", lifecycle_cfg.get("enable_dream", True))
        self.dream_interval_hours = float(lifecycle_cfg.get("dream_interval_hours", "6.0"))
        self.dream_recent_seeds = int(lifecycle_cfg.get("dream_recent_seeds", 3))
        self.dream_recent_k = int(lifecycle_cfg.get("dream_recent_k", 5))
        self.dream_mid_seeds = int(lifecycle_cfg.get("dream_mid_seeds", 2))
        self.dream_mid_k = int(lifecycle_cfg.get("dream_mid_k", 3))
        # Consolidation（后台自动巩固 / 盲抽升格为事实与信念）已按架构决定永久停用：
        # 认知只能由模型现场基于证据提审，再经人工转正。configured 固定 False，使任何
        # 运行模式或遗留配置都无法重新打开它，同时保留能力 gate 以维持运行模式契约。
        self.enable_consolidation = runtime_capability_enabled(self.runtime_mode, "consolidation", False)

        # 初始化数据目录
        data_path = get_astrbot_data_path() or os.path.dirname(os.path.dirname(__file__))
        self.data_dir = os.path.join(data_path, "plugin_data", "astrbot_plugin_wave_memory")
        os.makedirs(self.data_dir, exist_ok=True)

        # 数据库备份生命周期安全管理（由 DatabaseBackupManager 统一负责，零阻塞主线程）
        self.backup_manager = DatabaseBackupManager(self.data_dir, config=self.config)
        self.backup_manager.run_backup_safe()

        # 初始化核心组件
        db_path = os.path.join(self.data_dir, "wave_memory.db")
        index_path = os.path.join(self.data_dir, "memory.hnsw")
        tag_catalog_index_path = os.path.join(self.data_dir, "tag_catalog.hnsw")

        self.db = WaveMemoryDB(db_path, dimension=self.dimension)
        try:
            self.bot_registry.attach(self.db.bot_profiles, connection=self.db.conn)
        except Exception as exc:
            # 表损坏等异常不阻止启动：继续用静态配置里的 Bot，9876 Bot 页会显示未接入。
            logger.error(f"[WaveMemory] Bot 注册表接入数据库失败，暂用静态配置: {exc}")
            _record_err("BotRegistry", exc)
        self._bot_qq_ids[:] = list(self._bot_registry)
        if getattr(self.db, "soul_repository", None):
            self.db.soul_repository.manual_adjustment_delta_cap = _positive_float(
                affinity_cfg.get("manual_adjustment_delta_cap"), 20.0
            )
        self.data_governance_jobs = DataGovernancePreviewJobs(
            source_db_path=self.db.db_path,
            snapshot_dir=os.path.join(self.data_dir, "data_governance_snapshots"),
        )
        self.scope_recovery_jobs = build_scope_recovery_handlers(
            source_db_path=self.db.db_path,
            snapshot_dir=os.path.join(self.data_dir, "scope_recovery_snapshots"),
        )
        # facts 时间衰减配置
        self._facts_decay_rate = float(storage_cfg.get("facts_decay_rate", "0.005"))
        self.db.set_facts_decay_rate(self._facts_decay_rate)

        self.memory_index = VectorIndex(
            dimension=self.dimension,
            max_elements=self.memory_index_policy.max_vectors,
            index_path=index_path,
            kind="memory",
            strict_manifest=False,
            allow_resize=True,
            generation_retention=self.index_generation_retention,
        )

        self.tag_catalog_index = VectorIndex(
            dimension=self.dimension,
            max_elements=self.tag_index_max_vectors,
            index_path=tag_catalog_index_path,
            kind="tag_catalog",
            strict_manifest=False,
            allow_resize=True,
            generation_retention=self.index_generation_retention,
        )
        # Existing services/WebUI use ``tag_index`` for formal semantic Catalog
        # behavior. Keep that compatibility alias intentionally Catalog-only.
        self.tag_index = self.tag_catalog_index

        # DB facade 不再持有可写索引引用；索引只消费 committed outbox。
        self.db.memory_index = None

        self.embedding_service = EmbeddingService(
            context=context,
            provider_id=self.embedding_provider_id,
            dimension=self.dimension,
        )

        # PairSimilarityService
        self.pair_sim_service = PairSimilarityService(db=self.db)

        # 语义增益配置
        self.semantic_gain_config = SemanticGainConfig()

        # 共现矩阵（有向序位 + 语义增益）
        self.intrinsic_residual = None  # 先声明，后面初始化
        residual_map = {}

        # Bound retained neighbours per Tag: the graph is rebuilt in full and held
        # resident, so an unbounded neighbour width is a steady-memory risk.
        self.cooccurrence_max_neighbors = _bounded_index_int(
            "cooccurrence_max_neighbors_per_tag",
            DEFAULT_MAX_NEIGHBORS_PER_TAG,
            1,
        )
        self.cooccurrence = DirectedCooccurrence(
            self.db,
            pair_sim_service=self.pair_sim_service,
            residual_map=residual_map,
            semantic_gain_config=self.semantic_gain_config,
            max_neighbors_per_tag=self.cooccurrence_max_neighbors,
        )

        self.intrinsic_residual = IntrinsicResidualCalculator(
            db=self.db, cooccurrence=self.cooccurrence
        )
        # 加载已有残差
        residual_map = self.intrinsic_residual.load()
        self.cooccurrence.residual_map = residual_map

        self.cooccurrence_scheduler = CooccurrenceScheduler(
            cooccurrence=self.cooccurrence,
            threshold_pct=DEFAULT_REBUILD_THRESHOLD_PCT,
            cooldown_sec=DEFAULT_REBUILD_COOLDOWN_SEC,
            on_rebuild_complete=self._on_cooccurrence_rebuilt,
        )

        # 脉冲传播
        self.spike_router = SpikeRouter(
            self.cooccurrence,
            residual_map=residual_map,
        ) if self.enable_spike else None

        # 残差金字塔（传 db）
        self.residual_pyramid = ResidualPyramid(self.tag_catalog_index, db=self.db) if self.enable_pyramid else None

        # EPA
        self.epa = EPAModule(self.db) if self.enable_epa else None

        # 测地线重排
        self.geodesic = GeodesicReranker(self.db) if self.enable_geodesic else None

        # 书设知识索引：memory_only/compat_only 默认关闭 BookLore，避免加载世界观/小说知识能力。
        # 书设是独立 Catalog 知识库（直读 book_lore.db），不是 Learning reviewed projection。
        self.enable_book_lore = runtime_capability_enabled(self.runtime_mode, "book_lore", True)
        self.lore_db_path = os.path.join(self.data_dir, "book_lore.db")
        self.book_lore_catalog_scope = self._build_book_lore_catalog_scope()
        if self.enable_book_lore:
            try:
                from ..engine.book_lore_index import (
                    DEFAULT_COMMUNITY_MAX_ELEMENTS,
                    DEFAULT_ENTITY_MAX_ELEMENTS,
                    DEFAULT_NOTES_MAX_ELEMENTS,
                )

                # 书设按每天一两章持续增长，因此不设条数上限：写入永不被拒绝。
                # 内存控制来自“启动时贴合已有语料 + 之后小步扩容”，而不是
                # 预分配大块空槽，也不由内存预算裁剪其容量。
                self.book_lore_index = BookLoreIndex(
                    dimension=self.dimension,
                    data_dir=self.data_dir,
                    max_elements=DEFAULT_ENTITY_MAX_ELEMENTS,
                    community_max_elements=DEFAULT_COMMUNITY_MAX_ELEMENTS,
                    notes_max_elements=DEFAULT_NOTES_MAX_ELEMENTS,
                )
                self.book_lore_index.load_id_maps()
            except Exception as e:
                logger.debug(f"[WaveMemory] BookLoreIndex init skipped: {e}")
                self.book_lore_index = None
        else:
            self.book_lore_index = None

        # 热配置
        self.hot_config = HotConfig(initial_config={
            "spike": {"firing_threshold": 0.10, "base_decay": 0.25, "wormhole_decay": 0.70,
                      "tension_threshold": 1.0, "max_hops": 4},
            "query": {"min_similarity": self.min_similarity, "boost_alpha_base": 0.3,
                      "group_weight_current": float(social_cfg.get("group_weight_current", 1.5)),
                      "group_weight_cross": float(social_cfg.get("group_weight_cross", 0.8))},
            "geodesic": {"energy_weight": 0.3},
            "residual": {"boost_range": 0.6},
            "social": {
                "abuse_trigger_count": int(social_cfg.get("abuse_trigger_count", 3)),
                "abuse_cooldown_base": int(social_cfg.get("abuse_cooldown_base", 600)),
                "abuse_cooldown_max": int(social_cfg.get("abuse_cooldown_max", 3600)),
                "aba_window_seconds": int(social_cfg.get("aba_window_seconds", 30)),
            },
        })
        configure_social_limits(
            step_cap=_positive_float(social_cfg.get("affinity_step_cap"), 2.0),
            hostility_step_cap=_positive_float(social_cfg.get("affinity_hostility_step_cap"), 3.0),
            impact_cap=_positive_float(social_cfg.get("impact_cap"), 5.0),
        )
        # 9876 保存的热参数覆盖（数据库持久化），叠加在静态配置之上。
        self._apply_persisted_hot_overrides()
        self.hot_config.on_change(self._on_core_hot_config_change)
        if self.spike_router:
            self.hot_config.on_change(self.spike_router.on_config_change)

        # 仍需独立连接的 legacy v2.1 清理必须在唯一 writer lease 获取前完成。
        self._run_pre_writer_migrations()

        # Stage 1/3 生产写入口：领域真相由单 writer 提交，派生状态只消费 committed outbox。
        self.memory_index_projection = MemoryIndexProjection(
            self.db.db_path,
            self.memory_index,
            policy=self.memory_index_policy,
        )
        self.tag_index_projection = TagIndexProjection(self.tag_catalog_index, database_path=self.db.db_path)
        self.cooccurrence_projection = CooccurrenceProjection(
            self.cooccurrence,
            scheduler=self.cooccurrence_scheduler,
        )
        self.runtime_refresh_projection = RuntimeRefreshProjection(
            callbacks={"memory": self._on_memory_projection_refresh}
        )
        from ..services.derived_projections import FtsCjkProjection

        self.fts_cjk_projection = FtsCjkProjection(self.db.db_path)
        self.write_gateway = ProductionWriteGateway(
            self.db.db_path,
            consumers={
                self.memory_index_projection.consumer_name: self.memory_index_projection,
                self.tag_index_projection.consumer_name: self.tag_index_projection,
                self.cooccurrence_projection.consumer_name: self.cooccurrence_projection,
                self.runtime_refresh_projection.consumer_name: self.runtime_refresh_projection,
                self.fts_cjk_projection.consumer_name: self.fts_cjk_projection,
            },
        )
        # 三个关系变化上限从配置读取：schema 里一直有这三个键，
        # 但此前构造时没传，导致界面可调却永远走默认值 5/15/8。
        self.relationship_service = RelationshipEventService(
            self.db.conn,
            single_delta_cap=_positive_float(lifecycle_cfg.get("relationship_single_delta_cap"), 5.0),
            daily_delta_cap=_positive_float(lifecycle_cfg.get("relationship_daily_delta_cap"), 15.0),
            hostility_delta_cap=_positive_float(lifecycle_cfg.get("relationship_hostility_delta_cap"), 8.0),
            repository=self.db.soul_repository,
            coordinator=self.write_gateway.coordinator,
        )
        self.scoped_projection_writer = CoordinatorScopedProjectionWriter(
            self.write_gateway.coordinator,
            fewshot_repository=self.db.fewshot_repository,
        )
        self.quality_gate = QualityGate(repository=self.write_gateway.quality_repository)

        # 查询引擎
        self.query_engine = QueryEngine(
            db=self.db,
            memory_index=self.memory_index,
            embedding_service=self.embedding_service,
            config={
                **query_cfg,
                "cross_group_enabled": self.cross_group_enabled,
                "shared_memory_grants_enabled": self.shared_memory_grants_enabled,
                "cold_recall_enabled": self.cold_recall_enabled,
                "cold_candidate_limit": self.memory_index_policy.candidate_limit,
            },
            tag_index=self.tag_catalog_index,
            tag_catalog_index=self.tag_catalog_index,
            cooccurrence=self.cooccurrence,
            spike_router=self.spike_router,
            residual_pyramid=self.residual_pyramid,
            epa=self.epa,
            geodesic=self.geodesic,
            write_gateway=self.write_gateway,
        )

        # Tag 提取器
        self.tag_extractor = None
        if self.tag_extraction_enabled and self.tag_llm_provider_id:
            self.tag_extractor = TagExtractor(
                context=context,
                provider_id=self.tag_llm_provider_id,
                max_tags=self.max_tags,
                blacklist=tag_cfg.get("tag_blacklist", ""),
                db=self.db,
                embedding_service=self.embedding_service,
                tag_index=self.tag_catalog_index,
                provider_fallback_ids=self.llm_fallback_provider_ids,
            )

        # 异步写入器（带 source 分层门控）
        # 收集所有 bot 的关键词用于 classify_source
        all_bot_keywords = set()
        for profile in self._bot_registry.values():
            all_bot_keywords.update(profile.all_keywords)

        self.writer = MessageWriter(
            db=self.db,
            memory_index=self.memory_index,
            embedding_service=self.embedding_service,
            bot_keywords=all_bot_keywords,
            noise_max_length=int(self.config.get("Eviction_Settings", {}).get("noise_max_length", 10)),
            quality_gate=self.quality_gate,
            write_gateway=self.write_gateway,
            on_vector_backfill_requested=self._on_vector_backfill_requested,
            storage_capacity_policy=self.storage_capacity_policy,
        )

        # LivingMemory-compatible surface（兼容已有记忆生态，不伪装插件名）
        livingmemory_surface = build_livingmemory_compat_surface(
            query_engine=self.query_engine,
            writer=self.writer,
        )
        self.memory_engine = livingmemory_surface.memory_engine
        self.initializer = livingmemory_surface.initializer
        self.livingmemory_compat_enabled = True
        self.detected_memory_plugins = detect_memory_plugins(context=self.context)
        for warning in build_duplicate_memory_warnings(self.detected_memory_plugins):
            logger.warning(f"[WaveMemory] {warning['message']} plugin={warning['plugin_id']} name={warning['name']}")

        # TagWorker（匀速后台标签提取）
        self.tag_worker = None
        tag_worker_cfg = self.config.get("TagWorker_Settings", {})
        worker_enabled = _parse_bool_config_value(tag_worker_cfg.get("worker_enabled"), True)
        if self.tag_extractor and worker_enabled:
            self.tag_worker = TagWorker(
                db=self.db,
                tag_extractor=self.tag_extractor,
                embedding_service=self.embedding_service,
                tag_index=self.tag_index,
                config=tag_worker_cfg,
                bot_keywords=all_bot_keywords,
                write_gateway=self.write_gateway,
            )
            # Cooccurrence refresh is driven by committed memory.tags_applied outbox events.
            self.tag_worker.on_tags_written = None

        # 所有插件级后台协程统一交由命名 TaskSupervisor 追踪。
        self.task_supervisor = TaskSupervisor()
        self._task_sequence = 0
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        maintenance_handlers = {
            "maintenance.memory_index.rebuild": self._maintenance_rebuild_memory_index,
            "maintenance.tag_index.rebuild": self._maintenance_rebuild_tag_index,
            "maintenance.cooccurrence.rebuild": self._maintenance_rebuild_cooccurrence,
            "maintenance.pair_similarity.rebuild": self._maintenance_rebuild_pair_similarity,
            "maintenance.vector_backfill.run": self._maintenance_run_vector_backfill,
            "maintenance.tag_audit.run": self._maintenance_run_tag_audit,
            "maintenance.tag_backfill.run": self._maintenance_run_tag_backfill,
            "maintenance.import.run": self._maintenance_run_import,
            "maintenance.fts_cjk.rebuild": self._maintenance_rebuild_fts_cjk,
        }
        maintenance_handlers.update(self.data_governance_jobs.handlers())
        maintenance_handlers.update(self.scope_recovery_jobs)
        # WebUI 的记忆重嵌入 / 批量打标端点入队这些 kind；缺少注册会让作业以
        # job_handler_missing 失败（DurableJobRunner 找不到 handler 即 mark_failed）。
        from ..services.memory_jobs import MemoryDurableJobHandlers

        maintenance_handlers.update(
            MemoryDurableJobHandlers(
                write_gateway=self.write_gateway,
                db=self.db,
                embedding_service=self.embedding_service,
                tag_extractor=self.tag_extractor,
            ).handlers()
        )
        self.maintenance_job_runner = DurableJobRunner(
            self.write_gateway.jobs,
            maintenance_handlers,
        )

        # 服务占位（initialize 中实际创建，防止消息先到时 AttributeError）
        self.jargon_service = None
        self.few_shot_service = None
        self.meta_thinking = None
        self.dream_service = None
        self.self_reflect = None
        self.consolidation = None
        self.eviction_service = None
        self.belief_engine = None
        self.belief_emergence = None
        self.reflection_trigger = ReflectionTriggerService(self.db)
        self._belief_tag_refresh_delay_seconds = 0.4
        self._pending_belief_tag_refresh: dict[tuple[str, str, str, int], asyncio.Task] = {}
        self.concern_tracker = None
        self.mood_trajectory = None
        self.subjective_time = None
        self.desire_engine = None
        self.lifecycle = None
        self.webui = None
        self.injection_trace_store = None
        self.injection_shadow_channels = []
        self._terminated = False

        self.inbound_pipeline = InboundMessagePipeline(self)
        self.platform_context_manager = PlatformContextManager(self.context, self._bot_registry)

        logger.info(
            f"[WaveMemory] Init: {self.db.get_memory_count()} memories, "
            f"{self.db.get_tag_count()} tags, "
            f"dim={self.dimension}, "
            f"spike={self.enable_spike}, pyramid={self.enable_pyramid}, "
            f"epa={self.enable_epa}, geodesic={self.enable_geodesic}"
        )

    def _run_pre_writer_migrations(self) -> None:
        """Run legacy path-based migrations before WriteCoordinator owns the lease."""
        from pathlib import Path

        migration_marker = Path(self.data_dir) / ".v2_1_migrated"
        if migration_marker.exists():
            return
        try:
            from ..engine.db.migrations.v2_1_cleanup import run_migration

            bot_ids_for_migration = {
                "qq_ids": [p.qq_id for p in self._bot_registry.values() if p.qq_id],
                "db_ids": [p.db_id for p in self._bot_registry.values() if p.db_id],
                "names": [p.name for p in self._bot_registry.values() if p.name],
            }
            success = run_migration(self.db.db_path, bot_ids_for_migration)
            if success:
                migration_marker.touch()
                logger.info("[WaveMemory] v2.1 migration completed, marker created")
        except Exception as exc:
            # Retried on the next process initialization, still before lease acquisition.
            logger.warning(f"[WaveMemory] v2.1 migration failed (non-fatal): {exc}")

    def _rebuild_scope_resolver(self) -> None:
        """按当前 Bot 注册表重建 ScopeResolver；失败时保持 fail closed。"""
        try:
            from ..services.scopes import ScopeResolver, bindings_from_profiles

            bindings = bindings_from_profiles(self.bot_registry.all())
            self.scope_resolver = ScopeResolver(bindings)
            logger.info(f"[WaveMemory] ScopeResolver initialized: {len(bindings)} bot bindings")
        except Exception as exc:
            self.scope_resolver = None
            logger.warning(f"[WaveMemory] ScopeResolver unavailable; message ingress fail closed: {exc}")
            _record_err("ScopeResolution", "scope_resolver_unavailable")

    def _meta_thinking_bot_maps(self) -> dict:
        profiles = list(self._bot_registry.values())
        interests: list[str] = []
        for profile in profiles:
            interests.extend(profile.all_keywords)
        return {
            "bot_qq_ids": [p.qq_id for p in profiles if p.qq_id],
            "bot_prompts": {p.qq_id: p.meta_prompt for p in profiles if p.meta_prompt},
            "bot_names": {p.qq_id: p.name for p in profiles},
            "bot_db_ids": {p.qq_id: p.db_id for p in profiles},
            "extra_interests": list(dict.fromkeys(interests)),
        }

    def _bot_keywords(self) -> set[str]:
        keywords: set[str] = set()
        for profile in self._bot_registry.values():
            keywords.update(profile.all_keywords)
        return keywords

    def _on_bot_registry_changed(self, registry) -> None:
        """Bot 在 9876 上被新增、修改或停用后，原地刷新各模块持有的身份信息。"""
        self._bot_qq_ids[:] = list(self._bot_registry)
        self._rebuild_scope_resolver()
        keywords = self._bot_keywords()
        for holder in (getattr(self, "writer", None), getattr(self, "tag_worker", None)):
            if holder is not None and hasattr(holder, "bot_keywords"):
                holder.bot_keywords = set(keywords)
        meta = getattr(self, "meta_thinking", None)
        if meta is not None and hasattr(meta, "update_bots"):
            meta.update_bots(**self._meta_thinking_bot_maps())
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None and hasattr(lifecycle, "sync_bot_identities"):
            lifecycle.sync_bot_identities({p.db_id: p.qq_id for p in registry.all() if p.db_id})
        for tool in getattr(self, "_bot_aware_tools", ()):
            if hasattr(tool, "bot_db_ids"):
                tool.bot_db_ids = {p.qq_id: p.db_id for p in self._bot_registry.values()}
        logger.info(f"[WaveMemory] Bot 注册表已热重载: {len(registry.all())} 个启用的 Bot")

    def _setup_injection_shadow_pipeline(self) -> None:
        """初始化新注入编排器通道链；失败不影响旧 inject_memory fallback。"""
        from ..webui.container import get_container

        if not getattr(self, "injection_shadow_enabled", True) and not getattr(self, "injection_orchestrator_active_enabled", False):
            logger.info("[WaveMemory] Injection orchestrator disabled")
            return
        if not getattr(self, "injection_channel_config", None):
            logger.warning("[WaveMemory] Injection orchestrator shadow skipped: channel config unavailable")
            return
        try:
            from ..services.injection.trace_store import InjectionTraceStore

            self.injection_trace_store = InjectionTraceStore(
                self.db.conn,
                max_preview_chars=getattr(self, "injection_trace_max_preview_chars", 1200),
                retention_days=getattr(self, "injection_trace_retention_days", 14),
                max_rows=getattr(self, "injection_trace_max_rows", 5000),
                cleanup_on_record=True,
            )
            self.injection_trace_store.ensure_schema()
            # 通道定义在 services/injection/channel_registry.py（外部扩展经 extensions/ 登记）。
            self.injection_shadow_channels = self._channel_registry().build(self)
            get_container().injection_channels = list(self.injection_shadow_channels)
            logger.info(f"[WaveMemory] Injection orchestrator shadow ready: {len(self.injection_shadow_channels)} channels")
        except Exception as e:
            logger.warning(f"[WaveMemory] Injection orchestrator shadow init failed: {e}")
            _record_err("InjectionShadow", e)
            self.injection_trace_store = None
            self.injection_shadow_channels = []

    def _apply_persisted_hot_overrides(self) -> None:
        overrides_repo = getattr(self.db, "config_overrides", None)
        if overrides_repo is None:
            return
        try:
            known = self.hot_config.tunable_keys()
            stored = overrides_repo.values()
            applied = {key: value for key, value in stored.items() if key in known}
            unknown = sorted(set(stored) - known)
            if applied:
                self.hot_config.update(applied)
                logger.info(f"[WaveMemory] 已加载 9876 热参数覆盖: {sorted(applied)}")
            if unknown:
                logger.warning(f"[WaveMemory] 忽略未知的热参数覆盖（可能来自新版本）: {unknown}")
        except Exception as exc:
            logger.warning(f"[WaveMemory] 读取热参数覆盖失败，使用静态配置: {exc}")
            _record_err("HotConfig", exc)
        self._on_core_hot_config_change({})

    def _on_core_hot_config_change(self, _changed: dict) -> None:
        from ..services.injection.orchestrator import set_slow_warning_ms

        set_slow_warning_ms(self.hot_config.get("injection.slow_warning_ms", 2000))

    def _channel_registry(self):
        registry = getattr(self, "channel_registry", None)
        if registry is None:
            from ..services.injection.channel_registry import ChannelRegistry, builtin_channel_specs

            registry = ChannelRegistry()
            registry.register_many(builtin_channel_specs())
            self.channel_registry = registry
        return registry

    def _setup_runtime_context_preparer(self) -> None:
        """Runtime API（Cortico 等）复用与 AstrBot 相同的编排器和通道实例。"""
        from ..webui.container import get_container
        from ..services.injection.runtime_prepare import RuntimeContextPreparer

        def _exclude_sources(bot_id: str) -> list[str]:
            profile = self.bot_registry.get(bot_id)
            return list(profile.exclude_sources) if profile else []

        self.runtime_context_preparer = RuntimeContextPreparer(
            channels_provider=lambda: getattr(self, "injection_shadow_channels", None) or [],
            config_resolver=self._effective_injection_config,
            context_config_builder=self._build_shadow_context_config,
            query_options_factory=lambda cfg: QueryOptions(
                touch=True,
                stages=cfg.query_stages,
                params=cfg.query_params,
            ),
            trace_store=getattr(self, "injection_trace_store", None),
            mode_provider=lambda: getattr(self, "runtime_mode_name", "full"),
            recent_messages=lambda scope, limit: self._get_recent_messages(None, scope=scope, max_messages=limit),
            exclude_sources_for=_exclude_sources,
        )
        get_container().runtime_context_preparer = self.runtime_context_preparer
        get_container().ingress_health = self.ingress_health
        get_container().tag_worker_getter = lambda: getattr(self, "tag_worker", None)
        get_container().observation_ingestor = self.ingest_observation

    def _setup_service_registry(self) -> None:
        """登记可在 9876 上单独启停的后台服务（getter 每次读当前实例，热插拔后也准确）。"""
        from ..services.service_registry import ServiceRegistry, ServiceSpec
        from ..webui.container import get_container

        registry = ServiceRegistry(self.task_supervisor)
        for spec in (
            ServiceSpec("writer", "记忆写入器", lambda: getattr(self, "writer", None), owner="message-writer",
                        description="消息入库队列（核心服务，不能停）", stoppable=False),
            ServiceSpec("tag_worker", "标签提取", lambda: getattr(self, "tag_worker", None), owner="tag-worker",
                        description="后台匀速给新记忆打标签"),
            ServiceSpec("dream", "做梦", lambda: getattr(self, "dream_service", None), owner="dream",
                        description="定时回放近期与中期记忆，生成联想",
                        factory=self._create_dream_service,
                        config_keys=("Lifecycle_Settings.enable_dream", "Lifecycle_Settings.dream_interval_hours",
                                     "Lifecycle_Settings.dream_recent_seeds", "Lifecycle_Settings.dream_recent_k",
                                     "Lifecycle_Settings.dream_mid_seeds", "Lifecycle_Settings.dream_mid_k")),
            ServiceSpec("eviction", "记忆淘汰", lambda: getattr(self, "eviction_service", None), owner="eviction",
                        description="定时清理噪声与过期聊天记忆的向量",
                        factory=self._create_eviction_service, config_keys=("Eviction_Settings.",)),
            ServiceSpec("lifecycle", "好感生命周期", lambda: getattr(self, "lifecycle", None), owner="lifecycle",
                        description="好感触达/衰减、表达模式聚合；停止前会先落盘缓冲"),
            ServiceSpec("maintenance_jobs", "维护任务执行器", lambda: getattr(self, "maintenance_job_runner", None),
                        owner="durable-jobs", description="执行索引重建、回填等可恢复任务"),
        ):
            registry.register(spec)
        self.service_registry = registry
        get_container().service_registry = registry
