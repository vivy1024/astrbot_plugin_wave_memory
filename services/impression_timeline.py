"""Append-only impression history helpers.

Current ``metadata.impression`` remains the latest pointer. History is stored in
``metadata.impression_history`` and never used as relationship events.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any


HISTORY_LIMIT = 20
LEDGER_LIMIT = 50
TRAJECTORY_LIMIT = 3
DIMENSION_KEYS = ("familiarity", "trust", "fun", "depth", "hostility")
MEANINGFUL_EVENT_TYPES = frozenset({
    "deep_talk",
    "bot_praised",
    "bot_attacked",
    "confession",
    "joke",
    "gift_or_feed",
    "ignored_boundary",
    "direct_reply",
    "correction",
    "relationship.manual_calibration",
})
NOISY_EVENT_TYPES = frozenset({"message_seen"})


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def snapshot_from_relationship(relationship: Mapping[str, Any] | None) -> dict[str, float]:
    payload = _as_mapping(relationship)
    values = _as_mapping(payload.get("values"))
    dimensions = _as_mapping(payload.get("dimensions"))
    snapshot: dict[str, float] = {}
    for key in DIMENSION_KEYS:
        item = _as_mapping(values.get(key))
        number = _finite(item.get("effective_value"))
        if number is None:
            number = _finite(dimensions.get(key))
        if number is not None:
            snapshot[key] = round(number, 1)
    affinity = _finite(payload.get("affinity"))
    if affinity is not None:
        snapshot["affinity"] = round(affinity, 1)
    return snapshot


def meaningful_event_anchor(history: Sequence[Any] | None) -> dict[str, Any] | None:
    for item in history or ():
        if not isinstance(item, Mapping):
            continue
        event_type = str(item.get("event_type") or "").strip()
        if event_type in NOISY_EVENT_TYPES:
            continue
        reason = str(item.get("reason") or "").strip().replace("\n", " ")
        if event_type not in MEANINGFUL_EVENT_TYPES and not reason:
            continue
        if event_type == "direct_reply" and (not reason or reason == "看见一条群友消息"):
            continue
        delta = _finite(item.get("delta"))
        anchor = {
            "event_type": event_type or "互动",
            "reason": reason[:80],
            "dimension": str(item.get("dimension") or "").strip(),
        }
        if delta is not None:
            anchor["delta"] = round(delta, 2)
        event_id = item.get("event_id") or item.get("id")
        if event_id not in {None, ""}:
            anchor["event_id"] = event_id
        return {key: value for key, value in anchor.items() if value not in {None, ""}}
    return None


def _context_fields(*, snapshot: Mapping[str, Any] | None, event: Mapping[str, Any] | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    cleaned_snapshot = {
        str(key): round(number, 1)
        for key, value in _as_mapping(snapshot).items()
        if (number := _finite(value)) is not None
    }
    if cleaned_snapshot:
        fields["snapshot"] = cleaned_snapshot
    cleaned_event = {
        str(key): value
        for key, value in _as_mapping(event).items()
        if value not in {None, ""}
    }
    if cleaned_event:
        fields["event"] = cleaned_event
    return fields


def synthesize_milestone_phrase(
    *,
    event_type: str,
    reason: str,
    before_affinity: int,
    after_affinity: int,
    dimension: str,
    delta: float,
) -> str:
    """基于真实好感变动与事件因果，合成客观可信的交往里程碑短语。"""
    direction = "升至" if after_affinity > before_affinity else "降至" if after_affinity < before_affinity else "保持在"
    diff_text = f"好感从 {before_affinity} {direction} {after_affinity}"
    normalized_reason = str(reason or "").strip().replace("\n", " ")[:60]
    if event_type == "bot_attacked":
        return f"发生言语冲突（{normalized_reason}），{diff_text}"
    if event_type == "ignored_boundary":
        return f"触犯互动边界（{normalized_reason}），{diff_text}"
    if event_type == "bot_praised":
        return f"获得正面赞赏（{normalized_reason}），{diff_text}"
    if event_type == "deep_talk":
        return f"进行深入长谈（{normalized_reason}），{diff_text}"
    if event_type == "joke":
        return f"共同玩梗接梗（{normalized_reason}），{diff_text}"
    if event_type == "gift_or_feed":
        return f"收到投喂或心意（{normalized_reason}），{diff_text}"
    if event_type == "correction":
        return f"纠正事实误读（{normalized_reason}），{diff_text}"
    if event_type == "direct_reply":
        if "久别" in normalized_reason:
            return f"隔别多日后重新对上话（{normalized_reason}），{diff_text}"
        if "首次" in normalized_reason:
            return f"破冰初次直接交流（{normalized_reason}），{diff_text}"
        return f"日常直接互动（{normalized_reason}），{diff_text}"
    return f"关系发生变化（{normalized_reason}），{diff_text}"


def record_affinity_milestone(
    metadata: Any,
    *,
    event_type: str,
    reason: str,
    before_affinity: int,
    after_affinity: int,
    dimension: str,
    delta: float,
    now: float | None = None,
    event_id: int | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """仅当产生真实好感度变化时，在印象时间线钉下一枚带因果快照的钢印。"""
    if before_affinity == after_affinity or event_type == "message_seen":
        return _as_mapping(metadata)
    phrase = synthesize_milestone_phrase(
        event_type=event_type,
        reason=reason,
        before_affinity=before_affinity,
        after_affinity=after_affinity,
        dimension=dimension,
        delta=delta,
    )
    event_meta = {
        "event_type": event_type,
        "reason": reason,
        "dimension": dimension,
        "delta": delta,
        "before_affinity": before_affinity,
        "after_affinity": after_affinity,
    }
    if event_id is not None:
        event_meta["event_id"] = event_id
    result = append_impression(
        metadata,
        phrase,
        now=now,
        actor="affinity_milestone",
        snapshot=snapshot,
        event=event_meta,
    )
    history = result.get("impression_history")
    if not isinstance(history, list) or not history:
        result["impression_history"] = [{
            "text": phrase,
            "updated_at": float(now if now is not None else time.time()),
            "actor": "affinity_milestone",
            **_context_fields(snapshot=snapshot, event=event_meta),
        }]
    return result


def append_impression(
    metadata: Any,
    text: str,
    *,
    now: float | None = None,
    actor: str = "llm",
    snapshot: Mapping[str, Any] | None = None,
    event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _as_mapping(metadata)
    current = str(text or "").strip()[:120]
    if not current:
        return payload
    previous = str(payload.get("impression") or "").strip()
    stamp = float(now if now is not None else time.time())
    history = payload.get("impression_history")
    if not isinstance(history, list):
        history = []
        if previous:
            history.append({"text": previous, "updated_at": payload.get("impression_updated_at"), "actor": "legacy"})
    context = _context_fields(snapshot=snapshot, event=event)
    if previous and previous != current:
        archived = {
            "text": previous,
            "updated_at": payload.get("impression_updated_at"),
            "superseded_at": stamp,
            "actor": actor,
        }
        archived.update(context)
        history.append(archived)
    payload["impression_history"] = history[-HISTORY_LIMIT:]
    payload["impression"] = current
    payload["impression_updated_at"] = stamp
    if context.get("snapshot"):
        payload["impression_snapshot"] = context["snapshot"]
    if context.get("event"):
        payload["impression_event"] = context["event"]
    payload.pop("impression_cleared_at", None)
    payload.pop("impression_cleared_reason", None)
    return payload


def clear_impression(metadata: Any, *, reason: str, now: float | None = None, actor: str = "webui") -> dict[str, Any]:
    payload = _as_mapping(metadata)
    previous = str(payload.get("impression") or "").strip()
    if not previous:
        raise LookupError("impression_not_found")
    stamp = float(now if now is not None else time.time())
    history = payload.get("impression_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "text": previous,
        "updated_at": payload.get("impression_updated_at"),
        "cleared_at": stamp,
        "cleared_reason": str(reason or "").strip(),
        "actor": actor,
        **_context_fields(
            snapshot=payload.get("impression_snapshot") if isinstance(payload.get("impression_snapshot"), Mapping) else None,
            event=payload.get("impression_event") if isinstance(payload.get("impression_event"), Mapping) else None,
        ),
    })
    payload["impression_history"] = history[-HISTORY_LIMIT:]
    payload["impression"] = ""
    payload["impression_updated_at"] = stamp
    payload["impression_cleared_at"] = stamp
    payload["impression_cleared_reason"] = str(reason or "").strip()
    return payload


def _format_event(event: Mapping[str, Any] | None) -> str:
    payload = _as_mapping(event)
    event_type = str(payload.get("event_type") or "互动").strip()
    reason = str(payload.get("reason") or "").strip()
    dimension = str(payload.get("dimension") or "").strip()
    delta = _finite(payload.get("delta"))
    parts = [event_type]
    if reason:
        parts.append(reason[:80])
    if dimension and delta is not None:
        sign = "+" if delta > 0 else ""
        parts.append(f"{dimension}{sign}{delta:g}")
    return "：".join(parts[:2]) + (f"（{parts[2]}）" if len(parts) > 2 else "")


def relationship_context(repository: Any, scope: Any) -> tuple[dict[str, float], dict[str, Any] | None]:
    if repository is None or scope is None or not hasattr(repository, "get_state"):
        return {}, None
    try:
        state = repository.get_state(scope, subject_principal_id=getattr(scope, "subject_principal_id", None), limit=25, offset=0)
    except Exception:
        return {}, None
    payload = state if isinstance(state, Mapping) else {}
    relationship = payload.get("relationship") if isinstance(payload.get("relationship"), Mapping) else {}
    history = payload.get("relationship_history") if isinstance(payload.get("relationship_history"), Mapping) else {}
    items = history.get("items") if isinstance(history, Mapping) else None
    return snapshot_from_relationship(relationship), meaningful_event_anchor(items if isinstance(items, Sequence) else None)


def append_ledger_entry(
    metadata: Any,
    *,
    event_type: str,
    dimension: str,
    delta: float,
    reason: str,
    at: float | None = None,
    event_id: int | None = None,
) -> dict[str, Any]:
    """Append one scored social-ledger row. Does not touch the summary impression."""
    payload = _as_mapping(metadata)
    normalized_type = str(event_type or "").strip()
    normalized_dimension = str(dimension or "").strip()
    normalized_reason = str(reason or "").strip().replace("\n", " ")[:80]
    amount = _finite(delta)
    if not normalized_type or not normalized_dimension or amount is None:
        return payload
    if normalized_type == "message_seen":
        return payload
    ledger = payload.get("impression_ledger")
    if not isinstance(ledger, list):
        ledger = []
    entry: dict[str, Any] = {
        "event_type": normalized_type,
        "dimension": normalized_dimension,
        "delta": round(amount, 2),
        "reason": normalized_reason,
        "at": float(at if at is not None else time.time()),
    }
    if event_id not in {None, ""}:
        try:
            entry["event_id"] = int(event_id)
        except (TypeError, ValueError):
            pass
    ledger.append(entry)
    payload["impression_ledger"] = ledger[-LEDGER_LIMIT:]
    return payload


def ledger_entries(metadata: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    payload = _as_mapping(metadata)
    stored = payload.get("impression_ledger")
    if not isinstance(stored, list):
        return []
    items = [item for item in stored if isinstance(item, Mapping) and str(item.get("event_type") or "") != "message_seen"]
    cap = max(1, int(limit or 5))
    return [dict(item) for item in items[-cap:]]


def injection_lines(
    metadata: Any,
    *,
    trajectory_limit: int = TRAJECTORY_LIMIT,
    history: Sequence[Any] | None = None,
) -> list[str]:
    payload = _as_mapping(metadata)
    current = str(payload.get("impression") or "").strip()
    lines: list[str] = []
    if current:
        lines.append(f"你对这个人的印象：{current}")
    recent_ledger = ledger_entries(payload, limit=3)
    if recent_ledger:
        rendered = []
        for item in recent_ledger:
            amount = _finite(item.get("delta"))
            sign = "+" if amount is not None and amount > 0 else ""
            delta_text = f"{item.get('dimension')}{sign}{amount:g}" if amount is not None else str(item.get("dimension") or "")
            reason = str(item.get("reason") or "").strip()
            rendered.append(f"{item.get('event_type')} {delta_text}" + (f"：{reason}" if reason else ""))
        if rendered:
            lines.append("最近关系账本：" + "；".join(rendered))
    event = payload.get("impression_event") if isinstance(payload.get("impression_event"), Mapping) else meaningful_event_anchor(history)
    formatted = _format_event(event)
    if formatted and formatted != "互动" and not recent_ledger:
        lines.append(f"最近关系线索：{formatted}")
    stored = payload.get("impression_history")
    if not isinstance(stored, list):
        return lines
    previous = [
        str(item.get("text") or "").strip()
        for item in stored
        if isinstance(item, Mapping) and str(item.get("text") or "").strip() and str(item.get("text") or "").strip() != current
    ]
    previous = [text for text in previous if text][-trajectory_limit:]
    if previous:
        lines.append("印象演变：" + " → ".join(previous + ([current] if current else [])))
    return lines
