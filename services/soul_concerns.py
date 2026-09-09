"""Soul Concern 状态机：只处理正式 scoped_soul_concerns。"""
from __future__ import annotations

from typing import Any

VALID_CONCERN_STATUSES = ("active", "dormant", "progressing", "resolved", "expired", "archived")
_TRANSITIONS = {
    "active": {"dormant", "progressing", "resolved", "expired", "archived"},
    "dormant": {"active", "expired", "archived"},
    "progressing": {"active", "resolved", "expired", "archived"},
    "resolved": {"archived", "active"},
    "expired": {"archived", "active"},
    "archived": set(),
}


def normalize_concern_status(value: Any, *, default: str = "active") -> str:
    status = str(value or default).strip() or default
    if status not in VALID_CONCERN_STATUSES:
        raise ValueError("invalid_concern_status")
    return status


def next_concern_status(current: str, *, action: str, now: float, expected_resolution_at: float | None = None, last_progress_at: float | None = None, dormant_after_seconds: float = 14 * 86400) -> str:
    current = normalize_concern_status(current)
    action = str(action or "").strip()
    if action == "progress":
        return "progressing"
    if action == "resolve":
        return "resolved"
    if action == "expire":
        return "expired"
    if action == "archive":
        return "archived"
    if action == "reopen":
        return "active"
    if action == "tick":
        if expected_resolution_at and now > float(expected_resolution_at) and current in {"active", "dormant", "progressing"}:
            return "expired"
        last = float(last_progress_at or 0)
        if current == "active" and last and now - last >= dormant_after_seconds:
            return "dormant"
        return current
    raise ValueError("invalid_concern_action")


def apply_concern_transition(item: dict[str, Any], *, action: str, now: float, resolution_note: str = "") -> dict[str, Any]:
    current = normalize_concern_status(item.get("status"))
    nxt = next_concern_status(
        current,
        action=action,
        now=now,
        expected_resolution_at=item.get("expected_resolution_at"),
        last_progress_at=item.get("last_progress_at") or item.get("last_triggered"),
    )
    if nxt != current and nxt not in _TRANSITIONS[current]:
        raise ValueError("invalid_concern_transition")
    updated = dict(item)
    updated["status"] = nxt
    if action == "progress":
        updated["last_progress_at"] = now
        updated["last_triggered"] = now
    if action == "resolve":
        updated["resolution_note"] = str(resolution_note or updated.get("resolution_note") or "").strip()
        updated["last_progress_at"] = now
    if action == "reopen":
        updated["last_triggered"] = now
        updated["last_progress_at"] = now
    return updated


__all__ = ["VALID_CONCERN_STATUSES", "apply_concern_transition", "next_concern_status", "normalize_concern_status"]
