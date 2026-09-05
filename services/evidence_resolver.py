"""Resolve WebUI evidence descriptors against canonical scoped objects."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from ..domain.scope import RuntimeScope, ScopeValidationError, scope_to_dict
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope, ScopeValidationError, scope_to_dict


class EvidenceResolutionError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.reason_code = self.code
        super().__init__(message or self.code)


def _scope_key(scope: RuntimeScope) -> str:
    if scope.session is None:
        return scope.bot_id
    return f"{scope.bot_id}:{scope.session.id}:{scope.visibility}"


def _source_scope(value: Any) -> RuntimeScope:
    if not isinstance(value, Mapping):
        raise EvidenceResolutionError("relationship_evidence_scope_required")
    try:
        return RuntimeScope.from_dict(value)
    except Exception as exc:
        raise EvidenceResolutionError("relationship_evidence_scope_invalid") from exc


def _memory_descriptor(connection: Any, *, scope: RuntimeScope, item: Mapping[str, Any]) -> dict[str, Any]:
    raw_id = item.get("id")
    object_ref = item.get("object_ref")
    if isinstance(object_ref, Mapping):
        raw_id = object_ref.get("locator", raw_id)
    try:
        memory_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise EvidenceResolutionError("relationship_evidence_invalid") from exc
    if scope.session is None:
        raise EvidenceResolutionError("relationship_evidence_scope_required")
    if connection is None:
        raise EvidenceResolutionError("relationship_evidence_store_unavailable")
    columns: set[str] = set()
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
    except Exception:
        pass
    try:
        # First try exact scoped match if all required columns exist
        if {"id", "bot_id", "session_id", "visibility"}.issubset(columns):
            row = connection.execute(
                """SELECT content, timestamp, version, resolution_state, COALESCE(quarantine, 0)
                     FROM memories
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
                (memory_id, scope.bot_id, scope.session.id, scope.visibility),
            ).fetchone()
        # Fallback for group memories that may lack full scope columns or resolution_state
        if row is None and scope.visibility == "group" and scope.session is not None:
            group_col = "group_id" if "group_id" in columns else "session_id"
            if group_col in columns:
                row = connection.execute(
                    f"""SELECT content, timestamp, version,
                               {"COALESCE(resolution_state, 'resolved')" if "resolution_state" in columns else "'resolved'"},
                               {"COALESCE(quarantine, 0)" if "quarantine" in columns else "0"}
                          FROM memories
                         WHERE id=? AND {group_col}=?""",
                    (memory_id, scope.session.conversation_id if group_col == "group_id" else scope.session.id),
                ).fetchone()
    except Exception as exc:
        raise EvidenceResolutionError("relationship_evidence_store_unavailable") from exc
    if row is None:
        raise EvidenceResolutionError("relationship_evidence_not_found")
    if str(row[3] or "") not in {"resolved", ""}:
        raise EvidenceResolutionError("relationship_evidence_unavailable")
    if bool(row[4]):
        raise EvidenceResolutionError("relationship_evidence_quarantined")
    content = str(row[0] or "")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    supplied_hash = str(item.get("content_hash") or "").strip()
    if supplied_hash and supplied_hash != content_hash:
        raise EvidenceResolutionError("relationship_evidence_hash_mismatch")
    return {
        "kind": "memory",
        "type": "memory",
        "id": str(memory_id),
        "content_hash": content_hash,
        "captured_at": float(row[1] or 0.0),
        "source_scope": scope_to_dict(scope),
        "available": True,
    }


def _episode_descriptor(connection: Any, *, scope: RuntimeScope, item: Mapping[str, Any]) -> dict[str, Any]:
    if connection is None or scope.session is None or not scope.subject_principal_id:
        raise EvidenceResolutionError("relationship_evidence_scope_required")
    raw_id = item.get("id")
    try:
        episode_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise EvidenceResolutionError("relationship_evidence_invalid") from exc
    prefix = f"{scope.session.platform_id}:user:"
    if not scope.subject_principal_id.startswith(prefix):
        raise EvidenceResolutionError("relationship_evidence_scope_invalid")
    user_id = scope.subject_principal_id[len(prefix):]
    try:
        row = connection.execute(
            """SELECT episode_type, trigger_text, bot_inner_thought, bot_action,
                      bot_reply, user_reaction, outcome, source_memory_ids, created_at
                 FROM experience_episodes
                WHERE id=? AND bot_id=? AND group_id=? AND user_id=?""",
            (episode_id, scope.bot_id, scope.session.conversation_id, user_id),
        ).fetchone()
    except Exception as exc:
        raise EvidenceResolutionError("relationship_evidence_store_unavailable") from exc
    if row is None:
        raise EvidenceResolutionError("relationship_evidence_not_found")
    fingerprint = "|".join(str(value or "") for value in row[:8])
    content_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    supplied_hash = str(item.get("content_hash") or "").strip()
    if supplied_hash and supplied_hash != content_hash:
        raise EvidenceResolutionError("relationship_evidence_hash_mismatch")
    captured_at = float(row[8] or 0.0)
    return {
        "kind": "episode",
        "type": "episode",
        "id": str(episode_id),
        "content_hash": content_hash,
        "captured_at": captured_at,
        "source_scope": scope_to_dict(scope),
        "available": True,
        "title": str(row[0] or "experience_episode"),
        "locator": {"episode_id": episode_id},
    }


def resolve_relationship_evidence(connection: Any, *, scope: RuntimeScope, values: Any) -> list[dict[str, Any]]:
    """Resolve all calibration evidence against the exact target Scope."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise EvidenceResolutionError("relationship_evidence_required")
    if len(values) > 20:
        raise EvidenceResolutionError("relationship_evidence_too_large")
    normalized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise EvidenceResolutionError("relationship_evidence_invalid")
        source = _source_scope(item.get("source_scope"))
        if source != scope:
            raise EvidenceResolutionError("relationship_evidence_scope_mismatch")
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if kind == "memory":
            resolved = _memory_descriptor(connection, scope=scope, item=item)
        elif kind in {"episode", "experience_episode"}:
            resolved = _episode_descriptor(connection, scope=scope, item=item)
        else:
            raise EvidenceResolutionError("relationship_evidence_object_required")
        if not math.isfinite(float(resolved["captured_at"])) or float(resolved["captured_at"]) < 0:
            raise EvidenceResolutionError("relationship_evidence_invalid")
        summary = item.get("summary")
        if summary is not None:
            if not isinstance(summary, str):
                raise EvidenceResolutionError("relationship_evidence_invalid")
            summary = summary.strip()
            if summary:
                resolved["summary"] = summary[:500]
        normalized.append(resolved)
    return normalized


__all__ = ["EvidenceResolutionError", "resolve_relationship_evidence"]
