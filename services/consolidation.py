"""Wave Memory scoped consolidation service.

Only resolved, non-quarantined memories v2 records may enter this pipeline.  All
outputs are written through WaveMemoryDB's scoped derived-knowledge facade.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

from astrbot.api import logger

try:
    from ..domain.scope import RuntimeScope, SessionRef
    from ..engine.database import WaveMemoryDB
except ImportError:  # pragma: no cover - direct service imports in focused tests
    from domain.scope import RuntimeScope, SessionRef
    from engine.database import WaveMemoryDB
from .llm_fallback import (
    build_provider_chain,
    call_first_available_provider,
    is_unrecoverable_error,
)


CONSOLIDATION_PROMPT = """从以下群聊消息中提取结构化知识。

消息格式: [昵称(QQ号) 时间] 内容

---
{conversation}
---

请输出 JSON（不要输出其他内容）：
{{
  "summary": "一句话概括这段对话的核心内容",
  "topics": ["话题1", "话题2"],
  "relations": [{{"source": "人物或话题", "target": "人物/话题/事物", "type": "关系类型"}}]
}}

规则：
- topics 最多 3 个，用简短名词短语
- relations 最多 4 条；type 从 discusses、mentions、decides、supports、opposes、reacts_to、creates、uses、knows、relates_to 中选择
- 不要输出 facts / social / nicknames；客观事实与信念只能由对话现场工具提审
- 如果对话是无意义灌水，summary 写"日常灌水"，其他字段留空数组
- 直接输出 JSON，不要 markdown 代码块"""

_CURSOR_NAME = "messages_v2_id"
_EXCLUDED_SENDERS = ("bot_self", "angel_memory_import", "livingmemory_import", "legacy_import")


class ConsolidationService:
    """按完整 RuntimeScope 独立整合 memories v2 消息。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        context=None,
        provider_id: str = "",
        interval_hours: float = 4.0,
        batch_size: int = 50,
        topic_backfill: bool = True,
        skip_topics: list = None,
        belief_engine=None,
        bot_identifiers: set = None,
        provider_fallback_ids=None,
    ):
        self.db = db
        self.context = context
        self.provider_id = provider_id
        # 整合是每 4 小时一轮的后台任务，单渠道故障不应让摘要彻底停产。
        self.provider_ids = build_provider_chain(provider_id, provider_fallback_ids)
        self.interval = interval_hours * 3600
        self.batch_size = batch_size
        self.topic_backfill = topic_backfill
        self.skip_topics = set(skip_topics or ["日常闲聊", "日常灌水", "闲聊", "灌水", "群聊", "聊天", "日常"])
        self.belief_engine = belief_engine
        self._bot_identifiers: set = bot_identifiers or set()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # Health evidence: existence of this object proves nothing about output.
        self._last_run_ts: float = 0.0
        self._last_success_ts: float = 0.0
        self._last_error: str = ""
        self._last_error_unrecoverable: bool = False
        self._last_run_failures: int = 0
        self._last_run_scopes: int = 0

    def start(self, supervisor=None):
        self._running = True
        if supervisor is None:
            self._task = asyncio.create_task(self._loop())
        else:
            self._task = supervisor.start(
                "wave-memory:consolidation", self._loop(), owner="consolidation"
            )
        logger.info("[WaveMemory] ConsolidationService started (scoped v2)")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await asyncio.sleep(300)
        while self._running:
            try:
                await self.consolidate_once()
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[WaveMemory] Consolidation error: {exc}")
                await asyncio.sleep(300)

    async def consolidate_once(self) -> dict:
        """Enumerate only complete resolved v2 group scopes and process each cursor."""
        if not self.provider_ids or not self.context:
            logger.debug("[WaveMemory] Consolidation skipped: no LLM provider")
            return {"status": "skipped"}

        scopes = await asyncio.to_thread(self._list_memory_scopes)
        total_messages = total_relations = 0
        failures: list[str] = []
        unrecoverable = False
        for scope in scopes:
            try:
                result = await self._consolidate_scope(scope)
                total_messages += result.get("messages", 0)
                total_relations += result.get("relations", 0)
            except Exception as exc:
                failures.append(str(exc))
                if is_unrecoverable_error(exc):
                    unrecoverable = True
                logger.warning(
                    "[WaveMemory] Consolidation failed for scope %s/%s: %s",
                    scope.bot_id,
                    scope.session.id if scope.session else "missing-session",
                    exc,
                )

        self._last_run_ts = time.time()
        self._last_run_failures = len(failures)
        self._last_run_scopes = len(scopes)
        if total_messages:
            self._last_success_ts = self._last_run_ts
            self._last_error = ""
            self._last_error_unrecoverable = False
            logger.info(
                "[WaveMemory] Scoped consolidation done: %s messages, %s relations, %s scopes",
                total_messages,
                total_relations,
                len(scopes),
            )
        elif failures:
            # Every scope failed.  Keep the reason so the health panel can report a
            # stalled summary pipeline instead of "ok" just because the object exists.
            self._last_error = failures[-1]
            self._last_error_unrecoverable = unrecoverable
            logger.warning(
                "[WaveMemory] Consolidation produced no summary across %s scopes; last error: %s",
                len(scopes),
                failures[-1],
            )
        return {"messages": total_messages, "relations": total_relations, "groups": len(scopes)}

    def health_snapshot(self, *, stale_after_hours: float | None = None) -> dict:
        """Report whether consolidation actually produced summaries recently.

        The previous health check only asserted that the service object existed, so
        a 20-day summary outage caused by provider 503/402 still displayed as "ok".
        """
        stale_window = (
            self.interval * 3 if stale_after_hours is None else max(0.0, stale_after_hours) * 3600
        )
        now = time.time()
        snapshot = {
            "provider_ids": list(self.provider_ids),
            "last_run_ts": self._last_run_ts,
            "last_success_ts": self._last_success_ts,
            "last_error": self._last_error,
            "last_error_unrecoverable": self._last_error_unrecoverable,
            "failed_scopes": self._last_run_failures,
            "total_scopes": self._last_run_scopes,
        }
        if not self.provider_ids:
            snapshot.update(status="off", detail="未配置 LLM provider")
            return snapshot
        if self._last_run_ts <= 0:
            snapshot.update(status="pending", detail="尚未执行首轮整合")
            return snapshot
        if self._last_success_ts <= 0:
            hint = "余额/鉴权不可自愈" if self._last_error_unrecoverable else "上游不可用"
            snapshot.update(
                status="degraded",
                detail=f"整合从未成功产出摘要（{hint}）：{self._last_error[:160]}",
            )
            return snapshot
        idle = now - self._last_success_ts
        if stale_window > 0 and idle > stale_window:
            hours = idle / 3600
            snapshot.update(
                status="degraded",
                detail=f"已 {hours:.1f} 小时未产出新摘要；最后错误：{self._last_error[:160]}",
            )
            return snapshot
        snapshot.update(status="ok", detail="")
        return snapshot

    def _list_memory_scopes(self) -> list[RuntimeScope]:
        """Build RuntimeScope values from complete v2 tuples; malformed rows fail closed."""
        rows = self.db.conn.execute(
            """SELECT DISTINCT bot_id, session_id, visibility, group_id
                 FROM memories
                WHERE memory_type='message' AND content IS NOT NULL
                  AND bot_id IS NOT NULL AND session_id IS NOT NULL AND visibility='group'
                  AND resolution_state='resolved' AND quarantine=0"""
        ).fetchall()
        scopes: list[RuntimeScope] = []
        for bot_id, session_id, visibility, group_id in rows:
            try:
                platform_id, kind, conversation_id = session_id.split(":", 2)
                if group_id != conversation_id:
                    raise ValueError("group_id does not match canonical session")
                scopes.append(RuntimeScope(
                    bot_id=bot_id,
                    visibility=visibility,
                    session=SessionRef(
                        id=session_id,
                        platform_id=platform_id,
                        kind=kind,
                        conversation_id=conversation_id,
                    ),
                ))
            except Exception as exc:
                logger.warning("[WaveMemory] Skip unresolved/invalid consolidation scope: %s", exc)
        return scopes

    async def _consolidate_scope(self, scope: RuntimeScope) -> dict:
        """Consolidate one exact Scope tuple, advancing only its own cursor on success."""
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return {"messages": 0, "relations": 0}
        raw_cursor = self.db.get_scoped_consolidation_cursor(scope, cursor_name=_CURSOR_NAME)
        try:
            cursor = int(raw_cursor) if raw_cursor is not None else 0
        except (TypeError, ValueError):
            logger.warning("[WaveMemory] Invalid scoped consolidation cursor; refusing scope")
            return {"messages": 0, "relations": 0}

        messages = await asyncio.to_thread(self._fetch_messages, scope, cursor)
        if len(messages) < 5:
            return {"messages": 0, "relations": 0}

        conversation_lines, message_ids = [], []
        for memory_id, sender_name, sender_id, content, timestamp in messages:
            time_text = time.strftime("%H:%M", time.localtime(timestamp))
            conversation_lines.append(f"[{sender_name or sender_id or 'unknown'}({sender_id}) {time_text}] {content[:200]}")
            message_ids.append(memory_id)

        response = await call_first_available_provider(
            self.context,
            self.provider_ids,
            log_prefix="[Consolidation]",
            prompt=CONSOLIDATION_PROMPT.replace("{conversation}", "\n".join(conversation_lines)),
            system_prompt="你是记忆整合系统，只输出 JSON。",
        )
        if not response or not response.completion_text:
            return {"messages": 0, "relations": 0}
        structured = self._parse_response(response.completion_text)
        if not structured:
            return {"messages": 0, "relations": 0}

        summary = structured.get("summary", "")
        topics = structured.get("topics", [])
        relations = structured.get("relations", [])
        # 事实/信念不得从摘要盲抽入库；只保留话题标签与关系边，供检索。
        relations_written = await asyncio.to_thread(
            self._write_relations, scope, topics, [], relations,
        )
        if self.topic_backfill:
            await asyncio.to_thread(self._backfill_topic_tags, scope, message_ids, topics)

        # This is deliberately the final operation: failed LLM/output work is retried.
        self.db.advance_scoped_consolidation_cursor(
            scope, cursor_name=_CURSOR_NAME, cursor_value=str(message_ids[-1]),
        )
        return {"messages": len(message_ids), "relations": relations_written, "facts": 0, "summary": summary}

    def _fetch_messages(self, scope: RuntimeScope, cursor: int) -> list:
        """Read only records exactly matching the resolved Scope tuple."""
        return self.db.conn.execute(
            """SELECT id, sender_name, sender_id, content, timestamp
                 FROM memories
                WHERE id > ? AND group_id=? AND bot_id=? AND session_id=? AND visibility=?
                  AND resolution_state='resolved' AND quarantine=0
                  AND memory_type='message' AND content IS NOT NULL
                  AND sender_id NOT IN (?, ?, ?, ?)
                ORDER BY id ASC LIMIT ?""",
            (cursor, scope.session.conversation_id, scope.bot_id, scope.session.id, scope.visibility,
             *_EXCLUDED_SENDERS, self.batch_size),
        ).fetchall()

    # Kept as a fail-closed compatibility hook for callers that have not been moved to RuntimeScope.
    async def _consolidate_group(self, group_id: str, *args, **kwargs) -> dict:
        logger.warning("[WaveMemory] Legacy group-only consolidation rejected: %r", group_id)
        return {"messages": 0, "relations": 0}

    def _parse_response(self, text: str) -> Optional[dict]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return None
            try:
                value = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        return value if isinstance(value, dict) else None

    def _write_relations(self, scope: RuntimeScope, topics: list, facts: list, relations: list) -> int:
        """Write scoped tags and tag relations; no legacy tag table is touched."""
        tags: dict[str, int] = {}

        def ensure(name: str, tag_type: str) -> int | None:
            name = name.strip()[:100] if isinstance(name, str) else ""
            if not name:
                return None
            tag_id = self.db.upsert_scoped_tag(
                scope, name=name, tag_type=tag_type, confidence=0.7,
                metadata={"producer": "consolidation"},
            )
            tags[name] = tag_id
            return tag_id

        for topic in topics or []:
            ensure(topic, "topic")
        for fact in facts or []:
            text = (
                f"{fact.get('subject', '')}{fact.get('predicate', '')}{fact.get('object', '')}"
                if isinstance(fact, dict) else str(fact)
            )
            ensure(text, "fact")

        written = 0
        for relation in (relations or [])[:4]:
            if not isinstance(relation, dict):
                continue
            source, target = relation.get("source", ""), relation.get("target", "")
            relation_type = relation.get("type", "discusses")
            if not isinstance(source, str) or not isinstance(target, str) or not isinstance(relation_type, str):
                continue
            source_id = tags.get(source) or ensure(source, "entity")
            target_id = tags.get(target) or ensure(target, "entity")
            if source_id and target_id:
                self.db.upsert_scoped_tag_relation(
                    scope, source_tag_id=source_id, target_tag_id=target_id,
                    relation_type=relation_type, weight=1.0, confidence=0.7,
                    metadata={"producer": "consolidation"},
                )
                written += 1
        return written

    def _backfill_topic_tags(self, scope: RuntimeScope, memory_ids: list[int], topics: list[str]) -> None:
        """Link v2 messages to same-scope tags via the scoped facade."""
        for topic in topics or []:
            if not isinstance(topic, str) or len(topic.strip()) < 2 or topic.strip() in self.skip_topics:
                continue
            tag_id = self.db.upsert_scoped_tag(
                scope, name=topic.strip(), tag_type="topic", confidence=0.7,
                metadata={"producer": "consolidation"},
            )
            for position, memory_id in enumerate(memory_ids, 1):
                self.db.link_scoped_memory_tag(
                    scope, memory_id=memory_id, tag_id=tag_id, position=100 + position, relevance=0.6,
                )
