"""Inbound message pipeline handling command prefixes, concurrent locks, and lifecycle events."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

try:
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent
except ImportError:  # pragma: no cover - focused repository tests
    import logging
    logger = logging.getLogger(__name__)
    AstrMessageEvent = Any

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - focused repository tests
    from domain.scope import RuntimeScope


class InboundMessagePipeline:
    """Pipelines locked message processing, administrative commands, and lifecycle hooks."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.group_concurrency_locks: dict[str, asyncio.Lock] = {}

    async def process_message(
        self,
        event: AstrMessageEvent,
        runtime_scope: RuntimeScope,
        message: str,
        message_ts: float,
        group_id: str,
        sender_id: str,
        sender_name: str,
        bot_id: str,
    ) -> None:
        # ─── 抢词被打断检测 (Hesitation Memory Capture) ───
        if (
            runtime_scope.visibility == "group"
            and hasattr(self.plugin, "_pending_proactive_plans")
            and self.plugin._pending_proactive_plans.get(group_id)
        ):
            active_plan = self.plugin._pending_proactive_plans[group_id]
            self.plugin._pending_proactive_plans[group_id] = None
            try:
                bot_prof_temp = self.plugin._get_bot(bot_id)
                pe_bot_id_temp = bot_prof_temp.db_id if bot_prof_temp else "bot"
                user_prof_row = self.plugin.db.conn.execute(
                    "SELECT metadata FROM user_profiles WHERE user_id = ? AND group_id = ? AND bot_id = ?",
                    (sender_id, group_id, pe_bot_id_temp)
                ).fetchone()
                meta_to_write = {}
                if user_prof_row and user_prof_row[0]:
                    meta_to_write = json.loads(user_prof_row[0])
                hesitations_list = meta_to_write.setdefault("recent_hesitations", [])
                hesitations_list.append({
                    "ts": time.time(),
                    "topic": active_plan.get("topic", "闲聊"),
                    "motive": active_plan.get("motive", "想和你交谈"),
                })
                del hesitations_list[:-5]
                self.plugin.db.conn.execute(
                    "UPDATE user_profiles SET metadata = ? WHERE user_id = ? AND group_id = ? AND bot_id = ?",
                    (json.dumps(meta_to_write, ensure_ascii=False), sender_id, group_id, pe_bot_id_temp)
                )
                self.plugin.db.conn.commit()
                logger.info(f"[MetaThinking] 抢词咽回成功：用户 {sender_id} 在群组 {group_id} 抢答，原计划的主动插话“{active_plan.get('topic', '闲聊')}”已被咽回，写为犹豫记忆。")
            except Exception as e:
                logger.debug(f"[MetaThinking] 咽回犹豫记忆写入失败: {e}")

        scope_key = f"{runtime_scope.bot_id}:{runtime_scope.visibility}:{runtime_scope.session.id}"
        group_lock = self.group_concurrency_locks.setdefault(scope_key, asyncio.Lock())

        async with group_lock:
            await self._process_in_lock(
                event=event,
                runtime_scope=runtime_scope,
                locked_message=message,
                message_ts=message_ts,
                group_id=group_id,
                sender_id=sender_id,
                sender_name=sender_name,
                bot_id=bot_id,
            )

    async def _process_in_lock(
        self,
        *,
        event: AstrMessageEvent,
        runtime_scope: RuntimeScope,
        locked_message: str,
        message_ts: float,
        group_id: str,
        sender_id: str,
        sender_name: str,
        bot_id: str,
    ) -> None:
        msg_stripped = locked_message.strip()

        # 1. /teach 管理员教导命令
        if (
            runtime_scope.visibility == "group"
            and (msg_stripped.startswith("/teach ") or msg_stripped.startswith("/teach:"))
        ):
            admin_ids = self.plugin._get_admin_ids() if hasattr(self.plugin, "_get_admin_ids") else set()
            if sender_id in admin_ids:
                content = msg_stripped[7:].strip(":： \n")
                if content and len(content) >= 4:
                    await self.plugin.writer.enqueue({
                        "scope": runtime_scope,
                        "group_id": group_id,
                        "content": f"[管理员教导] {content}",
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "timestamp": time.time(),
                        "event_id": getattr(event, "message_id", None),
                        "importance": 2.5,
                        "source": "teach",
                    })
                    fact_match = re.match(r"^(.+?)(是|的|=|→)(.+)$", content)
                    if fact_match:
                        subject = fact_match.group(1).strip()
                        predicate = fact_match.group(2).strip() or "是"
                        obj = fact_match.group(3).strip()
                        if subject and obj:
                            scoped_repo = getattr(self.plugin.db, "scoped_knowledge", None)
                            if scoped_repo is not None:
                                scoped_repo.upsert_scoped_fact(
                                    runtime_scope,
                                    subject=subject,
                                    predicate=predicate,
                                    object=obj,
                                    confidence=0.95,
                                    status="pending",
                                    provenance={
                                        "source": "teach",
                                        "event_id": str(getattr(event, "message_id", "") or ""),
                                    },
                                )
                    logger.info(f"[WaveMemory] /teach: {content[:50]}")
                return

        # 2. 显式记忆控制前缀 (记住/忘记)
        _remember_prefixes = ("记住", "记下", "remember")
        _forget_prefixes = ("忘记", "忘掉", "forget", "别记")
        for prefix in _remember_prefixes:
            if msg_stripped.startswith(prefix):
                content = msg_stripped[len(prefix):].strip(":： \n")
                if content and len(content) >= 4:
                    await self.plugin.writer.enqueue({
                        "scope": runtime_scope,
                        "group_id": group_id,
                        "content": f"[用户要求记住] {content}",
                        "sender_id": sender_id,
                        "sender_name": sender_name if sender_name else "",
                        "timestamp": time.time(),
                        "event_id": getattr(event, "message_id", None),
                        "importance": 2.0,
                        "source": "explicit",
                    })
                    logger.info(f"[WaveMemory] 显式要求记住: {content[:50]}")
                return

        for prefix in _forget_prefixes:
            if msg_stripped.startswith(prefix):
                content = msg_stripped[len(prefix):].strip(":： \n")
                if content and len(content) >= 4:
                    deleted_count = self.plugin.db.soft_delete_similar(
                        content,
                        group_id=group_id,
                        sender_id=sender_id,
                    )
                    logger.info(f"[WaveMemory] 显式要求忘记: {content[:50]}, 软删除 {deleted_count} 条")
                return

        # 3. 正常消息异步推写入库
        await self.plugin.writer.enqueue({
            "scope": runtime_scope,
            "group_id": group_id,
            "content": locked_message,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "timestamp": message_ts,
            "event_id": getattr(event, "message_id", None),
            "importance": 1.0,
        })

        # 4. 派发给 Jargon 系统进行语用积累（只记账，不再后台盲抽）
        if runtime_scope.visibility == "group" and getattr(self.plugin, "jargon_service", None):
            self.plugin.jargon_service.feed_message(locked_message, runtime_scope, sender_id, timestamp=message_ts)

        # 5. 派发给自省系统与生命周期
        if runtime_scope.visibility == "group" and getattr(self.plugin, "self_reflect", None) and group_id:
            try:
                correction_message_id = getattr(event, "message_id", None)
                await self.plugin.self_reflect.check_correction(
                    locked_message,
                    sender_name,
                    group_id,
                    bot_id=runtime_scope.bot_id,
                    scope=runtime_scope,
                    message_id=correction_message_id,
                )
            except Exception:
                pass

        if runtime_scope.visibility == "group" and getattr(self.plugin, "lifecycle", None):
            bot_ids = self.plugin._bot_qq_ids
            is_at_bot = any(bid in (event.message_str or "") for bid in bot_ids)
            is_reply_to_bot = False
            if hasattr(event, "message_obj") and event.message_obj:
                raw = event.message_str or ""
                if "[引用消息" in raw and any(bid in raw for bid in bot_ids):
                    is_reply_to_bot = True
            hour = int(time.strftime("%H", time.localtime()))
            self.plugin.lifecycle.process_scoped_message(
                scope=runtime_scope,
                content=locked_message,
                is_at_bot=is_at_bot,
                is_reply_to_bot=is_reply_to_bot,
                hour=hour,
            )

        # 6. 欲望引擎检测
        desire_engine = getattr(self.plugin, "desire_engine", None)
        if desire_engine:
            raw_msg = event.message_str or ""
            if "redbag" in raw_msg or "红包" in locked_message:
                desire_engine.trigger(
                    desire_type="想抢红包",
                    trigger_desc=f"{sender_name}发了红包",
                    intensity=0.6,
                    action="react_to_hongbao",
                    ttl=30.0,
                )
