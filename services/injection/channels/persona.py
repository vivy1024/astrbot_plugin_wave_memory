"""Persona/Soul/Experience 注入通道。

第 13 项只迁移稳定自我人格、精选自我经历和对话者画像；belief、jargon、few-shot、mood 等仍由后续独立通道迁移。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
try:
    from ....domain.scope import validate_formal_command_scope
except ImportError:  # pragma: no cover - standalone repository tests
    from domain.scope import validate_formal_command_scope
from ..channel_base import InjectionResult, estimate_injection_tokens
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
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("persona", {}))


def _persona_cfg(ctx: Any) -> Mapping[str, Any]:
    return _mapping(_mapping(getattr(ctx, "config", {})).get("persona", {}))


def _preview(text: str | None, limit: int = 160) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class PersonaChannel:
    """生成稳定自我人格、精选自我经历和对话者画像。"""

    name = "persona"

    def __init__(
        self,
        *,
        composer: Any = None,
    ):
        self.composer = composer

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"persona channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="persona channel disabled by config")
        runtime_scope = getattr(ctx, "scope", None)
        scope_decision = validate_formal_command_scope("persona.inject", runtime_scope)
        if not scope_decision.allowed:
            return InjectionResult.empty(
                self.name,
                latency_ms=self._latency_ms(started),
                reason=scope_decision.reason_code or "scope_rejected",
            )
        max_items = _as_int(cfg.get("max_items"), 3)
        token_budget = _as_int(cfg.get("token_budget"), 350)
        if max_items <= 0:
            return InjectionResult.empty(self.name, reason="persona max_items is zero")

        try:
            candidates = await self._build_candidates(ctx)
            selected, filtered = self._filter_and_budget(candidates, max_items=max_items, token_budget=token_budget)
            if not selected:
                result = InjectionResult.empty(
                    self.name,
                    latency_ms=self._latency_ms(started),
                    reason="no safe persona blocks",
                )
                result.filtered = [self._audit_filtered(item) for item in filtered]
                return result
            text = "\n\n".join(item["text"] for item in selected)
            return InjectionResult.hit(
                self.name,
                text,
                items=[self._audit_item(item) for item in selected],
                filtered=[self._audit_filtered(item) for item in filtered],
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    async def _build_candidates(self, ctx: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        debug: Mapping[str, Any] = {}

        runtime_scope = getattr(ctx, "scope", None)
        if self.composer and runtime_scope is not None and runtime_scope.session is not None:
            payload = await self.composer.build_self_persona(
                bot_id=runtime_scope.bot_id,
                group_id=runtime_scope.session.conversation_id,
                sender_id=getattr(ctx, "sender_id", "") or "",
                sender_name=getattr(ctx, "sender_name", "") or "",
                message=getattr(ctx, "message", "") or "",
                recent_context=list(getattr(ctx, "recent_context", []) or []),
                scope=runtime_scope,
            )
            payload = _mapping(payload)
            debug = _mapping(payload.get("debug", {}))
            self_persona = str(payload.get("persona_block") or "").strip()
            if self_persona:
                candidates.append({
                    "block": "self_persona",
                    "text": self_persona,
                    "source": "PersonaComposer.persona_block",
                    "source_ids": list(debug.get("persona_sources", []) or []),
                })
            experience = str(payload.get("experience_block") or "").strip()
            if experience:
                candidates.append({
                    "block": "self_experience",
                    "text": experience,
                    "source": "PersonaComposer.experience_block",
                    "source_ids": list(debug.get("experience_ids", []) or []),
                })

        return candidates

    @staticmethod
    def _filter_and_budget(
        candidates: list[dict[str, Any]],
        *,
        max_items: int,
        token_budget: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        selected: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        used_tokens = 0
        for item in candidates:
            text = item.get("text", "")
            if not text:
                continue
            if is_identity_contamination(text):
                filtered.append({**item, "filter_reason": "identity_contamination", "filter_channel": "persona"})
                continue
            if len(selected) >= max_items:
                filtered.append({**item, "filter_reason": "max_items", "filter_channel": "persona"})
                continue
            tokens = estimate_injection_tokens(text)
            if selected and token_budget >= 0 and used_tokens + tokens > token_budget:
                filtered.append({**item, "filter_reason": "token_budget", "filter_channel": "persona"})
                continue
            selected.append(item)
            used_tokens += tokens
        return selected, filtered

    @staticmethod
    def _audit_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "block": item.get("block", ""),
            "source": item.get("source", ""),
            "source_ids": list(item.get("source_ids", []) or []),
            "preview": _preview(item.get("text", "")),
        }

    @staticmethod
    def _audit_filtered(item: Mapping[str, Any]) -> dict[str, Any]:
        payload = PersonaChannel._audit_item(item)
        payload["filter_reason"] = item.get("filter_reason", "filtered")
        payload["filter_channel"] = item.get("filter_channel", "persona")
        return payload

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["PersonaChannel"]
