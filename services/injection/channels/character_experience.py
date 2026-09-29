"""角色经历注入通道：启用了角色经历的 Bot 想起自己进群之前的经历。

- 只读当前 RuntimeScope 的 ``bot_id`` 名下、状态为「启用」的经历，其他 Bot 完全看不到；
- Bot 资料里没开启角色经历（``character_experience.enabled``）的 Bot 通道直接停用；
- 只召回「当前时间点」及之前阶段的经历（没分阶段的不受限制），避免剧透、自相矛盾；
- 渲染时标注角色自身时间，例如「【你过去的经历·第1年12月初】……」，与群里发生的事分开写，
  并说明这是她自己的过去，不是群友的事。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from ..query_gate import should_skip_retrieval
from .safety import is_channel_allowed_in_mode

try:
    from ....domain.character_experience import CharacterExperienceSettings
    from ....domain.scope import RuntimeScope, validate_formal_command_scope
except ImportError:  # pragma: no cover - 仓库测试把插件根目录当顶层包
    from domain.character_experience import CharacterExperienceSettings
    from domain.scope import RuntimeScope, validate_formal_command_scope

LABEL = "你过去的经历"


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
    per_call = _mapping(getattr(ctx, "channel_options", {}))
    if "character_experience" in per_call:
        return _mapping(per_call.get("character_experience"))
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("character_experience", {}))


def _preview(text: Any, limit: int = 120) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def time_label(item: Mapping[str, Any]) -> str:
    """角色自身时间：经历自己的时间 → 所在阶段的时间 → 阶段名；都没有就不标。"""
    for key in ("story_time", "stage_story_time", "stage_title"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def render_line(item: Mapping[str, Any]) -> str:
    label = time_label(item)
    head = f"【{LABEL}·{label}】" if label else f"【{LABEL}】"
    return head + str(item.get("content") or "").strip()


def render_block(name: str, lines: list[str]) -> str:
    who = f"（{name}）" if name else ""
    return "\n".join([
        "<your_past>",
        f"下面是你{who}自己进群之前的亲身经历，按你自己的时间标注。它们是你的过去，不是群友的事，"
        "也不是刚刚在群里发生的事；话题相关时可以自然地想起、提起，不要逐条复述：",
        *lines,
        "</your_past>",
    ])


class CharacterExperienceChannel:
    """按 Bot 召回角色经历（向量 + 中文全文索引），只取当前时间点及之前。"""

    name = "character_experience"

    def __init__(
        self,
        *,
        service: Any = None,
        embedding_service: Any = None,
        profile_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        self.service = service
        self.embedding_service = embedding_service
        self.profile_lookup = profile_lookup

    def _profile(self, bot_id: str) -> Any:
        if self.profile_lookup is None:
            return None
        try:
            return self.profile_lookup(bot_id)
        except Exception:
            return None

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"character_experience channel disabled in {mode} mode")
        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="character_experience channel disabled by config")
        scope = getattr(ctx, "scope", None)
        decision = validate_formal_command_scope("character_experience.inject", scope)
        if not decision.allowed or not isinstance(scope, RuntimeScope):
            return InjectionResult.empty(self.name, reason=decision.reason_code or "scope_rejected")
        profile = self._profile(scope.bot_id)
        settings = getattr(profile, "character_experience", None)
        if not isinstance(settings, CharacterExperienceSettings) or not settings.enabled:
            # 只有在 Bot 资料里开启了角色经历的 Bot 才会想起自己的角色经历
            return InjectionResult.disabled(self.name, reason="character_experience_not_enabled_for_bot")
        if self.service is None:
            return InjectionResult.empty(self.name, reason="character_experience_service_unavailable")
        skip, skip_reason = should_skip_retrieval(ctx)
        if skip:
            return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason=skip_reason)
        message = str(getattr(ctx, "message", "") or "")
        try:
            vector = None
            if self.embedding_service is not None:
                try:
                    vector = await self.embedding_service.get_embedding(message)
                except Exception:
                    vector = None  # 向量服务挂了还能靠关键词想起
            recalled = await self.service.recall(scope.bot_id, message, settings=settings, query_vector=vector)
            filtered = [self._audit_filtered(item) for item in recalled.get("filtered", [])]
            lines: list[str] = []
            items: list[dict[str, Any]] = []
            used = 0
            for item in recalled.get("items", []):
                content = str(item.get("content") or "")
                if is_identity_contamination(content):
                    filtered.append(self._audit_filtered({**item, "filter_reason": "identity_contamination"}))
                    continue
                line = render_line(item)
                if lines and used + len(line) > settings.max_chars:
                    filtered.append(self._audit_filtered({**item, "filter_reason": "max_chars"}))
                    continue
                if not lines and len(line) > settings.max_chars:
                    line = line[: max(1, settings.max_chars - 1)] + "…"
                lines.append(line)
                used += len(line)
                items.append(self._audit_item(scope.bot_id, item, line))
            warnings = [recalled["warning"]] if recalled.get("warning") else []
            if not lines:
                result = InjectionResult.empty(
                    self.name,
                    latency_ms=self._latency_ms(started),
                    reason="no character experiences recalled" + (f" ({recalled['warning']})" if recalled.get("warning") else ""),
                )
                result.filtered = filtered
                return result
            return InjectionResult.hit(
                self.name,
                render_block(str(getattr(profile, "name", "") or ""), lines),
                items=items,
                filtered=filtered,
                warnings=warnings,
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _audit_item(bot_id: str, item: Mapping[str, Any], line: str) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "experience_id": item.get("id"),
            "stage_key": item.get("stage_key", ""),
            "story_time": time_label(item),
            "canon_level": item.get("canon_level", ""),
            "score": item.get("score"),
            "similarity": item.get("similarity"),
            "keyword_hit": bool(item.get("keyword_hit")),
            "preview": _preview(item.get("content")),
            "rendered_text": line,
            "dedupe_key": f"character_experience:{bot_id}:{item.get('id')}",
        }

    @staticmethod
    def _audit_filtered(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "experience_id": item.get("id"),
            "score": item.get("score"),
            "preview": _preview(item.get("content")),
            "filter_reason": item.get("filter_reason", "filtered"),
            "filter_channel": "character_experience",
        }

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["CharacterExperienceChannel", "render_block", "render_line", "time_label"]
