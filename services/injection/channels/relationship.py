"""正式 Scoped Relationship/affinity 注入通道。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from .safety import is_channel_allowed_in_mode

try:
    from ....domain.scope import RuntimeScope, is_visible_scene, scene_key
    from ....engine.db.person_identity_repo import linked_principals_via, local_user_id
except ImportError:  # pragma: no cover - focused repository tests
    from domain.scope import RuntimeScope, is_visible_scene, scene_key
    from engine.db.person_identity_repo import linked_principals_via, local_user_id


# 共同交往锚点：最多几条、每条截多少字（与印象时间线单条摘要同长）
SHARED_ANCHOR_LIMIT = 3
SHARED_ANCHOR_CHARS = 90


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("affinity", {}))


class RelationshipChannel:
    """Read only current Bot + group + sender relationship state."""

    name = "affinity"

    def __init__(self, *, repository: Any = None, db: Any = None):
        self.repository = repository
        self.db = db

    async def _run_sync(self, func, *args, finish_on_cancel: bool = False, **kwargs):
        """正式连接可跨线程；旧直连测试/外部调用保持线程绑定。"""
        cm = getattr(self.repository, "cm", None) or getattr(self.db, "_cm", None)
        if cm is None:
            cm = getattr(self.db, "conn", None)
        if not hasattr(cm, "write_transaction"):
            return func(*args, **kwargs)
        task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
        if not finish_on_cancel:
            return await task
        # 旧 metadata 迁移必须完整退出事务；取消请求后不留下后台写任务。
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    @staticmethod
    def _profile_metadata(db, conn, scope, sender_id, group_id) -> Mapping[str, Any]:
        if conn is None or not sender_id or not group_id:
            return {}
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_profiles'").fetchone():
            return {}
        params = (sender_id, group_id, scope.bot_id)
        sql = "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?"
        row = conn.execute(sql, params).fetchone()
        metadata = json.loads(row[0]) if row and row[0] else {}
        if not isinstance(metadata, Mapping):
            raise ValueError("invalid_profile_metadata")
        from ...impression_timeline import _TIMELINE_JSON_KEYS, migrate_and_strip_profile_metadata

        if db is None or not any(key in metadata for key in _TIMELINE_JSON_KEYS):
            return metadata
        if getattr(db, "person_timeline", None) is None:
            return metadata
        transaction = getattr(conn, "write_transaction", None)
        if not callable(transaction):
            # 没有正式事务边界时只读 legacy 摘要，不执行半迁移。
            return metadata
        with transaction() as writer:
            # 在写锁内重新取值，避免并发请求把同一 legacy 历史重复迁入。
            row = writer.execute(sql, params).fetchone()
            current = json.loads(row[0]) if row and row[0] else {}
            if not isinstance(current, Mapping):
                raise ValueError("invalid_profile_metadata")
            cleaned = migrate_and_strip_profile_metadata(
                db, bot_id=scope.bot_id, user_id=sender_id, group_id=group_id,
                metadata=current, connection=writer,
            )
            if cleaned != current:
                writer.execute(
                    "UPDATE user_profiles SET metadata=? WHERE user_id=? AND group_id=? AND bot_id=?",
                    (json.dumps(cleaned, ensure_ascii=False), *params),
                )
            return cleaned

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"affinity channel disabled in {mode} mode")
        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="affinity channel disabled by config")
        scope = getattr(ctx, "scope", None)
        if (
            not isinstance(scope, RuntimeScope)
            or scope.visibility not in {"group", "private"}
            or scope.session is None
        ):
            return InjectionResult.empty(self.name, reason="runtime_scope_required")
        if not scope.subject_principal_id or self.repository is None:
            return InjectionResult.empty(self.name, reason="relationship_subject_or_repository_unavailable")
        # 以印象时间线为主：印象跨群合并（含管理员确认的关联账号），具体内容（印象、观感、
        # 关系线索）群里的处处可见，私聊的只在该私聊可见。好感分数只作印象之后的一行次要参考，
        # 同样按场合汇总：群聊的行处处计入，私聊的行只在同一私聊计入。
        scene = scene_key(scope)
        try:
            state = await self._run_sync(
                self.repository.get_state, scope,
                subject_principal_id=scope.subject_principal_id, limit=25, offset=0,
            )
            relationship = _mapping(_mapping(state).get("relationship"))
            dimensions = _mapping(relationship.get("dimensions"))
            values = _mapping(relationship.get("values"))
            history = list(_mapping(_mapping(state).get("relationship_history")).get("items") or [])
            impression_meta: Mapping[str, Any] = {}
            timeline_events: list[Any] = []
            sender_id = str(getattr(ctx, "sender_id", "") or "").strip()
            group_id = str(scope.session.conversation_id or "").strip()
            if sender_id and scope.subject_principal_id != f"{scope.session.platform_id}:user:{sender_id}":
                return InjectionResult.empty(self.name, reason="relationship_subject_mismatch")
            if getattr(ctx, "group_id", None) and str(ctx.group_id) != group_id:
                return InjectionResult.empty(self.name, reason="relationship_session_mismatch")
            db = self.db or getattr(self.repository, "db", None) or getattr(self.repository, "_db", None)
            timeline = getattr(db, "person_timeline", None) if db is not None else None
            if timeline is None:
                timeline = getattr(self.repository, "person_timeline", None)
            cm = getattr(self.repository, "cm", None)
            if cm is None and db is not None:
                cm = getattr(db, "conn", None)
            conn = getattr(db, "conn", None) if db else None
            if conn is None and cm is not None:
                conn = getattr(cm, "conn", None) or cm
            if db is None and timeline is not None:
                db = type("_TimelineFacade", (), {
                    "person_timeline": timeline,
                    "conn": conn,
                })()
            impression_meta = await self._run_sync(
                self._profile_metadata, db, conn, scope, sender_id, scene,
                finish_on_cancel=True,
            )
            try:
                from ...impression_timeline import (
                    affinity_shift_range,
                    effective_unsettled_state,
                    impression_prompting,
                    injection_lines,
                    load_timeline_events,
                    meaningful_event_anchor,
                    should_trigger_affinity_transition,
                    unsettled_energy_line,
                )
                from ...relationship_evidence_display import relationship_injection_summary_snippet
                from ...relationship_narrative import affinity_score_line, shared_experience_lines
            except ImportError:  # pragma: no cover
                from services.impression_timeline import (
                    affinity_shift_range,
                    effective_unsettled_state,
                    impression_prompting,
                    injection_lines,
                    load_timeline_events,
                    meaningful_event_anchor,
                    should_trigger_affinity_transition,
                    unsettled_energy_line,
                )
                from services.relationship_evidence_display import (
                    relationship_injection_summary_snippet,
                )
                from services.relationship_narrative import affinity_score_line, shared_experience_lines
            if db is not None and sender_id and group_id:
                timeline_events = await self._run_sync(
                    load_timeline_events, db,
                    bot_id=scope.bot_id,
                    user_id=sender_id,
                    query="",
                    limit=None,
                    connection=conn,
                    strict=True,
                    viewer_scene=scene,
                    user_ids=self._linked_user_ids(conn, scope, sender_id),
                )
            unsettled = {"energy": 0.0, "traces": []}
            if db is not None and sender_id:
                # 与结算工具读同一份：本场合行 + 跨群行合并、按时间衰减并封顶
                unsettled = await self._run_sync(
                    effective_unsettled_state,
                    db,
                    bot_id=scope.bot_id,
                    user_id=sender_id,
                    scene=scene,
                    connection=conn,
                )
            context_config = _mapping(getattr(ctx, "config", {}))
            half_life_days = context_config.get(
                "timeline_decay_half_life_days",
                _mapping(context_config.get("Inject_Settings")).get("timeline_decay_half_life_days"),
            )
            energy = float(unsettled.get("energy") or 0.0)
            # 1. 印象：时间线摘要（按半衰期挑选、最多 8 条、按时间排序）+ 最近一条印象。
            #    关系由模型读印象时间线自己判断；分数只在后面附一行次要参考，不带态度或档位。
            rendered_lines = await asyncio.to_thread(
                injection_lines, impression_meta,
                history=history,
                now=float(getattr(ctx, "now", 0.0) or time.time()),
                events=timeline_events,
                half_life_days=half_life_days,
            )
            parts: list[str] = []
            filtered_lines: list[str] = []

            def add(line: str) -> None:
                if not line:
                    return
                (filtered_lines if is_identity_contamination(line) else parts).append(line)

            for line in rendered_lines:
                add(line)
            # 2. 共同交往锚点：只取与这个人相关的条目（主体或结构化参与者），不再塞 Bot 自己的群日记
            soul_items = list(_mapping(_mapping(state).get("timeline")).get("items") or [])
            anchors = shared_experience_lines(
                soul_items, subject_principal_id=scope.subject_principal_id,
                limit=SHARED_ANCHOR_LIMIT, snippet_chars=SHARED_ANCHOR_CHARS,
            )
            # 好感分数：当前场合可见的汇总（跨群合并、含关联账号；私聊只计同一私聊），
            # 汇总不可用时退回本场合的关系行
            trigger_dims: dict[str, Any] = {}
            for name in ("familiarity", "trust", "fun", "depth", "hostility"):
                item = _mapping(values.get(name))
                value = item.get("effective_value", dimensions.get(name))
                if value is not None:
                    trigger_dims[name] = value
            totals = await self._run_sync(self._relationship_totals, scope)
            if totals:
                score_affinity = totals.get("merged_affinity")
                score_dims: Mapping[str, Any] = _mapping(totals.get("merged_dimensions"))
            else:
                score_affinity = relationship.get("affinity")
                score_dims = trigger_dims or dimensions
            has_scores = score_affinity is not None or bool(score_dims)
            if not has_scores and not parts and not anchors:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="relationship_unknown")
            visible_traces = [
                trace for trace in list(unsettled.get("traces") or [])
                if not isinstance(trace, Mapping) or is_visible_scene(trace.get("group_id"), scene)
            ]
            # 每轮观感标签：默认交给事后记账，不在每轮回复里要求；开启时恢复旧行为
            prompting = impression_prompting()
            per_turn_tag = _as_bool(cfg.get("per_turn_impression_tag"), prompting["per_turn_tag"])
            if per_turn_tag:
                add(unsettled_energy_line(impression_meta, energy=energy, traces=visible_traces))
            if not any(line.startswith("最近关系线索：") or line.startswith("印象时间线") for line in parts):
                anchor = meaningful_event_anchor(history)
                reason = str((anchor or {}).get("reason") or "").strip()
                event_type = str((anchor or {}).get("event_type") or "").strip()
                if anchor and reason:
                    add(f"最近关系线索：{event_type}：{reason}")
            evidence_snip = relationship_injection_summary_snippet(
                relationship.get("evidence"), max_chars=80
            )
            if evidence_snip:
                add(f"历史关系摘要（只读）：{evidence_snip}")
            for summary in anchors:
                add(f"共同交往锚点：{summary}")
            # 2.5 好感分数：印象之后一行紧凑的次要参考；只有分数没有印象的人也注入这一行
            add(affinity_score_line(score_affinity, score_dims, has_impression=bool(parts)))
            # 3. 结算提示：未结算能量已满或关系可能质变时
            transition_hint = ""
            if should_trigger_affinity_transition(impression_meta, trigger_dims or dimensions, energy=energy):
                bounds = affinity_shift_range(impression_meta, dimension="trust", energy=energy)
                if per_turn_tag:
                    transition_hint = (
                        "【该结算了】未结算能量已满或关系可能质变。"
                        f"若做阶段性定性，调用 wave_memory_record_social_impression，"
                        f"本轮增量须在 [{bounds['min']}, {bounds['max']}]，无变动填 0。"
                        "日常观感继续写 <<impression:当下观感 | impact:1-5>>，不要覆盖这句结算。"
                    )
                elif not (prompting["bookkeeping_active"] and scope.visibility == "group"):
                    # 事后记账只覆盖群聊；私聊或记账未启动时仍需在回复里结算
                    transition_hint = (
                        "【该结算了】对对方攒了不少没定性的观感。若要做阶段性定性，"
                        "调用 wave_memory_record_social_impression，"
                        f"增量须在 [{bounds['min']}, {bounds['max']}]，无变动填 0。"
                    )
                # 群聊且事后记账已启动：记账回看时按未结算能量列出「需要结算的人」，这里不提示
            # 结算提示固定追加在末尾，保证裁决节点指令完整。
            if transition_hint:
                parts.append(transition_hint)
            if not parts:
                # 认识这个人，但既没有印象、线索，也没有可用的分数：不注入空段落
                return InjectionResult.empty(
                    self.name, latency_ms=self._latency_ms(started), reason="relationship_no_impression",
                )
            text = "\n".join(parts)
            revision = relationship.get("revision") or _mapping(state).get("revision") or 0
            return InjectionResult.hit(
                self.name,
                text,
                items=[{
                    "source": "scoped_soul_relationship",
                    "subject_principal_id": scope.subject_principal_id,
                    "revision": revision,
                    "preview": text[:180],
                    "rendered_text": text,
                    "dedupe_key": f"relationship:{scope.bot_id}:{scope.session.id}:{scope.subject_principal_id}:{revision}",
                }],
                latency_ms=self._latency_ms(started),
                # 时间线已按条数与长度收敛（约 1–1.5 千字），不被预算截断，保证末尾的结算提示完整；
                # 必保留文本在编排器里独立计量，不挤占其他通道的预算。
                preserve_full_text=True,
                filtered=[{"filter_reason": "identity_contamination", "filter_channel": self.name} for _ in filtered_lines],
            )
        except Exception as exc:
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _linked_user_ids(conn: Any, scope: RuntimeScope, sender_id: str) -> list[str]:
        """同一个人在其他平台账号的本地 ID（管理员确认的关联）；无关联时只含自身。"""
        if conn is None or not scope.subject_principal_id:
            return [sender_id]
        linked = linked_principals_via(conn, scope.bot_id, scope.subject_principal_id)
        return list(dict.fromkeys([sender_id, *(local_user_id(item) for item in linked if local_user_id(item))]))

    def _relationship_totals(self, scope: RuntimeScope) -> dict[str, Any]:
        """当前场合可见的关系分数汇总；仓储不支持或无记录时返回空 dict（退回本场合的关系行）。"""
        summarizer = getattr(self.repository, "summarize_cross_group_relationship", None)
        if not callable(summarizer) or not scope.subject_principal_id or scope.session is None:
            return {}
        try:
            data = summarizer(
                scope, subject_principal_id=scope.subject_principal_id, viewer_session_id=scope.session.id,
            )
        except Exception:
            # 不支持按场合过滤的旧仓储：宁可只用本场合的行，也不混入其他私聊
            return {}
        if not isinstance(data, Mapping) or not data.get("available"):
            return {}
        return dict(data)

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["RelationshipChannel"]
