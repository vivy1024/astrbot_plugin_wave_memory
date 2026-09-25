"""BookLore 书设知识注入通道。

书设是独立 Catalog 知识库，不是 Learning projection。
注入只读 raw book_lore.db + 社区向量索引，不经 reviewed_book_lore_projections。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from .safety import is_channel_allowed_in_mode

try:
    from ....domain.bot_profile import bot_catalog_scope
    from ....domain.scope import CatalogScope, validate_formal_command_scope
    from ....engine.external_book_lore import ExternalBookLoreStore
except ImportError:  # pragma: no cover - direct services imports in isolated tests
    from domain.bot_profile import bot_catalog_scope
    from domain.scope import CatalogScope, validate_formal_command_scope
    from engine.external_book_lore import ExternalBookLoreStore


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


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("book_lore", {}))


def _preview(text: str | None, limit: int = 160) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class BookLoreChannel:
    """复用 BookLore 社区向量检索的世界知识通道。"""

    name = "book_lore"

    def __init__(
        self,
        *,
        book_lore_index: Any = None,
        embedding_service: Any = None,
        lore_db_path: str = "",
        catalog_scope: CatalogScope | None = None,
        lore_store: ExternalBookLoreStore | None = None,
        # 兼容旧装配参数；正式路径不再读取 reviewed projection。
        projection_repository: Any = None,
        # bot_id → BotProfile；按 Profile 的书设语料决定这个 Bot 读不读书设。
        profile_lookup: Callable[[str], Any] | None = None,
    ):
        self.book_lore_index = book_lore_index
        self.embedding_service = embedding_service
        self.lore_db_path = lore_db_path
        self.catalog_scope = catalog_scope
        self.lore_store = lore_store
        self.projection_repository = projection_repository
        self.profile_lookup = profile_lookup

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"book_lore channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="book_lore channel disabled by config")
        catalog_scope, reason = bot_catalog_scope(self._profile(ctx), self.catalog_scope)
        if reason:
            return InjectionResult.disabled(self.name, reason=reason)
        if not isinstance(catalog_scope, CatalogScope):
            return InjectionResult.disabled(self.name, reason="catalog_scope_required")
        scope_decision = validate_formal_command_scope("catalog.read", catalog_scope)
        if not scope_decision.allowed:
            return InjectionResult.disabled(
                self.name,
                reason=scope_decision.reason_code or "catalog_scope_required",
            )
        if not self.book_lore_index or not self.embedding_service or (
            not self.lore_store and not self.lore_db_path
        ):
            return InjectionResult.empty(self.name, reason="book_lore dependencies unavailable")

        top_k = _as_int(cfg.get("top_k"), 1)
        min_score = _as_float(cfg.get("min_score"), 0.35)
        if top_k <= 0:
            return InjectionResult.empty(self.name, reason="book_lore top_k is zero")

        try:
            vector = await self.embedding_service.get_embedding(getattr(ctx, "message", "") or "")
            if vector is None:
                return InjectionResult.empty(
                    self.name,
                    latency_ms=self._latency_ms(started),
                    reason="embedding unavailable",
                )
            hits = self.book_lore_index.search_communities(vector, k=top_k) or []
            selected: list[dict[str, Any]] = []
            filtered: list[dict[str, Any]] = []
            if hits:
                store = self.lore_store or ExternalBookLoreStore(self.lore_db_path)
                rows = store.communities_by_ids([cid for cid, _ in hits], scope=catalog_scope)
                by_id = {str(row.get("id")): row for row in rows}
                for cid, score in hits:
                    score = float(score or 0.0)
                    if score < min_score:
                        filtered.append({
                            "community_id": cid,
                            "score": score,
                            "filter_reason": "min_score",
                            "filter_channel": "book_lore",
                        })
                        continue
                    row = by_id.get(str(cid))
                    if not row:
                        filtered.append({
                            "community_id": cid,
                            "score": score,
                            "filter_reason": "missing_community",
                            "filter_channel": "book_lore",
                        })
                        continue
                    item = {
                        "community_id": cid,
                        "score": score,
                        "title": row.get("title") or "",
                        "summary": row.get("summary") or "",
                    }
                    if is_identity_contamination(f"{item['title']} {item['summary']}"):
                        filtered.append({
                            **item,
                            "filter_reason": "identity_contamination",
                            "filter_channel": "book_lore",
                        })
                        continue
                    selected.append(item)

            if not selected:
                result = InjectionResult.empty(
                    self.name,
                    latency_ms=self._latency_ms(started),
                    reason="no book lore hits",
                )
                result.filtered = [self._audit_filtered(item) for item in filtered]
                return result

            lines = ["<world_knowledge>"]
            for item in selected:
                lines.append(f"{item['title']}：{str(item['summary'])[:300]}")
            lines.append("</world_knowledge>")
            return InjectionResult.hit(
                self.name,
                "\n".join(lines),
                items=[self._audit_item(item) for item in selected],
                filtered=[self._audit_filtered(item) for item in filtered],
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    def _profile(self, ctx: Any) -> Any:
        if self.profile_lookup is None:
            return None
        try:
            return self.profile_lookup(str(getattr(ctx, "bot_profile_id", "") or ""))
        except Exception:
            return None

    @staticmethod
    def _audit_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "community_id": item.get("community_id"),
            "title": item.get("title", ""),
            "score": item.get("score"),
            "preview": _preview(item.get("summary", "")),
        }

    @staticmethod
    def _audit_filtered(item: Mapping[str, Any]) -> dict[str, Any]:
        payload = BookLoreChannel._audit_item(item)
        payload["filter_reason"] = item.get("filter_reason", "filtered")
        payload["filter_channel"] = item.get("filter_channel", "book_lore")
        return payload

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["BookLoreChannel"]
