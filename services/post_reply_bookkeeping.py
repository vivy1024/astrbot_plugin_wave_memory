"""事后记账：Bot 在群里说完话、对话停一会儿之后，回看这一段。

过去这些全靠她在回复当下顺手调工具：好感结算、人情、事实、黑话、关切。模型忙着回话时
很少会想起记账（线上 28 小时约 30 次回复只记了 2 次；21 个人的未结算能量攒满了却一直没结算）。
这里把记账从回复里拆出来：

- 触发：Bot 在某群发言后，该群 ``delay_seconds`` 内没有它的新发言（一段对话告一段落）；
  同一群两次记账至少间隔 ``session_cooldown_seconds``，全局每天最多 ``daily_limit`` 次。
- 输入：这段对话（上次记账之后的最多 ``window_messages`` 条）、在场群友里未结算能量已满需要结算的人、
  本群未了的关切。
- 输出：模型按固定 JSON 列出要做的记账，逐条交给对应的正式工具执行——校验、作用域隔离、好感
  变动范围、事实待审/自动审核都与现场调用完全相同，这里不另开写入口。
- 不再归纳信念：信念是她从自己的经历里慢慢形成的看法，只由每日日记之后的反思产生
  （``services/belief_reflection.py``）；旧模型输出里残留的 ``beliefs`` 字段直接忽略。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
    from .identity_safety import prepend_identity_safety_system_prompt
    from .impression_timeline import UNSETTLED_ENERGY_FULL, affinity_shift_range, effective_unsettled_state
    from .tool_registry import runtime_tool_context
    from .reply_feedback import BOOKKEEPING_LEARNING_PROMPT
    from ..tools.person_identity import resolve_user_id
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.identity_safety import prepend_identity_safety_system_prompt
    from services.impression_timeline import UNSETTLED_ENERGY_FULL, affinity_shift_range, effective_unsettled_state
    from services.tool_registry import runtime_tool_context
    from services.reply_feedback import BOOKKEEPING_LEARNING_PROMPT
    from tools.person_identity import resolve_user_id

PROMPT = """你是{bot_name}。下面是你刚在群里参与的一段对话（「{bot_name}」是你自己说的话）。
现在对话告一段落，请以你自己的视角回看，把值得记下来的事整理出来。宁缺毋滥：没有就留空数组，
不要编造，所有内容都必须能在对话原文里找到依据。

【对话】
{transcript}

