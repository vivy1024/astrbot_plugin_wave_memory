"""FTS5 精确检索注入通道。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

try:
    from ....domain.scope import RuntimeScope
except ImportError:  # 兼容独立测试/插件顶级加载
    from domain.scope import RuntimeScope

from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult, estimate_injection_tokens
from ..query_gate import should_skip_retrieval
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


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("fts5", {}))


def _memory_scope(ctx: Any) -> RuntimeScope | None:
    """仅接受完整、已解析的 group/private RuntimeScope。"""
    scope = getattr(ctx, "scope", None)
    if not isinstance(scope, RuntimeScope) or scope.visibility not in {"group", "private"}:
        return None
    if (
        scope.session is None
        or scope.session.kind != scope.visibility
        or not scope.bot_id
        or not scope.session.id
        or not scope.session.conversation_id
    ):
        return None
    return scope


def _keywords(message: str, limit: int = 6) -> list[str]:
    try:
        import jieba  # type: ignore

        try:
            jieba.setLogLevel(40)
        except Exception:
            pass
        candidates = jieba.cut(message or "")
    except Exception:  # pragma: no cover
        candidates = re.findall(r"[\w\u4e00-\u9fff]{2,}", message or "")
    words: list[str] = []
    for candidate in candidates:
        word = str(candidate or "").strip()
        if len(word) >= 2 and word not in words:
            words.append(word)
        if len(words) >= limit:
            break
    return words


def _match_expr(words: list[str]) -> str:
    quoted = []
    for word in words:
        safe = word.replace('"', ' ').strip()
        if safe:
            quoted.append(f'"{safe}"')
    return " OR ".join(quoted)


def _preview(text: str | None, limit: int = 160) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _audit_memory(memory: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "id": memory.get("id"),
        "source": memory.get("source", "live"),
        "sender_id": memory.get("sender_id", ""),
        "sender_name": memory.get("sender_name", ""),
        "group_id": memory.get("group_id", ""),
        "score": memory.get("score"),
        "preview": _preview(memory.get("content", "")),
    }
    # Preserve grant/fanout markers for observability (read-only; not used for touch).
    if memory.get("_shared_grant") or memory.get("shared_grant"):
        payload["_shared_grant"] = True
    if memory.get("_fanout_duplicate") or memory.get("fanout_duplicate"):
        payload["_fanout_duplicate"] = True
    if memory.get("fanout_family_id") is not None:
        payload["fanout_family_id"] = memory.get("fanout_family_id")
    return payload


class FTS5Channel:
    """基于 fts_memories 的精确关键词召回通道。"""

    name = "fts5"

    def __init__(
        self,
        *,
        db: Any,
        cross_group_enabled: bool = True,
        shared_memory_grants_enabled: bool = False,
    ):
        self.db = db
        self.cross_group_enabled = _as_bool(cross_group_enabled, True)
        self.shared_memory_grants_enabled = _as_bool(shared_memory_grants_enabled, False)

    def _memory_columns(self) -> set[str]:
        try:
            return {
                str(row[1])
                for row in self.db.conn.execute("PRAGMA table_info(memories)").fetchall()
            }
        except Exception:
            return set()

    @staticmethod
    def _column_expr(columns: set[str], name: str, fallback: str = "''") -> str:
        return f"m.{name}" if name in columns else fallback

    def _grant_ids_for_scope(self, scope: RuntimeScope) -> list[int]:
        if scope.visibility == "private" or self.cross_group_enabled or not self.shared_memory_grants_enabled:
            return []
        if scope.session is None:
            return []
        try:
            from engine.shared_grant_recall import load_active_grant_memory_ids
        except ImportError:  # pragma: no cover
            from ....engine.shared_grant_recall import load_active_grant_memory_ids
        return load_active_grant_memory_ids(
            self.db,
            bot_id=scope.bot_id,
            session_id=scope.session.id,
            visibility=scope.visibility,
            group_id=scope.session.conversation_id,
        )

    def _scope_predicate(
        self,
        scope: RuntimeScope,
        *,
        columns: set[str],
        alias: str = "m",
        grant_ids: list[int] | None = None,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build one authorization predicate for both FTS and LIKE fallback.

        A group RuntimeScope authorizes global recall only when the explicit
        cross-group switch is on.  Fully-unscoped historical rows are included as
        a legacy compatibility lane, but partial Scope rows never qualify.

        When cross-group is off and shared grants are enabled, formal rows whose
        ids appear in the consumer's active grant list may also be read.
        """
        assert scope.session is not None
        prefix = f"{alias}." if alias else ""
        # Retrieval is not Scope-gated: any non-quarantined, non-deleted row is
        # searchable. Scope only narrows to current group when cross-group is off.
        active_base = f"""
            COALESCE({prefix}quarantine, 0) = 0
            AND COALESCE({prefix}memory_type, 'message') NOT IN
                ('archived', 'evicted', 'deleted', 'noise')
            AND COALESCE({prefix}source, '') != 'noise'
        """
        if scope.visibility == "private":
            # Private MATCH and LIKE fallback share this exact owner predicate;
            # grants/cross-group/legacy lanes are intentionally unreachable.
            required = {"bot_id", "session_id", "visibility", "group_id"}
            if not required <= columns:
                return "0=1", ()
            return (
                f"({active_base}) AND {prefix}bot_id = ? AND {prefix}session_id = ? "
                f"AND {prefix}visibility = ? AND COALESCE({prefix}group_id, '') = ?",
                (scope.bot_id, scope.session.id, "private", scope.session.conversation_id),
            )
        visibility_exclusion = (
            f"COALESCE({prefix}visibility, '') != 'private'"
            if "visibility" in columns else "1=1"
        )
        active = f"""{active_base}
            AND {visibility_exclusion}
            AND COALESCE({prefix}group_id, '') NOT LIKE 'private:%'
        """
        if self.cross_group_enabled:
            return f"({active})", ()
        current_group = scope.session.conversation_id
        local = f"({active}) AND COALESCE({prefix}group_id, '') = ?"
        params: list[Any] = [current_group]
        ids = list(grant_ids or [])
        if ids:
            try:
                from engine.shared_grant_recall import formal_grant_id_predicate
            except ImportError:  # pragma: no cover
                from ....engine.shared_grant_recall import formal_grant_id_predicate
            grant_sql, grant_params = formal_grant_id_predicate(ids, alias=alias)
            grant_safe = (
                f"{visibility_exclusion} "
                f"AND COALESCE({prefix}group_id, '') NOT LIKE 'private:%'"
            )
            return f"(({local}) OR (({grant_sql}) AND {grant_safe}))", tuple(params) + grant_params
        return local, tuple(params)

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"fts5 channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="fts5 channel disabled by config")
        top_k = _as_int(cfg.get("top_k"), 10)
        token_budget = _as_int(cfg.get("token_budget"), 350)
        min_score = _as_float(cfg.get("min_score"), 0.0)
        if top_k <= 0:
            return InjectionResult.empty(self.name, reason="fts5 top_k is zero")
        if _memory_scope(ctx) is None:
            return InjectionResult.empty(self.name, reason="fts5 requires resolved group/private RuntimeScope")
        # Generic short utterances match on filler words and crowd out real hits.
        skip, skip_reason = should_skip_retrieval(ctx)
        if skip:
            return InjectionResult.empty(
                self.name, latency_ms=self._latency_ms(started), reason=skip_reason
            )

        try:
            words = _keywords(getattr(ctx, "message", "") or "")
            if not words:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no exact keywords")
            expr = _match_expr(words)
            if not expr:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="empty match expression")
            memories = self._query_memories(ctx, expr=expr, words=words, top_k=top_k)
            try:
                from engine.memory_collapse import collapse_memories
            except ImportError:  # pragma: no cover - package import path
                from ....engine.memory_collapse import collapse_memories
            current_group_id = ""
            scope = _memory_scope(ctx)
            if scope is not None and scope.session is not None:
                current_group_id = scope.session.conversation_id
            memories = collapse_memories(memories, current_group_id=current_group_id)
            selected, filtered = self._filter_and_budget(memories, top_k=top_k, token_budget=token_budget, min_score=min_score)
            if not selected:
                result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no fts5 hits")
                result.filtered = [self._audit_filtered(item) for item in filtered]
                return result
            text = self._format(selected)
            return InjectionResult.hit(
                self.name,
                text,
                items=[_audit_memory(memory) for memory in selected],
                filtered=[self._audit_filtered(item) for item in filtered],
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    def _query_memories(self, ctx: Any, *, expr: str, words: list[str], top_k: int) -> list[dict[str, Any]]:
        scope = _memory_scope(ctx)
        if scope is None:  # build() guards this too; keep direct callers fail-closed.
            return []
        limit = max(20, top_k * 3)
        columns = self._memory_columns()
        if not {"id", "content", "group_id"} <= columns:
            return []
        if scope.visibility == "private" and not {"bot_id", "session_id", "visibility"} <= columns:
            return []
        grant_ids = self._grant_ids_for_scope(scope)
        predicate, params = self._scope_predicate(scope, columns=columns, grant_ids=grant_ids)
        visibility_expr = self._column_expr(columns, "visibility")
        origin_expr = (
            "COALESCE(m.origin_fingerprint, '')"
            if "origin_fingerprint" in columns else "''"
        )
        provenance_expr = (
            "COALESCE(m.provenance, '')" if "provenance" in columns else "''"
        )
        try:
            rows = self.db.conn.execute(
                f"""SELECT m.id, m.content, m.sender_id, m.sender_name, m.timestamp,
                           m.importance, m.source, m.group_id, m.memory_type, {visibility_expr},
                           {origin_expr}, {provenance_expr}
                      FROM fts_memories
                      JOIN memories AS m ON m.id = fts_memories.rowid
                     WHERE fts_memories MATCH ? AND {predicate}
                     ORDER BY rank LIMIT ?""",
                (expr, *params, limit),
            ).fetchall()
            if not rows:
                rows = self._scoped_like_search(
                    words=words, limit=limit, scope=scope, columns=columns, grant_ids=grant_ids
                )
        except Exception:
            # Missing schema or an invalid FTS expression fails closed; never fall
            # back to an unfiltered historical read outside the shared predicate.
            return []

        result: list[dict[str, Any]] = []
        for row in rows:
            provenance: dict[str, Any] = {}
            raw_prov = row[11] if len(row) > 11 else ""
            if isinstance(raw_prov, str) and raw_prov.strip():
                try:
                    loaded = json.loads(raw_prov)
                    if isinstance(loaded, dict):
                        provenance = loaded
                except Exception:
                    provenance = {}
            item = {
                "id": row[0], "content": row[1] or "", "sender_id": row[2],
                "sender_name": row[3], "timestamp": row[4], "importance": row[5],
                "source": row[6], "group_id": row[7], "memory_type": row[8],
                "visibility": row[9] if len(row) > 9 else "", "score": 1.0,
                "origin_fingerprint": row[10] if len(row) > 10 else "",
                "provenance": provenance,
            }
            if str(provenance.get("projection_kind") or "") == "fanout_duplicate":
                item["_fanout_duplicate"] = True
                item["fanout_family_id"] = provenance.get("fanout_family_id")
            result.append(item)
        if grant_ids and scope.session is not None:
            try:
                from engine.shared_grant_recall import tag_shared_grant_rows
            except ImportError:  # pragma: no cover
                from ....engine.shared_grant_recall import tag_shared_grant_rows
            result = tag_shared_grant_rows(
                result, grant_ids, current_group_id=scope.session.conversation_id
            )
        return result

    def _scoped_like_search(
        self,
        *,
        words: list[str],
        limit: int,
        scope: RuntimeScope,
        columns: set[str] | None = None,
        grant_ids: list[int] | None = None,
    ) -> list[tuple[Any, ...]]:
        if not words or scope.session is None:
            return []
        columns = columns if columns is not None else self._memory_columns()
        if scope.visibility == "private" and not {"bot_id", "session_id", "visibility"} <= columns:
            return []
        ids = list(grant_ids) if grant_ids is not None else self._grant_ids_for_scope(scope)
        predicate, scope_params = self._scope_predicate(scope, columns=columns, grant_ids=ids)
        conditions = " OR ".join(["m.content LIKE ?"] * len(words))
        params = [f"%{word}%" for word in words]
        visibility_expr = self._column_expr(columns, "visibility")
        origin_expr = "COALESCE(m.origin_fingerprint, '')" if "origin_fingerprint" in columns else "''"
        provenance_expr = "COALESCE(m.provenance, '')" if "provenance" in columns else "''"
        return self.db.conn.execute(
            f"""SELECT m.id, m.content, m.sender_id, m.sender_name, m.timestamp,
                       m.importance, m.source, m.group_id, m.memory_type, {visibility_expr},
                       {origin_expr}, {provenance_expr}
                  FROM memories AS m
                 WHERE {predicate} AND ({conditions})
                 ORDER BY m.timestamp DESC LIMIT ?""",
            [*scope_params, *params, limit],
        ).fetchall()

    @staticmethod
    def _filter_and_budget(
        memories: list[dict[str, Any]],
        *,
        top_k: int,
        token_budget: int,
        min_score: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        selected: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        used_tokens = 0
        for memory in memories:
            if memory.get("score", 0.0) < min_score:
                filtered.append({**memory, "filter_reason": "min_score", "filter_channel": "fts5"})
                continue
            if is_identity_contamination(memory.get("content", "")):
                filtered.append({**memory, "filter_reason": "identity_contamination", "filter_channel": "fts5"})
                continue
            if len(selected) >= top_k:
                filtered.append({**memory, "filter_reason": "top_k", "filter_channel": "fts5"})
                continue
            tokens = estimate_injection_tokens(memory.get("content", ""))
            if selected and token_budget >= 0 and used_tokens + tokens > token_budget:
                filtered.append({**memory, "filter_reason": "token_budget", "filter_channel": "fts5"})
                continue
            selected.append(memory)
            used_tokens += tokens
        return selected, filtered

    @staticmethod
    def _format(memories: list[dict[str, Any]]) -> str:
        lines = ["<exact_memory>"]
        for memory in memories:
            sender = memory.get("sender_name") or memory.get("sender_id") or "unknown"
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(memory.get("timestamp") or 0)))
            private = memory.get("visibility") == "private"
            location = "[原词命中]" if private else f"[原词命中][群 {memory.get('group_id') or 'unknown-group'}]"
            lines.append(
                f"{location} {sender}({ts}): {memory.get('content', '')} "
                f"(relevance: {float(memory.get('score') or 0):.2f})"
            )
        lines.append("</exact_memory>")
        return "\n".join(lines)

    @staticmethod
    def _audit_filtered(memory: Mapping[str, Any]) -> dict[str, Any]:
        payload = _audit_memory(memory)
        payload["filter_reason"] = memory.get("filter_reason", "filtered")
        payload["filter_channel"] = memory.get("filter_channel", "fts5")
        return payload

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["FTS5Channel"]
