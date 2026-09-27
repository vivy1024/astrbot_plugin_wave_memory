"""每日日记：每晚为羽书当天说过话的每个群写一篇第一人称日记。

素材三份：
1. 群分析插件当天的分析（话题与总评）——有就用，没开或当天失败就跳过；
2. 羽书当天自己在这个群说的话；
3. 当天记下的印象、人情备忘与关切。

写法沿用 VCP 日记的形态：正文之后一行 ``Tag: …``，话题名与人名当标签。落两处：
- 经历时间线（经 ``wave_memory_record_diary_episode`` 工具，与现场写日记同一校验）；
- 记忆库（source=experience），检索时能直接找到。

同一群同一天只写一篇（按经历时间线里当天是否已有日记判断，重启不会重复写）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
    from .concern_followup import _scope_from_row
    from .daily_diary_bridge import find_daily_analysis_db
    from .identity_safety import prepend_identity_safety_system_prompt
    from .tool_registry import runtime_tool_context
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.concern_followup import _scope_from_row
    from services.daily_diary_bridge import find_daily_analysis_db
    from services.identity_safety import prepend_identity_safety_system_prompt
    from services.tool_registry import runtime_tool_context

PROMPT = """你是{bot_name}。今天是 {date}，一天快结束了，请以第一人称写一篇今天在「{group_name}」的日记。

【群里今天聊了什么】
{topics}

【你今天自己说过的一些话】
{own_lines}

【今天记下的人和事】
{notes}

