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
    from ....domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - focused repository tests
    from domain.scope import RuntimeScope


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
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return InjectionResult.empty(self.name, reason="runtime_scope_required")
        if not scope.subject_principal_id or self.repository is None:
            return InjectionResult.empty(self.name, reason="relationship_subject_or_repository_unavailable")
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
                self._profile_metadata, db, conn, scope, sender_id, group_id,
                finish_on_cancel=True,
            )
            try:
                from ...impression_timeline import (
                    affinity_shift_range,
                    injection_lines,
                    load_timeline_events,
                    load_unsettled_state,
                    should_trigger_affinity_transition,
                    unsettled_energy_line,
                )
            except ImportError:  # pragma: no cover
                from services.impression_timeline import (
                    affinity_shift_range,
                    injection_lines,
                    load_timeline_events,
                    load_unsettled_state,
                    should_trigger_affinity_transition,
                    unsettled_energy_line,
                )
            if db is not None and sender_id and group_id:
                timeline_events = await self._run_sync(
                    load_timeline_events, db,
                    bot_id=scope.bot_id,
                    user_id=sender_id,
                    query="",
                    limit=None,
                    connection=conn,
                    strict=True,
                )
            unsettled = {"energy": 0.0, "traces": []}
            if db is not None and sender_id and group_id:
                unsettled = await self._run_sync(
                    load_unsettled_state,
                    db,
                    bot_id=scope.bot_id,
                    user_id=sender_id,
                    group_id=group_id,
                    connection=conn,
                )
            context_config = _mapping(getattr(ctx, "config", {}))
            half_life_days = context_config.get(
                "timeline_decay_half_life_days",
                _mapping(context_config.get("Inject_Settings")).get("timeline_decay_half_life_days"),
            )
            rendered_lines = await asyncio.to_thread(
                injection_lines, impression_meta,
                history=history,
                now=float(getattr(ctx, "now", 0.0) or time.time()),
                events=timeline_events,
                half_life_days=half_life_days,
            )
            impression_block: list[str] = []
            filtered_lines: list[str] = []
            for line in rendered_lines:
                if line:
                    (filtered_lines if is_identity_contamination(line) else impression_block).append(line)
            has_timeline = any(line.startswith("印象时间线") for line in impression_block)
            if relationship.get("affinity") is None and not impression_block:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="relationship_unknown")
            energy_line = unsettled_energy_line(
                impression_meta,
                energy=float(unsettled.get("energy") or 0.0),
                traces=list(unsettled.get("traces") or []),
            )
            if energy_line and not is_identity_contamination(energy_line):
                impression_block.append(energy_line)
            labels = []
            for name in ("familiarity", "trust", "fun", "depth", "hostility"):
                item = _mapping(values.get(name))
                value = item.get("effective_value", dimensions.get(name))
                if value is not None:
                    labels.append(f"{name}={round(float(value), 1)}")
            parts = list(impression_block)
            status = (
                "[当前关系状态：仅用于调整对当前用户的自然回应，不代表必须改变事实或主动提及关系] "
                f"态度={relationship.get('state') or 'unknown'}，综合值={relationship.get('affinity')}"
            )
            if labels:
                status += "；" + "、".join(labels)
            if relationship.get("affinity") is not None:
                parts.append(status)
            if not any(line.startswith("最近关系线索：") or line.startswith("印象时间线") for line in impression_block):
                try:
                    from ...impression_timeline import meaningful_event_anchor
                except ImportError:  # pragma: no cover
                    from services.impression_timeline import meaningful_event_anchor
                anchor = meaningful_event_anchor(history)
                reason = str((anchor or {}).get("reason") or "").strip()
                event_type = str((anchor or {}).get("event_type") or "").strip()
                if anchor and reason and not is_identity_contamination(reason):
                    parts.append(f"最近关系线索：{event_type}：{reason}")
            try:
                from ...relationship_evidence_display import relationship_injection_summary_snippet
            except ImportError:  # pragma: no cover
                from services.relationship_evidence_display import (
                    relationship_injection_summary_snippet,
                )
            evidence_snip = relationship_injection_summary_snippet(
                relationship.get("evidence"), max_chars=80
            )
            if evidence_snip and not is_identity_contamination(evidence_snip):
                parts.append(f"历史关系摘要（只读，不改变好感度）：{evidence_snip}")
            # 关切常驻句已移除：未决事项只通过时间线线索在规则门命中时出现。
            try:
                timeline_items = list(_mapping(_mapping(state).get("timeline")).get("items") or [])
                for item in timeline_items[:3]:
                    if not isinstance(item, Mapping):
                        continue
                    summary = str(item.get("event_summary") or "").strip()
                    if summary and not is_identity_contamination(summary):
                        parts.append(f"共同交往锚点：{summary}")
            except Exception:
                pass
            trigger_dims: dict[str, Any] = {}
            for name in ("familiarity", "trust", "fun", "depth", "hostility"):
                item = _mapping(values.get(name))
                value = item.get("effective_value", dimensions.get(name))
                if value is not None:
                    trigger_dims[name] = value
            transition_hint = ""
            if should_trigger_affinity_transition(impression_meta, trigger_dims or dimensions, energy=float(unsettled.get("energy") or 0.0)):
                bounds = affinity_shift_range(impression_meta, dimension="trust", energy=float(unsettled.get("energy") or 0.0))
                transition_hint = (
                    "【该结算了】未结算能量已满或关系可能质变。"
                    f"若做阶段性定性，调用 wave_memory_record_social_impression，"
                    f"本轮增量须在 [{bounds['min']}, {bounds['max']}]，无变动填 0。"
                    "日常观感继续写 <<impression:当下观感 | impact:1-5>>，不要覆盖这句结算。"
                )

            # 印象时间线全量注入：不再按 900 字符预算截断历史背景；
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
                preserve_full_text=has_timeline,
                filtered=[{"filter_reason": "identity_contamination", "filter_channel": self.name} for _ in filtered_lines],
            )
        except Exception as exc:
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["RelationshipChannel"]
