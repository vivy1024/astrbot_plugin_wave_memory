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
from ..domain.scope import RuntimeScope, scene_key
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
from ..services.inbound_message_handler import InboundMessagePipeline, bot_reply_event_id, event_message_id
from ..services.game_events import GameEventError, game_event_payload
from ..services.backup_lifecycle import DatabaseBackupManager
from ..services.runtime_mode import effective_native_injection_enabled, effective_query_feature, resolve_runtime_mode, runtime_capability_enabled, should_self_heal_advanced_query
from ..services.compat import build_duplicate_memory_warnings, build_livingmemory_compat_surface, detect_memory_plugins
from ..services.impression_timeline import configure_social_limits, parse_impression_mark, parse_impression_polarity, persist_unsettled_trace
from ..services.lifecycle import LifecycleService
from ..tools.livingmemory_compat_tools import build_livingmemory_compat_tools
from ..engine.book_lore_index import BookLoreIndex
from ..services.meta_thinking import MetaThinking
from ..services.self_reflect import SelfReflectService
from ..services.llm_fallback import LLMFallbackClient, build_provider_chain
from ..services.eviction import EvictionService
from ..services.belief_engine import BeliefEngine
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

        # 先完成入库/命令判断，主动跟进不能把原本未被点名的普通句子变成显式命令。
        apply_decision = getattr(self, "_apply_reply_decision", None)
        if callable(apply_decision):
            apply_decision(event, runtime_scope, sender_id, sender_name, message)

    def _apply_reply_decision(self, event, scope, sender_id, sender_name, message) -> None:
        from datetime import datetime
        from ..domain.mood_policy import night_active
        from ..services.proactive_policy import proactive_event_allowed
        from ..services.reply_decision import (
            ReplySignals,
            attach_reply_decision,
            detect_message_air_flags,
            feedback_polarity,
            score_topic_division,
        )

        service = getattr(self, "reply_decision", None)
        followup = getattr(self, "concern_followup", None)
        direct = scope.visibility == "private" or bool(getattr(event, "is_at_or_wake_command", False))
        # 真人发言即视为有人接话，清空本会话的主动冷落计数（Bot 发言不算，见下方 sender_is_bot）
        _note_human = getattr(service, "note_human_activity", None)
        allowed = proactive_event_allowed(event)
        decision = None
        followup_gate = {}

        # 读取 BotProfile.proactive 配置（每个 Bot 独立风格/话题分工/限流）
        registry = getattr(self, "bot_registry", None)
        profile = registry.get(scope.bot_id) if registry is not None else None
        proactive_cfg = getattr(profile, "proactive", None)
        proactive_enabled = bool(getattr(proactive_cfg, "enabled", False))
        proactive_mode = str(getattr(proactive_cfg, "mode", "") or "").lower()

        # 读取发消息者 Bot 标签与协同上限
        discovery = getattr(self, "bot_discovery", None)
        sender_tag = getattr(event, "_wave_memory_sender_bot_tag", None)
        if sender_tag is None and discovery is not None and sender_id:
            try:
                sender_tag = discovery.get_tag(sender_id, display_name=sender_name)
            except Exception:
                sender_tag = None
        sender_is_bot = bool(sender_tag and sender_tag.get("is_bot"))
        if callable(_note_human) and not sender_is_bot and scope.session is not None:
            try:
                _note_human(scope.session.id)
            except Exception:
                pass
        bot_collab_exceeded = False
        if sender_is_bot and direct and discovery is not None and scope.visibility == "group":
            max_collab = int(getattr(proactive_cfg, "bot_collab_max_turns", 2) if proactive_cfg else 2)
            ok_collab, _ = discovery.check_and_record_collab_turn(
                bot_id=scope.bot_id,
                session_id=scope.session.id,
                sender_id=sender_id,
                max_turns=max_collab,
                record=True,
            )
            bot_collab_exceeded = not ok_collab

        def skipped(reason, remaining):
            followup_gate.update(reason=reason, remaining=remaining)

        def evaluate(concern=None):
            nonlocal decision
            if service is None:
                return True
            # 若 BotProfile 显式配了 proactive mode，优先应用到当前评估上下文
            global_mode = service.settings(scope.bot_id, scope.session.id)["mode"]
            effective_mode = proactive_mode if proactive_enabled and proactive_mode in {"off", "shadow", "enforce"} else global_mode
            relevance = None
            feedback = None
            backlog = 0
            in_duo_pingpong = False
            addressed_other = False
            is_cmd_or_farewell = False
            followup_eligible = False
            topic_match = None
            avoid_topic_hit = False
            mind_drive = None
            mind_energy = None
            mind_stance = ""
            body_drive = None
            silence_messages = 0
            group_msgs_per_min = 0
            ignored_streak = 0
            quiet_hours_active = False
            shared_cooldown_remaining = 0.0
            proactive_hourly_limit_hit = False
            consecutive_proactive_hit = False
            mood_influence = float(getattr(proactive_cfg, "mood_influence", 1.0) if proactive_cfg else 1.0)

            if (effective_mode != "off" or proactive_enabled) and not direct:
                meta = getattr(self, "meta_thinking", None)
                interesting = getattr(meta, "is_interesting", None)
                if callable(interesting):
                    try:
                        relevance = 1.0 if interesting(message) else 0.0
                    except Exception:
                        pass
                prefix = f"{scope.bot_id}:{scope.visibility}:{scope.session.id}:"
                backlog = sum(key.startswith(prefix) for key in getattr(self, "_semantic_message_buffers", {}))
                feedback_service = getattr(self, "reply_feedback_service", None)
                read_feedback = getattr(feedback_service, "decision_signals", None)
                if concern is not None and getattr(feedback_service, "enabled", False) is True and callable(read_feedback):
                    try:
                        from inspect import signature
                        kwargs = {"limit": 8}
                        if "since_ts" in signature(read_feedback).parameters:
                            kwargs["since_ts"] = service.clock() - 86400
                        feedback = feedback_polarity(read_feedback(scope, **kwargs))
                    except Exception:
                        pass

                # 扩展：本地零 Token 语境红线、双人拉锯、接话窗口、话题分工、心智读数与共享锁
                if proactive_enabled or getattr(profile, "interest_keywords", None):
                    bot_terms = getattr(profile, "identity_terms", ()) or ()
                    bot_qqs = getattr(profile, "self_ids", ()) or ()
                    msg_obj = getattr(event, "message_obj", None)
                    at_ids = []
                    quote_sender = ""
                    if msg_obj and getattr(msg_obj, "message", None):
                        for comp in msg_obj.message:
                            cname = comp.__class__.__name__
                            if cname == "At" and getattr(comp, "qq", None):
                                at_ids.append(str(comp.qq))
                            elif cname == "Reply":
                                quote_sender = str(getattr(comp, "sender_id", "") or "")
                    flags = detect_message_air_flags(
                        message,
                        bot_terms=bot_terms,
                        bot_qq_ids=bot_qqs,
                        at_qq_ids=at_ids,
                        quote_sender_id=quote_sender,
                    )
                    addressed_other = flags["addressed_other"]
                    is_cmd_or_farewell = flags["is_command"] or flags["is_farewell"]

                    if scope.visibility == "group" and sender_id:
                        in_duo_pingpong = service.observe_group_speaker(scope.session.id, sender_id)

                    # 接话窗口：刚回复过此人且未转向他人
                    tracker = getattr(self, "_reply_tracker", None) or {}
                    last_reply_ts = tracker.get(reply_tracker_key(scope.bot_id, scope.session.conversation_id, sender_id), 0)
                    followup_win = float(getattr(proactive_cfg, "followup_window_seconds", 120.0) if proactive_cfg else 120.0)
                    if last_reply_ts and 0 < service.clock() - last_reply_ts <= followup_win and not addressed_other:
                        followup_eligible = True

                    # 话题分工打分
                    pref_topics = getattr(proactive_cfg, "preferred_topics", ()) if proactive_cfg else ()
                    avoid_topics = getattr(proactive_cfg, "avoid_topics", ()) if proactive_cfg else ()
                    interest_kws = getattr(profile, "interest_keywords", ()) if profile else ()
                    topic_match, avoid_topic_hit, _ = score_topic_division(
                        message,
                        preferred_topics=pref_topics,
                        interest_keywords=interest_kws,
                        avoid_topics=avoid_topics,
                    )
                    if relevance is None and topic_match > 0:
                        relevance = topic_match

                    # 心智读数与群心流节奏
                    mood_service = getattr(self, "mood_service", None)
                    if mood_service is not None and hasattr(mood_service, "mind_state"):
                        try:
                            mind = mood_service.mind_state(scope.bot_id, viewer_scope=scope)
                            if mind and mind.get("enabled"):
                                mind_drive = mind.get("social_drive")
                                mind_energy = mind.get("energy")
                                mind_stance = str(mind.get("stance") or "")
                                focus = mind.get("group_focus") or {}
                                silence_messages = int(focus.get("silence_messages") or 0)
                                group_msgs_per_min = int(focus.get("msgs_per_minute") or 0)
                                ignored_streak = int(focus.get("ignored_streak") or 0)
                        except Exception:
                            pass

                    # 激素身体的主动意愿：只有这个 Bot 开了身体才有值，关着就不参与打分
                    body_service = getattr(self, "body_service", None)
                    if body_service is not None and body_service.active_for(scope.bot_id):
                        try:
                            body_reading = body_service.reading(scope.bot_id)
                            if body_reading is not None:
                                body_drive = body_reading.proactive_drive
                        except Exception:
                            body_drive = None

                    # 夜间免打扰与单向滑动配额 + 双 Bot 群级互斥冷却
                    if proactive_enabled and proactive_cfg is not None:
                        hour = datetime.fromtimestamp(service.clock()).hour
                        quiet_hours_active = night_active(hour, proactive_cfg.quiet_hours_start, proactive_cfg.quiet_hours_end)
                        quota = service.proactive_quota_status(
                            bot_id=scope.bot_id,
                            session_id=scope.session.id,
                            visibility=scope.visibility,
                            min_gap_seconds=proactive_cfg.min_gap_seconds,
                            max_per_hour=proactive_cfg.max_per_hour,
                            max_consecutive_turns=proactive_cfg.max_consecutive_turns,
                        )
                        proactive_hourly_limit_hit = bool(quota["hourly_limit_hit"])
                        consecutive_proactive_hit = bool(quota["consecutive_hit"])
                        backoff_remaining = 0.0
                        backoff = getattr(service, "proactive_backoff_seconds", None)
                        if callable(backoff):
                            backoff_remaining = float(backoff(
                                bot_id=scope.bot_id,
                                session_id=scope.session.id,
                                visibility=scope.visibility,
                                base_gap_seconds=proactive_cfg.min_gap_seconds,
                            ) or 0.0)
                        followup_gate["remaining"] = max(
                            float(followup_gate.get("remaining") or 0.0),
                            float(quota["gap_remaining"]),
                            backoff_remaining,
                        )
                        arbiter = getattr(service, "arbiter", None)
                        if arbiter is not None:
                            shared_cooldown_remaining = arbiter.shared_cooldown_remaining(
                                bot_id=scope.bot_id,
                                session_id=scope.session.id,
                                cooldown_seconds=proactive_cfg.shared_group_cooldown_seconds,
                            )

            # 若 BotProfile 单独启用了主动模式且未走旧 config override，注入临时 threshold/mode 视图
            original_bots_cfg = service.config.get("bots")
            injected_override = False
            if proactive_enabled and proactive_cfg is not None:
                bots_map = dict(original_bots_cfg) if isinstance(original_bots_cfg, dict) else {}
                bot_entry = dict(bots_map.get(scope.bot_id) or {})
                bot_entry.setdefault("mode", proactive_mode if proactive_mode in {"off", "shadow", "enforce"} else "enforce")
                bot_entry.setdefault("score_threshold", proactive_cfg.score_threshold)
                bots_map[scope.bot_id] = bot_entry
                service.config["bots"] = bots_map
                injected_override = True

            try:
                decision = service.decide(ReplySignals(
                    bot_id=scope.bot_id, session_id=scope.session.id, visibility=scope.visibility,
                    message=message, direct=direct, candidate=concern is not None,
                    platform_allowed=allowed if not direct else not bool(getattr(event, "call_llm", False)),
                    stopped=bool((callable(getattr(event, "is_stopped", None)) and event.is_stopped())
                                 or getattr(event, "stopped", False) or getattr(event, "_force_stopped", False)),
                    relevance=relevance, backlog=backlog, feedback=feedback,
                    concern=concern is not None,
                    cooldown_remaining=followup_gate.get("remaining", 0.0),
                    candidate_gate=followup_gate.get("reason"),
                    proactive_enabled=proactive_enabled,
                    sender_is_bot=sender_is_bot,
                    bot_collab_exceeded=bot_collab_exceeded,
                    addressed_other=addressed_other,
                    is_command_or_farewell=is_cmd_or_farewell,
                    in_duo_pingpong=in_duo_pingpong,
                    followup_eligible=followup_eligible,
                    topic_match=topic_match,
                    avoid_topic_hit=avoid_topic_hit,
                    mind_drive=mind_drive,
                    mind_energy=mind_energy,
                    mind_stance=mind_stance,
                    mood_influence=mood_influence,
                    body_drive=body_drive,
                    silence_messages=silence_messages,
                    group_msgs_per_min=group_msgs_per_min,
                    ignored_streak=ignored_streak,
                    quiet_hours_active=quiet_hours_active,
                    shared_cooldown_remaining=shared_cooldown_remaining,
                    proactive_hourly_limit_hit=proactive_hourly_limit_hit,
                    consecutive_proactive_hit=consecutive_proactive_hit,
                ))
            finally:
                if injected_override:
                    if original_bots_cfg is None:
                        service.config.pop("bots", None)
                    else:
                        service.config["bots"] = original_bots_cfg

            # 如果通过读空气主动唤醒，争抢本群同消息仲裁锁（防双 Bot 同条齐发）
            if decision.get("proactive_wake") and decision.get("allow") and getattr(service, "arbiter", None) is not None:
                msg_key = str(getattr(event, "message_id", None) or hash(message[:120]))
                won = service.arbiter.try_claim_message(
                    bot_id=scope.bot_id,
                    session_id=scope.session.id,
                    message_key=msg_key,
                    score=float(decision.get("score") or 0.0),
                )
                if not won:
                    decision["allow"] = False
                    decision["would_reply"] = False
                    decision["proactive_wake"] = False
                    decision["decision"] = "suppress"
                    decision["reasons"].append("arbiter_yielded_to_peer")

            attach_reply_decision(event, decision)
            return decision["allow"]

        concern = None
        if followup is not None and scope.visibility == "group" and not direct:
            concern = followup.claim_followup(
                scope, sender_id, sender_name, message, allowed=allowed, accept=evaluate, on_skip=skipped,
            )
        if decision is None:
            evaluate()
        if concern is not None:
            event._wave_memory_concern_followup = {"concern": concern, "proactive": True}
            event.is_at_or_wake_command = True
        elif decision and decision.get("proactive_wake") and decision.get("allow"):
            # 读空气决定主动接话：唤醒事件并标记主动接话上下文（允许模型用 hold_peace 自主放弃）
            event._wave_memory_air_proactive = {"score": decision.get("score"), "reasons": decision.get("reasons", [])}
            event.is_at_or_wake_command = True
        elif decision and decision["enforced"]:
            event.stop_event()

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
            settle = getattr(self, "_settle_concern_claim", None)
            if callable(settle):
                settle(event, spoke=False)
            return

        import re as _re
        from astrbot.core.message.components import Plain

        # 决定「这次不回复」：模型显式调用 wave_memory_hold_peace、或回一句 [沉默]，都丢掉整条回复
        hold_peace = getattr(event, "_wave_memory_hold_peace", None) or {}
        chosen_silence_text = False
        if getattr(event, "_wave_memory_concern_followup", None) and not hold_peace:
            from ..services.concern_followup import SILENCE_MARK

            texts = [comp.text for comp in result.chain if isinstance(comp, Plain) and comp.text]
            only_text = len(texts) == len(result.chain)
            if only_text and "".join(texts).strip().strip("。.").replace(" ", "") in {SILENCE_MARK, SILENCE_MARK.strip("[]")}:
                chosen_silence_text = True
        if hold_peace or chosen_silence_text:
            result.chain.clear()
            settle = getattr(self, "_settle_concern_claim", None)
            if callable(settle):
                settle(event, spoke=False)
            mood_service = getattr(self, "mood_service", None)
            silence_scope = getattr(event, "_wave_memory_runtime_scope", None)
            if mood_service is not None and isinstance(silence_scope, RuntimeScope) and silence_scope.session is not None:
                try:
                    mood_service.note_silence_or_ignored(silence_scope.bot_id, silence_scope.session.id, reason="hold_peace")
                except Exception:
                    pass
            followup = getattr(self, "concern_followup", None)
            if followup is not None:
                followup._count("chose_silence_tool" if hold_peace else "chose_silence_text")
            logger.info(
                "[WaveMemory] 决定不回复已生效%s",
                f"（{hold_peace['reason']}）" if hold_peace.get("reason") else "",
            )
            return

        _IMPRESSION_RE = r'(?:<<\s*impression\s*[:：](.+?)>>|\[\s*impression\s*[:：](.+?)\])'
        extracted_impression = None
        extracted_impact = 1.0
        extracted_polarity = 0

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
                            extracted_polarity = parse_impression_polarity(imp_val)
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

                # 群聊、私聊、直播一视同仁：观感都攒进这个人唯一的未结算池，场合只用于可见性
                if isinstance(runtime_scope, RuntimeScope) and runtime_scope.visibility in {"group", "private"} and runtime_scope.session:
                    group_id = scene_key(runtime_scope)
                    principal = runtime_scope.subject_principal_id or ""
                    principal_prefix = f"{runtime_scope.session.platform_id}:user:"
                    sender_id = principal[len(principal_prefix):] if principal.startswith(principal_prefix) else ""
                    bot_db_id = runtime_scope.bot_id

                    if sender_id and group_id and sender_id != "bot":
                        _finder = getattr(self, "_latest_sender_evidence", None)
                        evidence_id, evidence_quote = _finder(runtime_scope, sender_id) if callable(_finder) else (None, "")
                        persist_unsettled_trace(
                            self.db,
                            bot_id=bot_db_id,
                            user_id=sender_id,
                            group_id=group_id,
                            text=extracted_impression,
                            impact=extracted_impact,
                            polarity=extracted_polarity,
                            source_memory_id=evidence_id,
                            quote=evidence_quote,
                        )
                    if sender_id and group_id and sender_id != "bot" and runtime_scope.visibility == "group":
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

        visible = any(
            (isinstance(comp, Plain) and (comp.text or "").strip()) or not isinstance(comp, Plain)
            for comp in result.chain
        )
        settle = getattr(self, "_settle_concern_claim", None)
        if callable(settle):
            settle(event, spoke=visible)

    async def _handle_bot_sent(self, event: AstrMessageEvent):
        """捕获 bot 回复，写入记忆 + 异步更新好感度。"""
        # Count successful sends even when storing bot messages is disabled or
        # the reply is short/media-only. Never count at request/generation time.
        service = getattr(self, "reply_decision", None)
        reply_scope = getattr(event, "_wave_memory_runtime_scope", None)
        is_proactive_sent = bool(getattr(event, "_wave_memory_concern_followup", None) or getattr(event, "_wave_memory_air_proactive", None))
        if service is not None and isinstance(reply_scope, RuntimeScope) and reply_scope.session is not None:
            sent_result = event.get_result()
            if sent_result and sent_result.chain and not getattr(event, "_wave_memory_reply_counted", False):
                service.note_reply(reply_scope.bot_id, reply_scope.session.id, reply_scope.visibility, proactive=is_proactive_sent)
                event._wave_memory_reply_counted = True
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
        timestamp: float | None = None,
    ) -> None:
        """Bot 自己说出的话：入库、互动计数、未结算印象、自省记录。

        AstrBot 的 after_message_sent 与 Runtime API（Cortico 的 qq.self / 直播口播）共用这一段。
        ``timestamp``：Runtime API 批量补写时带上说话的时刻，与同会话的弹幕按先后交错；
        AstrBot 钩子不传，用入库时刻。
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

        try:
            spoken_at = float(timestamp) if timestamp not in (None, "") else 0.0
        except (TypeError, ValueError):
            spoken_at = 0.0
        await self.writer.enqueue({
            "scope": runtime_scope,
            "group_id": group_id,
            "sender_id": "bot",
            "sender_name": self._get_bot_name(bot_id) if bot_id else self._bot_display_name(bot_db_id),
            "content": bot_text,
            "timestamp": spoken_at if 0 < spoken_at < float("inf") else time.time(),
            "event_id": bot_reply_event_id(message_id, bot_text),
        })

        # 兜底：decorating 未跑时，日常观感只进未结算表，不写 metadata.impression。
        import re as _re
        _IMPRESSION_RE = r'(?:<<\s*impression\s*[:：](.+?)>>|\[\s*impression\s*[:：](.+?)\])'
        _impression_matches = list(_re.finditer(_IMPRESSION_RE, bot_text, _re.DOTALL))
        if _impression_matches and sender_id and group_id and sender_id != "bot":
            for _match in _impression_matches:
                _raw_mark = (_match.group(1) or _match.group(2) or "").strip()
                _body, _impact = parse_impression_mark(_raw_mark)
                if _body:
                    try:
                        _finder = getattr(self, "_latest_sender_evidence", None)
                        _eid, _quote = _finder(runtime_scope, sender_id) if callable(_finder) else (None, "")
                        persist_unsettled_trace(
                            self.db,
                            bot_id=bot_db_id,
                            user_id=sender_id,
                            group_id=scene_key(runtime_scope),
                            text=_body,
                            impact=_impact,
                            polarity=parse_impression_polarity(_raw_mark),
                            source_memory_id=_eid,
                            quote=_quote,
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

        # 心情 v2 的疲劳输入：一段时间内她说了多少话 + 心流记录是否为主动接话。
        mood_service = getattr(self, "mood_service", None)
        if mood_service is not None:
            try:
                try:
                    mood_service.note_bot_reply(runtime_scope.bot_id, session_id=runtime_scope.session.id)
                except TypeError:
                    mood_service.note_bot_reply(runtime_scope.bot_id)
            except Exception as exc:
                logger.debug(f"[WaveMemory] mood reply count failed: {exc!r}")

        # 事后记账：这段对话停下来之后单独回看一遍（见 services/post_reply_bookkeeping.py）
        bookkeeper = getattr(self, "post_reply_bookkeeper", None)
        if bookkeeper is not None and runtime_scope.visibility in {"group", "private"}:
            try:
                bookkeeper.schedule(replace(runtime_scope, subject_principal_id=None))
            except Exception as exc:
                logger.debug(f"[WaveMemory] bookkeeping schedule failed: {exc!r}")

    def _latest_sender_evidence(self, scope: RuntimeScope, sender_id: str) -> tuple[int | None, str]:
        """观感的原话出处：该会话里此人最近一条未隔离发言（id 与 ≤80 字原文）。查不到返回 (None, "")。"""
        conn = getattr(getattr(self, "db", None), "conn", None)
        if conn is None or not isinstance(scope, RuntimeScope) or scope.session is None or not sender_id:
            return None, ""
        try:
            row = conn.execute(
                """SELECT id, content FROM memories
                    WHERE bot_id=? AND session_id=? AND visibility=? AND sender_id=?
                      AND COALESCE(quarantine, 0)=0 AND content IS NOT NULL
                    ORDER BY id DESC LIMIT 1""",
                (scope.bot_id, scope.session.id, scope.visibility, str(sender_id)),
            ).fetchone()
        except Exception:
            return None, ""
        if not row:
            return None, ""
        return int(row[0]), " ".join(str(row[1] or "").split())[:80]

    def _bot_display_name(self, db_id: str) -> str:
        profile = self.bot_registry.get(db_id) if getattr(self, "bot_registry", None) else None
        return profile.name if profile else "bot"

    async def ingest_observation(self, observation: dict, runtime_scope: RuntimeScope) -> dict:
        """宿主无关的写入入口：Runtime API 的一条观察走与 AstrBot 消息钩子相同的处理。

        ``kind``：``message``（别人说的话，默认）、``danmaku``（弹幕）、``self``（Bot 自己说的话）、
        ``game_event``（Bot 自己的游戏经历）。别人说的话进 InboundMessagePipeline（记住/忘记/teach、
        入库、黑话积累、纠错自省、好感触达）；Bot 自己的话进 ``_process_bot_reply``；游戏经历进
        ``_process_game_event``（只入库，不走群友处理）。``event_id`` 用平台原始消息号，
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
                timestamp=observation.get("timestamp"),
            )
            return {"status": "accepted", "kind": "self"}
        if kind == "game_event":
            return await self._process_game_event(observation, runtime_scope)
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
        event = _ObservationEvent(
            message_id=event_id,
            message_str=f"{content} {marker}".strip(),
            is_at_or_wake_command=bool(observation.get("is_at_bot")),
        )
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

    async def _process_game_event(self, observation: dict, runtime_scope: RuntimeScope) -> dict:
        """Bot 自己的游戏经历：按她的经历入库（source=game_event），不走群友处理与 Bot 回复流程。"""
        try:
            item = game_event_payload(
                observation,
                runtime_scope,
                bot_name=self._bot_display_name(runtime_scope.bot_id),
            )
        except GameEventError as exc:
            return {"status": "rejected", "error": exc.code}
        await self.writer.enqueue(item)
        # 死亡 / 危机这类刚发生的经历：几分钟后让她回想一次，同一场直播里就能用上
        lessons = getattr(self, "game_lessons", None)
        if lessons is not None:
            try:
                lessons.note_event(runtime_scope.bot_id, str(observation.get("event_type") or ""), item["timestamp"])
            except Exception as exc:
                logger.debug(f"[WaveMemory] 游戏经验回想没排上: {exc!r}")
        return {"status": "accepted", "kind": "game_event"}

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
