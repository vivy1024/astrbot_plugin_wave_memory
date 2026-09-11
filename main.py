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
from .services.inbound_message_handler import InboundMessagePipeline
from .services.backup_lifecycle import DatabaseBackupManager
from .services.runtime_mode import effective_native_injection_enabled, effective_query_feature, resolve_runtime_mode, runtime_capability_enabled, should_self_heal_advanced_query
from .services.compat import build_duplicate_memory_warnings, build_livingmemory_compat_surface, detect_memory_plugins
from .services.impression_timeline import parse_impression_mark, persist_unsettled_trace
from .services.lifecycle import LifecycleService
from .services.persona_evolution import PersonaEvolution
from .tools.memory_search import WaveMemorySearchTool, WaveMemoryRememberTool
from .tools.deep_search import WaveMemoryDeepSearchTool
from .tools.extra_tools import WaveMemoryFactsTool
from .tools.person_search import WaveMemoryPersonSearchTool
from .tools.injection_explain import WaveMemoryExplainInjectionTool
from .tools.memory_feedback import WaveMemoryFeedbackMemoryTool
from .tools.config_suggestion import WaveMemorySuggestConfigTool
from .tools.review_candidate import WaveMemorySubmitReviewCandidateTool
from .tools.affinity_update import WaveMemoryAffinityTool, WaveMemoryAffinityUpdateTool
from .tools.social_impression import WaveMemoryRecordSocialImpressionTool
from .tools.social_anchor import WaveMemoryNoteSocialAnchorTool
from .tools.cultural_moment import WaveMemoryMarkCulturalMomentTool
from .tools.fact_proposal import WaveMemoryProposeFactTool
from .tools.belief_proposal import WaveMemoryProposeBeliefTool
from .tools.episode import WaveMemoryNoteEpisodeTool
from .tools.diary_episode import WaveMemoryRecordDiaryEpisodeTool
from .tools.browse_chat import WaveMemoryBrowseRecentChatTool
from .tools.concern import WaveMemoryNoteConcernTool
from .tools.livingmemory_compat_tools import build_livingmemory_compat_tools
from .engine.book_lore_index import BookLoreIndex
from .services.meta_thinking import MetaThinking
from .services.dream import DreamService
from .services.self_reflect import SelfReflectService
from .services.llm_fallback import LLMFallbackClient, build_provider_chain

# 运行时错误收集（WebUI 可视化）
def _record_err(source: str, msg):
    try:
        from .utils.health_registry import record_error
        record_error(source, str(msg))
    except Exception:
        pass
from .services.eviction import EvictionService
from .services.concern_tracker import ConcernTracker
from .services.mood_trajectory import MoodTrajectory
from .services.subjective_time import SubjectiveTime
from .services.desire_engine import DesireEngine
from .services.belief_engine import BeliefEngine
from .services.belief_emergence import BeliefEmergenceService
from .services.belief_gating import snapshot_from_relationship
from .services.proactive_audit import read_proactive_relationship_context, record_proactive_timeline
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


@dataclass
class BotProfile:
    """配置驱动的 Bot 身份描述，消除所有硬编码。"""
    qq_id: str
    name: str
    db_id: str = ""                          # 数据库标识（如 "yushu"）
    aliases: list[str] = field(default_factory=list)  # 别名，用于兴趣词匹配
    meta_prompt: str = ""                    # 自定义 MetaThinking prompt（留空用默认模板）
    proactive_enabled: bool = True
    proactive_interval_seconds: int = 600
    proactive_max_per_hour: int = 3
    exclude_sources: list[str] = field(default_factory=list)  # 排除的记忆 source
    interest_keywords: list[str] = field(default_factory=list)  # 自定义兴趣词

    @property
    def all_keywords(self) -> list[str]:
        """该 bot 的所有兴趣关键词（名字 + 别名 + 自定义词）。"""
        words = [self.name] + self.aliases + self.interest_keywords
        return [w for w in words if w]


