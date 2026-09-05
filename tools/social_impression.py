"""WaveMemory social impression tool — records LLM-driven interpersonal mindset and relationship verdicts."""

from __future__ import annotations

import json
import time
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

try:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover
    from typing import Generic, TypeVar
    _T = TypeVar("_T")
    class FunctionTool(Generic[_T]): pass
    class ContextWrapper(Generic[_T]): pass
    class AstrAgentContext: pass

try:
    from ..domain.scope import RuntimeScope
    from ..services.impression_timeline import append_impression, append_ledger_entry, record_affinity_milestone
    from .person_identity import display_name_for_user, resolve_user_id
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.impression_timeline import append_impression, append_ledger_entry, record_affinity_milestone
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


_SHIFT_DIMENSION_MAP = {
    "subtle_increase": ("trust", 1.5, "direct_reply"),
    "subtle_decrease": ("trust", -1.5, "direct_reply"),
    "breakthrough": ("trust", 4.0, "deep_talk"),
    "boundary_breach": ("hostility", 6.0, "ignored_boundary"),
}


@dataclass
class WaveMemoryRecordSocialImpressionTool(FunctionTool[AstrAgentContext]):
    """记录对特定群友的主观印象定性，并在有依据时裁决关系好感变动。"""

    name: str = "wave_memory_record_social_impression"
    description: str = (
        "当你对当前对话群友的看法产生实质改变、或双方互动值得留下一笔印象时调用。"
        "不要在平淡无奇的日常闲聊中频繁调用。必须提供 target_user, impression, shift_reason, shift_level。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user": {
                "type": "string",
                "description": "当前交互对象的 QQ 号、名字或别名",
            },
            "impression": {
                "type": "string",
                "description": "基于当前语境与立场，你对该用户的真实主观印象（1-2句话，有依据）",
            },
            "shift_reason": {
                "type": "string",
                "description": "导致你产生这层看法或关系变化的核心原因（如：熟练使用群黑话接梗、真诚探讨现实困境等）",
            },
            "shift_level": {
                "type": "string",
                "enum": ["steady", "subtle_increase", "subtle_decrease", "breakthrough", "boundary_breach"],
                "description": "关系变动定性：steady(保持稳定)/subtle_increase(轻微增进)/subtle_decrease(轻微降温)/breakthrough(重大突破)/boundary_breach(触碰底线)",
            },
        },
        "required": ["target_user", "impression", "shift_reason", "shift_level"],
    })

    db: Any = field(default=None, repr=False)
    relationship_events: Any = field(default=None, repr=False)
    bot_db_ids: dict[str, str] = field(default_factory=dict, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db:
            return "数据库未初始化"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        runtime_scope, error_code = require_group_runtime_scope(ctx, "social_impression.record")
        if error_code:
            return scope_error_message("印象记录", error_code)
        assert runtime_scope is not None

        target = str(kwargs.get("target_user") or "").strip()
        impression = str(kwargs.get("impression") or "").strip()
        shift_reason = str(kwargs.get("shift_reason") or "").strip()
        shift_level = str(kwargs.get("shift_level") or "steady").strip().lower()

        if not target or not impression or not shift_reason:
            return "target_user、impression 与 shift_reason 均为必填项"

        user_id = resolve_user_id(self.db, target, runtime_scope)
        if not user_id:
            return f"没有在当前群找到目标用户「{target}」，无法记录印象"

        target_scope = RuntimeScope(
            bot_id=runtime_scope.bot_id,
            visibility="group",
            session=runtime_scope.session,
            subject_principal_id=f"{runtime_scope.session.platform_id}:user:{user_id}",
        )

        now = time.time()
        applied_delta = 0.0
        dimension_text = "心智定性"
        event_id = 0
        before_aff = 0
        after_aff = 0

        # 如果有实质好感变化定性，调用关系服务记录事件
        if shift_level in _SHIFT_DIMENSION_MAP and self.relationship_events is not None:
            dim_name, delta_val, formal_type = _SHIFT_DIMENSION_MAP[shift_level]
            try:
                result = self.relationship_events.record_event(
                    scope=target_scope,
                    event_type=formal_type,
                    dimension=dim_name,
                    delta=delta_val,
                    reason=shift_reason,
                    created_at=now,
                )
                if result:
                    event_id = int(getattr(result, "event_id", 0) or 0)
                    applied_delta = float(getattr(result, "applied_delta", 0.0) or 0.0)
                    before_aff = int(getattr(result, "before_affection", 0) or 0)
                    after_aff = int(getattr(result, "after_affection", 0) or 0)
                    dimension_text = f"{dim_name} {applied_delta:+g}"
            except Exception as e:
                return f"关系事件记录失败: {e}"

        # 写入 user_profiles metadata 中的 impression 与 impression_ledger
        try:
            row = self.db.conn.execute(
                "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                (user_id, runtime_scope.session.conversation_id, runtime_scope.bot_id),
            ).fetchone()
            metadata: dict[str, Any] = {}
            if row and row[0]:
                try:
                    loaded = json.loads(row[0])
                    if isinstance(loaded, dict):
                        metadata = loaded
                except Exception:
                    metadata = {}

            # 更新当前印象与时间线
            if event_id > 0 and before_aff != after_aff:
                metadata = record_affinity_milestone(
                    metadata,
                    event_type=_SHIFT_DIMENSION_MAP.get(shift_level, ("direct_reply",))[2],
                    reason=f"{shift_reason}：{impression}",
                    before_affinity=before_aff,
                    after_affinity=after_aff,
                    dimension=_SHIFT_DIMENSION_MAP.get(shift_level, ("trust",))[0],
                    delta=applied_delta,
                    now=now,
                    event_id=event_id,
                )
            else:
                metadata = append_impression(
                    metadata,
                    impression,
                    now=now,
                    actor="social_verdict",
                    event={"reason": shift_reason, "shift_level": shift_level},
                )

            if event_id > 0 and shift_level in _SHIFT_DIMENSION_MAP:
                dim_name, _, formal_type = _SHIFT_DIMENSION_MAP[shift_level]
                metadata = append_ledger_entry(
                    metadata,
                    event_type=formal_type,
                    dimension=dim_name,
                    delta=applied_delta,
                    reason=shift_reason,
                    at=now,
                    event_id=event_id,
                )

            self.db.conn.execute(
                """INSERT INTO user_profiles (user_id, group_id, bot_id, metadata, interaction_count, last_seen)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(user_id, group_id, bot_id) DO UPDATE SET
                   metadata=excluded.metadata, last_seen=excluded.last_seen""",
                (user_id, runtime_scope.session.conversation_id, runtime_scope.bot_id, json.dumps(metadata, ensure_ascii=False), now),
            )
            self.db.conn.commit()
        except Exception as e:
            return f"印象持久化失败: {e}"

        display = display_name_for_user(self.db, user_id, runtime_scope) or target
        status_msg = f"已记录对「{display}」的印象：{impression}"
        if applied_delta != 0.0:
            status_msg += f"\n好感度变动（{dimension_text}）：{before_aff} → {after_aff}（{shift_reason}）"
        return status_msg
