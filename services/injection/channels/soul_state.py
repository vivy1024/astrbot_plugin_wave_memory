"""Bot 自身状态注入通道：心情（自然语言）+ 近期时间线。

心情 v2 来自 ``BotMoodService``：一个 Bot 一份心情，注入情绪词与 1–2 条最近原因，
不输出 valence/arousal 裸数字；私聊来源的原因在别处只说「私下有件事」。旧的按群
``scoped_soul_mood`` 只是群氛围，不再注入。群日记在人格通道的自我经历里已经出现，
这里的时间线不再重复。
"""

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


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _is_diary(item: Mapping[str, Any], summary: str) -> bool:
    """群日记（``【日记】…``）已在人格通道的自我经历里注入，这里不再重复。"""
    return summary.startswith("【日记】") or (
        str(item.get("event_type") or "") == "episode" and "日记" in summary[:8]
    )


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("soul_state", {}))


class SoulStateChannel:
    """注入 Bot 此刻的心情与近期时间线；没有心情服务时只注入时间线。"""

    name = "soul_state"
    # build 里没有 await、全是同步读库：编排器放到工作线程跑，不占事件循环
    offload_to_thread = True

    def __init__(self, *, repository: Any = None, mood_service: Any = None):
        self.repository = repository
        self.mood_service = mood_service

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"soul_state channel disabled in {mode} mode")
        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="soul_state channel disabled by config")
        scope = getattr(ctx, "scope", None)
        # Bot 自身状态属于 Bot 本人：群聊与私聊都注入（私密内容由仓储按场合过滤）。
        if (
            not isinstance(scope, RuntimeScope)
            or scope.visibility not in {"group", "private"}
            or scope.session is None
        ):
            return InjectionResult.empty(self.name, reason="runtime_scope_required")
        if self.repository is None:
            return InjectionResult.empty(self.name, reason="soul_repository_unavailable")
        try:
            # 热路径只要时间线前几条与 revision；get_state 的 mood/concerns/关系查询留给 WebUI。
            # 多取几条是因为日记条目会被跳过。
            inject_cfg = _mapping(getattr(ctx, "config", {})).get("Inject_Settings", {}) or {}
            soul_timeline_max = _as_int(inject_cfg.get("soul_timeline_max_items"), 3)
            recent = getattr(self.repository, "recent_timeline", None)
            if callable(recent):
                state = _mapping(recent(scope, limit=max(8, soul_timeline_max * 3)))
            else:
                state = _mapping(self.repository.get_state(scope, limit=25, offset=0))
            timeline = _mapping(state.get("timeline")).get("items") or []
            lines: list[str] = []
            mood_service = self.mood_service
            if mood_service is not None:
                try:
                    mood_lines = mood_service.render_lines(scope.bot_id, scope)
                except Exception:
                    # 心情读取失败不拖垮整个通道：时间线照常注入。
                    mood_lines = []
                # 原因里可能引用群友原话：逐行过身份安全检查，只丢有问题的那一行。
                lines.extend(line for line in mood_lines if not is_identity_contamination(line))
            # 关切不再常驻注入：本轮是否惦记由 MetaThinking × 印象时间线线索决定。
            recent_timeline = []
            for item in timeline:
                if len(recent_timeline) >= max(1, soul_timeline_max):
                    break
                if not isinstance(item, Mapping):
                    continue
                summary = str(item.get("event_summary") or "").strip()[:100]
                if _is_diary(item, summary):
                    continue
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
