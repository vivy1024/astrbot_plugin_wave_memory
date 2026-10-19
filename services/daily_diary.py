"""每日日记：每晚为羽书当天说过话的每个群写一篇第一人称日记，并在次日补写漏掉的日期。

素材四份：
1. 群分析插件当天的分析（话题与总评）——有就用，没开或当天失败就跳过；
2. 羽书当天自己在这个群说的话；
3. 当天她自己的游戏经历（Runtime API 写入的 source=game_event，如直播间里的死亡、目标、发现）；
4. 当天记下的印象、人情备忘与关切。

写法沿用 VCP 日记的形态：正文之后一行 ``Tag: …``，话题名与人名当标签。落两处：
- 经历时间线（经 ``wave_memory_record_diary_episode`` 工具，与现场写日记同一校验）；
- 记忆库（source=experience），检索时能直接找到。

同一群同一天只写一篇（按经历时间线里当天是否已有日记判断，重启不会重复写）。
每写完一轮就通知监听方（``add_pass_listener``）：信念反思接在这里，保证在当天日记写完之后进行。

**为什么会漏写，以及怎么补（线上实测，勿删）**

线上 10-14 / 10-16 / 10-17 三天日记全空，日志直接给出原因：当晚 23:0x 与 23:4x 两轮
调用分别撞上 ``RateLimitError(429)`` 与 ``InternalServerError(503 model upstream unavailable)``。
原来的实现对这些失败没有任何补救，因为：

- ``write_due`` 只处理 ``_day_start(now)`` 这**一天**；一旦跨过午夜，``_day_start`` 翻页，
  前一天的漏写再也不会被重试 —— 一次失败就是永久丢失；
- 所有素材查询只有下界 ``timestamp >= start``，没有上界，既没法安全地补历史
  （会把之后所有天的素材混进那一天），``_written`` 还会把「昨天写过」误判成「今天写过」；
- ``_sessions_today`` 额外带 ``id > MAX(id) - 20000`` 的窗口。线上单日入库上万条
  （10-14 为 14481 条），补记两天前的日期时当天 Bot 消息的 id 已落到窗口下界之外，
  查询会静默返回空，补记彻底失效 —— 所以这里改成纯时间区间过滤。

现在的策略：``write_due`` 先无条件下界内补写最近 ``backfill_days`` 天（默认 3）里
每一个「当天有发言、当天没日记」的日期，再按 ``hour`` 判断是否写今天。
``write_one`` 接收目标日的时间戳，日记的 ``timestamp`` / ``occurred_at`` / ``created_at``
都落在被补的那一天，群分析素材也能按 ``date_str`` 对上。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
    from .belief_lifecycle import INJECTABLE_STATUSES
    from .concern_followup import _scope_from_row
    from .daily_diary_bridge import find_daily_analysis_db
    from .game_events import GAME_EVENT_SOURCE
    from .identity_safety import prepend_identity_safety_system_prompt, quarantine_long_text
    from .tool_registry import runtime_tool_context
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.belief_lifecycle import INJECTABLE_STATUSES
    from services.concern_followup import _scope_from_row
    from services.daily_diary_bridge import find_daily_analysis_db
    from services.game_events import GAME_EVENT_SOURCE
    from services.identity_safety import prepend_identity_safety_system_prompt, quarantine_long_text
    from services.tool_registry import runtime_tool_context

PROMPT = """你是{bot_name}。今天是 {date}，一天快结束了，请以第一人称写一篇今天在「{group_name}」的日记。

【群里今天聊了什么】
{topics}

【今天自己说过的一些话】
{own_lines}
{games}
【今天记下的人和事】
{notes}
{concerns}
{beliefs}
要求：
- 写你自己的经历、感受和看法，不是群聊流水账；提到群友用他们的名字。
- 正文 250 到 600 字。最后另起一行写「Tag: 」加 3 到 8 个标签（话题名、人名），用逗号分隔。
- 不要编造素材里没有的事；素材很少就写短一点。
- **对照你已有的看法**：今天有没有哪件事、哪个人的反应跟往常不一样？和往常一样就不必硬写，
  一旦发现了不同，就把它写出来，写清是哪件事让你这么觉得。
