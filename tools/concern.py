"""独立灵魂关切工具。

只负责把模型的现场判断送进正式写入链；状态合法性由领域状态机与命令 handler
把关，工具自身不改状态、不写库。
"""
from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

try:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover
    from typing import Generic, TypeVar

    T = TypeVar("T")

    class FunctionTool(Generic[T]):
        pass

    class ContextWrapper(Generic[T]):
        pass

    class AstrAgentContext:
        pass

try:
    from ..services.identity_safety import is_identity_contamination
    from .scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message
except ImportError:  # pragma: no cover
    from services.identity_safety import is_identity_contamination
    from tools.scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message

# 模型可以做的动作。expire/archive 属于系统整理与人工裁决，不开放给现场工具。
_MODEL_ACTIONS = ("note", "progress", "resolve", "reopen")

_ACTION_MESSAGES = {
    "created": "已记录未决关切：{topic}。它不会自动成为社交锚点或正式认知。",
    "reinforced": "已强化未决关切 concern:{id}，仍是当前心事。",
    "note_ignored_closed": "关切 concern:{id} 当前为已结案状态，不会因再次提及自动复活；确需重新挂心请用 reopen。",
    "progress": "已记下关切 concern:{id} 有新进展。",
    "resolve": "关切 concern:{id} 已结案，之后不再作为心事注入。",
    "reopen": "已重新挂心关切 concern:{id}。",
}


@dataclass
class WaveMemoryNoteConcernTool(FunctionTool[AstrAgentContext]):
    name: str = "wave_memory_note_concern"
    description: str = (
        "记录或推进当前群聊中尚未结案的灵魂关切：note=记下新挂念，progress=得知有新进展，"
        "resolve=确认已解决并结案（必须给 note 说明），reopen=重新挂心。"
        "它是心事，不是人情锚点，也不会自动成为事实或信念。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_MODEL_ACTIONS),
                "description": "对关切执行的动作，默认 note",
            },
            "topic": {"type": "string", "description": "未决挂念的一句话主题；note 时必填"},
            "concern_id": {"type": "integer", "description": "已有关切 id，用于 progress/resolve/reopen"},
            "intensity": {"type": "number", "description": "关心强度，0 到 1"},
            "concern_type": {"type": "string", "description": "可选类型，如 follow_up / wellbeing / unfinished_task"},
            "note": {"type": "string", "description": "结案或进展说明；resolve 时必填"},
        },
        "required": ["action"],
    })
    db: Any = field(default=None, repr=False)
    concern_tracker: Any = field(default=None, repr=False)
    write_gateway: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        scope, error = require_group_runtime_scope(ctx, "concern.note")
        if error:
            return scope_error_message("灵魂关切", error)

        action = str(kwargs.get("action") or "note").strip().lower()
        if action not in _MODEL_ACTIONS:
            return f"不支持的关切动作：{action}；可选 {' / '.join(_MODEL_ACTIONS)}"
        topic = str(kwargs.get("topic") or "").strip()
        if action == "note" and not topic:
            return "note 动作必须提供 topic"
        if topic and is_identity_contamination(topic):
            return "关切记录被拒绝：检测到身份角色扮演污染"
        note = str(kwargs.get("note") or "").strip()
        if action == "resolve" and not note:
            return "resolve 必须提供 note 说明这件事如何结案"

        concern_id: int | None = None
        raw_id = kwargs.get("concern_id")
        if raw_id is not None and str(raw_id).strip() != "":
            try:
                concern_id = int(raw_id)
            except (TypeError, ValueError):
                return "concern_id 必须是正整数"
            if concern_id <= 0:
                return "concern_id 必须是正整数"
        if action != "note" and concern_id is None and not topic:
            return f"{action} 需要提供 concern_id 或 topic 以定位已有关切"

        gateway = self.write_gateway or getattr(self.db, "write_gateway", None)
        if gateway is None or not callable(getattr(gateway, "transition_concern", None)):
            return "灵魂关切写入未就绪：缺少正式写入网关"

        intensity = kwargs.get("intensity") if action == "note" else None
        auto_mid = resolve_source_memory_id(self.db, scope, quote=topic or note)
        evidence = [{"kind": "concern_tool", "action": action}]
        if auto_mid:
            evidence.append({"kind": "memory", "id": auto_mid})
        if topic:
            evidence.append({"kind": "concern_topic", "topic": topic[:80]})
        try:
            result = await gateway.transition_concern(
                scope=scope,
                action=action,
                concern_id=concern_id,
                topic=topic or None,
                note=note,
                intensity=None if intensity is None else float(intensity),
                concern_type=str(kwargs.get("concern_type") or "").strip(),
                evidence=evidence,
                idempotency_hint=str(kwargs.get("idempotency_key") or "").strip() or None,
            )
        except Exception as exc:
            return f"关切更新失败：{exc}"

        tracker = self.concern_tracker
        if tracker is not None and hasattr(tracker, "invalidate"):
            try:
                tracker.invalidate(scope)
            except Exception:  # 缓存失效失败不影响已提交的正式写入
                pass

        change = str(result.get("action") or action)
        template = _ACTION_MESSAGES.get(change, "已更新关切 concern:{id}。")
        return template.format(id=result.get("concern_id"), topic=topic)


__all__ = ["WaveMemoryNoteConcernTool"]
