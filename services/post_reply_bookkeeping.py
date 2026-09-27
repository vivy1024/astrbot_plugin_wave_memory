"""事后记账：羽书在群里说完话、对话停一会儿之后，单独回看这一段，把该记的记下来。

过去这些全靠她在回复当下顺手调工具：好感结算、人情、事实、信念、黑话、关切。模型忙着回话时
很少会想起记账（线上 28 小时约 30 次回复只记了 2 次；21 个人的未结算能量攒满了却一直没结算）。
这里把记账从回复里拆出来：

- 触发：羽书在某群发言后，该群 ``delay_seconds`` 内没有她的新发言（一段对话告一段落）；
  同一群两次记账至少间隔 ``session_cooldown_seconds``，全局每天最多 ``daily_limit`` 次。
- 输入：这段对话（上次记账之后的最多 ``window_messages`` 条）、在场群友里未结算能量已满需要结算的人、
  本群未了的关切、已批准的事实（供提信念）。
- 输出：模型按固定 JSON 列出要做的记账，逐条交给对应的正式工具执行——校验、作用域隔离、好感
  变动范围、事实待审/自动审核都与羽书现场调用完全相同，这里不另开写入口。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
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
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.identity_safety import prepend_identity_safety_system_prompt
    from services.impression_timeline import UNSETTLED_ENERGY_FULL, affinity_shift_range, effective_unsettled_state
    from services.tool_registry import runtime_tool_context

PROMPT = """你是{bot_name}。下面是你刚在群里参与的一段对话（「{bot_name}」是你自己说的话）。
现在对话告一段落，请以你自己的视角回看，把值得记下来的事整理出来。宁缺毋滥：没有就留空数组，
不要编造，所有内容都必须能在对话原文里找到依据。

【对话】
{transcript}

