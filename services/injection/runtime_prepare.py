"""宿主无关的注入入口：Cortico 等外部应用经 Runtime API 拿到与 AstrBot 相同的记忆块。

v5 的 ``/context/prepare`` 是另写的简化版（书设写死、``LIKE`` 模糊匹配、不读通道配置、
不留 trace）。这里直接复用 AstrBot 路径的 ``InjectionOrchestrator`` 和同一套通道实例，
区别只有三点：

- 没有 AstrBot 事件：最近上下文由调用方传入，或从该会话最近的已解析记忆里取；
- 按 ``tier`` 选通道子集（Cortico 首轮 full、后续 light、工具调用 minimal）；
- trace 标注 ``source``（如 ``cortico``），注入观测台可以按来源筛选。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any

from .context import InjectionContext
from .orchestrator import InjectionOrchestrator, OrchestrationResult

try:
    from ...domain.scope import RuntimeScope
except ImportError:  # top-level import in isolated tests
    from domain.scope import RuntimeScope

# 各档位允许运行的通道；未列出的通道本次不运行（不改变全局配置）。
# 通道名以 channel.name 为准（关系通道叫 affinity）。
TIER_CHANNELS: dict[str, frozenset[str] | None] = {
    "full": None,
    "light": frozenset({"safety", "memory", "fts5", "facts", "persona", "belief", "jargon", "affinity", "soul_state"}),
    "minimal": frozenset({"safety", "memory", "fts5", "facts", "affinity"}),
}


def _text_part(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


class RuntimeContextPreparer:
    def __init__(
        self,
        *,
        channels_provider: Callable[[], Iterable[Any]],
        config_resolver: Callable[[RuntimeScope], Any],
        context_config_builder: Callable[..., dict[str, Any]],
        query_options_factory: Callable[[Any], Any],
        trace_store: Any = None,
        mode_provider: Callable[[], str] = lambda: "full",
        recent_messages: Callable[[RuntimeScope, int], list[str]] | None = None,
        exclude_sources_for: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._channels_provider = channels_provider
        self._config_resolver = config_resolver
        self._context_config_builder = context_config_builder
        self._query_options_factory = query_options_factory
        self._trace_store = trace_store
        self._mode_provider = mode_provider
        self._recent_messages = recent_messages
        self._exclude_sources_for = exclude_sources_for

    def available(self) -> bool:
        return bool(list(self._channels_provider() or ()))

    async def prepare(
        self,
        *,
        scope: RuntimeScope,
        message: str,
        sender_id: str = "",
        sender_name: str = "",
        tier: str = "full",
        recent_context: list[str] | None = None,
        source: str = "runtime",
        dry_run: bool = False,
        channel_filter: Iterable[str] | None = None,
    ) -> OrchestrationResult:
        if not isinstance(scope, RuntimeScope):
            raise ValueError("scope must be a RuntimeScope")
        effective = self._config_resolver(scope)
        if effective is None:
            raise ValueError("effective channel config unavailable for scope")
        allowed = TIER_CHANNELS.get(tier, TIER_CHANNELS["full"])
        if channel_filter:
            requested = frozenset(channel_filter)
            allowed = requested if allowed is None else allowed & requested
        channels = [ch for ch in (self._channels_provider() or ()) if allowed is None or getattr(ch, "name", "") in allowed]

        if recent_context is None and self._recent_messages is not None:
            try:
                recent_context = self._recent_messages(scope, 8)
            except Exception:
                recent_context = []
        recent_context = [str(item) for item in (recent_context or []) if str(item or "").strip()][-12:]
        exclude_sources = self._exclude_sources_for(scope.bot_id) if self._exclude_sources_for else []

        trace_id = f"{source}-{time.time_ns()}"
        group_id = scope.session.conversation_id if scope.session is not None and scope.visibility == "group" else None
        req = SimpleNamespace(prompt=message, system_prompt="", contexts=[], extra_user_content_parts=[])
        ctx = InjectionContext(
            event=None,
            req=req,
            message=message,
            group_id=group_id,
            sender_id=sender_id,
            sender_name=sender_name,
            bot_id=scope.bot_id,
            bot_profile_id=scope.bot_id,
            scope=scope,
            recent_context=recent_context,
            mode=self._mode_provider(),
            config=self._context_config_builder(
                channel_config=effective,
                exclude_sources=exclude_sources,
                recent_context=recent_context,
                realtime_ctx={},
            ),
            channel_options=effective.to_dict()["channels"],
            query_options=self._query_options_factory(effective),
            dry_run=dry_run,
            now=time.time(),
            trace_id=trace_id,
            source=source,
        )
        orchestrator = InjectionOrchestrator(
            channels=channels,
            config=effective,
            trace_store=self._trace_store,
            text_part_factory=_text_part,
        )
        return await orchestrator.run(ctx)


def channel_stats(result: OrchestrationResult) -> dict[str, dict[str, Any]]:
    """每个通道本次的状态、条数和耗时（Runtime API 响应里给调用方看）。"""
    stats: dict[str, dict[str, Any]] = {}
    for item in result.channel_results:
        stats[item.channel] = {
            "status": item.status,
            "items": len(item.items or []),
            "tokens": item.tokens,
            "latency_ms": item.latency_ms,
        }
        if item.error:
            stats[item.channel]["error"] = item.error
    return stats


def persona_lore_block(profile: Any) -> str:
    persona = getattr(profile, "persona", None)
    lines = list(getattr(persona, "lore_lines", None) or [])
    if not lines:
        return ""
    title = str(getattr(persona, "lore_title", "") or "常驻书设").strip()
    return f"[世界观书设：{title}]\n" + "\n".join(f"- {line}" for line in lines)


__all__ = ["RuntimeContextPreparer", "TIER_CHANNELS", "channel_stats", "persona_lore_block"]
