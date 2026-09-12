"""安全注入通道。

该通道只做注入链路内部的安全边界与审计：
- 当前消息命中身份接管诱导时返回身份安全守卫文本；
- 为其他通道召回项提供身份污染与近期上下文去重过滤工具；
- 暴露运行模式下的通道允许策略，避免纯记忆/兼容模式误启高级通道。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ...identity_safety import build_identity_safety_injection, is_identity_contamination
from ..channel_base import InjectionResult

_ADVANCED_FULL_ONLY = frozenset({"persona", "holyman_persona", "belief", "jargon", "fewshot", "book_lore", "affinity", "soul_state"})
_MEMORY_ONLY_ALLOWED = frozenset({"safety", "memory", "facts", "fts5"})
_COMPAT_ONLY_ALLOWED = frozenset({"safety"})
_KNOWN_CHANNELS = _MEMORY_ONLY_ALLOWED | _ADVANCED_FULL_ONLY
_TEXT_FIELDS = ("content", "summary", "text")


def _compact(text: str | None) -> str:
    return str(text or "").replace(" ", "").replace("\n", "").replace("\t", "")


def _extract_text(item: Any, text_fields: Sequence[str] = _TEXT_FIELDS) -> str:
    if isinstance(item, Mapping):
        parts = [str(item.get(field) or "").strip() for field in text_fields if item.get(field)]
        return "\n".join(parts)
    return str(item or "")


def _with_filter_reason(item: Any, reason: str) -> dict[str, Any]:
    if isinstance(item, Mapping):
        payload = dict(item)
    else:
        payload = {"content": str(item or "")}
    payload["filter_reason"] = reason
    payload["filter_channel"] = "safety"
    return payload


def is_recent_context_duplicate(
    text: str | None,
    recent_context: Iterable[str] | None,
    *,
    min_chars: int = 8,
) -> bool:
    """判断召回文本是否已经出现在近期上下文中。"""

    compact_text = _compact(text)
    if len(compact_text) < min_chars:
        return False
    for recent in recent_context or []:
        compact_recent = _compact(recent)
        if len(compact_recent) < min_chars:
            continue
        if compact_text in compact_recent or compact_recent in compact_text:
            return True
    return False


def is_channel_allowed_in_mode(channel_name: str, mode: str) -> bool:
    """返回某通道在运行模式下是否允许参与原生注入。"""

    name = str(channel_name or "").strip()
    normalized_mode = mode if mode in {"full", "memory_only", "compat_only"} else "full"
    if normalized_mode == "compat_only":
        return name in _COMPAT_ONLY_ALLOWED
    if normalized_mode == "memory_only":
        return name in _MEMORY_ONLY_ALLOWED
    return name in _KNOWN_CHANNELS


class SafetyChannel:
    """安全通道，返回身份守卫并为其他通道提供过滤能力。"""

    name = "safety"

    async def build(self, ctx: Any) -> InjectionResult:
        guard = build_identity_safety_injection(getattr(ctx, "message", ""))
        if not guard:
            return InjectionResult.empty(self.name, reason="no safety guard needed")
        return InjectionResult.hit(self.name, guard, warnings=["identity_safety_guard"])

    def filter_items(
        self,
        items: Iterable[Mapping[str, Any]] | None,
        *,
        ctx: Any | None = None,
        text_fields: Sequence[str] = _TEXT_FIELDS,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """过滤身份污染与近期重复召回项，返回 `(kept, filtered)`。"""

        kept: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        recent_context = getattr(ctx, "recent_context", []) if ctx is not None else []

        for item in items or []:
            text = _extract_text(item, text_fields)
            if is_identity_contamination(text):
                filtered.append(_with_filter_reason(item, "identity_contamination"))
                continue
            if is_recent_context_duplicate(text, recent_context):
                filtered.append(_with_filter_reason(item, "recent_context_duplicate"))
                continue
            kept.append(dict(item))
        return kept, filtered


__all__ = ["SafetyChannel", "is_channel_allowed_in_mode", "is_recent_context_duplicate"]
