"""Scoped Fact 的共享读侧实现。

事实列表现在有两处消费者：只读的 `/api/knowledge/facts` 与可审核的 `/api/facts`。
证据装配（哪些记忆算健康、content_hash 取哪一列、evidence_status 怎么判）必须只有
一份实现，否则两个页面对同一条事实会给出不同结论。
"""

from __future__ import annotations

import json
from typing import Any

try:
    from .api_contract import page_response
except ImportError:  # pragma: no cover - 插件根目录直接导入
    from api_contract import page_response


def columns(conn: Any, table: str) -> set[str]:
    """读取表的列名；表不存在时返回空集，调用方据此安全降级。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return set()
    return {str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def row_dicts(cursor: Any) -> list[dict[str, Any]]:
    names = [description[0] for description in cursor.description or []]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return default
        return parsed
    return default


def fact_evidence(conn: Any, items: list[dict[str, Any]], scope: tuple[str, str, str]) -> None:
    """就地补齐 provenance / evidence / evidence_status。

    证据只接受同一 Scope 内 resolution_state='resolved' 且未隔离的记忆；跨 Bot、跨群
    或已隔离的记忆一律判为不可溯源。
    """
    memory_ids = sorted({
        int(item["source_memory_id"])
        for item in items
        if str(item.get("source_memory_id") or "").strip().isdigit()
    })
    healthy_meta: dict[int, dict[str, Any]] = {}
    memory_columns = columns(conn, "memories")
    required = {"id", "bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
    if memory_ids and required <= memory_columns:
        placeholders = ",".join("?" for _ in memory_ids)
        cols_to_select = ["id"]
        for extra_col in ("content_hash", "timestamp", "created_at", "content"):
            if extra_col in memory_columns:
                cols_to_select.append(extra_col)
        select_sql = (
            f"SELECT {', '.join(cols_to_select)} FROM memories WHERE id IN ({placeholders}) "
            "AND bot_id=? AND session_id=? AND visibility=? "
            "AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0"
        )
        for row in conn.execute(select_sql, (*memory_ids, *scope)).fetchall():
            mid = int(row[0])
            row_dict = dict(zip(cols_to_select, row))
            content_str = str(row_dict.get("content") or "").strip()
            healthy_meta[mid] = {
                "content_hash": row_dict.get("content_hash"),
                "captured_at": row_dict.get("timestamp") or row_dict.get("created_at"),
                "summary": content_str[:120] if content_str else None,
            }

    for item in items:
        if "provenance" in item:
            item["provenance"] = json_value(item.get("provenance"), {})
        source_id = item.get("source_memory_id")
        try:
            source_id = int(source_id) if str(source_id or "").strip().isdigit() else None
        except (TypeError, ValueError):
            source_id = None
        if source_id is not None and source_id in healthy_meta:
            meta = healthy_meta[source_id]
            item["evidence"] = [{
                "type": "memory",
                "id": str(source_id),
                "source_scope": {
                    "bot_id": scope[0],
                    "session_id": scope[1],
                    "visibility": scope[2],
                },
                "availability": "available",
                "content_hash": meta.get("content_hash"),
                "captured_at": meta.get("captured_at"),
                "summary": meta.get("summary"),
            }]
        else:
            item["evidence"] = []
        item["evidence_status"] = "available" if item["evidence"] else "unavailable"


# 「建议批准 / 建议拒绝」只是给人工审核的提示，绝不自动改状态。
REVIEW_HINT_APPROVE_MIN_CONFIDENCE = 0.7
REVIEW_HINT_REJECT_MAX_CONFIDENCE = 0.5


def review_hint(item: dict[str, Any]) -> str:
    """按证据可溯源性与置信度给出审核建议，供 WebUI 标记排序使用。"""
    if str(item.get("status") or "") not in {"pending", "quarantined", "conflict"}:
        return ""
    confidence = item.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if item.get("evidence_status") == "available" and confidence is not None and confidence >= REVIEW_HINT_APPROVE_MIN_CONFIDENCE:
        return "suggest_approve"
    if item.get("evidence_status") != "available" and confidence is not None and confidence < REVIEW_HINT_REJECT_MAX_CONFIDENCE:
        return "suggest_reject"
    return "needs_review"


def fact_object_ref(item: dict[str, Any], scope: Any, registry: Any) -> dict[str, Any] | None:
    """签发事实变更用的 opaque ObjectRef；无注册表时返回 None（能力显示为不可用）。"""
    if registry is None:
        return None
    revision = fact_revision(item)
    locator = int(item["id"])
    ref = registry.issue(kind="fact", locator=locator, scope=scope, revision=revision)
    return {
        "ref": ref,
        "kind": "fact",
        "locator": locator,
        "scope_key": scope.session.id if scope.session else scope.bot_id,
        "scope_query": {
            "bot_id": scope.bot_id,
            "session_id": scope.session.id if scope.session else None,
            "visibility": scope.visibility,
        },
        "version": revision,
    }


def fact_revision(item: dict[str, Any]) -> int:
    """事实对象使用 scoped_facts 数据库行自身的单调递增 revision 进行 CAS 版本校验。"""
    try:
        rev = int(item.get("revision", 1))
        return max(1, rev)
    except (TypeError, ValueError):
        return 1


def facts_page_response(items: list[dict[str, Any]], *, total: int, limit: int, offset: int) -> Any:
    return page_response(items, total=total, limit=limit, offset=offset)


__all__ = [
    "columns",
    "fact_evidence",
    "fact_object_ref",
    "fact_revision",
    "facts_page_response",
    "json_value",
    "review_hint",
    "row_dicts",
]
