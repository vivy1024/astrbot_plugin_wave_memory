"""Proactive relationship context reading and audit timeline recording."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)
try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
try:
    from .belief_gating import snapshot_from_relationship
    from .identity_safety import is_identity_contamination
except ImportError:  # pragma: no cover
    from services.belief_gating import snapshot_from_relationship
    from services.identity_safety import is_identity_contamination


async def read_proactive_relationship_context(
    scope: RuntimeScope | None,
    event: Any,
    *,
    coordinator: Any,
    repository: Any,
    bot_ids: set[str] | list[str] | tuple[str, ...] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Read formal relationship snapshots for the current human message turn."""
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        return {"ok": False, "reason_code": "scope_required", "snapshots": [], "source_memories": []}
    if coordinator is None or repository is None or not callable(getattr(repository, "list_relationships", None)):
        return {
            "ok": False,
            "reason_code": "proactive_dependencies_unavailable",
            "snapshots": [],
            "source_memories": [],
        }

    current_time = float(now if now is not None else time.time())
    window_start = current_time - 180.0
    self_id = str(event.get_self_id() or "").strip() if hasattr(event, "get_self_id") else ""
    known_bot_ids = {str(item).strip() for item in bot_ids if str(item).strip()}
    excluded_senders = {"", "bot", "bot_self", "unknown", self_id, *known_bot_ids}
    principal_prefix = f"{scope.session.platform_id}:user:"
    current_principal = str(scope.subject_principal_id or "")
    current_sender_id = (
        current_principal[len(principal_prefix):]
        if current_principal.startswith(principal_prefix)
        else ""
    )
    if (
        not current_sender_id
        or ":" in current_sender_id
        or current_sender_id in excluded_senders
    ):
        return {"ok": False, "reason_code": "subject_missing", "snapshots": [], "source_memories": []}

    def read_memories(connection):
        return connection.execute(
            """SELECT id, sender_id, timestamp
                 FROM memories
                WHERE bot_id=? AND session_id=? AND visibility=?
                  AND timestamp>=? AND timestamp<=?
                  AND resolution_state='resolved'
                  AND COALESCE(quarantine, 0)=0
                  AND sender_id IS NOT NULL AND TRIM(sender_id)<>''
                ORDER BY timestamp DESC, id DESC
                LIMIT 20""",
            (scope.bot_id, scope.session.id, scope.visibility, window_start, current_time),
        ).fetchall()

    try:
        rows = await coordinator.read(read_memories)
    except Exception as exc:
        logger.warning("[MetaThinking] proactive canonical memory read failed: %s", exc)
        return {
            "ok": False,
            "reason_code": "proactive_memory_read_failed",
            "error": str(exc),
            "snapshots": [],
            "source_memories": [],
        }

    human_rows: list[dict[str, Any]] = []
    for row in rows or ():
        try:
            memory_id = int(row[0])
            sender_id = str(row[1] or "").strip()
            timestamp = float(row[2])
        except (TypeError, ValueError):
            continue
        if (
            not sender_id
            or sender_id in excluded_senders
            or ":" in sender_id
            or not math.isfinite(timestamp)
        ):
            continue
        human_rows.append({"memory_id": memory_id, "sender_id": sender_id, "timestamp": timestamp})

    selected: list[dict[str, Any]] = [{
        "memory_id": None,
        "event_id": str(getattr(event, "message_id", "") or ""),
        "sender_id": current_sender_id,
        "timestamp": current_time,
        "role": "primary",
        "source": "resolved_event",
    }]
    lower_bound = max(window_start, current_time - 45.0)
    seen_senders: set[str] = {current_sender_id}
    for item in human_rows:
        if item["timestamp"] < lower_bound or item["timestamp"] > current_time:
            continue
        if item["sender_id"] in seen_senders:
            continue
        seen_senders.add(item["sender_id"])
        item = dict(item)
        item["role"] = "co_subject"
        item["source"] = "canonical_memory"
        selected.append(item)
        if len(selected) >= 4:
            break

    snapshots = []
    subjects = []
    for item in selected:
        principal = f"{scope.session.platform_id}:user:{item['sender_id']}"
        subject_scope = RuntimeScope(
            bot_id=scope.bot_id,
            visibility=scope.visibility,
            session=scope.session,
            subject_principal_id=principal,
        )
        try:
            rows = await asyncio.to_thread(
                repository.list_relationships,
                subject_scope,
                subject_principal_id=principal,
            )
        except Exception as exc:
            logger.warning("[MetaThinking] proactive relationship read failed subject=%s: %s", principal, exc)
            return {
                "ok": False,
                "reason_code": "proactive_relationship_read_failed",
                "error": str(exc),
                "snapshots": [],
                "source_memories": selected,
            }
        relationship = rows[0] if isinstance(rows, list) and rows else None
        snapshots.append(snapshot_from_relationship(principal, relationship))
        subjects.append(principal)

    return {
        "ok": True,
        "reason_code": "relationship_context_ready",
        "snapshots": snapshots,
        "subjects": subjects,
        "source_memories": selected,
        "primary_subject": subjects[0],
    }


