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
    from ..services.impression_timeline import (
        affinity_shift_range,
        clear_unsettled_state,
        event_type_for_shift,
        load_unsettled_state,
        persist_timeline_event,
        propose_affinity_shift,
        strip_timeline_json,
        synthesize_milestone_phrase,
    )
    from .person_identity import display_name_for_user, resolve_user_id
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.impression_timeline import (
        affinity_shift_range,
        clear_unsettled_state,
        event_type_for_shift,
        load_unsettled_state,
        persist_timeline_event,
        propose_affinity_shift,
        strip_timeline_json,
        synthesize_milestone_phrase,
    )
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


@dataclass
class WaveMemoryRecordSocialImpressionTool(FunctionTool[AstrAgentContext]):
    """记录对特定群友的主观印象，并在系统给出的有限范围内裁决好感变动。"""

    name: str = "wave_memory_record_social_impression"
    description: str = (
        "当你对当前对话群友的看法产生实质改变、或双方互动值得留下一笔印象时调用。"
        "好感变动必须落在系统给出的本轮范围内，不能一次大跳。"
        "必须提供 target_user, impression, shift_reason；无变动时 affinity_delta 填 0。"
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
                "description": "导致你产生这层看法或关系变化的核心原因",
            },
            "affinity_delta": {
                "type": "number",
                "description": "本轮好感增量，必须落在注入给出的范围内；无变动填 0",
            },
            "dimension": {
                "type": "string",
                "enum": ["trust", "fun", "depth", "hostility", "familiarity"],
                "description": "变动落在哪一维；默认 trust。敌意升高才用 hostility",
            },
        },
        "required": ["target_user", "impression", "shift_reason"],
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
        dimension = str(kwargs.get("dimension") or "trust").strip().lower() or "trust"
        raw_delta = kwargs.get("affinity_delta")
        if raw_delta in {None, ""}:
            # 兼容旧测试/旧调用：shift_level 映射成范围内的小步，不再允许 breakthrough 大跳。
            shift_level = str(kwargs.get("shift_level") or "").strip().lower()
            legacy = {
                "steady": 0.0,
                "subtle_increase": 1.5,
                "subtle_decrease": -1.5,
                "breakthrough": 2.0,
                "boundary_breach": 3.0,
            }
            if shift_level == "boundary_breach":
                dimension = "hostility"
            raw_delta = legacy.get(shift_level, 0.0)

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
        formal_type = "direct_reply"

        group_id = runtime_scope.session.conversation_id
        unsettled = load_unsettled_state(
            self.db,
            bot_id=runtime_scope.bot_id,
            user_id=user_id,
            group_id=group_id,
        )
        energy = float(unsettled.get("energy") or 0.0)

        verdict = propose_affinity_shift({}, dimension=dimension, requested_delta=raw_delta, energy=energy)
        if not verdict["ok"]:
            bounds = affinity_shift_range({}, dimension=dimension, energy=energy)
            if verdict.get("error") == "affinity_delta_out_of_range":
                return (
                    f"好感变动被拒绝：{verdict.get('requested')} 超出本轮范围 "
                    f"[{bounds['min']}, {bounds['max']}]（维度 {bounds['dimension']}）。不能一次大跳，请在范围内重提。"
                )
            return "好感增量无效，请提交范围内的数字，无变动填 0"

        requested = float(verdict["delta"])
        if requested != 0.0 and self.relationship_events is not None:
            formal_type = event_type_for_shift(verdict["dimension"], requested)
            try:
                result = self.relationship_events.record_event(
                    scope=target_scope,
                    event_type=formal_type,
                    dimension=verdict["dimension"],
                    delta=requested,
                    reason=shift_reason,
                    created_at=now,
                )
                if result:
                    event_id = int(getattr(result, "event_id", 0) or 0)
                    applied_delta = float(getattr(result, "applied_delta", 0.0) or 0.0)
                    before_aff = int(getattr(result, "before_affection", 0) or 0)
                    after_aff = int(getattr(result, "after_affection", 0) or 0)
                    dimension_text = f"{verdict['dimension']} {applied_delta:+g}"
            except Exception as e:
                return f"关系事件记录失败: {e}"

        try:
            if event_id > 0 and before_aff != after_aff:
                phrase = synthesize_milestone_phrase(
                    event_type=formal_type,
                    reason=f"{shift_reason}：{impression}",
                    before_affinity=before_aff,
                    after_affinity=after_aff,
                    dimension=verdict["dimension"],
                    delta=applied_delta,
                )
                persist_timeline_event(
                    self.db,
                    bot_id=runtime_scope.bot_id,
                    user_id=user_id,
                    group_id=group_id,
                    kind="affinity",
                    summary=phrase,
                    detail=impression,
                    occurred_at=now,
                    provenance={
                        "actor": "social_verdict",
                        "event_type": formal_type,
                        "reason": shift_reason,
                        "dimension": verdict["dimension"],
                        "delta": applied_delta,
                        "before_affinity": before_aff,
                        "after_affinity": after_aff,
                        "event_id": event_id,
                    },
                )
            else:
                persist_timeline_event(
                    self.db,
                    bot_id=runtime_scope.bot_id,
                    user_id=user_id,
                    group_id=group_id,
                    kind="impression",
                    summary=impression,
                    detail=impression,
                    occurred_at=now,
                    provenance={
                        "actor": "social_verdict",
                        "reason": shift_reason,
                        "dimension": verdict["dimension"],
                        "delta": requested,
                    },
                )

            clear_unsettled_state(
                self.db,
                bot_id=runtime_scope.bot_id,
                user_id=user_id,
                group_id=group_id,
            )
            row = None
            try:
                row = self.db.conn.execute(
                    "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                    (user_id, group_id, runtime_scope.bot_id),
                ).fetchone()
            except Exception:
                row = None
            leftover: dict[str, Any] = {}
            if row and row[0]:
                try:
                    loaded = json.loads(row[0])
                    if isinstance(loaded, dict):
                        leftover = strip_timeline_json(loaded)
                except Exception:
                    leftover = {}
            self.db.conn.execute(
                """INSERT INTO user_profiles (user_id, group_id, bot_id, metadata, interaction_count, last_seen)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(user_id, group_id, bot_id) DO UPDATE SET
                   metadata=excluded.metadata,
                   last_seen=excluded.last_seen""",
                (user_id, group_id, runtime_scope.bot_id, json.dumps(leftover, ensure_ascii=False), now),
            )
            self.db.conn.commit()
        except Exception as e:
            return f"印象持久化失败: {e}"

        display = display_name_for_user(self.db, user_id, runtime_scope) or target
        status_msg = f"已记录对「{display}」的印象：{impression}"
        if applied_delta != 0.0:
            status_msg += f"\n好感度变动（{dimension_text}）：{before_aff} → {after_aff}（{shift_reason}）"
        return status_msg
