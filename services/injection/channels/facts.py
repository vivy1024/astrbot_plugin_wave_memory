"""Facts 关键词召回注入通道。"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from ...identity_safety import is_identity_contamination
from ...fact_policy import is_effective
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


def _importance(provenance: Any) -> int:
    try:
        value = int(float(_mapping(provenance).get("importance", 3)))
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, value))


def _row_to_fact(row: Mapping[str, Any], *, now: float, decay_rate: float) -> dict[str, Any]:
    """Normalize a scoped repository DTO into the fact shape used by this channel."""
    confidence = float(row.get("confidence") if row.get("confidence") is not None else 1.0)
    anchor = row.get("updated_at") or row.get("created_at") or now
    try:
        age_days = max(0.0, (now - float(anchor)) / 86400.0)
    except (TypeError, ValueError):
        age_days = 0.0
    decay = max(0.1, 1.0 - age_days * decay_rate) if decay_rate > 0 else 1.0
    importance = _importance(row.get("provenance"))
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
        # 重要性（1-5，默认 3）只调整先后：3 不变，5 为 ×1.4，1 为 ×0.6；不过滤任何事实
        "importance": importance,
        "effective_confidence": confidence * decay * (1.0 + 0.2 * (importance - 3)),
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
    # build 里没有 await、全是同步读库：编排器放到工作线程跑，不占事件循环
    offload_to_thread = True

    # 已知实体名缓存：{bot_id: (加载时间, 实体名列表)}
    _ENTITY_TTL_SECONDS = 120.0

    def __init__(self, *, db: Any, facts_decay_rate: float = 0.005):
        self.db = db
        self.facts_decay_rate = facts_decay_rate
        self._entity_cache: dict[str, tuple[float, list[str]]] = {}

    def _known_entities(self, scope: Any) -> list[str]:
        """当前 Bot 事实里出现过的实体名（新到旧）。分词会把少见人名切碎，这里按原样子串匹配。"""
        bot_id = str(scope.bot_id)
        cache_key = f"{bot_id}|{scope.visibility}|{scope.session.id if scope.visibility == 'private' else ''}"
        cached = self._entity_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < self._ENTITY_TTL_SECONDS:
            return cached[1]
        names: list[str] = []
        repo = getattr(self.db, "scoped_knowledge", None)
        cm = getattr(repo, "cm", None)
        if cm is not None:
            try:
                source_clause = "visibility='group'"
                source_params = [bot_id]
                if scope.visibility == "private":
                    source_clause += " OR (visibility='private' AND session_id=?)"
                    source_params.append(scope.session.id)
                rows = cm.execute_read(
                    f"""SELECT DISTINCT subject FROM scoped_facts WHERE bot_id=? AND ({source_clause})
                        AND status IN ('active','approved') LIMIT 5000""", source_params,
                ).fetchall()
                names.extend(str(row[0]) for row in rows if row and row[0])
            except Exception:
                pass
        unique = list(dict.fromkeys(name.strip() for name in names if len(name.strip()) >= 2))
        self._entity_cache[cache_key] = (now, unique)
        return unique

    def _message_keywords(self, ctx: Any, scope: Any) -> list[str]:
        """消息里按原样认出的已知实体名优先，其余为分词关键词。"""
        message = str(getattr(ctx, "message", "") or "")
        entities = [name for name in self._known_entities(scope) if name in message][:8]
        return list(dict.fromkeys([*entities, *_keywords(message)]))

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
            keywords = self._message_keywords(ctx, scope)
            if not keywords:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no fact keywords")
            fetch_limit = max(max_items * 12, 100)
            rows: list[Mapping[str, Any]] = []
            if repo is not None:
                # 当前场合（群或私聊）自己的事实；私聊事实只在该私聊出现。
                try:
                    rows = list(repo.list_scoped_facts(scope, limit=fetch_limit) or [])
                except Exception:
                    rows = []
                # 事实不分群：同一 Bot 在其他群得知的事实同样成立，私聊里也认得；按关键词在库内过滤。
                lister = getattr(repo, "list_bot_scoped_facts", None)
                if callable(lister):
                    try:
                        seen_ids = {row.get("id") for row in rows}
                        for row in lister(scope, keywords=keywords, limit=fetch_limit) or []:
                            if row.get("id") not in seen_ids:
                                seen_ids.add(row.get("id"))
                                rows.append(row)
                    except Exception:
                        pass
            now = float(getattr(ctx, "now", 0.0) or time.time())
            # Repository DTOs may omit ownership after a scoped query. If they
            # include it, reject mismatches before either retrieval path.
            rows = [row for row in rows if self._scope_matches(row, scope)]
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

    @staticmethod
    def _is_fact_eligible(row: Mapping[str, Any], now: float | None = None) -> bool:
        """统一事实准入过滤：排查 status、review_status 与 relation。

        适用于 primary 查询与 one-hop 联想，确保已拒绝、未审核、冲突或已归档事实
        不会通过任何一条召回路径进入注入。
        """
        if not is_effective(row, time.time() if now is None else now):
            return False
        review_status = str(row.get("review_status") or "").strip().lower()
        if review_status in {"pending", "rejected", "quarantined", "conflict"}:
            return False
        relation = str(row.get("relation") or "").strip().lower()
        if relation in {"conflicts"}:
            return False
        return True

    @staticmethod
    def _scope_matches(row: Mapping[str, Any], scope: Any) -> bool:
        if "bot_id" in row and row["bot_id"] != scope.bot_id:
            return False
        if row.get("visibility") == "private":
            return scope.visibility == "private" and row.get("session_id") == scope.session.id
        return row.get("visibility") in (None, "group")

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
            if (self._is_fact_eligible(row, now)
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
            and self._is_fact_eligible(row, now)
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


    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["FactsChannel"]