async def record_proactive_timeline(
    scope: RuntimeScope,
    reply_text: str,
    policy: dict[str, Any],
    source_memories: list[dict[str, Any]],
    *,
    coordinator: Any,
    repository: Any,
) -> int:
    """Audit a sent proactive reply through the writer-owned transaction."""
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        raise ValueError("scope_required")
    if coordinator is None or repository is None:
        raise RuntimeError("proactive_audit_dependencies_unavailable")
    normalized_reply = str(reply_text or "").strip()[:160]
    if not normalized_reply or is_identity_contamination(normalized_reply):
        raise ValueError("proactive_reply_invalid_for_audit")
    group_scope = RuntimeScope(
        bot_id=scope.bot_id,
        visibility=scope.visibility,
        session=scope.session,
        subject_principal_id=None,
    )
    evidence: list[dict[str, Any]] = [{
        "kind": "proactive_policy",
        "policy_version": policy.get("policy_version"),
        "decision": policy.get("decision"),
        "reason_code": policy.get("reason_code"),
        "behavior_type": policy.get("behavior_type"),
        "trigger_weight": policy.get("trigger_weight", 0.0),
        "depth_factor": policy.get("depth_factor", 0.0),
        "concern_score": policy.get("concern_score", 0.0),
        "is_interesting": bool(policy.get("is_interesting")),
    }]
    for snapshot in policy.get("snapshots") or ():
        if not isinstance(snapshot, dict):
            continue
        evidence.append({
            "kind": "relationship_snapshot",
            "subject_principal_id": snapshot.get("subject_principal_id"),
            "revision": snapshot.get("revision"),
            "dimensions": dict(snapshot.get("dimensions") or {}),
        })
    for item in source_memories[:4]:
        if not isinstance(item, dict):
            continue
        if item.get("memory_id") is None:
            evidence.append({
                "kind": "source_event",
                "event_id": item.get("event_id"),
                "timestamp": item.get("timestamp"),
                "role": item.get("role"),
            })
        else:
            evidence.append({
                "kind": "source_memory",
                "memory_id": item.get("memory_id"),
                "timestamp": item.get("timestamp"),
                "role": item.get("role"),
            })
    reply_hash = "sha256:" + hashlib.sha256(normalized_reply.encode("utf-8")).hexdigest()
    evidence.append({
        "kind": "proactive_reply",
        "reply_sha256": reply_hash,
        "reply_length": len(normalized_reply),
    })

    def persist(connection):
        existing = connection.execute(
            """SELECT id FROM scoped_soul_timeline
                 WHERE bot_id=? AND session_id=? AND visibility=?
                   AND subject_principal_id IS NULL
                   AND event_type='proactive.interjection'
                   AND evidence LIKE ?
                 ORDER BY id DESC LIMIT 1""",
            (group_scope.bot_id, group_scope.session.id, group_scope.visibility, f"%{reply_hash}%"),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        return repository.add_timeline_event(
            group_scope,
            event_summary=normalized_reply,
            emotional_weight=float(policy.get("trigger_weight") or 0.0),
            event_type="proactive.interjection",
            evidence=evidence,
            connection=connection,
        )

    return int(await coordinator.transaction(persist, actor="proactive.interjection"))
