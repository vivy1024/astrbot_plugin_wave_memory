"""正式 Scoped Mood/Concern/Timeline 注入通道。"""

from __future__ import annotations

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
    return _mapping(_mapping(config.get("channels", {})).get("soul_state", {}))


class SoulStateChannel:
    """Inject only formal current Scope Soul state; no legacy mood/concern fallback."""

    name = "soul_state"

    def __init__(self, *, repository: Any = None):
        self.repository = repository

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"soul_state channel disabled in {mode} mode")
        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="soul_state channel disabled by config")
        scope = getattr(ctx, "scope", None)
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return InjectionResult.empty(self.name, reason="runtime_scope_required")
        if self.repository is None:
            return InjectionResult.empty(self.name, reason="soul_repository_unavailable")
        try:
            state = _mapping(self.repository.get_state(scope, limit=25, offset=0))
            mood = _mapping(state.get("mood"))
            timeline = _mapping(state.get("timeline")).get("items") or []
            lines: list[str] = []
            if mood.get("state") == "known":
                components = _mapping(mood.get("components"))
                lines.append(
                    f"近期情绪：valence={round(float(components.get('valence', mood.get('value', 0.0))), 2)}，"
                    f"arousal={round(float(components.get('arousal', 0.0)), 2)}"
                    + (f"（原因：{str(mood.get('cause'))[:80]}）" if mood.get("cause") else "")
                )
            # 关切不再常驻注入：本轮是否惦记由 MetaThinking × 印象时间线线索决定。
            recent_timeline = []
            for item in timeline[:2]:
                if not isinstance(item, Mapping):
                    continue
                summary = str(item.get("event_summary") or "").strip()[:100]
                if summary and not is_identity_contamination(summary):
                    recent_timeline.append(summary)
            if recent_timeline:
                lines.append("近期时间线：" + "；".join(recent_timeline))
            if not lines:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="soul_state_unknown")
            text = "[当前 Soul 状态：仅作为内部上下文，不要机械复述]\n" + "\n".join(f"- {line}" for line in lines)
            if is_identity_contamination(text):
                result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="identity_contamination")
                result.filtered = [{"filter_reason": "identity_contamination", "filter_channel": self.name}]
                return result
            revision = state.get("revision") or 0
            return InjectionResult.hit(
                self.name,
                text,
                items=[{
                    "source": "scoped_soul_state",
                    "revision": revision,
                    "preview": text[:180],
                    "rendered_text": text,
                    "dedupe_key": f"soul-state:{scope.bot_id}:{scope.session.id}:{revision}",
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


__all__ = ["SoulStateChannel"]
