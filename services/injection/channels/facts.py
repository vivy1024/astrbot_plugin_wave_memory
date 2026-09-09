"""Facts 关键词召回注入通道。"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
try:
    from ....domain.scope import validate_formal_command_scope
except ImportError:  # pragma: no cover - direct package imports in isolated tests
    from domain.scope import validate_formal_command_scope
from ..channel_base import InjectionResult, estimate_injection_tokens
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


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("facts", {}))


def _fact_line(fact: Mapping[str, Any]) -> str:
    return f"{fact.get('subject', '')} {fact.get('predicate', '')} {fact.get('object', '')}".strip()


def _keywords(message: str, limit: int = 8) -> list[str]:
    words: list[str] = []
    try:
        import jieba  # type: ignore

        try:
            jieba.setLogLevel(40)
        except Exception:
            pass
        candidates = jieba.cut(message or "")
    except Exception:  # pragma: no cover - jieba 缺失时兜底
        candidates = re.findall(r"[\w\u4e00-\u9fff]{2,}", message or "")
    for word in candidates:
        token = str(word or "").strip()
        if len(token) >= 2 and token not in words:
            words.append(token)
        if len(words) >= limit:
            break
    return words


def _row_to_fact(row: Mapping[str, Any], *, now: float, decay_rate: float) -> dict[str, Any]:
    """Normalize the scoped repository DTO without exposing a legacy table row."""
    confidence = float(row.get("confidence") if row.get("confidence") is not None else 1.0)
    anchor = row.get("updated_at") or row.get("created_at") or now
    try:
        age_days = max(0.0, (now - float(anchor)) / 86400.0)
    except (TypeError, ValueError):
        age_days = 0.0
    decay = max(0.1, 1.0 - age_days * decay_rate) if decay_rate > 0 else 1.0
    return {
        "rowid": row.get("id"),
        "subject": str(row.get("subject") or ""),
        "predicate": str(row.get("predicate") or ""),
        "object": str(row.get("object") or ""),
        "confidence": confidence,
        "status": row.get("status"),
        "relation": row.get("relation", "compatible"),
        "review_status": row.get("review_status", "approved"),
        "valid_from": row.get("valid_from"),
        "valid_until": row.get("valid_until"),
        "provenance": row.get("provenance") if isinstance(row.get("provenance"), Mapping) else {},
        "last_reinforced": row.get("updated_at"),
        "created_at": row.get("created_at"),
        "effective_confidence": confidence * decay,
    }


def _audit_fact(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rowid": fact.get("rowid"),
        "subject": fact.get("subject", ""),
        "predicate": fact.get("predicate", ""),
        "object": fact.get("object", ""),
        "confidence": fact.get("confidence"),
        "effective_confidence": fact.get("effective_confidence"),
        "relation": fact.get("relation"),
        "review_status": fact.get("review_status"),
        "source_tags": (fact.get("provenance") or {}).get("source_tags", []),
        "evidence": (fact.get("provenance") or {}).get("evidence", {}),
        "query_trace_id": (fact.get("provenance") or {}).get("query_trace_id", ""),
        "rendered_text": _fact_line(fact),
        "dedupe_key": "fact|" + "|".join(str(fact.get(key) or "") for key in ("subject", "predicate", "object", "valid_from", "valid_until", "relation")),
        "preview": _fact_line(fact),
    }


class FactsChannel:
    """基于当前 RuntimeScope 的 scoped facts 进行关键词召回。"""

    name = "facts"

    def __init__(self, *, db: Any, facts_decay_rate: float = 0.005):
        self.db = db
        self.facts_decay_rate = facts_decay_rate

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"facts channel disabled in {mode} mode")

        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="facts channel disabled by config")
        max_items = _as_int(cfg.get("max_items"), 5)
        token_budget = _as_int(cfg.get("token_budget"), 260)
        if max_items <= 0:
            return InjectionResult.empty(self.name, reason="facts max_items is zero")

        scope = getattr(ctx, "scope", None)
        scope_decision = validate_formal_command_scope("fact.read", scope)
        if not scope_decision.allowed:
            return InjectionResult.empty(
                self.name,
                latency_ms=self._latency_ms(started),
                reason=scope_decision.reason_code or "scope_rejected",
            )
        repo = getattr(self.db, "scoped_knowledge", None)

        try:
            keywords = _keywords(str(getattr(ctx, "message", "") or ""))
            if not keywords:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no fact keywords")
            fetch_limit = max(max_items * 12, 100)
            rows: list[Mapping[str, Any]] = []
            if repo is not None:
                try:
                    rows = list(repo.list_scoped_facts(scope, limit=fetch_limit) or [])
                except Exception:
                    rows = []
            if len(rows) < fetch_limit:
                rows.extend(self._legacy_fact_rows(ctx, scope, limit=fetch_limit - len(rows)))
            now = float(getattr(ctx, "now", 0.0) or time.time())
            facts, filtered = self._query_primary(rows, keywords=keywords, now=now)
            facts, budget_filtered = self._select_with_budget(facts, max_items=max_items, token_budget=token_budget)
            filtered.extend(budget_filtered)
            if facts:
                extra = self._query_one_hop(rows, facts, now=now, max_items=max_items - len(facts))
                extra, extra_budget_filtered = self._select_with_budget(
                    extra,
                    max_items=max_items - len(facts),
                    token_budget=max(0, token_budget - sum(estimate_injection_tokens(_fact_line(f)) for f in facts)),
                )
                facts.extend(extra)
                filtered.extend(extra_budget_filtered)
            if not facts:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no safe facts")
            lines = [_fact_line(fact) for fact in facts]
            text = "<known_facts>\n" + "\n".join(lines) + "\n</known_facts>"
            return InjectionResult.hit(
                self.name,
                text,
                items=[_audit_fact(fact) for fact in facts],
                filtered=[self._audit_filtered(fact) for fact in filtered],
                latency_ms=self._latency_ms(started),
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    def _query_primary(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        keywords: list[str],
        now: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        lowered_keywords = tuple(keyword.casefold() for keyword in keywords)
        facts = [
            _row_to_fact(row, now=now, decay_rate=self.facts_decay_rate)
            for row in rows
            if (row.get("status") in (None, "active", "reviewed", "approved")
            and row.get("review_status") not in ("pending", "rejected")
            and row.get("relation") not in ("conflicts",)
            and any(
                keyword in str(row.get("subject") or "").casefold()
                or keyword in str(row.get("object") or "").casefold()
                for keyword in lowered_keywords
            ))
        ]
        facts.sort(key=lambda fact: fact.get("effective_confidence", 0.0), reverse=True)
        unique: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for fact in facts:
            key = (fact.get("subject"), fact.get("predicate"), fact.get("object"), fact.get("valid_from"), fact.get("valid_until"), fact.get("relation"))
            if key not in seen:
                seen.add(key)
                unique.append(fact)
        return self._filter_identity(unique)

    def _query_one_hop(
        self,
        rows: Iterable[Mapping[str, Any]],
        facts: list[dict[str, Any]],
        *,
        now: float,
        max_items: int,
    ) -> list[dict[str, Any]]:
        if max_items <= 0 or not facts:
            return []
        hit_rowids = {fact["rowid"] for fact in facts if fact.get("rowid") is not None}
        entities = {
            str(value)
            for fact in facts
            for value in (fact.get("subject"), fact.get("object"))
            if value
        }
        if not hit_rowids or not entities:
            return []
        extras = [
            _row_to_fact(row, now=now, decay_rate=self.facts_decay_rate)
            for row in rows
            if row.get("id") not in hit_rowids
            and (str(row.get("subject") or "") in entities or str(row.get("object") or "") in entities)
        ]
        extras.sort(key=lambda fact: fact.get("effective_confidence", 0.0), reverse=True)
        return [
            fact for fact in extras
            if not is_identity_contamination(_fact_line(fact))
        ][:max_items]

    @staticmethod
    def _filter_identity(facts: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        kept: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for fact in facts:
            if is_identity_contamination(_fact_line(fact)):
                item = dict(fact)
                item["filter_reason"] = "identity_contamination"
                item["filter_channel"] = "facts"
                filtered.append(item)
            else:
                kept.append(fact)
        return kept, filtered

    @staticmethod
    def _select_with_budget(
        facts: list[dict[str, Any]],
        *,
        max_items: int,
        token_budget: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        selected: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        if token_budget <= 0:
            for fact in facts:
                item = dict(fact)
                item["filter_reason"] = "token_budget"
                item["filter_channel"] = "facts"
                filtered.append(item)
            return selected, filtered
        used_tokens = 0
        for fact in facts:
            if len(selected) >= max_items:
                item = dict(fact)
                item["filter_reason"] = "max_items"
                item["filter_channel"] = "facts"
                filtered.append(item)
                continue
            line_tokens = estimate_injection_tokens(_fact_line(fact))
            if selected and token_budget >= 0 and used_tokens + line_tokens > token_budget:
                item = dict(fact)
                item["filter_reason"] = "token_budget"
                item["filter_channel"] = "facts"
                filtered.append(item)
                continue
            selected.append(fact)
            used_tokens += line_tokens
        return selected, filtered

    @staticmethod
    def _audit_filtered(fact: Mapping[str, Any]) -> dict[str, Any]:
        payload = _audit_fact(fact)
        payload["filter_reason"] = fact.get("filter_reason", "filtered")
        payload["filter_channel"] = fact.get("filter_channel", "facts")
        return payload

    def _legacy_fact_rows(self, ctx: Any, scope: Any, *, limit: int) -> list[dict[str, Any]]:
        """Read-only fallback to the existing facts table. Does not copy rows."""
        if limit <= 0:
            return []
        conn = getattr(self.db, "conn", None)
        if conn is None or not hasattr(conn, "execute"):
            return []
        group_id = ""
        session = getattr(scope, "session", None)
        if session is not None:
            group_id = str(getattr(session, "conversation_id", "") or "")
        if not group_id:
            group_id = str(getattr(ctx, "group_id", "") or "")
        sender_id = str(getattr(ctx, "sender_id", "") or "").strip()
        sender_name = str(getattr(ctx, "sender_name", "") or "").strip()
        keywords = _keywords(str(getattr(ctx, "message", "") or ""), limit=6)
        now = float(getattr(ctx, "now", 0.0) or time.time())
        clauses = [
            "COALESCE(fact_type, '') != 'QUARANTINED_ROLEPLAY'",
            "(valid_until IS NULL OR valid_until > ?)",
        ]
        params: list[Any] = [now]
        if group_id:
            clauses.append("COALESCE(group_id, '') = ?")
            params.append(group_id)
        match_clauses: list[str] = []
        if sender_id:
            match_clauses.append("subject = ?")
            params.append(sender_id)
        if sender_name:
            match_clauses.append("subject = ?")
            params.append(sender_name)
        for token in keywords:
            like = f"%{token}%"
            match_clauses.append("(subject LIKE ? OR object LIKE ?)")
            params.extend((like, like))
        if match_clauses:
            clauses.append(f"({' OR '.join(match_clauses)})")
        sql = f"""SELECT id, subject, predicate, object, confidence, created_at, last_reinforced, fact_type
                    FROM facts
                   WHERE {' AND '.join(clauses)}
                   ORDER BY confidence DESC, id DESC
                   LIMIT ?"""
        params.append(limit)
        try:
            fetched = conn.execute(sql, tuple(params)).fetchall()
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        for row in fetched:
            rows.append({
                "id": row[0],
                "subject": row[1],
                "predicate": row[2],
                "object": row[3],
                "confidence": row[4],
                "created_at": row[5],
                "updated_at": row[6] or row[5],
                "status": "active",
                "review_status": "approved",
                "relation": "compatible",
                "provenance": {"source_table": "facts", "legacy": True},
            })
        return rows

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["FactsChannel"]
