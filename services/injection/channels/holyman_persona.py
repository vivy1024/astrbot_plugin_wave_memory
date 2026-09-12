"""Holyman-skills 人格包注入通道（可选，默认关闭）。

与 ``jargon`` 通道的区别：jargon 只做**理解参考**（解释用户消息里已有的词），
本通道按 SKILL.md 原生语义把人格包作为**风格参考**注入，因此会影响说话方式。

因为会改变风格，它：
- **默认关闭**，必须显式开启；
- 与既有 PersonaComposer 自我人格并存而非覆盖（优先级低于 persona/fewshot）；
- 整包过 ``is_identity_contamination`` 守卫，任何命中即整体拒绝注入，绝不半信半疑地放行。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

try:
    from ....domain.scope import validate_formal_command_scope
except ImportError:
    from domain.scope import validate_formal_command_scope
from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from .safety import is_channel_allowed_in_mode


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
        return int(value)
    except (TypeError, ValueError):
        return default


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("holyman_persona", {}))


def _preview(text: str | None, limit: int = 160) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class HolymanPersonaChannel:
    """把 holyman-skills 人格包作为可选风格层注入。"""

    name = "holyman_persona"

    def __init__(self, *, persona_pack: Any = None):
        self.persona_pack = persona_pack

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"{self.name} channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        # 关键：默认关闭。风格注入必须由用户显式开启。
        if not _as_bool(cfg.get("enabled"), False):
            return InjectionResult.disabled(self.name, reason="holyman persona disabled by default")

        runtime_scope = getattr(ctx, "scope", None)
        scope_decision = validate_formal_command_scope("holyman_persona.inject", runtime_scope)
        if not scope_decision.allowed:
            return InjectionResult.empty(
                self.name,
                reason=scope_decision.reason_code or "scope_rejected",
            )
        if self.persona_pack is None or not getattr(self.persona_pack, "available", False):
            return InjectionResult.empty(self.name, reason="holyman persona pack is unavailable")

        max_items = _as_int(cfg.get("max_items"), 6)
        if max_items <= 0:
            return InjectionResult.empty(self.name, reason="holyman_persona max_items is zero")

        try:
            pack = self.persona_pack.build_injection(
                getattr(ctx, "message", "") or "",
                max_blocks=max_items,
            )
            text = str(pack.get("text") or "").strip()
            if not text:
                return InjectionResult.empty(
                    self.name,
                    latency_ms=self._latency_ms(started),
                    reason="no usable holyman persona blocks",
                )
            if is_identity_contamination(text):
                result = InjectionResult.empty(
                    self.name,
                    latency_ms=self._latency_ms(started),
                    reason="no safe holyman persona",
                )
                result.filtered = [{
                    "source": "HolymanPersonaPack.build_injection",
                    "filter_reason": "identity_contamination",
                    "filter_channel": self.name,
                    "preview": _preview(text),
                }]
                return result

            items = []
            for block in pack.get("blocks", []) or []:
                items.append({
                    "block": block.get("block", ""),
                    "source": block.get("source", ""),
                    "safety": bool(block.get("safety")),
                    "source_layer": "holyman_persona",
                    "reference_only": False,
                    "runtime_match": True,
                    "matched_by": "persona_pack",
                    "evidence": block.get("source", ""),
                    "preview": block.get("preview", ""),
                    "dedupe_key": f"holyman_persona:{block.get('block', '')}",
                })
            return InjectionResult.hit(
                self.name,
                text,
                items=items,
                filtered=list(pack.get("filtered", []) or []),
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["HolymanPersonaChannel"]
