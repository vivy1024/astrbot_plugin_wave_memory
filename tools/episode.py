"""独立群经历记录工具。"""
from __future__ import annotations
from dataclasses import field
from typing import Any
from pydantic.dataclasses import dataclass
try:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:
    from typing import Generic, TypeVar
    T = TypeVar("T")
    class FunctionTool(Generic[T]): pass
    class ContextWrapper(Generic[T]): pass
    class AstrAgentContext: pass
try:
    from ..services.experience_episodes import ExperienceEpisodeService
    from .scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message
    from ..services.identity_safety import is_identity_contamination
except ImportError:
    from services.experience_episodes import ExperienceEpisodeService
    from tools.scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message
    from services.identity_safety import is_identity_contamination

@dataclass
class WaveMemoryNoteEpisodeTool(FunctionTool[AstrAgentContext]):
    name: str = "wave_memory_note_episode"
    description: str = "记录当前群聊中独立发生的共同经历、群事件及本轮结果；不等于人情锚点，不自动提升事实或信念。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {
            "episode_type": {"type": "string", "description": "经历类型，如 shared_event、bot_reply、group_turning_point"},
            "trigger_text": {"type": "string", "description": "触发事件的客观描述"},
            "bot_action": {"type": "string", "description": "bot采取的行动"},
            "bot_reply": {"type": "string", "description": "bot本轮回复，可选"},
            "user_reaction": {"type": "string", "description": "用户或群体反应"},
            "outcome": {"type": "string", "description": "事件结果"},
            "emotional_weight": {"type": "number", "description": "重要性/情绪权重，0到10"},
            "source_memory_ids": {"type": "array", "items": {"type": "integer"}, "description": "当前Scope内的证据记忆ID"},
        }, "required": ["episode_type", "trigger_text", "outcome"]
    })
    db: Any = field(default=None, repr=False)
    writer: Any = field(default=None, repr=False)
    write_gateway: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        scope, error = require_group_runtime_scope(ctx, "episode.note")
        if error:
            return scope_error_message("群经历记录", error)
        if self.db is None or scope is None or scope.session is None:
            return "数据库或群 Scope 未初始化"
        values = {k: str(kwargs.get(k) or "").strip() for k in ("episode_type", "trigger_text", "bot_action", "bot_reply", "user_reaction", "outcome")}
        if not values["episode_type"] or not values["trigger_text"] or not values["outcome"]:
            return "episode_type、trigger_text 与 outcome 均为必填项"
        if any(is_identity_contamination(v) for v in values.values() if v):
            return "群经历记录被拒绝：检测到身份角色扮演污染"
        ids = []
        for raw in kwargs.get("source_memory_ids") or []:
            try:
                mid = int(raw)
                if mid > 0: ids.append(mid)
            except (TypeError, ValueError):
                return "source_memory_ids 必须是正整数数组"
        conn = getattr(self.db, "conn", None)
        if conn is None:
            return "数据库连接不可用"
        gateway = self.write_gateway
        coordinator = getattr(gateway, "coordinator", None) or getattr(self.writer, "coordinator", None) or getattr(self.db, "coordinator", None)
        valid = []
        for mid in ids:
            row = conn.execute("SELECT 1 FROM memories WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND resolution_state='resolved' AND COALESCE(quarantine,0)=0", (mid, scope.bot_id, scope.session.id, scope.visibility)).fetchone()
            if not row:
                return f"证据 memory:{mid} 不属于当前 Scope"
            valid.append(mid)

        # 自动溯源兜底：若模型未填入证据 ID，自动根据触发描述反查或关联本群最新消息
        if not valid:
            auto_mid = resolve_source_memory_id(self.db, scope, quote=values["trigger_text"])
            if auto_mid:
                valid.append(auto_mid)
        try:
            weight = max(0.0, min(10.0, float(kwargs.get("emotional_weight", 0) or 0)))
            if gateway is None or not callable(getattr(gateway, "record_episode", None)):
                return "群经历保存失败: episode_writer_unavailable"
            episode_id = await gateway.record_episode(
                scope=scope,
                group_id=scope.session.conversation_id,
                user_id=(scope.subject_principal_id or "").rsplit(":", 1)[-1] or None,
                episode_type=values["episode_type"],
                fields={
                    "trigger_text": values["trigger_text"],
                    "bot_action": values["bot_action"],
                    "bot_reply": values["bot_reply"],
                    "user_reaction": values["user_reaction"],
                    "outcome": values["outcome"],
                },
                source_memory_ids=valid,
                emotional_weight=weight,
                idempotency_hint=str(kwargs.get("idempotency_key") or "").strip() or None,
            )
            return f"已记录独立群经历 episode:{episode_id}；它不会自动成为社交锚点或正式认知。"
        except Exception as exc:
            return f"群经历保存失败: {exc}"

__all__ = ["WaveMemoryNoteEpisodeTool"]
