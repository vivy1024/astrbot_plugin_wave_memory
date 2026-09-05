"""正式 Scoped Relationship/affinity 注入通道。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
from ...belief_gating import snapshot_from_relationship
from ...proactive_policy import relationship_behavior_guidance
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

    def __init__(self, *, repository: Any = None):
        self.repository = repository

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
            state = self.repository.get_state(scope, subject_principal_id=scope.subject_principal_id, limit=25, offset=0)
            relationship = _mapping(_mapping(state).get("relationship"))
            if relationship.get("affinity") is None:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="relationship_unknown")
            dimensions = _mapping(relationship.get("dimensions"))
            values = _mapping(relationship.get("values"))
            history = list(_mapping(_mapping(state).get("relationship_history")).get("items") or [])
            impression_meta: Mapping[str, Any] = {}
            try:
                sender_id = str(getattr(ctx, "sender_id", "") or "").strip()
                group_id = str(getattr(ctx, "group_id", "") or getattr(scope.session, "conversation_id", "") or "").strip()
                if sender_id and group_id:
                    db = getattr(self.repository, "db", None) or getattr(self.repository, "_db", None)
                    conn = getattr(db, "conn", None) if db else None
                    if conn is None:
                        cm = getattr(self.repository, "cm", None)
                        conn = getattr(cm, "conn", None) if cm else None
                    if conn is not None and hasattr(conn, "execute"):
                        import json as _json
                        row = conn.execute(
                            "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                            (sender_id, group_id, scope.bot_id),
                        ).fetchone()
                        if row and row[0]:
                            loaded = _json.loads(row[0])
                            if isinstance(loaded, Mapping):
                                impression_meta = loaded
            except Exception:
                impression_meta = {}
            try:
                from ...impression_timeline import injection_lines
            except ImportError:  # pragma: no cover
                from services.impression_timeline import injection_lines
            impression_block = [
                line for line in injection_lines(impression_meta, history=history)
                if line and not is_identity_contamination(line)
            ]
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
            parts.append(status)
            guidance = relationship_behavior_guidance((snapshot_from_relationship(scope.subject_principal_id, relationship),))
            parts.append(
                "关系行为指导（只调节表达，不改变事实）："
                f"模式={guidance.get('mode')}，开放度={guidance.get('openness')}，"
                f"玩笑={guidance.get('playfulness')}，连续性={guidance.get('continuity')}。"
                f"边界={guidance.get('boundary')}"
            )
            if not any(line.startswith("最近关系线索：") for line in impression_block):
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
            try:
                concern_items = list(_mapping(_mapping(state).get("concerns")).get("items") or [])
                active_topics = [
                    str(c.get("topic") or "").strip()
                    for c in concern_items
                    if isinstance(c, Mapping) and float(c.get("intensity") or 0.0) >= 0.4 and str(c.get("topic") or "").strip()
                ]
                if active_topics and not any(is_identity_contamination(t) for t in active_topics):
                    parts.append(f"当前前情关切（可在适当时机自然承接，切忌突兀说教）：{'；'.join(active_topics[:2])}")
            except Exception:
                pass
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
            text = "\n".join(part for part in parts if part)
            if len(text) > 900:
                kept: list[str] = []
                used = 0
                for part in parts:
                    extra = len(part) + (1 if kept else 0)
                    if used + extra > 900:
                        break
                    kept.append(part)
                    used += extra
                text = "\n".join(kept) if kept else text[:900]
            if is_identity_contamination(text):
                result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="identity_contamination")
                result.filtered = [{"filter_reason": "identity_contamination", "filter_channel": self.name}]
                return result
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
            )
        except Exception as exc:
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["RelationshipChannel"]
