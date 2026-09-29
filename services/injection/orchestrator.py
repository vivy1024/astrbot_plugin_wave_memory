"""注入编排器骨架。

通道只返回 `InjectionResult`；只有编排器负责拼接最终文本并写入 ProviderRequest。
"""

from __future__ import annotations

import asyncio
import logging
import math
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
# 默认值；运行时由热参数 injection.slow_warning_ms 通过 set_slow_warning_ms() 调整。
SLOW_INJECTION_WARNING_MS = 2000
_slow_warning_ms = SLOW_INJECTION_WARNING_MS


def set_slow_warning_ms(value: int) -> None:
    global _slow_warning_ms
    try:
        _slow_warning_ms = max(1, int(value))
    except (TypeError, ValueError):
        _slow_warning_ms = SLOW_INJECTION_WARNING_MS


def reply_decision_metadata(event: Any) -> dict[str, Any]:
    """Persist bounded rule diagnostics, never an event object or message body."""
    raw = getattr(event, "_wave_memory_reply_decision", None)
    if not isinstance(raw, dict):
        return {}
    decision: dict[str, Any] = {}
    for key in ("mode", "decision", "reason_code"):
        if isinstance(raw.get(key), str):
            decision[key] = raw[key][:120]
    for key in ("score", "threshold", "cooldown_remaining"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            decision[key] = value
    for key in ("would_reply", "enforced", "eligible"):
        if isinstance(raw.get(key), bool):
            decision[key] = raw[key]
    if isinstance(raw.get("reasons"), (list, tuple)):
        decision["reasons"] = [item[:160] for item in raw["reasons"][:20] if isinstance(item, str)]
    if isinstance(raw.get("factors"), dict):
        decision["factors"] = {
            key[:80]: value for key, value in list(raw["factors"].items())[:20]
            if isinstance(key, str) and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value)
        }
    return {"reply_decision": decision} if decision else {}



def _run_in_thread(channel: Any, ctx: Any) -> Any:
    # 这类通道的 build 里没有 await（纯同步读库），在工作线程自己的事件循环里一次跑完
    return asyncio.run(channel.build(ctx))


async def _build(channel: Any, ctx: Any) -> Any:
    """整段是同步读库的通道放到工作线程，不占事件循环。

    线上实测：6 个同步通道在事件循环上依次执行，所有通道的完成时间被拖到同一刻
    （中位数都是 67 ms、最坏 660 ms），并行名存实亡。只给 build 里确实没有 await、
    不碰事件循环对象的通道打 ``offload_to_thread = True``；共享的 SQLite 连接是
    ``check_same_thread=False`` 且库为串行化线程模式（threadsafety=3）。
    """
    if getattr(channel, "offload_to_thread", False):
        return await asyncio.to_thread(_run_in_thread, channel, ctx)
    return await channel.build(ctx)

# ── 单通道预算截断 ─────────────────────────────────────────────
# 结构行：XML 式标签行、整行方括号/书名号标题、短的冒号结尾标题。截断时不丢这些行。
_TAG_LINE_RE = re.compile(r"^</?[A-Za-z_][\w.-]*>$")
_CLOSING_TAG_RE = re.compile(r"^</[A-Za-z_][\w.-]*>$")
_BRACKET_LINE_RE = re.compile(r"^(?:\[[^\n]*\]|【[^\n]*】)$")


def _is_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _CLOSING_TAG_RE.match(stripped):
        return False
    if _TAG_LINE_RE.match(stripped) or _BRACKET_LINE_RE.match(stripped):
        return True
    return len(stripped) <= 40 and stripped.endswith(("：", ":")) and not stripped.startswith(("-", "·", "*"))


def _is_structural_line(line: str) -> bool:
    stripped = line.strip()
    return not stripped or bool(_CLOSING_TAG_RE.match(stripped)) or _is_header_line(stripped)


_EMPTY_TAG_PAIR_RE = re.compile(r"(?m)^[ \t]*<([A-Za-z_][\w.-]*)>[ \t]*\n(?:[ \t]*\n)*[ \t]*</\1>[ \t]*(?:\n|$)")


