"""Few-shot / Style 注入通道。"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any
from html import escape

try:
    from ....domain.scope import RuntimeScope
except ImportError:
    from domain.scope import RuntimeScope
from ...identity_safety import is_identity_contamination
from ...expression_policy import expression_is_safe
from ...preference_recall import select_preferences
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

    def __init__(self, *, few_shot_service: Any = None, profile_lookup: Any = None):
        self.few_shot_service = few_shot_service
        self.profile_lookup = profile_lookup

    def _profile(self, bot_id: str) -> Any:
        if self.profile_lookup is None:
            return None
        try:
            return self.profile_lookup(bot_id)
        except Exception:
            return None

    def _preferences(self, ctx: Any, profile: Any, max_items: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        persona = getattr(profile, "persona", None)
        settings = getattr(persona, "preferences", None) if persona is not None else None
        visibility = str(getattr(getattr(ctx, "scope", None), "visibility", "") or "")
        candidates = select_preferences(
            settings,
            str(getattr(ctx, "message", "") or ""),
            visibility=visibility,
            max_items=max_items,
        )
        selected: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for item in candidates:
            content = item.text if item.kind != "reaction" else item.reaction
            situation = item.situation
            if not _is_healthy(content) or (situation and not _is_healthy(situation)):
                reason = "identity_contamination" if is_identity_contamination(content) else "unhealthy_style"
                filtered.append({
                    "example_id": f"preference:{item.kind}", "source": "BotProfile.preferences",
                    "content": content, "preview": _preview(content),
                    "filter_reason": reason, "filter_channel": "fewshot",
                })
                continue
            selected.append({
                "example_id": f"preference:{item.kind}",
                "source": "BotProfile.preferences",
                "content": content,
                "preview": _preview(content),
                "kind": item.kind,
                "situation": situation,
                "reaction": item.reaction,
                "tags": list(item.tags),
                "strength": item.strength,
                "score": item.score,
            })
        return selected, filtered

    @staticmethod
    def _render_preferences(items: list[dict[str, Any]]) -> str:
        lines = [
            "<preferences>",
            "这些是你自己本来就有的喜恶，不是别人对你的要求；只在自然相关时流露，不要逐条复述。",
        ]
        for item in items:
            if item.get("kind") == "like":
                lines.append(f"- 喜欢：{escape(str(item.get('content') or ''))}")
            elif item.get("kind") == "dislike":
                lines.append(f"- 不喜欢：{escape(str(item.get('content') or ''))}")
            else:
                lines.append(
                    f"- 遇到「{escape(str(item.get('situation') or ''))}」时，你通常会："
                    f"{escape(str(item.get('reaction') or ''))}"
                )
        lines.append("</preferences>")
        return "\n".join(lines)

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"fewshot channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="fewshot channel disabled by config")
        runtime_scope = getattr(ctx, "scope", None)
        if not isinstance(runtime_scope, RuntimeScope):
            return InjectionResult.empty(self.name, reason="runtime_scope_required")
        bot_profile_id = str(getattr(ctx, "bot_profile_id", "") or "").strip()
        if not bot_profile_id or runtime_scope.bot_id != bot_profile_id:
            return InjectionResult.empty(self.name, reason="runtime_scope_bot_mismatch")

        profile = self._profile(bot_profile_id)
        max_preferences = min(2, _as_int(cfg.get("max_preferences"), 2))
        preference_selected, filtered = self._preferences(ctx, profile, max_preferences) if max_preferences > 0 else ([], [])

        max_items = min(3, _as_int(cfg.get("max_items"), 3))
        style_selected: list[dict[str, Any]] = []
        service_missing = not self.few_shot_service
        if runtime_scope.visibility == "group" and not service_missing and max_items > 0:
            try:
                structured = getattr(self.few_shot_service, "get_injection_examples", None)
                if callable(structured):
                    examples = structured(scope=runtime_scope, max_items=max_items, message=getattr(ctx, "message", ""))
                else:
                    text = self.few_shot_service.get_injection(scope=runtime_scope, max_items=max_items) or ""
                    ids = list(getattr(self.few_shot_service, "_last_injected_ids", []) or [])
                    examples = _extract_examples(str(text).strip(), ids) if text else []
                for example in examples[:max_items]:
                    content = example.get("content", "")
                    learned = example.get("source") == "ExpressionPolicy.recall"
                    if not _is_healthy(content) or (learned and not expression_is_safe(content)):
                        reason = "identity_contamination" if is_identity_contamination(content) else "unhealthy_style"
                        filtered.append({**example, "filter_reason": reason, "filter_channel": "fewshot"})
                        continue
                    style_selected.append(example)
            except Exception as exc:  # pragma: no cover - 防御性错误通道
                result = InjectionResult.error_result(self.name, exc)
                result.latency_ms = self._latency_ms(started)
                return result

        if not preference_selected and not style_selected:
            if runtime_scope.visibility != "group" and not preference_selected:
                reason = "group_scope_required" if not service_missing else "few-shot service is unavailable"
            elif service_missing and not preference_selected:
                reason = "few-shot service is unavailable"
            elif max_items <= 0 and not preference_selected:
                reason = "fewshot max_items is zero"
            else:
                reason = "no healthy style examples"
            result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason=reason)
            result.filtered = [self._audit_filtered(item) for item in filtered]
            return result

        blocks: list[str] = []
        audit_items: list[dict[str, Any]] = []
        if preference_selected:
            blocks.append(self._render_preferences(preference_selected))
            audit_items.extend(self._audit_item(item) for item in preference_selected)
        if style_selected:
            guidance = "仅参考适合当前情景的表达方式；保持自身身份，不代入示例说话人的经历。\n" if any(
                item.get("source") == "ExpressionPolicy.recall" for item in style_selected
            ) else ""
            blocks.append(
                "<style_examples>\n" + guidance
                + "\n".join(f"- {escape(item['content'])}" for item in style_selected)
                + "\n</style_examples>"
            )
            audit_items.extend(self._audit_item(item) for item in style_selected)
        return InjectionResult.hit(
            self.name,
            "\n\n".join(blocks),
            items=audit_items,
            filtered=[self._audit_filtered(item) for item in filtered],
            latency_ms=self._latency_ms(started),
        )

    @staticmethod
    def _audit_item(item: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "example_id": item.get("example_id"),
            "source": item.get("source", "FewShotService.get_injection"),
            "preview": item.get("preview") or _preview(item.get("content", "")),
        }
        if item.get("source") == "ExpressionPolicy.recall":
            for key in ("source_memory_id", "speaker_id", "situation", "bot_id", "session_id", "visibility", "quality_adjustment"):
                payload[key] = item.get(key)
        elif item.get("source") == "BotProfile.preferences":
            for key in ("kind", "situation", "reaction", "tags", "strength", "score"):
                payload[key] = item.get(key)
        return payload

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
