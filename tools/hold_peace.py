"""波次决定「这次不回复」的工具。

模型认为不需要接口、不该口头表态、或再回应会惹得争议时，可显式调用这个工具。
工具执行后会在事件上写下 ``_wave_memory_hold_peace={'reason': ...}``，装饰阶段
（``app/ingress.py``）把关消息链清空，这条回复就不会发出去。对方不会看到任何内容，
事件日志里会留下一条 bookkeeping 记录（reason 原文），不影响正式好感。
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except ImportError:  # pragma: no cover
    from typing import Generic, TypeVar
    T = TypeVar("T")
    class FunctionTool(Generic[T]): pass
    class ContextWrapper(Generic[T]): pass
    class AstrAgentContext: pass

HOLD_PEACE_FLAG = "_wave_memory_hold_peace"


@dataclass
class WaveMemoryHoldPeaceTool(FunctionTool[AstrAgentContext]):
    name: str = "wave_memory_hold_peace"
    description: str = (
        "判断现在不该接这个话：不必接口、下面会引发争论、说一句会失态、或和另一个 AI 互刷时，"
        "显式调用这个工具。调用后这条回复不发送，对方看不到任何东西。"
        "问一位关切的事主动开口时，若认为现在不合适开口，也请用这个工具而不是只回沉默两个字。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "一句话说为什么这次不接（留给自己的整洁原因，比如「深夜不追问陌生人」「两人对刷即将失态」）",
            },
        },
        "required": ["reason"],
    })

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        reason = str(kwargs.get("reason") or "").strip()[:80]
        if not reason:
            return "reason 必填"
        event = getattr(ctx, "context", None) and ctx.context.event
        if event is not None:
            setattr(event, HOLD_PEACE_FLAG, {"reason": reason})
        return "好的，这次不说话。"


__all__ = ["HOLD_PEACE_FLAG", "WaveMemoryHoldPeaceTool"]
