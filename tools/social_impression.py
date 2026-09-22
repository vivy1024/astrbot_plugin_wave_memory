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
        UNSETTLED_ENERGY_FULL,
        affinity_shift_range,
        clear_unsettled_state,
        consume_dimensional_energy,
        event_type_for_shift,
        load_unsettled_state,
        parse_dimensional_energy,
        persist_timeline_event,
        propose_affinity_shift,
        record_dimensional_unsettled_energy,
        strip_timeline_json,
        synthesize_milestone_phrase,
    )
    from .person_identity import display_name_for_user, resolve_user_id
    from .scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.impression_timeline import (
        UNSETTLED_ENERGY_FULL,
        affinity_shift_range,
        clear_unsettled_state,
        consume_dimensional_energy,
        event_type_for_shift,
        load_unsettled_state,
        parse_dimensional_energy,
        persist_timeline_event,
        propose_affinity_shift,
        record_dimensional_unsettled_energy,
        strip_timeline_json,
        synthesize_milestone_phrase,
    )
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message


@dataclass
class WaveMemoryRecordSocialImpressionTool(FunctionTool[AstrAgentContext]):
    """记录对特定群友的主观印象，并在系统给出的有限范围内裁决好感变动。"""

    name: str = "wave_memory_record_social_impression"
    description: str = (
        "综合记录对特定群友的阶段性主观印象，并根据真实互动同步更新好感度/关系维度。"
        "当你对群友的看法改变、或者双方对话产生信任/幽默/深度长谈/冒犯情绪变动时调用。"
        "常规微调 trust，接梗幽默用 fun，深度长谈用 depth，被严重冒犯用 hostility。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user": {
                "type": "string",
                "description": "当前交互对象的 QQ 号、群名片或常用别名",
            },
            "reason": {
                "type": "string",
                "description": "导致看法或关系产生变动的核心原因/互动事件（必填）",
            },
            "impression": {
                "type": "string",
                "description": "可选。基于当前互动的真实主观定性/看法（如：较真的技术伙伴、爱开玩笑的熟人；不填则自动基于 reason 生成）",
            },
            "dimension": {
                "type": "string",
                "enum": ["trust", "fun", "depth", "hostility", "familiarity"],
                "description": "好感变动落在哪一维；常规增减用 trust，幽默接梗用 fun，深度探讨用 depth，严重冒犯用 hostility。默认 trust",
            },
            "delta": {
                "type": "number",
                "description": "本轮好感增量，受步长约束（平时在 [-2.0, 2.0] 范围内；若未决能量蓄满跃迁可放宽至 [-5.0, 5.0]），无变动填 0",
            },
            "source_quote": {
                "type": "string",
                "description": "引起看法或好感变动的群友真实聊天原话，不填则自动回溯最近发言",
            },
            "shift_reason": {
                "type": "string",
                "description": "兼容字段，等同于 reason",
            },
            "affinity_delta": {
                "type": "number",
                "description": "兼容字段，等同于 delta",
            },
            "source_memory_id": {
                "type": "integer",
                "description": "可选。显式指定当轮对话记忆 ID",
            },
        },
        "required": ["target_user"],
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
        shift_reason = str(kwargs.get("reason") or kwargs.get("shift_reason") or "").strip()
        impression = str(kwargs.get("impression") or "").strip()
        if not impression and shift_reason:
            impression = f"阶段互动定性：{shift_reason}"
        source_quote = str(kwargs.get("source_quote") or "").strip()
        raw_mid = kwargs.get("source_memory_id")
        user_id = resolve_user_id(self.db, target, runtime_scope)
        if not user_id:
            return f"没有在当前群找到目标用户「{target}」，无法记录印象"

        # 若未提供 source_quote，自动回溯该群友最近一条记忆原话
        if not source_quote:
            mid_cand = resolve_source_memory_id(self.db, runtime_scope, sender_id=user_id)
            if mid_cand:
                conn = getattr(self.db, "conn", None) or getattr(self.db, "_conn", None)
                if conn:
                    try:
                        row = conn.execute("SELECT content FROM memories WHERE id=?", (mid_cand,)).fetchone()
                        if row and row[0]:
                            source_quote = str(row[0]).strip()[:100]
                    except Exception:
                        pass

        source_memory_id = resolve_source_memory_id(
            self.db,
            runtime_scope,
            quote=source_quote,
            explicit_id=raw_mid,
            sender_id=user_id,
        )
        dimension = str(kwargs.get("dimension") or "trust").strip().lower() or "trust"
        raw_delta = kwargs.get("delta") if kwargs.get("delta") is not None else kwargs.get("affinity_delta")
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

        if not target or (not impression and not shift_reason):
            return "target_user 以及 reason (或 impression) 均为必填项"

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
        # 跨群主体性与当前群兼容读取未决能量
        unsettled = load_unsettled_state(
            self.db,
            bot_id=runtime_scope.bot_id,
            user_id=user_id,
            group_id=group_id,
        )
        if float(unsettled.get("energy") or 0.0) <= 0:
            unsettled = load_unsettled_state(
                self.db,
                bot_id=runtime_scope.bot_id,
                user_id=user_id,
                group_id="",
            )
        energy_val = float(unsettled.get("energy") or 0.0)
        dim_energy = parse_dimensional_energy(unsettled.get("traces"))
        dim_val = max(dim_energy.get(dimension, 0.0), energy_val if energy_val >= UNSETTLED_ENERGY_FULL else 0.0)
        is_leap = dim_val >= UNSETTLED_ENERGY_FULL

        verdict = propose_affinity_shift({}, dimension=dimension, requested_delta=raw_delta, energy=dim_val, is_leap=is_leap)
        if not verdict["ok"]:
            bounds = affinity_shift_range({}, dimension=dimension, energy=dim_val, is_leap=is_leap)
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

        # detail 优先附带群友原话证据，杜绝无据悬空
        event_detail = f"{impression}\n原话证据：“{source_quote}”" if source_quote else impression

        try:
            if event_id > 0 and before_aff != after_aff:
                phrase = synthesize_milestone_phrase(
                    event_type=formal_type,
                    reason=f"{shift_reason}：{impression}",
                    before_affinity=before_aff,
                    after_affinity=after_aff,
                    dimension=verdict["dimension"],
                    delta=applied_delta,
                    is_leap=is_leap,
                )
                persist_timeline_event(
                    self.db,
                    bot_id=runtime_scope.bot_id,
                    user_id=user_id,
                    group_id=group_id,
                    kind="affinity",
                    summary=phrase,
                    detail=event_detail,
                    occurred_at=now,
                    provenance={
                        "actor": "social_verdict",
                        "event_type": formal_type,
                        "reason": shift_reason,
                        "source_quote": source_quote,
                        "source_memory_id": source_memory_id,
                        "dimension": verdict["dimension"],
                        "delta": applied_delta,
                        "before_affinity": before_aff,
                        "after_affinity": after_aff,
                        "event_id": event_id,
                        "is_leap": is_leap,
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
                    detail=event_detail,
                    occurred_at=now,
                    provenance={
                        "actor": "social_verdict",
                        "reason": shift_reason,
                        "source_quote": source_quote,
                        "source_memory_id": source_memory_id,
                        "dimension": verdict["dimension"],
                        "delta": requested,
                    },
                )

            # 能量结算与定向流转：
            # 若触发了跃迁或达到结算线，清空该维度与本群的未决能量；
            # 若未发生跃迁，则将本轮评估产生的微观冲击持续累加至该维度的能量池中。
            if is_leap or energy_val >= UNSETTLED_ENERGY_FULL:
                consume_dimensional_energy(
                    self.db,
                    bot_id=runtime_scope.bot_id,
                    user_id=user_id,
                    dimension=dimension,
                )
                clear_unsettled_state(
                    self.db,
                    bot_id=runtime_scope.bot_id,
                    user_id=user_id,
                    group_id=group_id,
                )
            else:
                record_dimensional_unsettled_energy(
                    self.db,
                    bot_id=runtime_scope.bot_id,
                    user_id=user_id,
                    group_id=group_id,
                    dimension=dimension,
                    text=impression,
                    impact=1.5,
                    now=now,
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
