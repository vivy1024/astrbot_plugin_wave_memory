"""LLM 请求钩子：MetaThinking 态度、工具过滤、记忆注入编排（从 main.py 拆出的 WaveMemoryPlugin mixin）。"""

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


class InjectionMixin:
    async def _handle_meta_thinking_check(self, event: AstrMessageEvent, req=None):
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
                from ..services.impression_timeline import timeline_cue_prompt
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

        concern_prompt = self._concern_followup_prompt(event, sender_id)
        if concern_prompt:
            try:
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(TextPart(text=concern_prompt))
            except Exception:
                extra = getattr(req, "extra_user_content_parts", None)
                if extra is None:
                    req.extra_user_content_parts = extra = []
                extra.append({"type": "text", "text": concern_prompt})

        # ─── 硬规则：极端攻击 + 辱骂冷却 ───
        from ..services.meta_thinking import EXTREME_ATTACK

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

    async def _handle_filter_bot_tools(self, event: AstrMessageEvent, req=None):
        registry = getattr(self, "tool_registry", None)
        func_tool = getattr(req, "func_tool", None) if req is not None else None
        if registry is None or func_tool is None:
            return
        runtime_scope = getattr(event, "_wave_memory_runtime_scope", None)
        profile = self.bot_registry.get(runtime_scope.bot_id) if isinstance(runtime_scope, RuntimeScope) else None
        try:
            removed = registry.filter_request_tools(func_tool, profile)
        except Exception as exc:
            logger.debug(f"[WaveMemory] tool filter skipped: {exc}")
            return
        if removed:
            logger.debug(f"[WaveMemory] tools hidden for this request: {removed}")

    async def _handle_inject_memory(self, event: AstrMessageEvent, req=None):
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

    def _concern_followup_prompt(self, event, sender_id: str) -> str:
        """「你惦记的事」提示：主动开口时必附；本来就要回复这个人时，没在冷却期内也附一次。"""
        followup = getattr(self, "concern_followup", None)
        if followup is None:
            return ""
        sender_name = ""
        if event.message_obj and event.message_obj.sender:
            sender_name = event.message_obj.sender.nickname or ""
        claimed = getattr(event, "_wave_memory_concern_followup", None)
        if isinstance(claimed, dict) and claimed.get("concern"):
            return followup.hint(claimed["concern"], sender_name, proactive=True)
        scope = getattr(event, "_wave_memory_runtime_scope", None)
        if not isinstance(scope, RuntimeScope) or not sender_id:
            return ""
        try:
            pending = [
                item for item in followup.open_concerns_for(scope, sender_id, sender_name)
                if not followup.recently_followed(item["id"])
            ]
        except Exception as exc:
            logger.debug("[WaveMemory] concern followup lookup failed: %s", exc)
            return ""
        if not pending:
            return ""
        followup.mark_followed(pending[0]["id"])
        return followup.hint(pending[0], sender_name, proactive=False)

    def _set_injection_channel_config(self, config) -> None:
        """WebUI 热应用通道配置时更新运行时注入编排器配置。"""
        self.injection_channel_config = config

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
        from ..services.config.channel_config import build_channel_config_from_plugin_config
        profile = self.bot_registry.get(scope.bot_id) if getattr(self, "bot_registry", None) else None
        config = build_channel_config_from_plugin_config(
            self.config,
            scope=scope,
            bot_channels=getattr(profile, "channels", None) or None,
        )
        registry = getattr(self, "channel_registry", None)
        return registry.with_external_defaults(config) if registry is not None else config

    def _build_shadow_context_config(self, *, channel_config, exclude_sources, recent_context: list[str], realtime_ctx: dict) -> dict:
        from ..services.impression_timeline import normalize_timeline_half_life

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
            from ..services.injection.context import InjectionContext
            from ..services.injection.shadow import run_injection_shadow

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
            from ..services.injection.context import InjectionContext
            from ..services.injection.active import run_injection_active
            from ..utils.perf import get_perf_tracker

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