要求：
- 写你自己的经历、感受和看法，不是群聊流水账；提到群友用他们的名字。
- 正文 250 到 600 字。最后另起一行写「Tag: 」加 3 到 8 个标签（话题名、人名），用逗号分隔。
- 不要编造素材里没有的事；素材很少就写短一点。
只输出一个 JSON 对象：{{"title": "日记标题", "content": "正文（含最后的 Tag 行）", "summary": "一句话概括今天"}}"""


def _day_start(now: float) -> float:
    local = datetime.fromtimestamp(now)
    return datetime(local.year, local.month, local.day).timestamp()


class DailyDiaryService:
    def __init__(
        self,
        db: Any,
        tool_registry: Any,
        llm: Any,
        writer: Any = None,
        *,
        enabled: bool = True,
        hour: int = 23,
        bot_name_for: Callable[[str], str] | None = None,
        group_name_for: Callable[[str, str], str] | None = None,
    ):
        self.db = db
        self.tool_registry = tool_registry
        self.llm = llm
        self.writer = writer
        self.enabled = bool(enabled) and llm is not None and tool_registry is not None
        self.hour = min(23, max(0, int(hour)))
        self.bot_name_for = bot_name_for or (lambda bot_id: bot_id)
        self.group_name_for = group_name_for or (lambda bot_id, group_id: group_id)
        self.stats: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    async def loop(self) -> None:
        await asyncio.sleep(300)
        while True:
            try:
                if datetime.now().hour >= self.hour:
                    await self.write_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[WaveMemory] 每日日记检查失败: {exc!r}")
            await asyncio.sleep(1800)

    def _sessions_today(self, start: float) -> list[tuple[str, str, str]]:
        return self.db.conn.execute(
            """SELECT DISTINCT bot_id, session_id, visibility FROM memories
                WHERE id > (SELECT COALESCE(MAX(id), 0) FROM memories) - 20000
                  AND sender_id='bot' AND visibility='group' AND timestamp >= ?""",
            (start,),
        ).fetchall()

    def _written(self, scope: RuntimeScope, start: float) -> bool:
        return self.db.conn.execute(
            """SELECT 1 FROM scoped_soul_timeline WHERE bot_id=? AND session_id=? AND visibility=?
                 AND event_summary LIKE '【日记】%' AND occurred_at >= ? LIMIT 1""",
            (scope.bot_id, scope.session.id, scope.visibility, start),
        ).fetchone() is not None

    async def write_due(self, *, now: float | None = None) -> int:
        if not self.enabled:
            return 0
        stamp = float(now if now is not None else time.time())
        start = _day_start(stamp)
        written = 0
        for bot_id, session_id, visibility in self._sessions_today(start):
            scope = _scope_from_row(bot_id, session_id, visibility)
            if scope is None or self._written(scope, start):
                continue
            try:
                if await self.write_one(scope, start=start, now=stamp):
                    written += 1
            except Exception as exc:
                self._count("error")
                logger.warning(f"[WaveMemory] 写日记失败 {session_id}: {exc!r}")
        return written

    def _topics(self, group_id: str, date: str) -> str:
        path = find_daily_analysis_db()
        if path is None:
            return "（群分析没有开启）"
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            rows = conn.execute(
                """SELECT data_json FROM stage_checkpoints WHERE stage_name='LLM_ANALYSIS' AND group_id=? AND date_str=?
                    ORDER BY created_at DESC LIMIT 2""",
                (group_id, date),
            ).fetchall()
            conn.close()
        except Exception:
            return "（今天的群分析读取失败）"
        lines, seen = [], set()
        for (raw,) in rows:
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                continue
            review = payload.get("chat_quality_review") or {}
            if review.get("title") and review["title"] not in seen:
                seen.add(review["title"])
                lines.append(f"群聊总评：{review['title']}（{review.get('subtitle') or ''}）")
            for topic in payload.get("topics") or []:
                name = str(topic.get("topic") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    people = "、".join(str(c) for c in (topic.get("contributors") or [])[:4])
                    lines.append(f"- {name}（{people}）：{str(topic.get('detail') or '')[:160]}")
        return "\n".join(lines[:10]) or "（今天没有群分析结果）"

    def _own_lines(self, scope: RuntimeScope, start: float) -> tuple[str, int | None]:
        rows = self.db.conn.execute(
            """SELECT id, content FROM memories
                WHERE bot_id=? AND session_id=? AND visibility=? AND sender_id='bot' AND timestamp >= ?
                ORDER BY id""",
            (scope.bot_id, scope.session.id, scope.visibility, start),
        ).fetchall()
        if not rows:
            return "", None
        step = max(1, len(rows) // 20)
        picked = rows[::step][:20]
        return "\n".join(f"- {str(r[1] or '')[:120]}" for r in picked), int(rows[-1][0])

    def _notes(self, scope: RuntimeScope, start: float) -> str:
        group_id = scope.session.conversation_id
        lines = []
        try:
            for kind, summary in self.db.conn.execute(
                """SELECT kind, summary FROM person_timeline_events
                    WHERE bot_id=? AND group_id=? AND kind IN ('impression', 'affinity') AND created_at >= ?
                    ORDER BY created_at DESC LIMIT 10""",
                (scope.bot_id, group_id, start),
            ):
                lines.append(f"- {'印象' if kind == 'impression' else '关系'}：{str(summary or '')[:100]}")
            for (summary,) in self.db.conn.execute(
                """SELECT event_summary FROM scoped_soul_timeline
                    WHERE bot_id=? AND session_id=? AND visibility=? AND occurred_at >= ? LIMIT 6""",
                (scope.bot_id, scope.session.id, scope.visibility, start),
            ):
                lines.append(f"- {str(summary or '')[:100]}")
        except Exception as exc:
            logger.debug(f"[WaveMemory] 日记素材读取失败: {exc!r}")
        return "\n".join(lines) or "（没有）"

    async def write_one(self, scope: RuntimeScope, *, start: float, now: float) -> bool:
        own, anchor_id = self._own_lines(scope, start)
        if not own:
            return False
        bot_name = self.bot_name_for(scope.bot_id) or scope.bot_id
        date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        group_id = scope.session.conversation_id
        prompt = PROMPT.format(
            bot_name=bot_name, date=date, group_name=self.group_name_for(scope.bot_id, group_id) or group_id,
            topics=self._topics(group_id, date), own_lines=own, notes=self._notes(scope, start),
        )
        response = await self.llm.text_chat(
            prompt=prompt, system_prompt=prepend_identity_safety_system_prompt(None, always=True), contexts=[],
        )
        match = re.search(r"\{.*\}", str(getattr(response, "completion_text", "") or ""), re.S)
        try:
            diary = json.loads(match.group(0)) if match else {}
        except ValueError:
            diary = {}
        title, content = str(diary.get("title") or "").strip(), str(diary.get("content") or "").strip()
        summary = str(diary.get("summary") or "").strip() or title
        if not title or len(content) < 60:
            self._count("skip_empty")
            return False
        record = self.tool_registry.get("wave_memory_record_diary_episode") if self.tool_registry else None
        if record is None:
            return False
        result = await record.instance.call(
            runtime_tool_context(scope, source="daily_diary"),
            diary_title=title, diary_content=content, episode_summary=summary, emotional_weight=0.6,
        )
        if self.writer is not None:
            try:
                await self.writer.enqueue({
                    "scope": scope,
                    "group_id": group_id,
                    "sender_id": "bot",
                    "sender_name": bot_name,
                    "content": f"【{date} 日记】{title}\n{content}",
                    "timestamp": now,
                    "source": "experience",
                    "importance": 1.5,
                    "event_id": f"diary:{scope.session.id}:{date}",
                })
            except Exception as exc:
                logger.warning(f"[WaveMemory] 日记写入记忆库失败: {exc!r}")
        self._count("written")
        logger.info(f"[WaveMemory] 已写日记 {scope.session.id} {date}：{title}（{str(result)[:40]}）")
        return True


__all__ = ["DailyDiaryService"]
