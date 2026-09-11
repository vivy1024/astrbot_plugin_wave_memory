"""注入编排器骨架。

通道只返回 `InjectionResult`；只有编排器负责拼接最终文本并写入 ProviderRequest。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from .channel_base import InjectionChannel, InjectionResult, estimate_injection_tokens
from .context import InjectionContext
from .trace_store import InjectionTraceStore, runtime_scope_metadata
from ..config.channel_config import ChannelConfigSet, channel_config_revision


logger = logging.getLogger(__name__)
# Warn only when total injection exceeds the remote-embedding memory budget.
SLOW_INJECTION_WARNING_MS = 2000


@dataclass
class OrchestrationResult:
    trace_id: str
    injected: bool
    final_text: str
    channel_results: list[InjectionResult] = field(default_factory=list)
    total_latency_ms: float = 0.0


class InjectionOrchestrator:
    """选择通道、并发执行、排序、预算、写入 TextPart 与 trace。"""

    def __init__(
        self,
        *,
        channels: Iterable[InjectionChannel],
        config: ChannelConfigSet,
        trace_store: InjectionTraceStore | None = None,
        text_part_factory: Callable[[str], Any] | None = None,
    ):
        self.channels = list(channels)
        self.config = config
        self.trace_store = trace_store
        self.text_part_factory = text_part_factory

    async def run(self, ctx: InjectionContext) -> OrchestrationResult:
        started = time.perf_counter()
        results = [result for result in await self._run_channels(ctx) if result.status != "disabled"]
        ordered = self._sort_results(results)
        final_text = self._compose_final_text(ordered)
        injected = False

        if final_text:
            if not ctx.dry_run:
                text_part = self._make_text_part(final_text)
                ctx.req.extra_user_content_parts.append(text_part)
            injected = True

        total_latency_ms = round((time.perf_counter() - started) * 1000, 2)
        if total_latency_ms > SLOW_INJECTION_WARNING_MS:
            logger.warning(
                "[WaveMemory] inject_memory 耗时过长: %.0fms > %dms | channels=%s",
                total_latency_ms,
                SLOW_INJECTION_WARNING_MS,
                self._channel_breakdown(ordered),
            )
        if self.trace_store and self.config.trace_enabled and not ctx.dry_run:
            trace_status, trace_error = self._trace_status_and_error(ordered, injected=injected)
            self.trace_store.safe_record(
                {
                    "trace_id": ctx.trace_id,
                    "timestamp": ctx.now or time.time(),
                    "mode": ctx.mode,
                    "group_id": ctx.group_id,
                    "sender_id": ctx.sender_id,
                    "sender_name": ctx.sender_name,
                    "bot_id": ctx.bot_id,
                    "bot_profile_id": ctx.bot_profile_id,
                    "metadata": {
                        **runtime_scope_metadata(ctx.scope),
                        "config_revision": channel_config_revision(self.config),
                    },
                    "message": ctx.message,
                    "final_text": final_text,
                    "total_latency_ms": total_latency_ms,
                    "total_tokens": estimate_injection_tokens(final_text),
                    "total_chars": len(final_text),
                    "status": trace_status,
                    "error": trace_error,
                },
                ordered,
            )

        return OrchestrationResult(
            trace_id=ctx.trace_id,
            injected=injected,
            final_text=final_text,
            channel_results=ordered,
            total_latency_ms=total_latency_ms,
        )

    async def _run_channels(self, ctx: InjectionContext) -> list[InjectionResult]:
        runnable = [channel for channel in self.channels if self._channel_runnable(channel)]
        tasks = [self._run_one(channel, ctx) for channel in runnable]
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))

    def _channel_runnable(self, channel: InjectionChannel) -> bool:
        name = getattr(channel, "name", "unknown")
        cfg = self.config.channels.get(name)
        if not cfg:
            return True
        if not cfg.enabled:
            return False
        if self.config.mode not in cfg.modes:
            return False
        return True

    async def _run_one(self, channel: InjectionChannel, ctx: InjectionContext) -> InjectionResult:
        name = getattr(channel, "name", "unknown")
        cfg = self.config.channels.get(name)
        timeout_ms = cfg.timeout_ms if cfg else 300
        started = time.perf_counter()
        channel_options = dict(ctx.channel_options or {})
        if cfg is not None:
            channel_options[name] = cfg.to_dict()
        channel_ctx = replace(ctx, channel_options=channel_options)
        # 超时只记警告，不取消、不丢结果。通道继续跑完再注入。
        task = asyncio.create_task(channel.build(channel_ctx))
        timed_out = False
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=max(timeout_ms / 1000.0, 0.001))
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "[WaveMemory] %s exceeded %sms; waiting for result without cancelling",
                name,
                timeout_ms,
            )
            try:
                result = await task
            except Exception as exc:
                result = InjectionResult.error_result(name, f"timed out after {timeout_ms}ms then failed: {exc}")
        except Exception as exc:
            result = InjectionResult.error_result(name, exc)
        if timed_out and result.status == "hit":
            warning = f"channel exceeded {timeout_ms}ms"
            if warning not in result.warnings:
                result.warnings.append(warning)
        result.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return result

    def _sort_results(self, results: list[InjectionResult]) -> list[InjectionResult]:
        def priority(result: InjectionResult) -> int:
            cfg = self.config.channels.get(result.channel)
            return cfg.priority if cfg else 0

        return sorted(results, key=lambda r: priority(r), reverse=True)

    def _channel_breakdown(self, results: list[InjectionResult]) -> list[dict[str, Any]]:
        return [
            {
                "channel": result.channel,
                "status": result.status,
                "ms": result.latency_ms,
            }
            for result in results
        ]

    def _trace_status_and_error(self, results: list[InjectionResult], *, injected: bool) -> tuple[str, str]:
        failing = [result for result in results if result.status in {"error", "timeout"}]
        if failing:
            error_summary = ", ".join(f"{result.channel}:{result.status}" for result in failing[:8])
            has_success = injected or any(result.status == "hit" for result in results)
            return ("degraded" if has_success else "error", error_summary)
        if injected or results:
            return "ok", ""
        return "empty", ""

    def _compose_final_text(self, results: list[InjectionResult]) -> str:
        parts: list[str] = []
        seen_semantic_keys: set[str] = set()
        remaining_budget = sum(
            cfg.token_budget for cfg in self.config.channels.values() if cfg.enabled and cfg.name != "safety"
        )
        preserved = {
            id(result) for result in results
            if result.channel == "affinity" and result.preserve_full_text
            and result.status == "hit" and result.text
        }
        preserved_channels = {result.channel for result in results if id(result) in preserved}
        # 必保留文本独立计量；它的超额不能吞掉其他通道预算，也不能转赠配额。
        remaining_budget -= sum(
            cfg.token_budget for name, cfg in self.config.channels.items()
            if cfg.enabled and name in preserved_channels
        )
        for result in results:
            if result.status != "hit" or not result.text:
                continue
            must_preserve = id(result) in preserved
            result_tokens = max(result.tokens, 0)
            if not must_preserve and (remaining_budget <= 0 or result_tokens > remaining_budget):
                result.status = "skipped"
                result.warnings.append("injection_budget_exceeded")
                continue
            cfg = self.config.channels.get(result.channel)
            if must_preserve and result_tokens > (cfg.token_budget if cfg else 0):
                warning = "full_timeline_exceeds_injection_budget"
                if warning not in result.warnings:
                    result.warnings.append(warning)
            items = [item for item in (result.items or []) if isinstance(item, dict)]
            item_keys = {str(item.get("dedupe_key")) for item in items if item.get("dedupe_key")}
            if item_keys:
                duplicate_items = [item for item in items if item.get("dedupe_key") in seen_semantic_keys]
                fresh_keys = item_keys - seen_semantic_keys
                if not fresh_keys:
                    continue
                text = result.text
                for item in duplicate_items:
                    rendered = str(item.get("rendered_text") or "")
                    if rendered:
                        text = re.sub(rf"(?m)^\\s*{re.escape(rendered)}\\s*\\n?", "", text)
                text = text.strip()
                if not text:
                    continue
                seen_semantic_keys.update(item_keys)
            else:
                normalized = self._normalize_dedupe_text(result.text)
                if normalized in seen_semantic_keys:
                    continue
                seen_semantic_keys.add(normalized)
                text = result.text
            parts.append(text)
            if not must_preserve:
                remaining_budget -= max(1, result_tokens if text == result.text else len(text) // 4)
        return "\n\n".join(parts)

    @staticmethod
    def _normalize_dedupe_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
        return re.sub(r"\\s+", " ", normalized).strip()

    def _make_text_part(self, text: str) -> Any:
        if self.text_part_factory:
            return self.text_part_factory(text)
        try:
            from astrbot.core.agent.message import TextPart
            return TextPart(text=text)
        except Exception:
            return {"type": "text", "text": text}