{settle_block}{concern_block}只输出一个 JSON 对象，字段如下（都可以是空数组）：
{{
  "impressions": [  // 对某位群友形成或更新了看法；需要结算的人请务必给出一条
    {{"user": "群友名字", "impression": "一句话观感", "dimension": "trust|fun|depth|hostility|familiarity",
      "delta": 数字（本轮好感变化，范围见上，无变化填 0）, "source_quote": "对方的原话"}}
  ],
  "facts": [  // 群友关于自己或他人的客观事实（生日、职业、在玩的游戏、养的宠物……）
    {{"subject": "谁", "predicate": "是/喜欢/在做…", "object": "什么", "source_quote": "原话",
      "importance": 1到5（5=身份/长期：职业、生日、家人、重大经历；3=一般喜好习惯；1=一时的琐事。都会记，只影响以后想起的先后）}}
  ],
  "jargon": [  // 群里特有的黑话、梗、昵称（外人看不懂的说法）
    {{"phrase": "词", "meaning": "在这个群里是什么意思，出自哪句话"}}
  ],
  "social_anchors": [  // 值得记住的人情往来
    {{"user": "群友名字", "anchor_type": "bot_helped_user|user_helped_bot|bot_promised_user|boundary_hit",
      "summary": "发生了什么", "is_active_concern": true/false（需要日后跟进就填 true）}}
  ],
  "concerns": [  // 待跟进的事：群友接下来要经历、之后值得问一句的事（考试、面试、看病、搬家、比赛、
                 // 出远门……），或你答应过别人还没做的事。已经发生完的事写进 facts，不写这里
    {{"topic": "一句话（谁、什么事）", "target_user": "关于谁",
      "due_date": "能从原话推出哪天发生/出结果就写 YYYY-MM-DD（今天是 {today}），推不出留空"}}
  ],
  "resolved_concerns": [  // 上面列出的未了关切里，这段对话里已经了结的
    {{"concern_id": 数字, "note": "怎么了结的"}}
  ],
  "mood": {{"direction": "up|down|none", "strength": 0到1, "reason": "一句话：谁对你做了什么 / 你自己遇到了什么"}}
  // 这段对话对你自己心情的影响。只看冲着你来的事和你自己的遭遇：被夸、被感谢、被骂、被嘲讽、
  // 被纠正、帮上了忙、事情办砸了（生图失败、答错）、被冷落。群里热闹或吵架本身不算。
  // 同一句损话，熟人间斗嘴和真心冒犯不一样，按你对这个人的了解判断。没有这类事就填 none
}}"""


def _json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return {}
    # Comments outside JSON strings only: a quoted URL/source message is data.
    raw = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*',
                 lambda item: "" if item.group(0).startswith("//") else item.group(0), match.group(0))
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _items(payload: dict[str, Any], key: str, limit: int) -> list[dict[str, Any]]:
    value = payload.get(key)
    return [item for item in value if isinstance(item, dict)][:limit] if isinstance(value, list) else []


class PostReplyBookkeeper:
    def __init__(
        self,
        db: Any,
        tool_registry: Any,
        llm: Any,
        *,
        enabled: bool = True,
        delay_seconds: float = 90.0,
        session_cooldown_seconds: float = 600.0,
        daily_limit: int = 150,
        window_messages: int = 40,
        bot_name_for: Callable[[str], str] | None = None,
        mood_recorder: Callable[..., Any] | None = None,
        feedback_recorder: Callable[..., Any] | None = None,
    ):
        self.db = db
        self.tool_registry = tool_registry
        self.llm = llm
        self.enabled = bool(enabled) and llm is not None and tool_registry is not None
        self.delay_seconds = max(1.0, float(delay_seconds))
        self.session_cooldown = max(0.0, float(session_cooldown_seconds))
        self.daily_limit = max(1, int(daily_limit))
        self.window_messages = max(6, int(window_messages))
        self.bot_name_for = bot_name_for or (lambda bot_id: bot_id)
        # 心情 v2：同一次 LLM 调用顺带输出的心情影响交给 BotMoodService（不新增调用；解析失败安全忽略）。
        self.mood_recorder = mood_recorder
        self.feedback_recorder = feedback_recorder
        self._timers: dict[tuple, asyncio.Task] = {}
        self._last_run: dict[tuple, float] = {}
        self._last_memory_id: dict[tuple, int] = {}
        self._day = ""
        self._runs_today = 0
        self.stats: dict[str, int] = {}

    def _count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    @staticmethod
    def _scope_key(scope: RuntimeScope) -> tuple:
        return scope.bot_id, scope.session.id, scope.visibility

    def _learning_enabled(self) -> bool:
        recorder_owner = getattr(self.feedback_recorder, "__self__", None)
        return self.feedback_recorder is not None and bool(getattr(recorder_owner, "learning_enabled", True))

    # ─── 调度 ───

    def schedule(self, scope: RuntimeScope) -> None:
        """Bot 在群里说了一句话：重置该 Bot/群的记账计时器。"""
        if not self.enabled or not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return
        key = self._scope_key(scope)
        timer = self._timers.get(key)
        if timer is not None and not timer.done():
            timer.cancel()
        try:
            self._timers[key] = asyncio.get_running_loop().create_task(self._delayed(scope))
        except RuntimeError:
            return

    def cancel_all(self) -> None:
        for timer in self._timers.values():
            if not timer.done():
                timer.cancel()
        self._timers.clear()

    async def _delayed(self, scope: RuntimeScope) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            return
        key = self._scope_key(scope)
        now = time.time()
        if now - self._last_run.get(key, 0.0) < self.session_cooldown:
            # 冷却中：到点再来一次，这段对话不会漏
            self._count("deferred_cooldown")
            await asyncio.sleep(max(1.0, self.session_cooldown - (now - self._last_run.get(key, 0.0))))
        day = time.strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._runs_today = day, 0
        if self._runs_today >= self.daily_limit:
            self._count("skip_daily_limit")
            return
        self._runs_today += 1
        self._last_run[key] = time.time()
        try:
            await self.run(scope)
        except Exception as exc:
            self._count("error")
            logger.warning(f"[WaveMemory] 事后记账失败 {key}: {exc!r}")

    # ─── 执行 ───

    def _transcript(self, scope: RuntimeScope, bot_name: str) -> tuple[list[tuple], str]:
        key = self._scope_key(scope)
        since = self._last_memory_id.get(key, 0)
        rows = self.db.conn.execute(
            """SELECT id, sender_id, sender_name, content FROM memories
                WHERE bot_id=? AND session_id=? AND visibility=? AND id > ?
                  AND COALESCE(quarantine, 0) = 0 AND content IS NOT NULL
                ORDER BY id DESC LIMIT ?""",
            (scope.bot_id, scope.session.id, scope.visibility, since, self.window_messages),
        ).fetchall()
        rows = list(reversed(rows))
        lines = []
        for _id, sender_id, sender_name, content in rows:
            speaker = bot_name if sender_id == "bot" else (sender_name or sender_id or "某人")
            lines.append(f"[memory:{_id} sender:{json.dumps(str(sender_id), ensure_ascii=False)}] {speaker}：{str(content or '')[:200]}")
        return rows, "\n".join(lines)

    def _settle_block(self, scope: RuntimeScope, rows: list[tuple]) -> str:
        people: dict[str, str] = {}
        for _id, sender_id, sender_name, _content in rows:
            if sender_id and sender_id != "bot":
                people[str(sender_id)] = sender_name or str(sender_id)
        lines = []
        for user_id, name in people.items():
            state = effective_unsettled_state(self.db, bot_id=scope.bot_id, user_id=user_id, scene=scope.session.conversation_id)
            energy = float(state.get("energy") or 0.0)
            if energy >= UNSETTLED_ENERGY_FULL:
                bounds = affinity_shift_range({}, energy=energy)
                lines.append(f"- {name}：未结算能量 {energy:g}，本轮好感变化可在 [{bounds['min']}, {bounds['max']}]")
        if not lines:
            return ""
        return "【需要结算的人】你对他们攒了很多没定性的观感，请在 impressions 里各给一条阶段性定性：\n" + "\n".join(lines[:8]) + "\n\n"

    def _concern_block(self, scope: RuntimeScope) -> str:
        rows = self.db.conn.execute(
            """SELECT id, topic FROM scoped_soul_concerns
                WHERE bot_id=? AND session_id=? AND visibility=? AND status IN ('active', 'progressing')
                ORDER BY intensity DESC, id DESC LIMIT 8""",
            (scope.bot_id, scope.session.id, scope.visibility),
        ).fetchall()
        if not rows:
            return ""
        return "【你还挂念着的事】\n" + "\n".join(f"- concern:{r[0]} {r[1]}" for r in rows) + "\n\n"

    @staticmethod
    def _call_succeeded(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        failure_markers = (
            "失败", "拒绝", "无效", "必填", "没有在", "不支持", "未就绪", "未找到",
            "无法", "错误", "连接已断开", "未初始化",
        )
        return not any(marker in value[:80] for marker in failure_markers)

    async def _call_action(self, name: str, scope: RuntimeScope, **kwargs) -> int:
        result = await self._call(name, scope, **kwargs)
        return 1 if self._call_succeeded(result) else 0

    def _subject_scope(self, scope: RuntimeScope, target_user: str) -> RuntimeScope | None:
        """把记账目标解析成正式主体，供需要 subject 的关系工具使用。"""
        if not isinstance(scope, RuntimeScope) or scope.session is None or not target_user:
            return None
        try:
            user_id = resolve_user_id(self.db, str(target_user), scope)
        except Exception:
            user_id = ""
        if not user_id:
            return None
        return replace(scope, subject_principal_id=f"{scope.session.platform_id}:user:{user_id}")

    async def _call(self, name: str, scope: RuntimeScope, *, subject_principal_id: str | None = None, **kwargs) -> str:
        record = self.tool_registry.get(name) if self.tool_registry is not None else None
        if record is None or not getattr(record, "enabled", True):
            return ""
        call_scope = scope
        if subject_principal_id:
            if scope.session is None:
                return "主体 Scope 缺少会话"
            call_scope = replace(scope, subject_principal_id=subject_principal_id)
        try:
            result = await record.instance.call(runtime_tool_context(call_scope, source="bookkeeping"), **kwargs)
        except Exception as exc:
            self._count(f"{name}:error")
            return f"失败: {exc}"
        text = str(result or "")
        failed = any(word in text[:20] for word in ("拒绝", "失败", "无效", "必填", "没有在", "不支持", "未就绪"))
        self._count(f"{name}:{'rejected' if failed else 'ok'}")
        return text

    async def run(self, scope: RuntimeScope) -> dict[str, int]:
        if not self.enabled or not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return {}
        bot_name = self.bot_name_for(scope.bot_id) or scope.bot_id
        rows, transcript = self._transcript(scope, bot_name)
        if len(rows) < 3 or not any(r[1] == "bot" for r in rows):
            self._count("skip_short")
            return {}
        prompt = PROMPT.format(
            bot_name=bot_name,
            transcript=transcript,
            settle_block=self._settle_block(scope, rows),
            concern_block=self._concern_block(scope),
            today=time.strftime("%Y-%m-%d"),
        )
        learning_enabled = self._learning_enabled()
        if learning_enabled:
            prompt_factory = getattr(getattr(self.feedback_recorder, "__self__", None), "bookkeeping_prompt", None)
            prompt += prompt_factory() if callable(prompt_factory) else BOOKKEEPING_LEARNING_PROMPT
        # 模型分工：路由客户端按这个 Bot 的模型覆盖选链（Bot Profile 的 model / models.bookkeeping）
        for_bot = getattr(self.llm, "for_bot", None)
        llm = for_bot(scope.bot_id) if callable(for_bot) else self.llm
        response = await llm.text_chat(
            prompt=prompt,
            system_prompt=prepend_identity_safety_system_prompt(None, always=True),
            contexts=[],
        )
        payload = _json_object(getattr(response, "completion_text", "") or "")
        self._last_memory_id[self._scope_key(scope)] = int(rows[-1][0])
        self._count("runs")
        if learning_enabled:
            try:
                counts = self.feedback_recorder(scope, payload, source_message_ids=tuple(int(row[0]) for row in rows))
                if isinstance(counts, dict):
                    for key in ("feedback", "expressions"):
                        self._count(key, int(counts.get(key, 0)))
            except Exception as exc:
                self._count("feedback_error")
                logger.warning(f"[WaveMemory] 事后反馈未写入: {type(exc).__name__}")
        if self.mood_recorder is not None:
            try:
                if self.mood_recorder(scope, payload.get("mood"), ref=str(rows[-1][0])) is not None:
                    self._count("mood")
            except Exception as exc:
                logger.debug(f"[WaveMemory] 事后记账心情记录失败: {exc!r}")
        done = 0
        for item in _items(payload, "impressions", 8):
            if item.get("user") and item.get("impression"):
                target_scope = self._subject_scope(scope, str(item["user"]))
                if target_scope is None:
                    self._count("wave_memory_record_social_impression:subject_missing")
                    continue
                done += await self._call_action(
                    "wave_memory_record_social_impression", scope,
                    subject_principal_id=target_scope.subject_principal_id,
                    target_user=str(item["user"]), impression=str(item["impression"]),
                    dimension=str(item.get("dimension") or "trust"), delta=item.get("delta", 0) or 0,
                    source_quote=str(item.get("source_quote") or ""), shift_reason="事后回看这段对话",
                )
        for item in _items(payload, "facts", 6):
            if all(item.get(k) for k in ("subject", "predicate", "object", "source_quote")):
                done += await self._call_action(
                    "wave_memory_propose_fact", scope,
                    subject=str(item["subject"]), predicate=str(item["predicate"]), object=str(item["object"]),
                    source_quote=str(item["source_quote"]), context_evidence="事后回看群聊时记下",
                    importance=item.get("importance"),
                )
        for item in _items(payload, "jargon", 4):
            if item.get("phrase") and item.get("meaning"):
                done += await self._call_action(
                    "wave_memory_mark_cultural_moment", scope,
                    moment_type="potential_jargon", target_phrase_or_snippet=str(item["phrase"]),
                    context_note=str(item["meaning"]),
                )
        for item in _items(payload, "social_anchors", 4):
            if item.get("user") and item.get("anchor_type") and item.get("summary"):
                target_scope = self._subject_scope(scope, str(item["user"]))
                if target_scope is None:
                    self._count("wave_memory_note_social_anchor:subject_missing")
                    continue
                done += await self._call_action(
                    "wave_memory_note_social_anchor", scope,
                    subject_principal_id=target_scope.subject_principal_id,
                    target_user=str(item["user"]), anchor_type=str(item["anchor_type"]),
                    summary=str(item["summary"]), is_active_concern=bool(item.get("is_active_concern")),
                )
        for item in _items(payload, "concerns", 3):
            if item.get("topic"):
                done += await self._call_action(
                    "wave_memory_note_concern", scope,
                    action="note", topic=str(item["topic"]), target_user=str(item.get("target_user") or ""),
                    concern_type="follow_up", due_date=str(item.get("due_date") or ""),
                )
        for item in _items(payload, "resolved_concerns", 4):
            if item.get("concern_id") and item.get("note"):
                done += await self._call_action(
                    "wave_memory_note_concern", scope,
                    action="resolve", concern_id=item["concern_id"], note=str(item["note"]),
                )
        if _items(payload, "beliefs", 20):
            # 旧提示词或模型惯性输出的信念一律不写：信念只来自反思
            self._count("beliefs_ignored", len(_items(payload, "beliefs", 20)))
        self._count("actions", done)
        if done:
            logger.info(f"[WaveMemory] 事后记账 {scope.session.id}：回看 {len(rows)} 条，记下 {done} 项")
        return {"actions": done, "messages": len(rows)}


__all__ = ["PostReplyBookkeeper"]
