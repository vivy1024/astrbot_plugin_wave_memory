"""消息入口：收消息、发送前清理、Bot 回复入库、Runtime 观察写入、群名（从 main.py 拆出的 WaveMemoryPlugin mixin）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Optional
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from ..domain.display_name import sanitize_display_name
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



def reply_tracker_key(bot_scope_id: str, group_id: str, sender_id: str) -> str:
    """ABA 连续对话的回复记录键：同一 Bot、同一群、同一个人。"""
    return f"{bot_scope_id}:{group_id}:{sender_id}"

class IngressMixin:
    async def _handle_message(self, event: AstrMessageEvent):
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

        ingress_health = getattr(self, "ingress_health", None)
        if scope_failure_reason:
            if ingress_health is not None:
                try:
                    self_id = str(event.get_self_id() or "")
                except Exception:
                    self_id = ""
                ingress_health.record_rejected(scope_failure_reason, self_id=self_id)
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
        if ingress_health is not None:
            ingress_health.record_accepted()
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

        # ─── 消息合并防抖机制 (Debounce Coalescing)，窗口见热参数 ingress.debounce_* ───
        # 不重写 event.message_obj.message：底层组件链由 AstrBot/适配器维护，
        # 插件越级替换会让后续引用/发送阶段把组件结构当作 Plain 文本嵌套序列化。
        sender_name = ""
        if event.message_obj and event.message_obj.sender:
            sender_name = sanitize_display_name(event.message_obj.sender.nickname)
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
            hot = getattr(self, "hot_config", None)
            debounce_window = float(hot.get("ingress.debounce_seconds", 4.0)) if hot else 4.0
            debounce_max = max(debounce_window, float(hot.get("ingress.debounce_max_seconds", 12.0)) if hot else 12.0)

            try:
                while True:
                    now_time = time.time()
                    elapsed_since_update = now_time - buffer["updated_ts"]
                    elapsed_since_start = now_time - buffer["first_ts"]

                    if elapsed_since_start >= debounce_max:
                        # 达到最长等待，强制截断
                        break

                    remaining_debounce = debounce_window - elapsed_since_update
                    if remaining_debounce <= 0:
                        # 合并窗口内没有新消息，防抖正常结束
                        break

                    wait_time = min(remaining_debounce, debounce_max - elapsed_since_start)
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

        # 惦记的人出现：没点名羽书也让她开口（关切驱动的主动跟进）。回复走正常链路，
        # 注入阶段会附上「你惦记的事」提示（见 injection._handle_meta_thinking_check）。
        followup = getattr(self, "concern_followup", None)
        if (
            followup is not None
            and runtime_scope.visibility == "group"
            and not getattr(event, "is_at_or_wake_command", False)
        ):
            concern = followup.claim_followup(runtime_scope, sender_id, sender_name, message)
            if concern is not None:
                event.is_at_or_wake_command = True
                try:
                    event._wave_memory_concern_followup = {"concern": concern, "proactive": True}
                except Exception:
                    pass
                logger.info(
                    "[WaveMemory] 关切跟进：%s 出现，主动回应 concern:%s（%s）",
                    sender_name or sender_id, concern["id"], concern["topic"][:40],
                )

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
        # 写入与读取必须用同一个键：此前写入用「用户:bot:可见性:会话」、读取用「用户:群号」，这条规则从未触发过。
        engage_scope = getattr(event, "_wave_memory_runtime_scope", None)
        engage_bot = engage_scope.bot_id if isinstance(engage_scope, RuntimeScope) else ""
        last_reply_ts = self._reply_tracker.get(reply_tracker_key(engage_bot, group_id, sender_id), 0)
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
            from ..services.impression_timeline import load_timeline_cue
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

    async def _handle_decorating_result(self, event: AstrMessageEvent):
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

    async def _handle_bot_sent(self, event: AstrMessageEvent):
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

        group = getattr(getattr(event, "message_obj", None), "group", None)
        await self._process_bot_reply(
            runtime_scope=runtime_scope,
            bot_text=bot_text,
            message_id=event_message_id(event),
            bot_self_id=event.get_self_id() or "",
            group_name=getattr(group, "group_name", None),
        )

    async def _process_bot_reply(
        self,
        *,
        runtime_scope: RuntimeScope,
        bot_text: str,
        message_id=None,
        bot_self_id: str = "",
        group_name: str | None = None,
    ) -> None:
        """Bot 自己说出的话：入库、互动计数、未结算印象、自省记录。

        AstrBot 的 after_message_sent 与 Runtime API（Cortico 的 qq.self / 直播口播）共用这一段。
        """
        group_id = runtime_scope.session.conversation_id
        if runtime_scope.visibility == "group":
            remember_group_name = getattr(self, "_remember_group_name", None)
            if callable(remember_group_name):
                remember_group_name(runtime_scope.bot_id, group_id, group_name)
        principal = runtime_scope.subject_principal_id or ""
        principal_prefix = f"{runtime_scope.session.platform_id}:user:"
        sender_id = principal[len(principal_prefix):] if principal.startswith(principal_prefix) else ""
        bot_id = bot_self_id
        bot_db_id = runtime_scope.bot_id

        # reply tracker 使用完整 Scope，避免群聊/私聊兼容 conversation id 相撞。
        if sender_id and group_id:
            self._reply_tracker[reply_tracker_key(runtime_scope.bot_id, group_id, sender_id)] = time.time()
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
            "sender_name": self._get_bot_name(bot_id) if bot_id else self._bot_display_name(bot_db_id),
            "content": bot_text,
            "timestamp": time.time(),
            "event_id": message_id,
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
                message_id=message_id,
            )

        # 事后记账：这段对话停下来之后单独回看一遍（见 services/post_reply_bookkeeping.py）
        bookkeeper = getattr(self, "post_reply_bookkeeper", None)
        if bookkeeper is not None and runtime_scope.visibility == "group":
            try:
                bookkeeper.schedule(replace(runtime_scope, subject_principal_id=None))
            except Exception as exc:
                logger.debug(f"[WaveMemory] bookkeeping schedule failed: {exc!r}")

    def _bot_display_name(self, db_id: str) -> str:
        profile = self.bot_registry.get(db_id) if getattr(self, "bot_registry", None) else None
        return profile.name if profile else "bot"

    async def ingest_observation(self, observation: dict, runtime_scope: RuntimeScope) -> dict:
        """宿主无关的写入入口：Runtime API 的一条观察走与 AstrBot 消息钩子相同的处理。

        ``kind``：``message``（别人说的话，默认）、``self``（Bot 自己说的话）。
        别人说的话进 InboundMessagePipeline（记住/忘记/teach、入库、黑话积累、纠错自省、
        好感触达）；Bot 自己的话进 ``_process_bot_reply``。``event_id`` 用平台原始消息号，
        与 AstrBot 路径同会话同消息号去重。
        """
        if runtime_scope.visibility not in {"group", "private"} or runtime_scope.session is None:
            return {"status": "rejected", "error": "memory_scope_visibility_unsupported"}
        content = str(observation.get("content") or "").strip()
        if not content:
            return {"status": "rejected", "error": "empty_content"}
        kind = str(observation.get("kind") or "message").strip().lower()
        event_id = observation.get("event_id")
        group_id = runtime_scope.session.conversation_id
        profile = self.bot_registry.get(runtime_scope.bot_id)
        group_name = observation.get("group_name")
        if kind == "self":
            await self._process_bot_reply(
                runtime_scope=runtime_scope,
                bot_text=content,
                message_id=event_id,
                bot_self_id=profile.qq_id if profile else "",
                group_name=group_name,
            )
            return {"status": "accepted", "kind": "self"}
        if kind not in {"message", "danmaku"}:
            return {"status": "rejected", "error": f"unknown_kind:{kind}"}
        principal = runtime_scope.subject_principal_id or ""
        prefix = f"{runtime_scope.session.platform_id}:user:"
        sender_id = str(observation.get("sender_id") or (principal[len(prefix):] if principal.startswith(prefix) else "")).strip()
        if not sender_id:
            return {"status": "rejected", "error": "sender_id_required"}
        if runtime_scope.visibility == "group" and group_name:
            self._remember_group_name(runtime_scope.bot_id, group_id, group_name)
        marker = " ".join(profile.self_ids) if (profile and observation.get("is_at_bot")) else ""
        event = _ObservationEvent(message_id=event_id, message_str=f"{content} {marker}".strip())
        await self.inbound_pipeline.process_message(
            event=event,
            runtime_scope=runtime_scope,
            message=content,
            message_ts=float(observation.get("timestamp") or time.time()),
            group_id=group_id,
            sender_id=sender_id,
            sender_name=str(observation.get("sender_name") or ""),
            bot_id=profile.qq_id if profile else "",
        )
        return {"status": "accepted", "kind": kind}

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
