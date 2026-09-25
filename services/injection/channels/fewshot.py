"""Few-shot / Style 注入通道。"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

try:
    from ....domain.scope import RuntimeScope
except ImportError:
    from domain.scope import RuntimeScope
from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from .safety import is_channel_allowed_in_mode

_AGGRESSIVE_STYLE_RE = re.compile(
    r"(怼回去|狠狠怼|别客气|骂回去|反击|嘴臭|阴阳怪气|傻逼|脑残|滚|nmsl|你妈|操你|fuck\s*you)",
    re.IGNORECASE,
)


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
    return _mapping(_mapping(config.get("channels", {})).get("fewshot", {}))


def _preview(text: str | None, limit: int = 160) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _is_healthy(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and not is_identity_contamination(value) and not _AGGRESSIVE_STYLE_RE.search(value)


def _extract_examples(text: str, ids: list[Any]) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        content = stripped.lstrip("- ").strip()
        if content:
            idx = len(examples)
            examples.append({
                "example_id": ids[idx] if idx < len(ids) else None,
                "content": content,
                "preview": _preview(content),
            })
    if not examples and text:
        examples.append({"example_id": ids[0] if ids else None, "content": text, "preview": _preview(text)})
    return examples


class FewShotChannel:
    """只读调用 FewShotService.get_injection，注入已批准健康风格样例。"""

    name = "fewshot"
    # build 里没有 await、全是同步读库：编排器放到工作线程跑，不占事件循环
    offload_to_thread = True

    def __init__(self, *, few_shot_service: Any = None):
        self.few_shot_service = few_shot_service

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"fewshot channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="fewshot channel disabled by config")
        if not self.few_shot_service:
            return InjectionResult.empty(self.name, reason="few-shot service is unavailable")
        runtime_scope = getattr(ctx, "scope", None)
        if not isinstance(runtime_scope, RuntimeScope):
            return InjectionResult.empty(self.name, reason="runtime_scope_required")
        bot_profile_id = str(getattr(ctx, "bot_profile_id", "") or "").strip()
        if not bot_profile_id or runtime_scope.bot_id != bot_profile_id:
            return InjectionResult.empty(self.name, reason="runtime_scope_bot_mismatch")
        max_items = _as_int(cfg.get("max_items"), 3)
        if max_items <= 0:
            return InjectionResult.empty(self.name, reason="fewshot max_items is zero")

        try:
            text = self.few_shot_service.get_injection(
                scope=runtime_scope,
                max_items=max_items,
            ) or ""
            text = str(text).strip()
            if not text:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no approved style examples")

            ids = list(getattr(self.few_shot_service, "_last_injected_ids", []) or [])
            examples = _extract_examples(text, ids)
            selected: list[dict[str, Any]] = []
            filtered: list[dict[str, Any]] = []
            for example in examples:
                content = example.get("content", "")
                if not _is_healthy(content):
                    reason = "identity_contamination" if is_identity_contamination(content) else "unhealthy_style"
                    filtered.append({**example, "filter_reason": reason, "filter_channel": "fewshot"})
                    continue
                selected.append(example)

            if not selected:
                result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no healthy style examples")
                result.filtered = [self._audit_filtered(item) for item in filtered]
                return result

            rebuilt = "<style_examples>\n" + "\n".join(f"- {item['content']}" for item in selected) + "\n</style_examples>"
            return InjectionResult.hit(
                self.name,
                rebuilt,
                items=[self._audit_item(item) for item in selected],
                filtered=[self._audit_filtered(item) for item in filtered],
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _audit_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "example_id": item.get("example_id"),
            "source": "FewShotService.get_injection",
            "preview": item.get("preview") or _preview(item.get("content", "")),
        }

    @staticmethod
    def _audit_filtered(item: Mapping[str, Any]) -> dict[str, Any]:
        payload = FewShotChannel._audit_item(item)
        payload["filter_reason"] = item.get("filter_reason", "filtered")
        payload["filter_channel"] = item.get("filter_channel", "fewshot")
        return payload

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["FewShotChannel"]