- 还惦记着的事可以顺笔提一句，但日记不是待办清单：没进展就不用逐条点名。
只输出一个 JSON 对象：{{"title": "日记标题", "content": "正文（含最后的 Tag 行）", "summary": "一句话概括今天"}}"""

# 关切与已有看法两段的段落外壳；两者都为空时整个不出现，不留空标题。
_CONCERNS_BLOCK = """
【你还惦记着、没结果的事】
{concerns}
"""
_BELIEFS_BLOCK = """
【你已经形成的看法】
{beliefs}
"""


def _day_start(now: float) -> float:
    local = datetime.fromtimestamp(now)
    return datetime(local.year, local.month, local.day).timestamp()


def _day_bounds(now: float) -> tuple[float, float]:
    """``(当天 00:00, 次日 00:00)`` 的半开区间。补记与写今天都只认这个区间。"""

    start = _day_start(now)
    local = datetime.fromtimestamp(start)
    next_day = datetime(local.year, local.month, local.day) + timedelta(days=1)
    return start, next_day.timestamp()


def _diary_stamp(day_start: float) -> float:
    """把日记的写入时间钉在目标日的 23:30，让 timestamp/occurred_at/created_at 都落在正确那天。"""

    local = datetime.fromtimestamp(day_start) + timedelta(hours=23, minutes=30)
    return local.timestamp()


# 游戏经历最多列这么多条；多出来时先保证目标、发现、里程碑，死亡只留第一次和最后一次并注明次数。
_GAME_LINES_MAX = 20

# 补记默认回看天数（可用 daily_diary_backfill_days 覆盖，0 表示只写今天、不补历史）。
_DEFAULT_BACKFILL_DAYS = 3

# ``wave_memory_record_diary_episode`` 落盘失败的判据。
# 用「失败特征」而不是「成功特征」判断，是为了不把其它实现/测试夹具的返回误判成失败：
# 工具在拒绝、冷却、依赖缺失、落库异常时都返回下列文案之一，成功时返回 ✅ 开头的确认。
_EPISODE_FAILURE_MARKERS = (
    "经历日记被拒绝",
    "今日群聊经历日记已存在",
    "日记记录失败",
    "均为必填项",
    "数据库或群 Scope 未初始化",
    "数据库连接不可用",
)


def _episode_persisted(response: str) -> bool:
    """``wave_memory_record_diary_episode`` 的返回是否代表经历已落盘。"""

    text = str(response or "").strip()
    if not text:
        return False
    return not any(marker in text for marker in _EPISODE_FAILURE_MARKERS)


def _game_event_type(provenance: Any) -> str:
    """provenance.metadata.origin_kind = runtime:game_event:<game>:<event_type>。"""
    try:
        meta = json.loads(provenance or "{}").get("metadata") or {}
    except (TypeError, ValueError, AttributeError):
        return ""
    origin = str(meta.get("origin_kind") or "")
    return origin.rsplit(":", 1)[-1] if origin.startswith("runtime:game_event:") else ""


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
        backfill_days: int = _DEFAULT_BACKFILL_DAYS,
        bot_name_for: Callable[[str], str] | None = None,
        group_name_for: Callable[[str, str], str] | None = None,
    ):
        self.db = db
        self.tool_registry = tool_registry
        self.llm = llm
        self.writer = writer
        self.enabled = bool(enabled) and llm is not None and tool_registry is not None
        self.hour = min(23, max(0, int(hour)))
        self.backfill_days = max(0, int(backfill_days))
        self.bot_name_for = bot_name_for or (lambda bot_id: bot_id)
        self.group_name_for = group_name_for or (lambda bot_id, group_id: group_id)
        self.stats: dict[str, int] = {}
        self._pass_listeners: list[Callable[[float], Awaitable[Any]]] = []

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def add_pass_listener(self, listener: Callable[[float], Awaitable[Any]]) -> None:
        """每写完一轮日记后调用 ``listener(now)``（信念反思接在这里）；监听方出错不影响日记。"""
        self._pass_listeners.append(listener)

    async def run_pass(self, *, now: float | None = None) -> int:
        """写一轮日记，然后通知监听方：日记写完之后才轮到反思。

        补记历史不会触发监听方 —— 信念反思按「自上次以来读到的新日记」推进，
        只需要在**今天**的日记写完后被叫醒一次，否则补记期间会被反复唤醒。
        """
        stamp = float(now if now is not None else time.time())
        written = await self.write_due(now=stamp)
        if datetime.fromtimestamp(stamp).hour >= self.hour:
            for listener in list(self._pass_listeners):
                try:
                    await listener(stamp)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._count("listener_error")
                    logger.warning(f"[WaveMemory] 日记之后的处理失败: {exc!r}")
        return written

    async def loop(self) -> None:
        """固定间隔跑一轮：补记不等到 ``hour``，跨天后立刻能补上昨晚失败的日期。"""
        await asyncio.sleep(300)
        while True:
            try:
                await self.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[WaveMemory] 每日日记检查失败: {exc!r}")
            await asyncio.sleep(1800)

    def _sessions_on(self, start: float, end: float) -> list[tuple[str, str, str]]:
        """目标日当天有过发言的群会话。

        这里**故意不带 id 窗口**：线上单日入库上万条，``id > MAX(id) - 20000`` 会让
        补记两天前的日期时查不到任何 session（当天 Bot 消息的 id 早已落到窗口下界之外），
        补记会静默失效。改为纯时间半开区间过滤。
        """
        return self.db.conn.execute(
            """SELECT DISTINCT bot_id, session_id, visibility FROM memories
                WHERE sender_id='bot' AND visibility='group' AND timestamp >= ? AND timestamp < ?""",
            (start, end),
        ).fetchall()

    def _written(self, scope: RuntimeScope, start: float, end: float) -> bool:
        return self.db.conn.execute(
            """SELECT 1 FROM scoped_soul_timeline WHERE bot_id=? AND session_id=? AND visibility=?
                 AND event_summary LIKE '【日记】%' AND occurred_at >= ? AND occurred_at < ? LIMIT 1""",
            (scope.bot_id, scope.session.id, scope.visibility, start, end),
        ).fetchone() is not None

    async def _write_day(self, start: float, end: float, *, now: float) -> int:
        """写目标日的日记；``now`` 是钉在目标日 23:30 的时间戳。"""
        written = 0
        for bot_id, session_id, visibility in self._sessions_on(start, end):
            scope = _scope_from_row(bot_id, session_id, visibility)
            if scope is None or self._written(scope, start, end):
                continue
            try:
                if await self.write_one(scope, start=start, end=end, now=now):
                    written += 1
            except Exception as exc:
                self._count("error")
                logger.warning(f"[WaveMemory] 写日记失败 {session_id}: {exc!r}")
        return written

    async def write_due(self, *, now: float | None = None) -> int:
        """先补最近的漏写日期，再（到点后）写今天。失败不会让那一天永久丢失。"""
        if not self.enabled:
            return 0
        stamp = float(now if now is not None else time.time())
        today_start, today_end = _day_bounds(stamp)
        written = 0
        # 补记：从昨天往前回看 backfill_days 天。与 hour 无关，跨天后第一轮就能补上昨晚。
        for offset in range(1, self.backfill_days + 1):
            day_start = today_start - offset * 86400.0
            day_start, day_end = _day_bounds(day_start)
            try:
                written += await self._write_day(day_start, day_end, now=_diary_stamp(day_start))
            except Exception as exc:
                self._count("backfill_error")
                logger.warning(f"[WaveMemory] 补写日记失败 {datetime.fromtimestamp(day_start):%Y-%m-%d}: {exc!r}")
        # 今天：只有到点之后才写
        if datetime.fromtimestamp(stamp).hour >= self.hour:
            written += await self._write_day(today_start, today_end, now=stamp)
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

    def _own_lines(self, scope: RuntimeScope, start: float, end: float) -> tuple[str, int | None]:
        # 游戏经历也记在 sender_id='bot' 名下，但不是她说的话，单独成段（_game_lines）。
        rows = self.db.conn.execute(
            """SELECT id, content FROM memories
                WHERE bot_id=? AND session_id=? AND visibility=? AND sender_id='bot'
                  AND timestamp >= ? AND timestamp < ?
                  AND COALESCE(source, '') != ?
                ORDER BY id""",
            (scope.bot_id, scope.session.id, scope.visibility, start, end, GAME_EVENT_SOURCE),
        ).fetchall()
        if not rows:
            return "", None
        step = max(1, len(rows) // 20)
        picked = rows[::step][:20]
        return "\n".join(f"- {str(r[1] or '')[:120]}" for r in picked), int(rows[-1][0])

    def _game_lines(self, scope: RuntimeScope, start: float, end: float) -> str:
        """当天她自己的游戏经历，按时间先后；太多时保留目标与发现，死亡只留首尾并注明次数。"""
        try:
            rows = self.db.conn.execute(
                """SELECT timestamp, content, provenance FROM memories
                    WHERE bot_id=? AND session_id=? AND visibility=? AND sender_id='bot' AND source=?
                      AND timestamp >= ? AND timestamp < ? AND COALESCE(quarantine, 0)=0
                    ORDER BY timestamp, id""",
                (scope.bot_id, scope.session.id, scope.visibility, GAME_EVENT_SOURCE, start, end),
            ).fetchall()
        except Exception as exc:
            logger.debug(f"[WaveMemory] 日记游戏素材读取失败: {exc!r}")
            return ""
        if not rows:
            return ""
        picked = [(float(r[0] or 0), str(r[1] or "")) for r in rows]
        note = ""
        if len(picked) > _GAME_LINES_MAX:
            deaths = [row for row, raw in zip(picked, rows) if _game_event_type(raw[2]) == "death"]
            if len(deaths) > 2:
                dropped = {id(row) for row in deaths[1:-1]}
                picked = [row for row in picked if id(row) not in dropped]
                note = f"\n- （这一天一共死了 {len(deaths)} 次，中间 {len(deaths) - 2} 次没列出）"
            if len(picked) > _GAME_LINES_MAX:
                step = len(picked) / _GAME_LINES_MAX
                picked = [picked[int(i * step)] for i in range(_GAME_LINES_MAX)]
        lines = "\n".join(
            f"- {datetime.fromtimestamp(ts).strftime('%H:%M')} {content[:120]}" for ts, content in picked
        )
        return lines + note

    def _notes(self, scope: RuntimeScope, start: float, end: float) -> str:
        group_id = scope.session.conversation_id
        lines = []
        try:
            for kind, summary in self.db.conn.execute(
                """SELECT kind, summary FROM person_timeline_events
                    WHERE bot_id=? AND group_id=? AND kind IN ('impression', 'affinity')
                      AND created_at >= ? AND created_at < ?
                    ORDER BY created_at DESC LIMIT 10""",
                (scope.bot_id, group_id, start, end),
            ):
                lines.append(f"- {'印象' if kind == 'impression' else '关系'}：{str(summary or '')[:100]}")
            for (summary,) in self.db.conn.execute(
                """SELECT event_summary FROM scoped_soul_timeline
                    WHERE bot_id=? AND session_id=? AND visibility=? AND occurred_at >= ? AND occurred_at < ?
                    LIMIT 6""",
                (scope.bot_id, scope.session.id, scope.visibility, start, end),
            ):
                lines.append(f"- {str(summary or '')[:100]}")
        except Exception as exc:
            logger.debug(f"[WaveMemory] 日记素材读取失败: {exc!r}")
        return "\n".join(lines) or "（没有）"

    def _concerns(self, scope: RuntimeScope) -> str:
        """这个群里还没了结的关切——她惦记着、值得在日记里提一句的事。

        ``social_anchor``（人情账）不算：它占了线上 125/171，是「谁帮过我」的台账，
        不是需要惦记的事，而且 ``concern_followup`` 本来就把它排除在跟进之外。
        """
        try:
            rows = self.db.conn.execute(
                """SELECT topic, expected_resolution_at FROM scoped_soul_concerns
                    WHERE bot_id=? AND session_id=? AND visibility=?
                      AND status IN ('active', 'progressing')
                      AND COALESCE(concern_type, '') != 'social_anchor'
                    ORDER BY intensity DESC, id DESC LIMIT 6""",
                (scope.bot_id, scope.session.id, scope.visibility),
            ).fetchall()
        except Exception as exc:
            logger.debug(f"[WaveMemory] 日记关切素材读取失败: {exc!r}")
            return ""
        lines = []
        for topic, due in rows:
            text = str(topic or "").strip()[:100]
            if not text:
                continue
            lines.append(f"- {text}" + (f"（说好 {due} 有结果）" if due else ""))
        return "\n".join(lines)

    def _beliefs(self, bot_id: str) -> str:
        """她自己已经形成的看法——用来对照「今天和往常有什么不同」。"""
        repo = getattr(self.db, "bot_beliefs", None)
        if repo is None:
            return ""
        try:
            rows = repo.list_beliefs(bot_id, statuses=sorted(INJECTABLE_STATUSES), limit=12)
        except Exception as exc:
            logger.debug(f"[WaveMemory] 日记看法素材读取失败: {exc!r}")
            return ""
        return "\n".join(
            f"- {str(row.get('content') or '').strip()[:80]}" for row in rows if str(row.get("content") or "").strip()
        )

    async def write_one(self, scope: RuntimeScope, *, start: float, end: float, now: float) -> bool:
        own, anchor_id = self._own_lines(scope, start, end)
        games = self._game_lines(scope, start, end)
        if not own and not games:
            return False
        bot_name = self.bot_name_for(scope.bot_id) or scope.bot_id
        date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        group_id = scope.session.conversation_id
        topics_text = self._topics(group_id, date)
        concerns = self._concerns(scope)
        beliefs = self._beliefs(scope.bot_id)
        prompt = PROMPT.format(
            bot_name=bot_name, date=date, group_name=self.group_name_for(scope.bot_id, group_id) or group_id,
            topics=topics_text, own_lines=own or "（今天没怎么说话）",
            games=f"\n【你今天在游戏里的经历】\n{games}\n" if games else "",
            notes=self._notes(scope, start, end),
            concerns=_CONCERNS_BLOCK.format(concerns=concerns) if concerns else "",
            beliefs=_BELIEFS_BLOCK.format(beliefs=beliefs) if beliefs else "",
        )
        # 模型分工：路由客户端按这个 Bot 的模型覆盖选链（Bot Profile 的 model / models.diary）
        for_bot = getattr(self.llm, "for_bot", None)
        llm = for_bot(scope.bot_id) if callable(for_bot) else self.llm
        response = await llm.text_chat(
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
        # 正文先做句级身份守卫，让**经历库与记忆库拿到同一份文本**。
        # 工具内部还会再剥一次（同一函数，幂等）；这里做是为了让 memories 与
        # experience_episodes 不会一个存原文、一个存净化文本。
        content, _stripped = quarantine_long_text(content)
        if len(content) < 60:
            self._count("skip_contaminated")
            return False
        record = self.tool_registry.get("wave_memory_record_diary_episode") if self.tool_registry else None
        if record is None:
            return False
        result = await record.instance.call(
            runtime_tool_context(scope, source="daily_diary"),
            diary_title=title, diary_content=content, episode_summary=summary, emotional_weight=6,
            # 把时间钉在目标日（补记时是过去的那一天），经历片段与时间线才会落对日期。
            occurred_at=now,
        )
        result_text = str(result or "")
        # 工具没写成功时**绝不能**再往记忆库写：那正是线上「memories 有日记、experience_episodes
        # 没有」的来源（47 vs 42）。守卫拒绝、6 小时冷却、落库异常都走这里。
        if not _episode_persisted(result_text):
            self._count("episode_rejected")
            logger.warning(f"[WaveMemory] 日记未落经历库，已跳过记忆写入 {scope.session.id} {date}：{result_text[:80]}")
            return False
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
                self._count("memory_enqueue_failed")
                logger.warning(f"[WaveMemory] 日记写入记忆库失败: {exc!r}")
        self._count("written")
        logger.info(f"[WaveMemory] 已写日记 {scope.session.id} {date}：{title}（{result_text[:40]}）")
        return True


__all__ = ["DailyDiaryService"]
