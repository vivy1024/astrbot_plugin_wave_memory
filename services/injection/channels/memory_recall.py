"""主记忆召回注入通道。"""

from __future__ import annotations

try:
    from ....domain import bot_identity
except ImportError:  # top-level import in isolated tests
    from domain import bot_identity

import time
from collections.abc import Mapping
from typing import Any

from ..channel_base import InjectionResult
from ..query_gate import should_skip_retrieval
from .safety import SafetyChannel, is_channel_allowed_in_mode

# Align with MessageWriter.classify_source: ordinary group chat is "chat".
# Excluding chat made the memory channel look empty despite stored rows.
_DEFAULT_SOURCE_FILTER = [
    "core",
    "chat",
    "evolution",
    "experience",
    "lore",
    "book_lore",
]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _get_channel_cfg(ctx: Any) -> Mapping[str, Any]:
    per_call = _mapping(getattr(ctx, "channel_options", {}))
    if "memory" in per_call:
        return _mapping(per_call.get("memory"))
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("memory", {}))


def _get_recall_cfg(ctx: Any) -> Mapping[str, Any]:
    return _mapping(_mapping(getattr(ctx, "config", {})).get("memory_recall", {}))


def _as_bool(value: Any, default: bool = False) -> bool:
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


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _preview(text: str | None, limit: int = 120) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _audit_item(memory: Mapping[str, Any]) -> dict[str, Any]:
    item = {
        "id": memory.get("id"),
        "source": memory.get("source", "live"),
        "score": memory.get("score"),
        "similarity": memory.get("similarity"),
        "preview": _preview(memory.get("content") or memory.get("summary") or ""),
    }
    if "timestamp" in memory:
        item["timestamp"] = memory.get("timestamp")
    if "group_id" in memory:
        item["group_id"] = memory.get("group_id")
    # Align with FTS audit: keep grant/cross markers for observability only.
    if memory.get("_shared_grant") or memory.get("shared_grant"):
        item["_shared_grant"] = True
    if memory.get("_is_cross_group") or memory.get("is_cross_group"):
        item["_is_cross_group"] = True
    if memory.get("_fanout_duplicate") or memory.get("fanout_duplicate"):
        item["_fanout_duplicate"] = True
    return item