def _cleanup_structure(text: str) -> str:
    """去掉截断后变空的标签对、末尾孤立的标题段，并压缩空行。"""
    previous = None
    while previous != text:
        previous = text
        text = _EMPTY_TAG_PAIR_RE.sub("", text)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()
    paragraphs = [paragraph for paragraph in text.split("\n\n") if paragraph.strip()]
    while len(paragraphs) > 1 and all(_is_header_line(line) or not line.strip() for line in paragraphs[-1].splitlines()):
        paragraphs.pop()
    if paragraphs:
        lines = paragraphs[-1].splitlines()
        while len(lines) > 1 and _is_header_line(lines[-1]):
            lines.pop()
        paragraphs[-1] = "\n".join(lines)
    return "\n\n".join(paragraphs).strip()


def _has_content(text: str) -> bool:
    return any(not _is_structural_line(line) for line in text.splitlines())


def _remove_last(text: str, fragment: str) -> str:
    position = text.rfind(fragment)
    if position < 0:
        return text
    end = position + len(fragment)
    if end < len(text) and text[end] == "\n":
        end += 1
    elif position > 0 and text[position - 1] == "\n":
        position -= 1
    return text[:position] + text[end:]


def _item_probes(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    probes: list[str] = []
    for key in ("rendered_text", "preview", "word", "content", "text", "summary"):
        value = str(item.get(key) or "").strip().strip("…").strip()
        if len(value) >= 2:
            probes.append(value[:24])
    return probes


def _truncate_by_items(text: str, items: list[Any], budget: int) -> tuple[str, list[Any], list[Any], str, int] | None:
    renders = []
    whole = text.strip()
    for index, item in enumerate(items):
        rendered = str(item.get("rendered_text") or "").strip() if isinstance(item, dict) else ""
        if rendered and rendered != whole and rendered in text:
            renders.append((index, rendered))
    if len(renders) < 2:
        return None
    current = text
    dropped: set[int] = set()
    for index, rendered in reversed(renders):
        if estimate_injection_tokens(current) <= budget or len(dropped) >= len(renders) - 1:
            break
        updated = _cleanup_structure(_remove_last(current, rendered))
        if updated == current:
            continue
        current = updated
        dropped.add(index)
    if not dropped or estimate_injection_tokens(current) > budget or not _has_content(current):
        return None
    kept = [item for index, item in enumerate(items) if index not in dropped]
    removed = [items[index] for index in sorted(dropped)]
    return current, kept, [item for item in removed if isinstance(item, dict)], "条目", len(dropped)


def _truncate_by_lines(text: str, items: list[Any], budget: int) -> tuple[str, list[Any], list[Any], str, int] | None:
    lines = text.split("\n")
    content = [index for index, line in enumerate(lines) if not _is_structural_line(line)]
    if len(content) < 2:
        return None
    removed: set[int] = set()
    current = text
    for index in reversed(content):
        if estimate_injection_tokens(current) <= budget or len(removed) >= len(content) - 1:
            break
        removed.add(index)
        current = _cleanup_structure("\n".join(line for position, line in enumerate(lines) if position not in removed))
    if not removed or estimate_injection_tokens(current) > budget or not _has_content(current):
        return None
    kept_items: list[Any] = []
    dropped_items: list[Any] = []
    for item in items:
        probes = _item_probes(item)
        if probes and not any(probe in current for probe in probes):
            dropped_items.append(item)
        else:
            kept_items.append(item)
    return current, kept_items, dropped_items, "行", len(removed)


def _truncate_to_budget(text: str, items: list[Any], budget: int) -> tuple[str, list[Any], list[Any], str, int] | None:
    """按条目、再按行截断到预算内；做不到（单条就超、只有一段正文）时返回 None。"""
    if budget <= 0:
        return None
    return _truncate_by_items(text, items, budget) or _truncate_by_lines(text, items, budget)


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
        if total_latency_ms > _slow_warning_ms:
            logger.warning(
                "[WaveMemory] inject_memory 耗时过长: %.0fms > %dms | channels=%s",
                total_latency_ms,
                _slow_warning_ms,
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
                        "source": getattr(ctx, "source", "astrbot") or "astrbot",
                        **reply_decision_metadata(ctx.event),
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
        """并行跑各通道；声明 ``offload_to_thread`` 的通道在工作线程里跑（见 :func:`_build`）。"""
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
        task = asyncio.create_task(_build(channel, channel_ctx))
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
        # 通道若在构造结果之后才填正文（或自己构造时没给 tokens），这里按正文补估算，
        # 保证单通道预算、总预算与 trace 的 tokens 列对所有通道都生效。
        if result.text:
            if result.tokens <= 0:
                result.tokens = estimate_injection_tokens(result.text)
            if result.chars <= 0:
                result.chars = len(result.text)
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
            cfg = self.config.channels.get(result.channel)
            # 单通道预算：每个通道不得超过自己的 token_budget（关系通道的全量保留文本除外）。
            if not must_preserve and not self._fit_channel_budget(result, cfg):
                continue
            result_tokens = max(result.tokens, 0)
            if not must_preserve and (remaining_budget <= 0 or result_tokens > remaining_budget):
                result.status = "skipped"
                result.warnings.append("injection_budget_exceeded")
                continue
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
                    rendered = str(item.get("rendered_text") or "").strip()
                    if rendered:
                        # 只去掉整行（或整段）等于该条目渲染文本的部分，连同行首尾空白与一个换行。
                        # 旧写法在 raw f-string 里写成 \\s，匹配的是字面反斜杠，去重从未生效。
                        text = re.sub(rf"(?m)^[ \t]*{re.escape(rendered)}[ \t]*(?:\n|$)", "", text)
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

    def _fit_channel_budget(self, result: InjectionResult, cfg: Any) -> bool:
        """把单个通道收进自己的 ``token_budget``；返回 False 表示本轮整段跳过。

        先按条目（``items[].rendered_text``）从后往前丢，再退到按行从后往前丢；
        标签、标题等结构行保留，至少留一条内容。截断或跳过都写进 ``warnings``，
        丢掉的条目记入 ``filtered``（``filter_reason=channel_token_budget``），观测台可见。
        safety 通道的身份守卫不参与单通道预算。
        """
        if cfg is None or result.channel == "safety":
            return True
        budget = max(int(getattr(cfg, "token_budget", 0) or 0), 0)
        tokens = estimate_injection_tokens(result.text)
        if tokens <= budget:
            return True
        fitted = _truncate_to_budget(result.text, list(result.items or []), budget)
        if fitted is None:
            result.status = "skipped"
            result.warnings.append(
                f"channel_token_budget_exceeded：本通道约 {tokens} token，超过自身预算 {budget}，"
                "无法按条目或行安全截断，本轮整段跳过"
            )
            logger.warning(
                "[WaveMemory] 注入通道 %s 超出自身预算（约 %d > %d token），无法安全截断，本轮跳过",
                result.channel,
                tokens,
                budget,
            )
            return False
        text, kept_items, dropped_items, unit, dropped_count = fitted
        result.text = text
        result.items = kept_items
        result.tokens = estimate_injection_tokens(text)
        result.chars = len(text)
        result.filtered = [
            *(result.filtered or []),
            *(
                {**item, "filter_reason": "channel_token_budget", "filter_channel": result.channel}
                for item in dropped_items
            ),
        ]
        result.warnings.append(
            f"channel_token_budget_truncated：本通道约 {tokens} token，超过自身预算 {budget}，"
            f"已按{unit}截断到约 {result.tokens} token，丢弃 {dropped_count} {'条' if unit == '条目' else '行'}"
        )
        logger.debug("[WaveMemory] 注入通道 %s 按%s截断到自身预算 %d token", result.channel, unit, budget)
        return True

    @staticmethod
    def _normalize_dedupe_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
        return re.sub(r"\s+", " ", normalized).strip()

    def _make_text_part(self, text: str) -> Any:
        if self.text_part_factory:
            return self.text_part_factory(text)
        try:
            from astrbot.core.agent.message import TextPart
            return TextPart(text=text)
        except Exception:
            return {"type": "text", "text": text}