def _stringify_config_value(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _parse_csv_config_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _parse_bool_config_value(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "否", "关闭"}:
            return False
    return bool(value)


def _parse_int_config_value(value, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bot_config(cfg: dict) -> BotProfile:
    """从显式配置字典解析 BotProfile；稳定 db_id 缺失时拒绝注册。"""
    cfg = cfg or {}
    qq_id = _stringify_config_value(cfg.get("qq_id"))
    name = _stringify_config_value(cfg.get("name"))
    db_id = _stringify_config_value(cfg.get("db_id"))
    if not qq_id or not db_id:
        raise ValueError("BotProfile requires explicit qq_id and stable db_id")
    return BotProfile(
        qq_id=qq_id,
        name=name,
        db_id=db_id,
        aliases=_parse_csv_config_value(cfg.get("aliases")),
        meta_prompt=_stringify_config_value(cfg.get("meta_prompt")),
        proactive_enabled=_parse_bool_config_value(cfg.get("proactive_enabled"), True),
        proactive_interval_seconds=_parse_int_config_value(cfg.get("proactive_interval_seconds"), 600),
        proactive_max_per_hour=_parse_int_config_value(cfg.get("proactive_max_per_hour"), 3),
        exclude_sources=_parse_csv_config_value(cfg.get("exclude_sources")),
        interest_keywords=_parse_csv_config_value(cfg.get("interest_keywords")),
    )


def _build_bot_registry(config: dict) -> dict[str, BotProfile]:
    """仅从显式用户配置构建 BotProfile registry；缺失身份时保持空并失败关闭。"""
    registry: dict[str, BotProfile] = {}
    for key in ("MetaThinking_Bot1", "MetaThinking_Bot2"):
        bot_cfg = (config or {}).get(key, {}) or {}
        if not _stringify_config_value(bot_cfg.get("qq_id")):
            continue
        try:
            profile = _parse_bot_config(bot_cfg)
        except ValueError as exc:
            logger.error("[WaveMemory] ignored incomplete BotProfile %s: %s", key, exc)
            continue
        registry[profile.qq_id] = profile
    return registry


@register(
    "astrbot_plugin_wave_memory",
    "vivy1024",
    "群聊长期记忆插件。日常检索只依赖向量模型，本地 SQLite 毫秒级召回，不装 Neo4j/ES。记忆注入和黑话、风格、信念、好感、时间线一起进回复，通道和预算可单独调。适合长期陪聊群 Bot；不是只塞最近几条的轻量摘要。",
    "5.0.0",
    "https://github.com/vivy1024/astrbot_plugin_wave_memory",
)
class WaveMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._terminated = False
        self._bot_qq_ids: list[str] = []
        self._group_names: dict[tuple[str, str], str] = {}

        # Bot identity 只能来自显式配置；缺失配置时 Scope 解析与相关能力失败关闭。
        self._bot_registry = _build_bot_registry(self.config)
        self._bot_qq_ids = [p.qq_id for p in self._bot_registry.values()]

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
        from .services.memory_budget_policy import MemoryBudgetPolicy

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
            from .services.config.channel_config import build_channel_config_from_plugin_config
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
        from .services.storage_capacity_policy import StorageCapacityPolicy

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
        self.cross_group_persona_merge = cross_group_cfg.get("cross_group_persona_merge", True)

        # 好感度引擎配置
        self.affinity_cfg = affinity_cfg

        # 生命周期配置：memory_only/compat_only 强制关闭高级社交/人格/情绪能力，避免旧 default=true 穿透模式边界。
        self.enable_affinity = runtime_capability_enabled(self.runtime_mode, "affinity", lifecycle_cfg.get("enable_affinity", True))
        self.enable_persona = runtime_capability_enabled(self.runtime_mode, "persona", lifecycle_cfg.get("enable_persona_evolution", True))
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
        data_path = get_astrbot_data_path() or os.path.dirname(__file__)
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
                from .engine.book_lore_index import (
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
        self.write_gateway = ProductionWriteGateway(
            self.db.db_path,
            consumers={
                self.memory_index_projection.consumer_name: self.memory_index_projection,
                self.tag_index_projection.consumer_name: self.tag_index_projection,
                self.cooccurrence_projection.consumer_name: self.cooccurrence_projection,
                self.runtime_refresh_projection.consumer_name: self.runtime_refresh_projection,
            },
        )
        self.relationship_service = RelationshipEventService(
            self.db.conn,
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
        if self.tag_extractor:
            tag_worker_cfg = self.config.get("TagWorker_Settings", {})
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
        }
        maintenance_handlers.update(self.data_governance_jobs.handlers())
        maintenance_handlers.update(self.scope_recovery_jobs)
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
        self.persona_evolution = None
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
            from .engine.db.migrations.v2_1_cleanup import run_migration

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

    def _spawn(self, coro, *, name: str | None = None, owner: str = "plugin") -> asyncio.Task:
        """通过统一 supervisor 创建可观察、可等待的命名后台任务。"""
        self._task_sequence += 1
        task_name = name or f"wave-memory:{owner}:{self._task_sequence}"
        return self.task_supervisor.start(task_name, coro, owner=owner)

    def _set_injection_channel_config(self, config) -> None:
        """WebUI 热应用通道配置时更新运行时注入编排器配置。"""
        self.injection_channel_config = config

    def _build_book_lore_catalog_scope(self) -> CatalogScope:
        """构造书设 CatalogScope；书设是独立知识库，不绑定群会话 RuntimeScope。"""
        study = self.config.get("Study_Settings", {}) or {}
        catalog_id = str(study.get("source_library_id") or "book-lore").strip() or "book-lore"
        corpus_id = str(study.get("source_corpus_id") or "default").strip() or "default"
        version = str(study.get("source_version") or "current").strip() or "current"
        return CatalogScope(catalog_id=catalog_id, corpus_id=corpus_id, version=version)

    def _llm_chain(self) -> list[str]:
        """构建统一的 LLM provider 回退链：Tag LLM 在前，配置的回退渠道在后。

        所有需要 LLM 的后台子系统共用同一条链，避免单渠道 503/402 让能力静默停产。
        """
        return build_provider_chain(
            self.tag_llm_provider_id,
            getattr(self, "llm_fallback_provider_ids", ""),
        )

    def _get_bot(self, bot_id: str) -> Optional[BotProfile]:
        """通过 QQ 号获取 Bot 配置，未找到返回 None。"""
        return self._bot_registry.get(bot_id)

    def _get_admin_ids(self) -> list:
        """从 AstrBot 框架配置获取管理员 ID 列表。"""
        try:
            from astrbot.core.config import get_config
            cfg = get_config()
            admins = cfg.get("admins_id", [])
            if admins:
                return [str(a) for a in admins if a and a != "astrbot"]
        except Exception:
            pass
        logger.warning("[WaveMemory] admin registry unavailable; no implicit bot administrator granted")
        return []

    def _get_bot_name(self, bot_id: str) -> str:
        """获取 bot 显示名，fallback 为 'bot'。"""
        p = self._bot_registry.get(bot_id)
        return p.name if p else "bot"

    def _remember_group_name(self, bot_id: str, group_id: str, group_name: str | None) -> None:
        """委托给 PlatformContextManager 统一维护。"""
        if hasattr(self, "platform_context_manager"):
            self.platform_context_manager.remember_group_name(bot_id, group_id, group_name)
        else:
            normalized_bot = str(bot_id or "").strip()
            normalized_group = str(group_id or "").strip()
            normalized_name = str(group_name or "").strip()
            if normalized_bot and normalized_group and normalized_name:
                self._group_names[(normalized_bot, normalized_group)] = normalized_name

    def _get_group_name(self, bot_id: str, group_id: str) -> str | None:
        """委托给 PlatformContextManager。"""
        if hasattr(self, "platform_context_manager"):
            return self.platform_context_manager.get_group_name(bot_id, group_id)
        normalized_bot = str(bot_id or "").strip()
        normalized_group = str(group_id or "").strip()
        direct = self._group_names.get((normalized_bot, normalized_group))
        if direct:
            return direct
        names = {
            name
            for (cached_bot, cached_group), name in self._group_names.items()
            if cached_bot and cached_group == normalized_group and name
        }
        return next(iter(names)) if len(names) == 1 else None

    async def _refresh_group_names_from_platforms(self) -> None:
        """委托给 PlatformContextManager。"""
        if hasattr(self, "platform_context_manager"):
            await self.platform_context_manager.refresh_group_names_from_platforms()

    async def _warm_group_names_when_platforms_ready(self) -> None:
        """委托给 PlatformContextManager。"""
        if hasattr(self, "platform_context_manager"):
            await self.platform_context_manager.warm_group_names_when_platforms_ready()

    def _setup_injection_shadow_pipeline(self) -> None:
        """初始化新注入编排器通道链；失败不影响旧 inject_memory fallback。"""
        from .webui.container import get_container

        if not getattr(self, "injection_shadow_enabled", True) and not getattr(self, "injection_orchestrator_active_enabled", False):
            logger.info("[WaveMemory] Injection orchestrator disabled")
            return
        if not getattr(self, "injection_channel_config", None):
            logger.warning("[WaveMemory] Injection orchestrator shadow skipped: channel config unavailable")
            return
        try:
            from .services.injection.trace_store import InjectionTraceStore
            from .services.injection.channels.safety import SafetyChannel
            from .services.injection.channels.memory_recall import MemoryRecallChannel
            from .services.injection.channels.facts import FactsChannel
            from .services.injection.channels.persona import PersonaChannel
            from .services.injection.channels.belief import BeliefChannel
            from .services.injection.channels.book_lore import BookLoreChannel
            from .services.injection.channels.fewshot import FewShotChannel
            from .services.injection.channels.jargon import JargonChannel
            from .services.injection.channels.fts5 import FTS5Channel
            from .services.injection.channels.relationship import RelationshipChannel
            from .services.injection.channels.soul_state import SoulStateChannel
            from .services.persona_composer import PersonaComposer

            self.injection_trace_store = InjectionTraceStore(
                self.db.conn,
                max_preview_chars=getattr(self, "injection_trace_max_preview_chars", 1200),
                retention_days=getattr(self, "injection_trace_retention_days", 14),
                max_rows=getattr(self, "injection_trace_max_rows", 5000),
                cleanup_on_record=True,
            )
            self.injection_trace_store.ensure_schema()
            safety = SafetyChannel()
            persona_composer = PersonaComposer(
                db=self.db,
                query_engine=self.query_engine,
                bot_profiles=self._bot_registry,
            )
            self.injection_shadow_channels = [
                safety,
                MemoryRecallChannel(query_engine=self.query_engine, safety_channel=safety),
                FTS5Channel(
                    db=self.db,
                    cross_group_enabled=self.cross_group_enabled,
                    shared_memory_grants_enabled=self.shared_memory_grants_enabled,
                ),
                FactsChannel(db=self.db, facts_decay_rate=getattr(self, "_facts_decay_rate", 0.005)),
                # PersonaEvolution 仍依赖 legacy social/facts read-model，不能进入正式注入。
                PersonaChannel(composer=persona_composer, persona_evolution=None),
                BeliefChannel(belief_engine=getattr(self, "belief_engine", None)),
                JargonChannel(jargon_service=getattr(self, "jargon_service", None)),
                FewShotChannel(few_shot_service=getattr(self, "few_shot_service", None)),
                RelationshipChannel(repository=self.db.soul_repository, db=self.db),
                SoulStateChannel(repository=self.db.soul_repository),
                BookLoreChannel(
                    book_lore_index=self.book_lore_index,
                    embedding_service=self.embedding_service,
                    lore_db_path=self.lore_db_path,
                    catalog_scope=self.book_lore_catalog_scope,
                ),
            ]
            get_container().injection_channels = list(self.injection_shadow_channels)
            logger.info(f"[WaveMemory] Injection orchestrator shadow ready: {len(self.injection_shadow_channels)} channels")
        except Exception as e:
            logger.warning(f"[WaveMemory] Injection orchestrator shadow init failed: {e}")
            _record_err("InjectionShadow", e)
            self.injection_trace_store = None
            self.injection_shadow_channels = []

    def _build_shadow_persona_realtime_ctx(
        self,
        *,
        scope: RuntimeScope | None,
        sender_id: str,
        sender_name: str,
    ) -> dict:
        """复用当前已解析群 Scope 内的实时画像上下文。"""
        realtime_ctx = {}
        if hasattr(self, '_hourly_reply_count') and sender_id in self._hourly_reply_count:
            realtime_ctx["hourly_at_count"] = self._hourly_reply_count[sender_id].get("count", 0)
        if (
            not isinstance(scope, RuntimeScope)
            or scope.visibility != "group"
            or scope.session is None
        ):
            return realtime_ctx
        try:
            params = (scope.bot_id, scope.session.id, scope.visibility)
            last_reply_row = self.db.conn.execute(
                """SELECT content FROM memories
                   WHERE sender_id='bot' AND bot_id=? AND session_id=? AND visibility=?
                     AND resolution_state='resolved' AND quarantine=0
                     AND content LIKE ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (*params, f"%{sender_name or sender_id}%"),
            ).fetchone()
            if not last_reply_row:
                last_reply_row = self.db.conn.execute(
                    """SELECT content FROM memories
                       WHERE sender_id='bot' AND bot_id=? AND session_id=? AND visibility=?
                         AND resolution_state='resolved' AND quarantine=0
                       ORDER BY timestamp DESC LIMIT 1""",
                    params,
                ).fetchone()
            if last_reply_row:
                realtime_ctx["last_bot_reply"] = str(last_reply_row[0] or "")[:80]
        except Exception:
            pass
        return realtime_ctx

    def _effective_injection_config(self, scope: RuntimeScope | None):
        """按 exact RuntimeScope 解析请求级配置；非法层级 fail closed。"""
        if not isinstance(scope, RuntimeScope):
            return None
        from .services.config.channel_config import build_channel_config_from_plugin_config
        return build_channel_config_from_plugin_config(self.config, scope=scope)

    def _build_shadow_context_config(self, *, channel_config, exclude_sources, recent_context: list[str], realtime_ctx: dict) -> dict:
        from .services.impression_timeline import normalize_timeline_half_life

        config = channel_config.to_dict() if channel_config is not None else {}
        # Read the resolved value, not raw Inject_Settings that would bypass overrides.
        config["timeline_decay_half_life_days"] = normalize_timeline_half_life(
            config.get("timeline_decay_half_life_days")
        )
        recall = dict(config.get("memory_recall") or {})
        recall.update({
            "context_messages": recent_context,
            "exclude_sources": exclude_sources,
        })
        config["memory_recall"] = recall
        # Active/shadow contexts expose the same normalized setting used by
        # QueryEngine and the FTS5 channel instance.
        config["cross_group_enabled"] = self.cross_group_enabled
        config["shared_memory_grants_enabled"] = self.shared_memory_grants_enabled
        config["persona"] = {"realtime_ctx": realtime_ctx}
        return config

    async def _run_injection_shadow_trace(
        self,
        *,
        event: AstrMessageEvent,
        req,
        message: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        bot_id: str,
        bot_profile: Optional[BotProfile],
        runtime_scope: RuntimeScope | None,
        exclude_sources,
        old_text: str,
    ) -> None:
        """运行新 Orchestrator 影子链路并写入 trace；绝不修改真实 req。"""
        if not getattr(self, "injection_shadow_enabled", True):
            return
        if not getattr(self, "injection_shadow_channels", None) or not getattr(self, "injection_trace_store", None):
            return
        try:
            from .services.injection.context import InjectionContext
            from .services.injection.shadow import run_injection_shadow

            effective_config = self._effective_injection_config(runtime_scope)
            if effective_config is None:
                return
            recent_context = self._get_recent_messages(event, scope=runtime_scope, max_messages=8)
            realtime_ctx = self._build_shadow_persona_realtime_ctx(
                scope=runtime_scope,
                sender_id=sender_id,
                sender_name=sender_name,
            )
            bot_profile_id = runtime_scope.bot_id
            trace_id = f"shadow-{time.time_ns()}"
            ctx = InjectionContext(
                event=event,
                req=req,
                message=message,
                group_id=group_id,
                sender_id=sender_id,
                sender_name=sender_name,
                bot_id=bot_id,
                bot_profile_id=bot_profile_id,
                scope=runtime_scope,
                recent_context=recent_context,
                mode=getattr(self, "runtime_mode_name", "full"),
                config=self._build_shadow_context_config(
                    channel_config=effective_config,
                    exclude_sources=exclude_sources,
                    recent_context=recent_context,
                    realtime_ctx=realtime_ctx,
                ),
                channel_options=effective_config.to_dict()["channels"],
                query_options=QueryOptions(
                    touch=True,
                    stages=effective_config.query_stages,
                    params=effective_config.query_params,
                ),
                now=time.time(),
                trace_id=trace_id,
            )
            result = await run_injection_shadow(
                ctx=ctx,
                channels=self.injection_shadow_channels,
                config=effective_config,
                trace_store=self.injection_trace_store,
                old_text=old_text,
            )
            matched = old_text == result.final_text
            logger.debug(
                f"[WaveMemory] injection shadow {'MATCH' if matched else 'DIFF'}: "
                f"trace={trace_id} old_chars={len(old_text or '')} new_chars={len(result.final_text or '')}"
            )
        except Exception as e:
            logger.debug(f"[WaveMemory] injection shadow skipped: {e}")
            _record_err("InjectionShadow", e)

    async def _run_injection_active_trace(
        self,
        *,
        event: AstrMessageEvent,
        req,
        message: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        bot_id: str,
        bot_profile: Optional[BotProfile],
        runtime_scope: RuntimeScope | None,
        exclude_sources,
    ) -> bool:
        """主动模式：规范 Orchestrator 直接写真实 ProviderRequest；失败返回 False 并关闭注入。"""
        if not getattr(self, "injection_orchestrator_active_enabled", False):
            return False
        if not getattr(self, "injection_shadow_channels", None):
            return False
        try:
            from .services.injection.context import InjectionContext
            from .services.injection.active import run_injection_active
            from .utils.perf import get_perf_tracker

            effective_config = self._effective_injection_config(runtime_scope)
            if effective_config is None:
                return False
            recent_context = self._get_recent_messages(event, scope=runtime_scope, max_messages=8)
            realtime_ctx = self._build_shadow_persona_realtime_ctx(
                scope=runtime_scope,
                sender_id=sender_id,
                sender_name=sender_name,
            )
            bot_profile_id = runtime_scope.bot_id
            trace_id = f"active-{time.time_ns()}"
            ctx = InjectionContext(
                event=event,
                req=req,
                message=message,
                group_id=group_id,
                sender_id=sender_id,
                sender_name=sender_name,
                bot_id=bot_id,
                bot_profile_id=bot_profile_id,
                scope=runtime_scope,
                recent_context=recent_context,
                mode=getattr(self, "runtime_mode_name", "full"),
                config=self._build_shadow_context_config(
                    channel_config=effective_config,
                    exclude_sources=exclude_sources,
                    recent_context=recent_context,
                    realtime_ctx=realtime_ctx,
                ),
                channel_options=effective_config.to_dict()["channels"],
                query_options=QueryOptions(
                    touch=True,
                    stages=effective_config.query_stages,
                    params=effective_config.query_params,
                ),
                now=time.time(),
                trace_id=trace_id,
            )
            result = await run_injection_active(
                ctx=ctx,
                channels=self.injection_shadow_channels,
                config=effective_config,
                trace_store=getattr(self, "injection_trace_store", None),
            )

            hit_results = [r for r in result.channel_results if r.status == "hit" and r.text]
            metric_sample = {
                "total_ms": result.total_latency_ms,
                "total_tokens": sum(r.tokens for r in hit_results),
                "total_chars": len(result.final_text),
                "parts_count": len(hit_results),
            }
            for channel_result in result.channel_results:
                prefix = channel_result.channel
                metric_sample[f"{prefix}_ms"] = channel_result.latency_ms
                metric_sample[f"{prefix}_tokens"] = channel_result.tokens
                metric_sample[f"{prefix}_chars"] = channel_result.chars
            try:
                get_perf_tracker().record_injection(metric_sample)
                if getattr(self, "db", None):
                    self.db.record_injection_metric(metric_sample)
                    self.db.cleanup_injection_metrics()
            except Exception as e:
                logger.warning(f"[WaveMemory] record orchestrator injection metrics failed: {e}")

            memory_ids = []
            for channel_result in result.channel_results:
                if channel_result.channel not in {"memory", "fts5"} or channel_result.status != "hit":
                    continue
                for item in channel_result.items or []:
                    mid = item.get("id")
                    if mid and mid not in memory_ids:
                        memory_ids.append(mid)
            for mid in memory_ids[:10]:
                try:
                    if (
                        not isinstance(runtime_scope, RuntimeScope)
                        or runtime_scope.visibility not in {"group", "private"}
                        or runtime_scope.session is None
                    ):
                        continue
                    row = self.db.conn.execute(
                        """SELECT importance FROM memories
                           WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
                             AND resolution_state='resolved' AND quarantine=0""",
                        (mid, runtime_scope.bot_id, runtime_scope.session.id, runtime_scope.visibility),
                    ).fetchone()
                    if row:
                        cur_imp = float(row[0] if row[0] is not None else 1.0)
                        if cur_imp < 3.0:
                            await self.write_gateway.set_memory_importance(
                                scope=runtime_scope,
                                memory_ids=[mid],
                                importance=min(3.0, cur_imp + 0.02),
                                idempotency_hint=f"orchestrator-hit:{trace_id}:{mid}",
                            )
                except Exception:
                    pass

            if result.injected:
                parts_detail = []
                for channel_result in hit_results:
                    count = len(channel_result.items or [])
                    parts_detail.append(f"{channel_result.channel}={count}" if count else channel_result.channel)
                logger.info(
                    f"[WaveMemory] inject_memory SUCCESS: orchestrator {len(hit_results)} parts "
                    f"[{','.join(parts_detail)}], {len(result.final_text)} chars, "
                    f"{result.total_latency_ms:.0f}ms | tokens={metric_sample['total_tokens']} trace={trace_id}"
                )
            else:
                logger.info(f"[WaveMemory] inject_memory: no orchestrator memories found to inject | trace={trace_id}")

            # Online embedding recall commonly needs ~1-2s; keep the warning
            # threshold aligned with the memory channel soft-timeout default.
            slow_ms = 2000
            if result.total_latency_ms > slow_ms:
                logger.warning(
                    f"[WaveMemory] inject_memory 耗时过长: {result.total_latency_ms:.0f}ms > {slow_ms}ms | "
                    f"channels={[{ 'channel': r.channel, 'status': r.status, 'ms': r.latency_ms } for r in result.channel_results]}"
                )
            return True
        except Exception as e:
            logger.warning(f"[WaveMemory] canonical injection orchestrator failed closed: {e}", exc_info=True)
            _record_err("InjectionActive", e)
            return False

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

    async def _initialize_once(self):
        """AstrBot 完成 handler 绑定后执行一次实际初始化。"""
        # 从现有 Bot Registry 构造唯一 ScopeResolver；领域切片未落地时显式保持 fail closed。
        try:
            from .services.scopes import BotIdentityBinding, ScopeResolver

            bindings = [
                BotIdentityBinding(
                    self_id=profile.qq_id,
                    db_id=profile.db_id,
                    display_name=profile.name,
                )
                for profile in self._bot_registry.values()
                if profile.qq_id and profile.db_id
            ]
            self.scope_resolver = ScopeResolver(bindings)
            logger.info(f"[WaveMemory] ScopeResolver initialized: {len(bindings)} bot bindings")
        except Exception as exc:
            self.scope_resolver = None
            logger.warning(f"[WaveMemory] ScopeResolver unavailable; message ingress fail closed: {exc}")
            _record_err("ScopeResolution", "scope_resolver_unavailable")

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

        # 初始化 EPA
        if self.epa:
            self._spawn(self._init_epa())

        # 注册 LLM 工具：memory_only 保留纯记忆工具；compat_only 仅暴露 LivingMemory 风格别名（如已启用）。
        livingmemory_alias_tools = build_livingmemory_compat_tools(
            self.memory_engine,
            enabled=self.livingmemory_alias_tools_enabled,
        )
        self.livingmemory_alias_tools_registered = bool(livingmemory_alias_tools)

        llm_tools = [*livingmemory_alias_tools]
        if runtime_capability_enabled(self.runtime_mode, "memory_tools", True):
            llm_tools.extend([
                WaveMemorySearchTool(query_engine=self.query_engine, db=self.db),
                WaveMemoryRememberTool(writer=self.writer),
                WaveMemoryDeepSearchTool(db=self.db),
                WaveMemoryFactsTool(db=self.db),
                WaveMemoryPersonSearchTool(db=self.db),
            ])
        if runtime_capability_enabled(self.runtime_mode, "agent_feedback_tools", True):
            llm_tools.extend([
                WaveMemoryExplainInjectionTool(db=self.db),
                WaveMemoryFeedbackMemoryTool(db=self.db),
                WaveMemorySuggestConfigTool(db=self.db),
                WaveMemorySubmitReviewCandidateTool(db=self.db),
            ])
        if runtime_capability_enabled(self.runtime_mode, "affinity_tools", True):
            _bot_db_ids_map = {profile.qq_id: profile.db_id for profile in self._bot_registry.values()}
            llm_tools.extend([
                WaveMemoryAffinityTool(db=self.db),
                WaveMemoryAffinityUpdateTool(
                    db=self.db,
                    relationship_events=self.relationship_service,
                    bot_db_ids=_bot_db_ids_map,
                ),
                WaveMemoryRecordSocialImpressionTool(
                    db=self.db,
                    relationship_events=self.relationship_service,
                    bot_db_ids=_bot_db_ids_map,
                ),
                WaveMemoryNoteSocialAnchorTool(
                    db=self.db,
                    concern_tracker=getattr(self, "concern_tracker", None),
                    repository=getattr(self.db, "soul_repository", None),
                    write_gateway=self.write_gateway,
                ),
                WaveMemoryMarkCulturalMomentTool(
                    db=self.db,
                    jargon_service=getattr(self, "jargon_service", None),
                ),
                WaveMemoryProposeFactTool(
                    db=self.db,
                    jargon_service=getattr(self, "jargon_service", None),
                ),
                WaveMemoryProposeBeliefTool(db=self.db),
                WaveMemoryNoteEpisodeTool(db=self.db, writer=self.writer, write_gateway=self.write_gateway),
                WaveMemoryRecordDiaryEpisodeTool(db=self.db, write_gateway=self.write_gateway),
                WaveMemoryNoteConcernTool(
                    db=self.db,
                    concern_tracker=getattr(self, "concern_tracker", None),
                    write_gateway=self.write_gateway,
                ),
            ])
        if runtime_capability_enabled(self.runtime_mode, "book_lore_tools", True):
            # 书设工具直读 Catalog（book_lore.db + HNSW），不经 Learning projection。
            try:
                from .tools.book_lore_query import WaveMemoryBookLoreQueryTool
                from .tools.book_lore_search import BookLoreGraphTool, BookLoreSearchTool

                llm_tools.append(
                    WaveMemoryBookLoreQueryTool(
                        book_lore_index=self.book_lore_index,
                        embedding_service=self.embedding_service,
                        lore_db_path=self.lore_db_path,
                        catalog_scope=self.book_lore_catalog_scope,
                    )
                )
                llm_tools.append(
                    BookLoreSearchTool(
                        book_lore_index=self.book_lore_index,
                        embedding_service=self.embedding_service,
                        lore_db_path=self.lore_db_path,
                        catalog_scope=self.book_lore_catalog_scope,
                    )
                )
                llm_tools.append(
                    BookLoreGraphTool(
                        lore_db_path=self.lore_db_path,
                        catalog_scope=self.book_lore_catalog_scope,
                    )
                )
            except Exception as exc:
                logger.info("[WaveMemory] book_lore search tool unavailable: %s", exc)

        if llm_tools:
            self.context.add_llm_tools(*llm_tools)

        # 启动 WebUI
        if self.webui_enabled:
            await self._refresh_group_names_from_platforms()
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
                    bot_registry=self._bot_registry,
                    group_name_resolver=self._get_group_name,
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
                bot_db_id=_first_bot.db_id if _first_bot else "yushu",
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

        # 人格进化引擎
        self.persona_evolution = PersonaEvolution(
            db=self.db,
            cross_group_merge=self.cross_group_persona_merge,
            affinity_cfg=self.affinity_cfg,
        ) if self.enable_persona else None

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
                    from .webui.container import get_container
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
                bot_prompts = {}
                interest_keywords = set()
                for profile in self._bot_registry.values():
                    if profile.meta_prompt:
                        bot_prompts[profile.qq_id] = profile.meta_prompt
                    interest_keywords.update(profile.all_keywords)

                self.meta_thinking = MetaThinking(
                    db=self.db,
                    context=self.context,
                    bot_qq_id=self._bot_qq_ids[0] if self._bot_qq_ids else "",
                    bot_qq_ids=self._bot_qq_ids,
                    bot_prompts=bot_prompts,
                    bot_names={p.qq_id: p.name for p in self._bot_registry.values()},
                    bot_db_ids={p.qq_id: p.db_id for p in self._bot_registry.values()},
                    admin_ids=self._get_admin_ids(),
                    config=meta_cfg,
                    global_fallback_ids=self.config.get("meta_thinking_fallback_ids", ""),
                    extra_interests=list(interest_keywords),
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

        # ─── 注册所有服务状态到健康面板（WebUI 可视化）───
        from .utils.health_registry import register as _reg
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

        missing_bot_profile_reason = "未配置 Bot Profile（MetaThinking_Bot1/2 缺 qq_id/db_id）"
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

    async def terminate(self):
        """Serialize teardown with initialization and make repeated callbacks harmless."""
        async with self._initialize_lock:
            if self._terminated:
                return
            self._terminated = True
            await self._terminate_once()

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

    # ─── Hook: MetaThinking 元思考（v1.3.0 改造：纯规则 + 态度注入，不调 LLM）───

    # 追踪 bot 最近回复了谁（用于 ABA 连续对话判断）
    _reply_tracker: dict = {}  # {f"{sender_id}:{group_id}": timestamp}

    def _should_engage(self, event: AstrMessageEvent) -> str:
        """规则链前置过滤：判断消息是否与 bot 相关。
        
        返回: 'must_reply' / 'may_reply' / 'skip'
        """
        is_at_bot = getattr(event, "is_at_or_wake_command", False)

        # 1. @bot 或唤醒词 → must_reply
        if is_at_bot:
            return "must_reply"

        message = event.get_message_str() or ""
        sender_id = event.get_sender_id() or ""
        group_id = event.get_group_id() or ""

        # 2. 私聊 → must_reply
        if not group_id or group_id.startswith("private:"):
            return "must_reply"

        # 3. 引用了 bot 消息 → must_reply
        if "[引用消息" in message:
            for bid in self._bot_qq_ids:
                if bid and bid in message:
                    return "must_reply"

        # 4. bot 30s 内回复过此人 → may_reply（ABA 连续对话）
        reply_key = f"{sender_id}:{group_id}"
        last_reply_ts = self._reply_tracker.get(reply_key, 0)
        aba_window = int(self.hot_config.get("social.aba_window_seconds", 30)) if hasattr(self, 'hot_config') else 30
        if time.time() - last_reply_ts < aba_window:
            return "may_reply"

        # 5. 包含兴趣关键词 → may_reply
        if self.meta_thinking and self.meta_thinking.is_interesting(message):
            return "may_reply"

        # 6. 本轮消息命中该人印象时间线 → may_reply（不强制回复，不建关切）
        if self._timeline_cue_hit(event, message, sender_id, group_id) is not None:
            return "may_reply"

        # 7. 其他 → skip
        return "skip"

    def _timeline_cue_bot_id(self, event) -> str:
        qq_id = ""
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            qq_id = str(getter() or "").strip()
        registry = getattr(self, "_bot_registry", None) or {}
        profile = registry.get(qq_id) if qq_id else None
        db_id = str(getattr(profile, "db_id", "") or "").strip()
        return db_id or qq_id

    def _timeline_cue_hit(self, event, message: str, sender_id: str, group_id: str) -> dict | None:
        """复用 MetaThinking 规则门。结果挂在 event 上，避免主 hook 再读一次库。"""
        cached = getattr(event, "_wave_memory_timeline_cue", None)
        if isinstance(cached, dict) or cached is False:
            return cached or None
        db = getattr(self, "db", None)
        if db is None or not sender_id or not group_id:
            return None
        try:
            from .services.impression_timeline import load_timeline_cue
        except Exception:
            return None
        bot_id = self._timeline_cue_bot_id(event)
        if not bot_id:
            return None
        try:
            hit = load_timeline_cue(
                db, bot_id=bot_id, user_id=sender_id, group_id=group_id, message=message
            )
        except Exception:
            hit = None
        try:
            event._wave_memory_timeline_cue = hit or False
        except Exception:
            pass
        return hit

    @filter.on_llm_request(priority=1)
    async def meta_thinking_check(self, event: AstrMessageEvent, req=None):
        """v1.3.0: 纯规则判断 + 态度注入，不独立调 LLM。
        
        - skip 的消息：直接 return（由 AstrBot 决定是否调 LLM）
        - must/may：保留硬规则（极端攻击/刷屏），态度由 persona_text 注入（inject_memory 通道 5）
        """
        if not req:
            return

        message = event.get_message_str() or ""
        sender_id = event.get_sender_id() or ""
        group_id = event.get_group_id() or ""
        bot_id = event.get_self_id() or ""
        is_at_bot = getattr(event, "is_at_or_wake_command", False)

        # 最高优先级身份/风格防线：不让历史回复、记忆或当前诱导覆盖当前人格。
        req.system_prompt = prepend_identity_safety_system_prompt(
            getattr(req, "system_prompt", ""), message, always=True
        )
        safety_injection = build_identity_safety_injection(message)
        if safety_injection:
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(TextPart(text=safety_injection))

        # ─── 规则链前置过滤 ───
        engage = self._should_engage(event)
        if engage == "skip":
            # 不相关消息，不做任何处理（AstrBot 不会调 LLM 因为没 @）
            return
        cue = getattr(event, "_wave_memory_timeline_cue", None)
        if isinstance(cue, dict):
            try:
                from .services.impression_timeline import timeline_cue_prompt
                prompt = timeline_cue_prompt(cue)
            except Exception:
                prompt = ""
            if prompt:
                try:
                    from astrbot.core.agent.message import TextPart
                    req.extra_user_content_parts.append(TextPart(text=prompt))
                except Exception:
                    extra = getattr(req, "extra_user_content_parts", None)
                    if extra is None:
                        req.extra_user_content_parts = extra = []
                    extra.append({"type": "text", "text": prompt})

        # ─── 硬规则：极端攻击 + 辱骂冷却 ───
        from .services.meta_thinking import EXTREME_ATTACK

        # 先检查冷却期（被辱骂后静默不回）
        if not hasattr(self, '_abuse_tracker'):
            self._abuse_tracker = {}  # {sender_id: {"count": N, "cooldown_until": ts}}
        if sender_id in self._abuse_tracker:
            tracker = self._abuse_tracker[sender_id]
            if time.time() < tracker.get("cooldown_until", 0):
                event.should_call_llm(False)
                return  # 冷却期间完全不回复
            # 冷却已过期：count 衰减（每过一次冷却期 -1，最低归 0）
            elif tracker.get("cooldown_until", 0) > 0:
                tracker["count"] = max(0, tracker["count"] - 1)
                tracker["cooldown_until"] = 0
                if tracker["count"] == 0:
                    del self._abuse_tracker[sender_id]

        if is_at_bot and EXTREME_ATTACK.search(message):
            import re
            bot_qq_id = self._bot_qq_ids[0] if self._bot_qq_ids else ""
            other_at = re.search(r'At[:：]?\d+', message.replace(bot_qq_id, ''))
            if not other_at:
                # 辱骂计数
                tracker = self._abuse_tracker.setdefault(sender_id, {"count": 0, "cooldown_until": 0})
                tracker["count"] += 1
                abuse_trigger = int(self.hot_config.get("social.abuse_trigger_count", 3))
                if tracker["count"] >= abuse_trigger:
                    # 触发冷却
                    base = int(self.hot_config.get("social.abuse_cooldown_base", 600))
                    cap = int(self.hot_config.get("social.abuse_cooldown_max", 3600))
                    cooldown = min(cap, base * (2 ** (tracker["count"] - abuse_trigger)))
                    tracker["cooldown_until"] = time.time() + cooldown
                    event.should_call_llm(False)
                    logger.info(f"[MetaThinking] 辱骂冷却: {sender_id} 冷却 {cooldown}s")
                    return
                # 前 2 次还是怼回去
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(TextPart(
                    text="[语气指令] 这个人在骂你/惹你不爽。用愤怒的语气怼回去，不要客气。"
                ))
                logger.info(f"[MetaThinking] 极端攻击检测: {sender_id} (第{tracker['count']}次)")
                return

        # ─── 硬规则：刷屏检测 ───
        if is_at_bot and self.meta_thinking:
            now = time.time()
            if sender_id not in self.meta_thinking._at_timestamps:
                self.meta_thinking._at_timestamps[sender_id] = []
            ts_list = self.meta_thinking._at_timestamps[sender_id]
            window = self.meta_thinking.spam_window_seconds
            self.meta_thinking._at_timestamps[sender_id] = [t for t in ts_list if now - t < window]
            self.meta_thinking._at_timestamps[sender_id].append(now)
            if (self.meta_thinking.spam_threshold > 0
                    and len(self.meta_thinking._at_timestamps[sender_id]) >= self.meta_thinking.spam_threshold):
                event.should_call_llm(False)
                logger.info(f"[MetaThinking] 刷屏拦截: {sender_id}")
                return

        # ─── 每小时 @计数器（供 persona 注入实时状态，不做硬拦截）───
        if is_at_bot:
            if not hasattr(self, '_hourly_reply_count'):
                self._hourly_reply_count = {}  # {sender_id: {"count": N, "hour": H}}
            now = time.time()
            current_hour = int(now // 3600)
            tracker = self._hourly_reply_count.setdefault(sender_id, {"count": 0, "hour": current_hour})
            if tracker["hour"] != current_hour:
                tracker["count"] = 0
                tracker["hour"] = current_hour
            tracker["count"] += 1
            # v2.0: 不再硬拦截，把频率信息注入 persona 让 bot 自己判断

        # ─── 态度判断由 inject_memory 的 PersonaEvolution 通道统一完成 ───
        # 不再有独立 LLM 调用。bot 在主对话中用自己的人格自然思考态度。
        # 好感度变化靠 LifecycleService 互动频率 + 极端事件规则驱动。

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

    # ─── Hook: 自动注入记忆 ───

    @filter.on_llm_request(priority=5)
    async def inject_memory(self, event: AstrMessageEvent, req=None):
        """Inject only through the canonical scoped orchestrator; failures are fail-closed."""
        if not self.enable_auto_inject or not req:
            return
        message = event.get_message_str()
        if not message or len(message.strip()) < 4:
            return
        req.system_prompt = prepend_identity_safety_system_prompt(
            getattr(req, "system_prompt", ""), message, always=True
        )
        runtime_scope = getattr(event, "_wave_memory_runtime_scope", None)
        if (
            not isinstance(runtime_scope, RuntimeScope)
            or runtime_scope.visibility not in {"group", "private"}
            or runtime_scope.session is None
        ):
            logger.debug("[WaveMemory] injection skipped: resolved memory RuntimeScope required")
            return
        if not getattr(self, "injection_orchestrator_active_enabled", False):
            logger.warning("[WaveMemory] canonical injection orchestrator is disabled; data injection skipped")
            return
        bot_id = event.get_self_id() or ""
        sender_id = event.get_sender_id() or ""
        sender_name = ""
        if event.message_obj and event.message_obj.sender:
            sender_name = event.message_obj.sender.nickname or ""
        bot_profile = self._get_bot(bot_id)
        exclude_sources = bot_profile.exclude_sources if bot_profile and bot_profile.exclude_sources else None
        handled = await self._run_injection_active_trace(
            event=event,
            req=req,
            message=message,
            group_id=runtime_scope.session.conversation_id,
            sender_id=sender_id,
            sender_name=sender_name,
            bot_id=bot_id,
            bot_profile=bot_profile,
            runtime_scope=runtime_scope,
            exclude_sources=exclude_sources,
        )
        if not handled:
            logger.error("[WaveMemory] canonical injection failed closed; historical injection was not invoked")
            return
        try:
            reflection_prompt = self.reflection_trigger.build_prompt(
                scope=runtime_scope,
                message=message,
                sender_id=sender_id,
                trace_id=f"active-{getattr(req, 'trace_id', '')}",
            )
            if reflection_prompt:
                try:
                    from astrbot.core.agent.message import TextPart
                    req.extra_user_content_parts.append(TextPart(text=reflection_prompt))
                except Exception:
                    req.extra_user_content_parts.append({"type": "text", "text": reflection_prompt})
        except Exception as exc:
            logger.debug("[WaveMemory] reflection trigger skipped: %s", exc)

    # ─── Hook: 捕获消息 ───

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """捕获所有消息，异步写入记忆。"""
        # 消息入口只解析一次 RuntimeScope。任何失败都必须发生在 writer/领域写之前。
        resolved_event_context = None
        scope_resolver = getattr(self, "scope_resolver", None)
        if scope_resolver is None:
            scope_failure_reason = "scope_resolver_unavailable"
        else:
            try:
                resolved_event_context = scope_resolver.resolve_event(event)
                scope_failure_reason = "" if getattr(resolved_event_context, "scope", None) is not None else "scope_missing"
            except Exception as exc:
                scope_failure_reason = str(getattr(exc, "reason_code", "") or "scope_resolution_error")

        if scope_failure_reason:
            counters = getattr(self, "_scope_resolution_failed_total", None)
            if not isinstance(counters, dict):
                counters = {}
                self._scope_resolution_failed_total = counters
            counters[scope_failure_reason] = counters.get(scope_failure_reason, 0) + 1

            now = time.time()
            last_warnings = getattr(self, "_scope_resolution_last_warning", None)
            if not isinstance(last_warnings, dict):
                last_warnings = {}
                self._scope_resolution_last_warning = last_warnings
            if now - float(last_warnings.get(scope_failure_reason, 0.0)) >= 60.0:
                last_warnings[scope_failure_reason] = now
                logger.warning(
                    f"[WaveMemory] scope_resolution_failed reason={scope_failure_reason} "
                    f"count={counters[scope_failure_reason]}"
                )
                _record_err("ScopeResolution", scope_failure_reason)
            return

        runtime_scope = resolved_event_context.scope
        # on_message 是唯一的事件 Scope 解析点；后续 LLM hook 若接收同一事件，
        # 只可透传该对象，不得再次从原始字段推断身份或会话。
        try:
            setattr(event, "_wave_memory_runtime_scope", runtime_scope)
        except Exception:
            # 部分 AstrBot 事件实现可能禁止扩展属性；注入路径保持显式 optional。
            pass
        # 基础记忆只接受解析完成的群聊/私聊 RuntimeScope。兼容字段仍名为
        # group_id，但它只是当前 canonical conversation id，不能决定授权边界。
        if runtime_scope.visibility not in {"group", "private"} or runtime_scope.session is None:
            logger.debug("[WaveMemory] message capture skipped: memory RuntimeScope required")
            return
        sender_id = resolved_event_context.sender_local_id
        group_id = resolved_event_context.conversation_local_id
        bot_id = resolved_event_context.bot_self_id
        if runtime_scope.visibility == "group":
            group = getattr(getattr(event, "message_obj", None), "group", None)
            remember_group_name = getattr(self, "_remember_group_name", None)
            if callable(remember_group_name):
                remember_group_name(
                    runtime_scope.bot_id,
                    group_id,
                    getattr(group, "group_name", None),
                )
        message = event.get_message_str() or ""

        # 先探测图片，避免纯图片消息被文本长度门槛误杀
        images = []
        if hasattr(event, "message_obj") and event.message_obj and event.message_obj.message:
            for comp in event.message_obj.message:
                if comp.__class__.__name__ == "Image":
                    images.append(comp)

        if not message.strip() and images:
            message = "[图片]"

        if len(message.strip()) < self.min_message_length and not images:
            return

        # 平台会把 bot 自己发出的文本/图片回推成普通消息事件。
        # ignore_bot_messages=true 时必须在这里也截断；after_message_sent 不是唯一入口。
        if sender_id and (sender_id == bot_id or sender_id in self._bot_qq_ids):
            event.should_call_llm(False)
            if self.ignore_bot_messages:
                return
            if images:
                await self.writer.enqueue({
                    "scope": runtime_scope,
                    "group_id": group_id,
                    "sender_id": "bot",
                    "sender_name": self._get_bot_name(sender_id if sender_id in self._bot_qq_ids else bot_id),
                    "content": message,
                    "timestamp": time.time(),
                    "event_id": getattr(event, "message_id", None),
                })
            return

        if runtime_scope.visibility == "group":
            if self.group_whitelist and group_id not in self.group_whitelist:
                return
            if self.group_blacklist and group_id in self.group_blacklist:
                return

        # 平台重连/重试可能把同一条 inbound 消息再次投递。防抖只会合并
        # 短时间内的文本，不能阻止较晚重投再次进入默认 LLM 回复链；因此按
        # formal Scope + sender + 平台消息 ID 做一个短时、内存有界的 ingress 去重。
        raw_event_id = getattr(event, "message_id", None)
        if raw_event_id is None:
            raw_event_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        event_id = str(raw_event_id or "").strip()
        if event_id:
            delivery_key = (
                f"{runtime_scope.bot_id}:{runtime_scope.visibility}:"
                f"{runtime_scope.session.id}:{sender_id}:{event_id}"
            )
            seen_deliveries = getattr(self, "_recent_message_deliveries", None)
            if not isinstance(seen_deliveries, dict):
                seen_deliveries = {}
                self._recent_message_deliveries = seen_deliveries
            now_delivery = time.time()
            cutoff = now_delivery - 120.0
            for stale_key, seen_at in tuple(seen_deliveries.items()):
                if seen_at < cutoff:
                    seen_deliveries.pop(stale_key, None)
            if delivery_key in seen_deliveries:
                logger.info(
                    "[WaveMemory] duplicate message delivery ignored scope=%s event_id=%s",
                    runtime_scope.session.id,
                    event_id,
                )
                event.should_call_llm(False)
                event.stop_event()
                return
            seen_deliveries[delivery_key] = now_delivery
            if len(seen_deliveries) > 2048:
                oldest_keys = sorted(seen_deliveries, key=seen_deliveries.get)[:1024]
                for stale_key in oldest_keys:
                    seen_deliveries.pop(stale_key, None)

        # ─── 4s 消息合并防抖机制 (Debounce Coalescing) ───
        # 不重写 event.message_obj.message：底层组件链由 AstrBot/适配器维护，
        # 插件越级替换会让后续引用/发送阶段把组件结构当作 Plain 文本嵌套序列化。
        sender_name = ""
        if event.message_obj and event.message_obj.sender:
            sender_name = event.message_obj.sender.nickname or ""
        message_ts = time.time()

        debounce_key = (
            f"{runtime_scope.bot_id}:{runtime_scope.visibility}:"
            f"{runtime_scope.session.id}:{sender_id}"
        )
        if not hasattr(self, "_semantic_message_buffers"):
            self._semantic_message_buffers = {}

        now_ms = time.time()
        buffer = self._semantic_message_buffers.get(debounce_key)

        if buffer:
            # 已经有活动的防抖协程，将消息追加到缓冲区
            buffer["updated_ts"] = now_ms
            buffer["last_event_id"] = id(event)
            buffer["messages"].append({
                "sender_name": sender_name,
                "text": message,
                "images": images
            })
            # 挂起拦截：不再继续 LLM/下游处理，由首条协程合并后统一放行
            event.should_call_llm(False)
            event.stop_event()
            return
        else:
            # 本轮消息的起航者（首条消息）
            buffer = {
                "first_ts": now_ms,
                "updated_ts": now_ms,
                "messages": [{
                    "sender_name": sender_name,
                    "text": message,
                    "images": images
                }],
                "last_event_id": id(event)
            }
            self._semantic_message_buffers[debounce_key] = buffer

            try:
                while True:
                    now_time = time.time()
                    elapsed_since_update = now_time - buffer["updated_ts"]
                    elapsed_since_start = now_time - buffer["first_ts"]

                    if elapsed_since_start >= 12.0:
                        # 达到最长 12s 强制截断
                        break

                    remaining_debounce = 4.0 - elapsed_since_update
                    if remaining_debounce <= 0:
                        # 4s 内没有新消息，防抖正常结束
                        break

                    wait_time = min(remaining_debounce, 12.0 - elapsed_since_start)
                    await asyncio.sleep(wait_time)
            finally:
                # 无论如何，移除 buffer
                self._semantic_message_buffers.pop(debounce_key, None)

            # 首条协程醒来后，开始整合成大消息并修改当前 event 发送
            merged_texts = []
            all_images = []
            for msg_item in buffer["messages"]:
                s_name = msg_item["sender_name"] or "用户"
                txt = msg_item["text"]
                if txt.strip():
                    merged_texts.append(f"{s_name}: {txt}")
                if msg_item.get("images"):
                    all_images.extend(msg_item["images"])

            if len(buffer["messages"]) > 1:
                merged_content = "\n".join(merged_texts)
            else:
                merged_content = buffer["messages"][0]["text"]

            if not merged_content and all_images:
                merged_content = "[图片]"

            # 只更新纯文本视图供 WaveMemory 后续逻辑使用。
            # 不重写 event.message_obj.message：底层组件链由 AstrBot/适配器维护，
            # 插件越级替换会让后续引用/发送阶段把组件结构当作 Plain 文本嵌套序列化。
            event.message_str = merged_content

            # 放行给后面的逻辑使用
            message = merged_content

        # 委托给 InboundMessagePipeline 管道执行抢词咽回、串行处理、命令拦截与生命周期派发
        await self.inbound_pipeline.process_message(
            event=event,
            runtime_scope=runtime_scope,
            message=message,
            message_ts=message_ts,
            group_id=group_id,
            sender_id=sender_id,
            sender_name=sender_name,
            bot_id=bot_id,
        )

    # ─── Hook: 发送前清理并提取 impression 标记 ───


    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在发送消息前提取 <<impression:...>> 标记并更新画像，彻底从消息链中剥离标记。"""
        result = event.get_result()
        if not result or not result.chain:
            return

        import re as _re
        from astrbot.core.message.components import Plain

        _IMPRESSION_RE = r'(?:<<\s*impression\s*[:：](.+?)>>|\[\s*impression\s*[:：](.+?)\])'
        extracted_impression = None
        extracted_impact = 1.0

        # 遍历 Plain 组件，提取并移除标记
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                matches = list(_re.finditer(_IMPRESSION_RE, comp.text, flags=_re.DOTALL))
                if matches:
                    for m in matches:
                        imp_val = (m.group(1) or m.group(2) or "").strip()
                        body, impact = parse_impression_mark(imp_val)
                        if body:
                            extracted_impression = body
                            extracted_impact = impact
                    # 清除文本中所有 impression 标记（包括前置换行和多余空白）
                    cleaned_text = _re.sub(r'\s*' + _IMPRESSION_RE + r'\s*', '', comp.text, flags=_re.DOTALL)
                    comp.text = cleaned_text

        # 日常标记只攒未结算能量，不覆盖正式印象指针、不钉时间线。
        if extracted_impression:
            try:
                runtime_scope = getattr(event, "_wave_memory_runtime_scope", None)
                if not isinstance(runtime_scope, RuntimeScope):
                    resolver = getattr(self, "scope_resolver", None)
                    if resolver is not None:
                        resolved_context = resolver.resolve_event(event)
                        runtime_scope = getattr(resolved_context, "scope", None)

                if isinstance(runtime_scope, RuntimeScope) and runtime_scope.visibility == "group" and runtime_scope.session:
                    group_id = runtime_scope.session.conversation_id
                    principal = runtime_scope.subject_principal_id or ""
                    principal_prefix = f"{runtime_scope.session.platform_id}:user:"
                    sender_id = principal[len(principal_prefix):] if principal.startswith(principal_prefix) else ""
                    bot_db_id = runtime_scope.bot_id

                    if sender_id and group_id and sender_id != "bot":
                        persist_unsettled_trace(
                            self.db,
                            bot_id=bot_db_id,
                            user_id=sender_id,
                            group_id=group_id,
                            text=extracted_impression,
                            impact=extracted_impact,
                        )
                        exists = self.db.conn.execute(
                            "SELECT 1 FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                            (sender_id, group_id, bot_db_id),
                        ).fetchone()
                        if exists is None:
                            self.db.conn.execute(
                                "INSERT INTO user_profiles (user_id, group_id, bot_id, metadata, interaction_count, last_seen) VALUES (?, ?, ?, ?, 1, ?)",
                                (sender_id, group_id, bot_db_id, "{}", time.time()),
                            )
                            self.db.conn.commit()
            except Exception as _e:
                logger.debug(f"[WaveMemory] impression update in on_decorating_result failed: {_e}")

    @filter.after_message_sent()
    async def on_bot_sent(self, event: AstrMessageEvent):
        """捕获 bot 回复，写入记忆 + 异步更新好感度。"""
        if self.ignore_bot_messages:
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        from astrbot.core.message.components import Image, Plain
        parts = []
        has_image = False
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = (comp.text or "").strip()
                if text:
                    parts.append(text)
            elif isinstance(comp, Image):
                parts.append("[图片]")
                has_image = True
        bot_text = " ".join(parts).strip()
        if not bot_text:
            return
        if not has_image and len(bot_text) < 4:
            return

        # on_message 已解析过的 Scope 可直接复用；after_message_sent 单独触发时才
        # 通过同一 resolver 解析。绝不把私聊拼成伪 group，也不回退到默认 Bot。
        runtime_scope = getattr(event, "_wave_memory_runtime_scope", None)
        if not isinstance(runtime_scope, RuntimeScope):
            resolver = getattr(self, "scope_resolver", None)
            try:
                resolved_context = resolver.resolve_event(event) if resolver is not None else None
                runtime_scope = getattr(resolved_context, "scope", None)
            except Exception as exc:
                runtime_scope = None
                scope_failure_reason = str(getattr(exc, "reason_code", "") or "scope_resolution_error")
            else:
                scope_failure_reason = "" if isinstance(runtime_scope, RuntimeScope) else "scope_missing"
        else:
            scope_failure_reason = ""

        if scope_failure_reason or not isinstance(runtime_scope, RuntimeScope):
            reason = scope_failure_reason or "scope_missing"
            counters = getattr(self, "_scope_resolution_failed_total", None)
            if not isinstance(counters, dict):
                counters = {}
                self._scope_resolution_failed_total = counters
            counters[reason] = counters.get(reason, 0) + 1
            logger.warning("[WaveMemory] bot_sent_scope_resolution_failed reason=%s count=%s", reason, counters[reason])
            _record_err("BotSentScopeResolution", reason)
            return

        try:
            setattr(event, "_wave_memory_runtime_scope", runtime_scope)
        except Exception:
            pass

        if runtime_scope.visibility not in {"group", "private"} or runtime_scope.session is None:
            reason = "memory_scope_visibility_unsupported"
            logger.warning("[WaveMemory] bot_sent_scope_rejected reason=%s", reason)
            _record_err("BotSentScopeResolution", reason)
            return

        group_id = runtime_scope.session.conversation_id
        if runtime_scope.visibility == "group":
            group = getattr(getattr(event, "message_obj", None), "group", None)
            remember_group_name = getattr(self, "_remember_group_name", None)
            if callable(remember_group_name):
                remember_group_name(
                    runtime_scope.bot_id,
                    group_id,
                    getattr(group, "group_name", None),
                )
        principal = runtime_scope.subject_principal_id or ""
        principal_prefix = f"{runtime_scope.session.platform_id}:user:"
        sender_id = principal[len(principal_prefix):] if principal.startswith(principal_prefix) else ""
        bot_id = event.get_self_id() or ""
        bot_db_id = runtime_scope.bot_id

        # reply tracker 使用完整 Scope，避免群聊/私聊兼容 conversation id 相撞。
        if sender_id and group_id:
            reply_scope_key = f"{runtime_scope.bot_id}:{runtime_scope.visibility}:{runtime_scope.session.id}"
            self._reply_tracker[f"{sender_id}:{reply_scope_key}"] = time.time()
            # 清理 60s 前的旧记录（防止内存泄漏）
            now = time.time()
            if len(self._reply_tracker) > 200:
                self._reply_tracker = {k: v for k, v in self._reply_tracker.items() if now - v < 60}

        # 用户画像仍以 (user_id, group_id, bot_id) 建模，只能由群聊更新。
        if runtime_scope.visibility == "group" and sender_id and sender_id != "bot":
            try:
                self.db.conn.execute(
                    """UPDATE user_profiles 
                       SET interaction_count = COALESCE(interaction_count, 0) + 1,
                           last_seen = ?
                       WHERE user_id = ? AND group_id = ? AND bot_id = ?""",
                    (time.time(), sender_id, group_id, bot_db_id),
                )
                self.db.conn.commit()
            except Exception:
                pass

        await self.writer.enqueue({
            "scope": runtime_scope,
            "group_id": group_id,
            "sender_id": "bot",
            "sender_name": self._get_bot_name(bot_id),
            "content": bot_text,
            "timestamp": time.time(),
            "event_id": getattr(event, "message_id", None),
        })

        # 兜底：decorating 未跑时，日常观感只进未结算表，不写 metadata.impression。
        import re as _re
        _IMPRESSION_RE = r'(?:<<\s*impression\s*[:：](.+?)>>|\[\s*impression\s*[:：](.+?)\])'
        _impression_matches = list(_re.finditer(_IMPRESSION_RE, bot_text, _re.DOTALL))
        if _impression_matches and sender_id and group_id and sender_id != "bot":
            for _match in _impression_matches:
                _body, _impact = parse_impression_mark((_match.group(1) or _match.group(2) or "").strip())
                if _body:
                    try:
                        persist_unsettled_trace(
                            self.db,
                            bot_id=bot_db_id,
                            user_id=sender_id,
                            group_id=group_id,
                            text=_body,
                            impact=_impact,
                        )
                    except Exception as _e:
                        logger.debug(f"[WaveMemory] impression update fallback failed: {_e}")

        # 自省 read-model 仅支持群聊，private 不记录派生回复状态。
        if runtime_scope.visibility == "group" and self.self_reflect:
            self.self_reflect.record_reply(
                bot_text,
                group_id,
                bot_id=runtime_scope.bot_id,
                scope=runtime_scope,
                message_id=getattr(event, "message_id", None),
            )

    # ─── 后台任务 ───

    async def _async_cache_warmup(self):
        """保留兼容任务入口，但不预热无 Scope 的 legacy persona。"""
        logger.debug("[WaveMemory] persona cache warmup withheld: scope_migration_required")

    async def _on_memory_projection_refresh(self, event) -> None:
        """Invalidate dependent reads and coalesce pending-belief evidence refresh.

        Capacity is handled inline by index resize (v4.2.1 semantics); a full
        index is a steady state, not a fault, so it no longer queues a rebuild.

        PairSimilarity is no longer rebuilt on every tag-change bucket.  That path
        previously materialised an O(n^2) upper triangle (~2M pairs for 2k tags)
        every five minutes and was the dominant CPU/WAL amplifier.  Tag changes only
        invalidate the small read cache; a sparse rebuild runs at startup when the
        projection table is empty, or via an explicit maintenance request.

        After TagWorker commits tags, pending beliefs that already cited the
        memory must recompute evidence-v1.  This does not extract new beliefs
        and never auto-approves quarantine / non-direct gates.
        """
        if event.event_type not in {
            "memory.tags_applied",
            "memory.tags_corrected",
            "memory.tags_correction_undone",
            "tag.merge",
            "tag.deactivate",
            "tag.governance.applied",
            "tag.governance.compensated",
        }:
            return
        # Drop stale O(1) lookups only.  Do not schedule a full pair rebuild here.
        self.pair_sim_service.clear_cache()
        if event.event_type in {
            "memory.tags_applied",
            "memory.tags_corrected",
            "memory.tags_correction_undone",
        }:
            self._schedule_belief_tag_refresh(event)

    def _schedule_belief_tag_refresh(self, event) -> None:
        """Debounce Tag-driven evidence refresh onto one pending belief at a time."""
        engine = getattr(self, "belief_engine", None)
        if engine is None:
            return
        payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
        try:
            memory_id = int(payload.get("memory_id") or event.aggregate_id)
        except (TypeError, ValueError):
            return
        if memory_id <= 0:
            return
        try:
            scope = RuntimeScope.from_dict(payload.get("scope") or {})
        except Exception:
            return
        if scope.visibility != "group" or scope.session is None:
            return
        try:
            belief_ids = self.db.list_scoped_belief_ids_citing_memory(scope, memory_id)
        except Exception as exc:
            logger.debug("[WaveMemory] belief tag refresh lookup failed: %s", exc)
            return
        delay = float(getattr(self, "_belief_tag_refresh_delay_seconds", 0.4) or 0.4)
        pending = getattr(self, "_pending_belief_tag_refresh", None)
        if pending is None:
            pending = {}
            self._pending_belief_tag_refresh = pending
        for belief_id in belief_ids:
            key = (scope.bot_id, scope.session.id, scope.visibility, int(belief_id))
            previous = pending.get(key)
            if previous is not None and not previous.done():
                previous.cancel()
            try:
                pending[key] = self._spawn(
                    self._run_belief_tag_refresh(scope, int(belief_id), delay, key),
                    owner="belief",
                )
            except Exception as exc:
                logger.debug("[WaveMemory] belief tag refresh schedule failed: %s", exc)
                pending.pop(key, None)

    async def _run_belief_tag_refresh(
        self,
        scope: RuntimeScope,
        belief_id: int,
        delay: float,
        key: tuple[str, str, str, int],
    ) -> None:
        pending = getattr(self, "_pending_belief_tag_refresh", {})
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            engine = getattr(self, "belief_engine", None)
            if engine is None:
                return
            await asyncio.to_thread(engine.refresh_evidence_after_tags, scope, belief_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[WaveMemory] belief tag refresh failed: %s", exc)
        finally:
            current = pending.get(key)
            task = asyncio.current_task()
            if current is task:
                pending.pop(key, None)

    @staticmethod
    def _vector_backfill_predicate(*, after_id: int = 0) -> tuple[str, tuple[object, ...]]:
        """Return the exact canonical predicate for recoverable missing vectors."""
        return (
            """m.id>? AND m.vector IS NULL
                 AND m.resolution_state='resolved'
                 AND COALESCE(m.quarantine, 0)=0
                 AND m.visibility IN ('group', 'private')
                 AND COALESCE(m.bot_id, '')<>''
                 AND COALESCE(m.session_id, '')<>''
                 AND COALESCE(m.source, '') NOT IN ('noise', 'identity_quarantine')""",
            (int(after_id),),
        )

    async def _has_pending_vector_backfill(self) -> bool:
        """Check for eligible vectorless canonical memories without touching writes."""
        where, params = self._vector_backfill_predicate()

        def _read(connection):
            return connection.execute(
                f"SELECT 1 FROM memories m WHERE {where} LIMIT 1", params
            ).fetchone() is not None

        return bool(await self.write_gateway.coordinator.read(_read))

    async def _on_vector_backfill_requested(self) -> None:
        """Coalesce post-timeout recovery only after source rows were committed."""
        if await self._has_pending_vector_backfill():
            await self._queue_vector_backfill(reason="embedding_terminal_timeout")

    async def _queue_vector_backfill(self, *, reason: str) -> str:
        """Queue one bounded, resumable recovery chain for missing embeddings."""
        token = "memory-vector-backfill"
        request = await self.write_gateway.jobs.create_request(
            idempotency_key="maintenance:memory-vector-backfill:v1",
            kind="maintenance.vector_backfill.run",
            scope={"kind": "system_maintenance"},
            payload={"kind": "memory_vector_backfill", "reason": str(reason)},
        )
        run = await self.write_gateway.jobs.schedule_run(
            request_id=request.request_id,
            schedule_slot=token,
            cursor_generation=0,
            cursor={"phase": "queued", "after_id": 0, "reason": str(reason)},
            # An already-completed pass may be safely restarted after a later
            # timeout creates a new vectorless memory; an active pass is reused.
            reschedule_terminal=True,
        )
        return run.run_id

    async def _queue_maintenance_repair(self, kind: str, *, reason: str) -> str:
        """Idempotently queue a recoverable repair instead of mutating derived state inline."""
        manifest = None
        try:
            if kind == "memory_index":
                manifest = self.memory_index.read_manifest(verify_checksum=False)
            elif kind == "tag_index":
                manifest = self.tag_catalog_index.read_manifest(verify_checksum=False)
        except Exception:
            manifest = None
        generation = 0 if manifest is None else int(manifest.generation)
        watermark = await self.write_gateway.coordinator.committed_watermark()
        # A full hot index must not receive one rebuild request per incoming
        # event while its physical generation is already capacity-bound.
        # The generation changes only after a successful rebuild, naturally
        # opening the next coalescing window when capacity is reached again.
        token = maintenance_repair_token(
            kind,
            reason,
            watermark=watermark,
            generation=generation,
        )
        request = await self.write_gateway.jobs.create_request(
            idempotency_key=f"maintenance:{token}",
            kind=f"maintenance.{kind}.rebuild",
            scope={"kind": "system_maintenance"},
            payload={"kind": kind, "reason": reason, "preflight_token": token},
        )
        run = await self.write_gateway.jobs.schedule_run(
            request_id=request.request_id,
            schedule_slot=token,
            cursor_generation=generation + 1,
            cursor={"phase": "queued", "watermark": watermark},
            # If the same drift token already terminated without removing drift,
            # atomically advance the slot generation instead of replaying terminal state.
            reschedule_terminal=True,
        )
        return run.run_id

    async def _maintenance_run_vector_backfill(self, run, request, runner):
        """Recover one bounded batch of missing embeddings through scoped commands."""
        import numpy as np

        payload = request.payload if isinstance(request.payload, dict) else {}
        try:
            batch_size = max(1, min(int(payload.get("batch_size", 16)), 32))
        except (TypeError, ValueError):
            batch_size = 16
        cursor = run.cursor if isinstance(run.cursor, dict) else {}
        try:
            after_id = max(0, int(cursor.get("after_id", 0)))
            timeout_batches = max(0, int(cursor.get("timeout_batches", 0)))
        except (TypeError, ValueError):
            after_id, timeout_batches = 0, 0
        where, params = self._vector_backfill_predicate(after_id=after_id)

        def _snapshot(connection):
            return connection.execute(
                f"""SELECT m.id, m.content, m.bot_id, m.session_id, m.visibility, m.group_id
                      FROM memories m
                     WHERE {where}
                     ORDER BY m.id ASC
                     LIMIT ?""",
                (*params, batch_size),
            ).fetchall()

        rows = await self.write_gateway.coordinator.read(_snapshot)
        if not rows:
            return {
                "kind": "memory_vector_backfill",
                "status": "completed",
                "processed": 0,
                "updated": 0,
                "after_id": after_id,
            }

        selected: list[tuple[int, str, RuntimeScope]] = []
        skipped_scope = 0
        for memory_id, content, bot_id, session_id, visibility, group_id in rows:
            try:
                raw_session_id = str(session_id)
                platform_id, kind, conversation_id = raw_session_id.split(":", 2)
                canonical_visibility = str(visibility)
                if (
                    canonical_visibility not in {"group", "private"}
                    or kind != canonical_visibility
                    or conversation_id != str(group_id)
                ):
                    raise ValueError("noncanonical_session_id")
                scope = RuntimeScope(
                    bot_id=str(bot_id),
                    visibility=canonical_visibility,
                    session=SessionRef(
                        id=raw_session_id,
                        platform_id=platform_id,
                        kind=kind,
                        conversation_id=conversation_id,
                    ),
                )
                text = str(content or "").strip()
                if not text:
                    raise ValueError("empty_content")
            except (TypeError, ValueError):
                skipped_scope += 1
                continue
            selected.append((int(memory_id), text, scope))

        next_after_id = int(rows[-1][0])
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress={
                "phase": "embed",
                "selected": len(selected),
                "skipped_scope": skipped_scope,
                "after_id": next_after_id,
            },
            cursor={"phase": "embed", "after_id": after_id, "timeout_batches": timeout_batches},
        )

        vectors = await self.writer._embed_with_limited_retry([item[1] for item in selected]) if selected else []
        if vectors is None:
            # A terminal writer retry records a temporary failure. Requeue this
            # exact cursor only a small, explicit number of times; vector=NULL
            # remains the durable recoverable state after that limit.
            timeout_batches += 1
            retry_queued = False
            if timeout_batches < 3:
                retry = await self.write_gateway.jobs.schedule_run(
                    request_id=request.request_id,
                    schedule_slot=run.schedule_slot,
                    cursor_generation=run.cursor_generation + 1,
                    cursor={
                        "phase": "retry",
                        "after_id": after_id,
                        "timeout_batches": timeout_batches,
                    },
                )
                retry_queued = retry.run_id != run.run_id
            return {
                "kind": "memory_vector_backfill",
                "status": "retry_queued" if retry_queued else "deferred",
                "processed": len(selected),
                "updated": 0,
                "after_id": after_id,
                "timeout_batches": timeout_batches,
                "retry_queued": retry_queued,
            }

        vectors = list(vectors)
        if len(vectors) < len(selected):
            vectors.extend([None] * (len(selected) - len(vectors)))
        updated = 0
        skipped_vector = 0
        write_failures = 0
        for (memory_id, _text, scope), vector in zip(selected, vectors):
            if await self.write_gateway.jobs.cancellation_requested(run.run_id):
                return {
                    "kind": "memory_vector_backfill",
                    "status": "cancel_requested",
                    "processed": updated + skipped_vector + write_failures,
                    "updated": updated,
                    "after_id": after_id,
                }
            try:
                normalized = np.asarray(vector, dtype=np.float32)
                if normalized.ndim != 1 or normalized.size != self.dimension or not np.isfinite(normalized).all():
                    raise ValueError("embedding_dimension_or_finiteness_invalid")
                changed = await self.write_gateway.backfill_memory_vector(
                    scope=scope,
                    memory_id=memory_id,
                    vector=normalized,
                    idempotency_hint=f"{memory_id}:{hashlib.sha256(normalized.tobytes()).hexdigest()}",
                )
                updated += int(changed)
            except (TypeError, ValueError):
                skipped_vector += 1
            except Exception:
                write_failures += 1
                logger.warning(
                    "[WaveMemory] vector backfill write skipped memory_id=%s",
                    memory_id,
                    exc_info=True,
                )

        remaining_where, remaining_params = self._vector_backfill_predicate(after_id=next_after_id)
        has_remaining = await self.write_gateway.coordinator.read(
            lambda connection: connection.execute(
                f"SELECT 1 FROM memories m WHERE {remaining_where} LIMIT 1",
                remaining_params,
            ).fetchone() is not None
        )
        next_run_id = None
        if has_remaining:
            next_run = await self.write_gateway.jobs.schedule_run(
                request_id=request.request_id,
                schedule_slot=run.schedule_slot,
                cursor_generation=max(run.cursor_generation + 1, next_after_id),
                cursor={"phase": "queued", "after_id": next_after_id, "timeout_batches": 0},
            )
            next_run_id = next_run.run_id
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={
                "phase": "write",
                "processed": len(selected),
                "updated": updated,
                "skipped_scope": skipped_scope,
                "skipped_vector": skipped_vector,
                "write_failures": write_failures,
            },
            cursor={"phase": "write", "after_id": next_after_id, "timeout_batches": 0},
        )
        return {
            "kind": "memory_vector_backfill",
            "status": "continued" if next_run_id else "completed",
            "processed": len(selected),
            "updated": updated,
            "skipped_scope": skipped_scope,
            "skipped_vector": skipped_vector,
            "write_failures": write_failures,
            "after_id": next_after_id,
            "next_run_id": next_run_id,
        }

    async def _maintenance_rebuild_memory_index(self, run, request, runner):
        """Build one bounded, Tag-admitted hot HNSW generation from canonical state."""
        import numpy as np

        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={"phase": "snapshot"},
            cursor={"phase": "snapshot"},
        )

        # Hold the projection lock before the writer-serialized snapshot and keep
        # it through swap/save.  Committed events after the snapshot then project
        # in order against the new generation instead of being overwritten by it.
        async with self.memory_index_projection._lock:
            def _snapshot(connection):
                candidates = select_hot_memory_candidates(
                    connection,
                    self.memory_index_policy,
                    self.dimension,
                )
                rows = [
                    (candidate.memory_id, np.asarray(candidate.vector, dtype=np.float32).copy())
                    for candidate in candidates
                    if candidate.vector is not None
                ]
                return candidates, rows, OutboxRepository.committed_watermark(connection)

            candidates, rows, watermark = await self.write_gateway.coordinator.read(_snapshot)

            def _build_fresh_index() -> VectorIndex:
                fresh_index = VectorIndex(
                    dimension=self.dimension,
                    max_elements=self.memory_index_policy.max_vectors,
                    index_path=None,
                    kind="memory",
                    allow_resize=True,
                )
                if rows:
                    fresh_index.add(
                        [int(row[0]) for row in rows],
                        np.asarray([row[1] for row in rows], dtype=np.float32),
                    )
                return fresh_index

            # HNSW construction is CPU- and allocation-heavy; never block the
            # AstrBot event loop while publishing a replacement generation.
            fresh = await asyncio.to_thread(_build_fresh_index)
            with self.memory_index._lock:
                self.memory_index.index = fresh.index
                self.memory_index.max_elements = fresh.max_elements
                self.memory_index.allow_resize = True
            manifest = await asyncio.to_thread(
                self.memory_index.save,
                db_watermark=int(watermark),
            )
            self.memory_index_projection._dirty = False
            self.memory_index_projection.set_hot_membership(candidates)
        return {
            "kind": "memory_index",
            "count": len(rows),
            "capacity": self.memory_index_policy.max_vectors,
            "per_scope_capacity": self.memory_index_policy.per_scope_max_vectors,
            "generation": None if manifest is None else manifest.generation,
            "db_watermark": int(watermark),
            "verified": manifest is not None and manifest.count == len(rows),
        }

    async def _maintenance_rebuild_tag_index(self, run, request, runner):
        """Rebuild the canonical semantic Tag Catalog HNSW under a hard capacity."""
        import numpy as np

        from .services.tag_index_capacity import hard_capacity, select_bounded_tag_vectors

        capacity = hard_capacity(self.tag_index_max_vectors, default=1)
        expected = self.dimension * np.dtype(np.float32).itemsize

        def _snapshot(connection):
            catalog_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'"
            ).fetchone()
            if not catalog_exists:
                return [], OutboxRepository.committed_watermark(connection)
            # Prefer frequently used active Catalog rows; never auto-expand past capacity.
            rows = connection.execute(
                """
                SELECT id, embedding
                FROM tag_catalog
                WHERE embedding IS NOT NULL AND status='active'
                ORDER BY COALESCE(updated_at, created_at, 0) DESC, id ASC
                LIMIT ?
                """,
                (capacity,),
            ).fetchall()
            return rows, OutboxRepository.committed_watermark(connection)

        rows, watermark = await self.write_gateway.coordinator.read(_snapshot)
        selected = select_bounded_tag_vectors(
            rows,
            capacity=capacity,
            dimension=self.dimension,
            vector_bytes_expected=expected,
        )
        valid_rows = [
            (tag_id, np.frombuffer(blob, dtype=np.float32).copy())
            for tag_id, blob in selected
        ]
        fresh = VectorIndex(
            dimension=self.dimension,
            max_elements=capacity,
            index_path=None,
            kind="tag_catalog",
            allow_resize=True,
        )
        if valid_rows:
            fresh.add(
                [row[0] for row in valid_rows],
                np.asarray([row[1] for row in valid_rows], dtype=np.float32),
            )
        with self.tag_catalog_index._lock:
            self.tag_catalog_index.index = fresh.index
            self.tag_catalog_index.max_elements = capacity
            self.tag_catalog_index.allow_resize = True
        manifest = await asyncio.to_thread(
            self.tag_catalog_index.save,
            db_watermark=int(watermark),
        )
        return {
            "kind": "tag_index",
            "source": "tag_catalog",
            "count": len(valid_rows),
            "capacity": capacity,
            "truncated": True,
            "generation": None if manifest is None else manifest.generation,
            "db_watermark": int(watermark),
            "verified": manifest is not None and manifest.count == len(valid_rows),
        }

    async def _read_proactive_relationship_context(
        self,
        scope: RuntimeScope | None,
        event,
        *,
        now: float | None = None,
    ) -> dict:
        """Read formal relationship snapshots for the current human message turn."""
        return await read_proactive_relationship_context(
            scope,
            event,
            coordinator=getattr(getattr(self, "write_gateway", None), "coordinator", None),
            repository=getattr(getattr(self, "db", None), "soul_repository", None),
            bot_ids=getattr(self, "_bot_qq_ids", ()),
            now=now,
        )

    async def _record_proactive_timeline(
        self,
        scope: RuntimeScope,
        reply_text: str,
        policy: dict,
        source_memories: list[dict],
    ) -> int:
        """Audit a sent proactive reply through the writer-owned transaction."""
        return await record_proactive_timeline(
            scope,
            reply_text,
            policy,
            source_memories,
            coordinator=getattr(getattr(self, "write_gateway", None), "coordinator", None),
            repository=getattr(getattr(self, "db", None), "soul_repository", None),
        )

    def _get_recent_messages(
        self,
        event,
        *,
        scope: RuntimeScope | None,
        max_messages: int = 8,
    ) -> list[str]:
        """仅返回当前已解析基础记忆 Scope 内的正式记忆；无法证明 Scope 时空返回。"""
        if (
            not isinstance(scope, RuntimeScope)
            or scope.visibility not in {"group", "private"}
            or scope.session is None
        ):
            return []
        try:
            rows = self.db.conn.execute(
                """SELECT content FROM memories
                   WHERE bot_id=? AND session_id=? AND visibility=?
                     AND resolution_state='resolved' AND quarantine=0
                     AND content IS NOT NULL
                   ORDER BY id DESC LIMIT ?""",
                (scope.bot_id, scope.session.id, scope.visibility, max_messages),
            ).fetchall()
            return [r[0] for r in reversed(rows)] if rows else []
        except Exception:
            return []

    async def _maintenance_run_import(self, run, request, runner):
        """Execute serializable Import requests under a durable lease and fail closed on unresolved scope."""
        mode = str(request.payload.get("mode") or "legacy")
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress={"phase": "preflight", "mode": mode},
            cursor={"phase": "preflight"},
        )

        if mode == "legacy":
            from .webui.importer import WaveMemoryImporter

            source = str(request.payload.get("source") or "")
            importer = WaveMemoryImporter(
                self.db,
                self.embedding_service,
                self.tag_extractor,
                memory_index=None,
                writer=None,
            )
            last = {}
            async for raw_event in importer.run(
                source=source,
                re_embed=bool(request.payload.get("re_embed", True)),
                extract_tags=bool(request.payload.get("extract_tags", True)),
                batch_size=max(1, min(int(request.payload.get("batch_size", 20)), 100)),
            ):
                try:
                    last = json.loads(raw_event)
                except (TypeError, ValueError, json.JSONDecodeError):
                    last = {"message": str(raw_event)}
                await self.write_gateway.jobs.update_progress(
                    run.run_id,
                    lease_owner=runner.lease_owner,
                    lease_seconds=120.0,
                    progress=last,
                    cursor={"phase": last.get("status", "running")},
                )
            return last

        if mode == "discovered_source":
            from .webui.source_discovery import SourceDiscovery

            source_id = str(request.payload.get("source_id") or "")
            source = next(
                (item for item in SourceDiscovery().discover_all() if item.get("id") == source_id),
                None,
            )
            if source is None:
                raise RuntimeError("import_source_not_found")
            target = str((source.get("adapter") or {}).get("target", "memories"))
            if target == "memories" or source.get("type") != "known":
                return {
                    "status": "blocked",
                    "reason_code": "unresolved_import_not_supported",
                    "source_id": source_id,
                    "message": "Import source has no verified RuntimeScope binding.",
                }
            return {
                "status": "blocked",
                "reason_code": "domain_import_gateway_required",
                "source_id": source_id,
                "target": target,
                "message": "Non-memory imports require a target-specific coordinator command.",
            }

        raise RuntimeError("import_mode_invalid")

    async def _maintenance_run_tag_backfill(self, run, request, runner):
        """Extract one bounded scoped Tag batch under a durable lease."""
        from .webui.tag_execution import tag_memory_batch

        batch_size = max(1, min(int(request.payload.get("batch_size", 20)), 50))
        min_length = max(0, int(request.payload.get("skip_short_min_length", 10)))

        def _snapshot(connection):
            return connection.execute(
                """SELECT m.id, m.content, m.sender_name, o.payload_json
                   FROM memories m
                   JOIN domain_outbox o
                     ON o.aggregate_kind='memory'
                    AND o.aggregate_id=CAST(m.id AS TEXT)
                    AND o.event_type='memory.created'
                   WHERE NOT EXISTS (
                       SELECT 1 FROM scoped_memory_tags smt WHERE smt.memory_id=m.id
                   )
                     AND m.resolution_state='resolved'
                     AND COALESCE(m.quarantine, 0)=0
                     AND LENGTH(COALESCE(m.content, '')) >= ?
                   ORDER BY m.id ASC LIMIT ?""",
                (min_length, batch_size),
            ).fetchall()

        rows = await self.write_gateway.coordinator.read(_snapshot)
        messages = []
        for memory_id, content, sender_name, payload_json in rows:
            try:
                scope = json.loads(payload_json).get("scope")
            except (TypeError, ValueError, json.JSONDecodeError):
                scope = None
            if isinstance(scope, dict):
                messages.append({
                    "id": int(memory_id),
                    "content": str(content or "")[:800],
                    "sender": str(sender_name or ""),
                    "scope": scope,
                })

        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress={"phase": "extract", "selected": len(messages)},
            cursor={"phase": "extract", "after_id": messages[-1]["id"] if messages else 0},
        )
        result = await asyncio.wait_for(
            tag_memory_batch(
                self.db,
                self.embedding_service,
                self.tag_extractor,
                messages,
                tag_batch_size=batch_size,
                tag_write_policy="missing_only",
                skip_short_min_length=min_length,
                write_gateway=self.write_gateway,
            ),
            timeout=110.0,
        )
        return {**result, "bounded": True}

    async def _maintenance_run_tag_audit(self, run, request, runner):
        """Run LLM Tag audit as a resumable durable job, never in an SSE request."""
        from .services.tag_auditor import TagAuditor

        provider_id = str(request.payload.get("provider_id") or self.tag_llm_provider_id or "")
        if not provider_id:
            raise RuntimeError("tag_audit_provider_not_configured")
        strategy = str(request.payload.get("strategy", "mixed"))
        if strategy not in {"mixed", "low_quality", "high_freq"}:
            raise RuntimeError("tag_audit_strategy_invalid")
        total_count = max(10, min(int(request.payload.get("total_count", 500)), 2000))
        batch_size = max(1, min(int(request.payload.get("batch_size", 50)), 100))
        auditor = TagAuditor(
            db=self.db,
            context=self.context,
            provider_id=provider_id,
        )

        async def _publish(suggestion):
            action = suggestion.get("action")
            if action == "merge":
                tag_ids = json.dumps(suggestion.get("source_ids", []))
                target_name = suggestion.get("target_name", "")
                target_type = suggestion.get("target_type", "")
            elif action == "retype":
                tag_ids = json.dumps([suggestion.get("tag_id")])
                target_name = None
                target_type = suggestion.get("new_type", "")
            elif action == "delete":
                tag_ids = json.dumps([suggestion.get("tag_id")])
                target_name = None
                target_type = None
            else:
                return

            def _insert(connection):
                connection.execute(
                    """
                    INSERT INTO tag_audit_suggestions(
                        action, tag_ids, target_name, target_type,
                        reason, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        action,
                        tag_ids,
                        target_name,
                        target_type,
                        suggestion.get("reason", ""),
                        time.time(),
                    ),
                )

            await self.write_gateway.coordinator.transaction(
                _insert,
                actor="maintenance.tag_audit",
            )

        last_event = {"progress": 0, "processed": 0, "total_suggestions": 0}
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            lease_seconds=120.0,
            progress=last_event,
            cursor={"processed": 0, "strategy": strategy},
        )
        iterator = auditor.run_audit(
            batch_size=batch_size,
            strategy=strategy,
            total_count=total_count,
            save_suggestion=_publish,
        ).__aiter__()
        while True:
            if await self.write_gateway.jobs.cancellation_requested(run.run_id):
                return {**last_event, "cancelled": True}
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=110.0)
            except StopAsyncIteration:
                break
            last_event = dict(event)
            await self.write_gateway.jobs.update_progress(
                run.run_id,
                lease_owner=runner.lease_owner,
                lease_seconds=120.0,
                progress=last_event,
                cursor={
                    "processed": int(last_event.get("processed", 0)),
                    "strategy": strategy,
                },
            )
        return last_event

    async def _maintenance_rebuild_pair_similarity(self, run, request, runner):
        """Compute a sparse Top-K PairSimilarity projection off the event loop.

        Payload knobs (all optional, hard-capped):
        - ``max_tags``: highest-frequency tags admitted to the rebuild set.
        - ``top_k``: neighbors retained per tag.
        - ``min_similarity``: floor similarity for a retained edge.
        """
        from .services.pair_similarity_projection import (
            ABSOLUTE_MAX_TAGS,
            ABSOLUTE_TOP_K,
            DEFAULT_MAX_TAGS,
            DEFAULT_MIN_SIMILARITY,
            DEFAULT_TOP_K,
        )

        payload = request.payload if isinstance(request.payload, dict) else {}
        try:
            max_tags = max(2, min(int(payload.get("max_tags", DEFAULT_MAX_TAGS)), ABSOLUTE_MAX_TAGS))
        except (TypeError, ValueError):
            max_tags = DEFAULT_MAX_TAGS
        try:
            top_k = max(1, min(int(payload.get("top_k", DEFAULT_TOP_K)), ABSOLUTE_TOP_K))
        except (TypeError, ValueError):
            top_k = DEFAULT_TOP_K
        try:
            min_similarity = float(payload.get("min_similarity", DEFAULT_MIN_SIMILARITY))
        except (TypeError, ValueError):
            min_similarity = DEFAULT_MIN_SIMILARITY

        def _snapshot(connection):
            return connection.execute(
                """
                SELECT id, vector
                FROM tags
                WHERE vector IS NOT NULL
                ORDER BY frequency DESC, id ASC
                LIMIT ?
                """,
                (max_tags,),
            ).fetchall()

        rows = await self.write_gateway.coordinator.read(_snapshot)
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={
                "phase": "compute",
                "tags": len(rows),
                "top_k": top_k,
                "min_similarity": min_similarity,
            },
            cursor={"phase": "compute", "tags": len(rows)},
        )

        params, cache = await asyncio.to_thread(
            self.pair_sim_service.compute_projection,
            rows,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        def _publish(connection):
            connection.execute("DELETE FROM tag_pair_similarity")
            if params:
                connection.executemany(
                    """
                    INSERT INTO tag_pair_similarity(
                        tag_id_a, tag_id_b, similarity, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    params,
                )

        await self.write_gateway.coordinator.transaction(_publish)
        self.pair_sim_service.install_projection(cache)
        return {
            "tags": len(rows),
            "pairs": len(params),
            "max_tags": max_tags,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "mode": "sparse_top_k",
        }

    async def _maintenance_rebuild_cooccurrence(self, run, request, runner):
        """Force one serialized scheduler rebuild for an operator maintenance request."""
        await self.write_gateway.jobs.update_progress(
            run.run_id,
            lease_owner=runner.lease_owner,
            progress={"phase": "rebuild"},
            cursor={"phase": "rebuild"},
        )
        scheduler_metrics = await self.cooccurrence_projection.force_rebuild(
            reason="maintenance.cooccurrence.rebuild"
        )
        watermark = await self.write_gateway.coordinator.committed_watermark()
        return {
            "kind": "cooccurrence",
            "nodes": self.cooccurrence.node_count,
            "edges": self.cooccurrence.edge_count,
            "db_watermark": int(watermark),
            "scheduler": scheduler_metrics,
            "verified": True,
        }

    async def _on_cooccurrence_rebuilt(self):
        """共现矩阵重建完成后，重算内生残差（30分钟最小间隔）。"""
        # 最小间隔保护
        now = time.time()
        last_ts = getattr(self, '_last_residual_compute_ts', 0)
        if now - last_ts < 1800:  # 30 分钟
            return
        self._last_residual_compute_ts = now

        try:
            residuals = await asyncio.to_thread(self.intrinsic_residual.compute_all)
            if residuals:
                self.intrinsic_residual.persist(residuals)
                if self.spike_router:
                    self.spike_router.residual_map = residuals
                self.cooccurrence.residual_map = residuals
        except Exception as e:
            logger.warning(f"[WaveMemory] Intrinsic residual computation failed: {e}")
            _record_err("IntrinsicResidual", e)

    async def _init_epa(self):
        """EPA 初始化（在线程池中执行，避免阻塞事件循环）。"""
        await asyncio.to_thread(self.epa.initialize)