def _filter_recent_timestamp(memories: list[dict[str, Any]], ctx: Any, minutes: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if minutes <= 0:
        return memories, []
    now = float(getattr(ctx, "now", 0.0) or 0.0)
    if now <= 0:
        return memories, []
    threshold = now - minutes * 60
    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for memory in memories:
        ts = memory.get("timestamp")
        if isinstance(ts, (int, float)) and ts >= threshold:
            item = dict(memory)
            item["filter_reason"] = "recent_timestamp"
            item["filter_channel"] = "memory"
            filtered.append(item)
        else:
            kept.append(memory)
    return kept, filtered


def _filter_min_score(
    memories: list[dict[str, Any]],
    min_score: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if min_score is None:
        return memories, []
    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for memory in memories:
        try:
            score = float(memory.get("similarity", memory.get("score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            item = dict(memory)
            item["filter_reason"] = "channel_min_score"
            item["filter_channel"] = "memory"
            filtered.append(item)
        else:
            kept.append(memory)
    return kept, filtered


class MemoryRecallChannel:
    """复用 QueryEngine 的主记忆召回通道。"""

    name = "memory"

    def __init__(self, *, query_engine: Any, safety_channel: SafetyChannel | None = None):
        self.query_engine = query_engine
        self.safety = safety_channel or SafetyChannel()

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"memory channel disabled in {mode} mode")

        channel_cfg = _get_channel_cfg(ctx)
        recall_cfg = _get_recall_cfg(ctx)
        top_k = _as_int(channel_cfg.get("top_k"), 5)
        if top_k <= 0:
            return InjectionResult.empty(self.name, reason="top_k is zero")
        # Short functional utterances have no retrieval target; recalling for them
        # only spends budget and increases misattribution risk.
        skip, skip_reason = should_skip_retrieval(ctx)
        if skip:
            return InjectionResult.empty(
                self.name, latency_ms=self._latency_ms(started), reason=skip_reason
            )

        try:
            memories = await self._query(ctx, top_k=top_k, recall_cfg=recall_cfg)
            memories = [dict(memory) for memory in memories or []]
            memories, score_filtered = _filter_min_score(memories, _as_float(channel_cfg.get("min_score")))
            memories, recent_ts_filtered = _filter_recent_timestamp(
                memories,
                ctx,
                _as_int(recall_cfg.get("skip_recent_minutes"), _as_int(_mapping(getattr(ctx, "config", {})).get("recent_dedup_minutes"), 0)),
            )
            memories, safety_filtered = self.safety.filter_items(memories, ctx=ctx)
            filtered = [_audit_filtered(item) for item in [*score_filtered, *recent_ts_filtered, *safety_filtered]]
            if not memories:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no safe memories")

            text = self._format(ctx, memories)
            if not text:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="formatted memory text is empty")
            return InjectionResult.hit(
                self.name,
                text,
                items=[_audit_item(memory) for memory in memories],
                filtered=filtered,
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    def _format(self, ctx: Any, memories: list[dict[str, Any]]) -> str:
        """Render recalled memories with ownership attribution when supported.

        Attribution needs the current speaker and the bot identities.  Query
        engines that predate the extra parameters (and test doubles) keep working
        through the plain call below.
        """
        group_id = getattr(ctx, "group_id", "") or ""
        bot_ids = {
            str(value).strip()
            for value in (
                getattr(ctx, "bot_id", "") or "",
                getattr(ctx, "bot_profile_id", "") or "",
            )
            if str(value or "").strip()
        }
        try:
            return self.query_engine.format_injection(
                memories,
                current_group_id=group_id,
                speaker_id=str(getattr(ctx, "sender_id", "") or ""),
                bot_ids=bot_ids,
            )
        except TypeError:
            return self.query_engine.format_injection(
                memories, current_group_id=group_id
            )

    async def _query(self, ctx: Any, *, top_k: int, recall_cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
        enable_shotgun = _as_bool(recall_cfg.get("enable_shotgun"), False)
        query_options = getattr(ctx, "query_options", None)
        query_collector = getattr(ctx, "query_collector", None)
        dry_run = bool(getattr(ctx, "dry_run", False))
        if dry_run and query_options is None:
            try:
                from engine.query_engine import QueryOptions
            except ImportError:  # pragma: no cover - AstrBot 包导入路径
                from ....engine.query_engine import QueryOptions
            query_options = QueryOptions(touch=False)

        # shotgun_query 尚无请求级 touch 接口；dry-run 必须走 batch6 正式 query
        # 的 QueryOptions(touch=False)，不能以访问计数写入换取“更像生产”的假预览。
        if enable_shotgun and not dry_run:
            context_messages = recall_cfg.get("context_messages") or getattr(ctx, "recent_context", []) or []
            return await self.query_engine.shotgun_query(
                text=getattr(ctx, "message", ""),
                context_messages=list(context_messages),
                group_id=getattr(ctx, "group_id", None),
                top_k=top_k,
                scope=getattr(ctx, "scope", None),
            )

        exclude_sources = recall_cfg.get("exclude_sources")
        source_filter = recall_cfg.get("source_filter")
        if source_filter is None and not exclude_sources:
            source_filter = list(_DEFAULT_SOURCE_FILTER) + sorted(bot_identity.experience_sources())
        return await self.query_engine.query(
            text=getattr(ctx, "message", ""),
            group_id=getattr(ctx, "group_id", None),
            top_k=top_k,
            exclude_sources=exclude_sources,
            source_filter=source_filter,
            scope=getattr(ctx, "scope", None),
            options=query_options,
            collector=query_collector,
        )

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


def _audit_filtered(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = _audit_item(item)
    payload["filter_reason"] = item.get("filter_reason", "filtered")
    payload["filter_channel"] = item.get("filter_channel", "memory")
    return payload


__all__ = ["MemoryRecallChannel"]