{settle_block}{concern_block}{fact_block}只输出一个 JSON 对象，字段如下（都可以是空数组）：
{{
  "impressions": [  // 对某位群友形成或更新了看法；需要结算的人请务必给出一条
    {{"user": "群友名字", "impression": "一句话观感", "dimension": "trust|fun|depth|hostility|familiarity",
      "delta": 数字（本轮好感变化，范围见上，无变化填 0）, "source_quote": "对方的原话"}}
  ],
  "facts": [  // 群友关于自己或他人的客观事实（生日、职业、在玩的游戏、养的宠物……）
    {{"subject": "谁", "predicate": "是/喜欢/在做…", "object": "什么", "source_quote": "原话"}}
  ],
  "jargon": [  // 群里特有的黑话、梗、昵称（外人看不懂的说法）
    {{"phrase": "词", "meaning": "在这个群里是什么意思，出自哪句话"}}
  ],
  "social_anchors": [  // 值得记住的人情往来
    {{"user": "群友名字", "anchor_type": "bot_helped_user|user_helped_bot|bot_promised_user|boundary_hit",
      "summary": "发生了什么", "is_active_concern": true/false（需要日后跟进就填 true）}}
  ],
  "concerns": [  // 你挂念、想之后跟进的事（对方说要去面试、身体不舒服、你答应了什么……）
    {{"topic": "一句话", "target_user": "关于谁"}}
  ],
  "resolved_concerns": [  // 上面列出的未了关切里，这段对话里已经了结的
    {{"concern_id": 数字, "note": "怎么了结的"}}
  ],
  "beliefs": [  // 只有当上面列出的已批准事实里有两条以上共同支撑一个判断时才写
    {{"belief_type": "person_judgment|world_view|preference", "content": "判断", "source_fact_ids": [事实id, ...]}}
  ]
}}"""


def _json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return {}
    raw = re.sub(r"//[^\n]*", "", match.group(0))
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
        self._timers: dict[str, asyncio.Task] = {}
        self._last_run: dict[str, float] = {}
        self._last_memory_id: dict[str, int] = {}
        self._day = ""
        self._runs_today = 0
        self.stats: dict[str, int] = {}

    def _count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    # ─── 调度 ───

    def schedule(self, scope: RuntimeScope) -> None:
        """羽书在群里说了一句话：重置该群的记账计时器。"""
        if not self.enabled or not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return
        key = scope.session.id
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
        key = scope.session.id
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
        key = scope.session.id
        since = self._last_memory_id.get(key, 0)
        rows = self.db.conn.execute(
            """SELECT id, sender_id, sender_name, content FROM memories
                WHERE bot_id=? AND session_id=? AND visibility=? AND id > ?
                  AND COALESCE(quarantine, 0) = 0 AND content IS NOT NULL
                ORDER BY id DESC LIMIT ?""",
            (scope.bot_id, key, scope.visibility, since, self.window_messages),
        ).fetchall()
        rows = list(reversed(rows))
        lines = []
        for _id, sender_id, sender_name, content in rows:
            speaker = bot_name if sender_id == "bot" else (sender_name or sender_id or "某人")
            lines.append(f"{speaker}：{str(content or '')[:200]}")
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

    def _fact_block(self, scope: RuntimeScope) -> str:
        repo = getattr(self.db, "scoped_knowledge", None)
        if repo is None:
            return ""
        facts = [f for f in repo.list_scoped_facts(scope, limit=60) if f.get("status") in {"active", "approved"}][:20]
        if len(facts) < 2:
            return ""
        return "【已批准的事实（提信念时引用 id）】\n" + "\n".join(
            f"- {f['id']}: {f['subject']} {f['predicate']} {f['object']}" for f in facts
        ) + "\n\n"

    async def _call(self, name: str, scope: RuntimeScope, **kwargs) -> str:
        record = self.tool_registry.get(name) if self.tool_registry is not None else None
        if record is None or not getattr(record, "enabled", True):
            return ""
        try:
            result = await record.instance.call(runtime_tool_context(scope, source="bookkeeping"), **kwargs)
        except Exception as exc:
            self._count(f"{name}:error")
            return f"失败: {exc}"
        text = str(result or "")
        failed = any(word in text[:20] for word in ("拒绝", "失败", "无效", "必填", "没有在", "不支持", "未就绪"))
        self._count(f"{name}:{'rejected' if failed else 'ok'}")
        return text

    async def run(self, scope: RuntimeScope) -> dict[str, int]:
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
            fact_block=self._fact_block(scope),
        )
        response = await self.llm.text_chat(
            prompt=prompt,
            system_prompt=prepend_identity_safety_system_prompt(None, always=True),
            contexts=[],
        )
        payload = _json_object(getattr(response, "completion_text", "") or "")
        self._last_memory_id[scope.session.id] = int(rows[-1][0])
        self._count("runs")
        done = 0
        for item in _items(payload, "impressions", 8):
            if item.get("user") and item.get("impression"):
                await self._call(
                    "wave_memory_record_social_impression", scope,
                    target_user=str(item["user"]), impression=str(item["impression"]),
                    dimension=str(item.get("dimension") or "trust"), delta=item.get("delta", 0) or 0,
                    source_quote=str(item.get("source_quote") or ""), shift_reason="事后回看这段对话",
                )
                done += 1
        for item in _items(payload, "facts", 6):
            if all(item.get(k) for k in ("subject", "predicate", "object", "source_quote")):
                await self._call(
                    "wave_memory_propose_fact", scope,
                    subject=str(item["subject"]), predicate=str(item["predicate"]), object=str(item["object"]),
                    source_quote=str(item["source_quote"]), context_evidence="事后回看群聊时记下",
                )
                done += 1
        for item in _items(payload, "jargon", 4):
            if item.get("phrase") and item.get("meaning"):
                await self._call(
                    "wave_memory_mark_cultural_moment", scope,
                    moment_type="potential_jargon", target_phrase_or_snippet=str(item["phrase"]),
                    context_note=str(item["meaning"]),
                )
                done += 1
        for item in _items(payload, "social_anchors", 4):
            if item.get("user") and item.get("anchor_type") and item.get("summary"):
                await self._call(
                    "wave_memory_note_social_anchor", scope,
                    target_user=str(item["user"]), anchor_type=str(item["anchor_type"]),
                    summary=str(item["summary"]), is_active_concern=bool(item.get("is_active_concern")),
                )
                done += 1
        for item in _items(payload, "concerns", 3):
            if item.get("topic"):
                await self._call(
                    "wave_memory_note_concern", scope,
                    action="note", topic=str(item["topic"]), target_user=str(item.get("target_user") or ""),
                )
                done += 1
        for item in _items(payload, "resolved_concerns", 4):
            if item.get("concern_id") and item.get("note"):
                await self._call(
                    "wave_memory_note_concern", scope,
                    action="resolve", concern_id=item["concern_id"], note=str(item["note"]),
                )
                done += 1
        for item in _items(payload, "beliefs", 2):
            ids = item.get("source_fact_ids")
            if item.get("content") and isinstance(ids, list) and len(ids) >= 2:
                await self._call(
                    "wave_memory_propose_belief", scope,
                    belief_type=str(item.get("belief_type") or "person_judgment"), content=str(item["content"]),
                    grounding_evidence="事后回看群聊时由已批准事实归纳", source_fact_ids=ids,
                )
                done += 1
        self._count("actions", done)
        if done:
            logger.info(f"[WaveMemory] 事后记账 {scope.session.id}：回看 {len(rows)} 条，记下 {done} 项")
        return {"actions": done, "messages": len(rows)}


__all__ = ["PostReplyBookkeeper"]
