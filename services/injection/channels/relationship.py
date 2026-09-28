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
        # 对一个人的态度不分场合：数值来自群聊与私聊的汇总；
        # 具体内容（印象、观感、关系线索）群里的处处可见，私聊的只在该私聊可见。
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
                from ....domain.relationship_policy import classify_relationship_tier
                from ...impression_timeline import (
                    affinity_shift_range,
                    effective_unsettled_state,
                    impression_prompting,
                    legacy_metadata_items,
                    load_timeline_events,
                    should_trigger_affinity_transition,
                    unsettled_energy_line,
                )
                from ...relationship_narrative import (
                    compress_impressions,
                    current_feeling_line,
                    history_events,
                    impression_lines,
                    relationship_sentence,
                    shared_experience_lines,
                )
            except ImportError:  # pragma: no cover
                from domain.relationship_policy import classify_relationship_tier
                from services.impression_timeline import (
                    affinity_shift_range,
                    effective_unsettled_state,
                    impression_prompting,
                    legacy_metadata_items,
                    load_timeline_events,
                    should_trigger_affinity_transition,
                    unsettled_energy_line,
                )
                from services.relationship_narrative import (
                    compress_impressions,
                    current_feeling_line,
                    history_events,
                    impression_lines,
                    relationship_sentence,
                    shared_experience_lines,
                )

            def events_or_legacy(events: list[Any], metadata: Mapping[str, Any]) -> list[Any]:
                return list(events) if events else legacy_metadata_items(metadata)

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
            now = float(getattr(ctx, "now", 0.0) or time.time())
            energy = float(unsettled.get("energy") or 0.0)
            visible_traces = [
                trace for trace in list(unsettled.get("traces") or [])
                if not isinstance(trace, Mapping) or is_visible_scene(trace.get("group_id"), scene)
            ]
            # 好感度不分群：汇总该用户在所有群的积累，作为对该用户的统一态度。
            # 记忆与印象时间线本就跨群，关系再按群拆分会让 bot 对不同群里的同一个人
            # 有 N 份互相矛盾的态度，不符合真人对人的认知方式。
            aggregate: Mapping[str, Any] = self._relationship_totals(scope, sender_id) if sender_id else {}
            merged_dimensions = _mapping(aggregate.get("merged_dimensions")) if aggregate else {}
            effective_dims: dict[str, Any] = {}
            for name in ("familiarity", "trust", "fun", "depth", "hostility"):
                item = _mapping(values.get(name))
                value = item.get("effective_value", dimensions.get(name))
                if value is not None:
                    effective_dims[name] = value
            status_dims = dict(merged_dimensions) if merged_dimensions else effective_dims
            timeline_items = [
                item for item in events_or_legacy(timeline_events, impression_meta)
                if isinstance(item, Mapping)
            ]
            if (
                relationship.get("affinity") is None
                and not timeline_items
                and not merged_dimensions
            ):
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="relationship_unknown")

            parts: list[str] = []
            filtered_lines: list[str] = []

            def add(line: str) -> None:
                if not line:
                    return
                (filtered_lines if is_identity_contamination(line) else parts).append(line)

            # 1. 长期关系：档位 + 依据，一句话、不带裸分数
            acquaintance: Mapping[str, Any] = {}
            if relationship.get("affinity") is not None or merged_dimensions:
                acquaintance = await self._run_sync(self._acquaintance, conn, scope, sender_id)
                add(relationship_sentence(
                    status_dims,
                    tier=classify_relationship_tier(status_dims),
                    first_seen=acquaintance.get("first_seen"),
                    interaction_count=acquaintance.get("interactions"),
                    now=now,
                ))
            # 2. 印象：当前看法 + 合并称呼 + 代表性的几件事 + 走势
            compressed = await asyncio.to_thread(
                compress_impressions, timeline_items, now=now, half_life_days=half_life_days,
            )
            for line in impression_lines(compressed, fallback_events=history_events(history, now=now)):
                add(line)
            # 3. 共同经历：只取与此人相关的，取不到就不写（不再塞 Bot 自己的群日记）
            soul_items = list(_mapping(_mapping(state).get("timeline")).get("items") or [])
            names = [str(getattr(ctx, "sender_name", "") or ""), *list(acquaintance.get("nicknames") or [])]
            shared = shared_experience_lines(
                soul_items, subject_principal_id=scope.subject_principal_id, display_names=names,
            )
            if shared:
                add("和对方的共同经历：" + "；".join(shared) + "。")
            try:
                from ...relationship_evidence_display import relationship_injection_summary_snippet
            except ImportError:  # pragma: no cover
                from services.relationship_evidence_display import (
                    relationship_injection_summary_snippet,
                )
            evidence_snip = relationship_injection_summary_snippet(
                relationship.get("evidence"), max_chars=80
            )
            if evidence_snip:
                add(f"历史关系摘要（只读，不改变好感度）：{evidence_snip}")
            # 4. 当下感受：最近一两天，随时间衰减，与长期关系分开
            add(current_feeling_line(
                history=history, traces=visible_traces, timeline_events=timeline_items, now=now,
            ))
            if parts and compressed.get("total"):
                add("（往事详情可用 wave_memory_person_search(query_type=timeline, person=当前人物) 查）")

            # 5. 观感标签与结算：默认交给事后记账，不在每轮回复里要求
            prompting = impression_prompting()
            per_turn_tag = _as_bool(cfg.get("per_turn_impression_tag"), prompting["per_turn_tag"])
            if per_turn_tag:
                add(unsettled_energy_line(impression_meta, energy=energy, traces=visible_traces))
            transition_hint = ""
            if should_trigger_affinity_transition(impression_meta, effective_dims or dimensions, energy=energy):
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
            text = "\n".join(parts) if parts else ""
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
                # 自然语言摘要已按条数与字数收敛（通常几百字），不被预算截断：保证末尾的结算提示完整
                preserve_full_text=bool(parts),
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

    @classmethod
    def _acquaintance(cls, conn: Any, scope: RuntimeScope, sender_id: str) -> dict[str, Any]:
        """认识多久、打过多少次交道、登记过的昵称：跨群与私聊汇总，只取数字与昵称，不带具体内容。"""
        if conn is None or not sender_id or not hasattr(conn, "execute"):
            return {}
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_profiles'").fetchone():
                return {}
            user_ids = cls._linked_user_ids(conn, scope, sender_id)
            marks = ",".join("?" for _ in user_ids)
            rows = conn.execute(
                f"""SELECT first_seen, interaction_count, nickname FROM user_profiles
                     WHERE bot_id=? AND user_id IN ({marks})""",
                (scope.bot_id, *user_ids),
            ).fetchall()
        except Exception:
            return {}
        first_seen = [float(row[0]) for row in rows if isinstance(row[0], (int, float)) and row[0] > 0]
        nicknames = list(dict.fromkeys(str(row[2]).strip() for row in rows if row[2] and str(row[2]).strip()))
        return {
            "first_seen": min(first_seen) if first_seen else None,
            "interactions": sum(int(row[1] or 0) for row in rows if isinstance(row[1], (int, float))),
            "nicknames": nicknames[:3],
        }

    def _relationship_totals(self, scope: Any, sender_id: str) -> dict[str, Any]:
        """汇总该用户在所有群的关系积累；不可用时返回空 dict。

        好感度不分群，因此这里拿到的是同一个人跨全部群的合并结果，不是「其他群的补充」。
        """
        summarizer = getattr(self.repository, "summarize_cross_group_relationship", None)
        if not callable(summarizer) or not sender_id:
            return {}
        subject = getattr(scope, "subject_principal_id", None)
        if not subject:
            return {}
        try:
            data = summarizer(scope, subject_principal_id=subject)
        except Exception:
            return {}
        if not isinstance(data, Mapping) or not data.get("available"):
            return {}
        return dict(data)

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["RelationshipChannel"]
