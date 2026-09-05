"""纯领域 Belief 生命周期状态机；不依赖 AstrBot 运行时。"""

from __future__ import annotations

try:
    from domain.scope import RuntimeScope
except ImportError:  # pragma: no cover
    from ..domain.scope import RuntimeScope

try:
    from .belief_confidence import is_activation_eligible
except ImportError:  # pragma: no cover
    from services.belief_confidence import is_activation_eligible


class BeliefLifecycleService:
    def __init__(self, repository, trace_store=None):
        self.repository = repository
        self.trace_store = trace_store

    def transition(self, scope: RuntimeScope, belief_id: int, action: str, query_trace_id: str | None = None) -> dict:
        if action not in {"approve", "archive"}:
            raise ValueError("belief_transition_unavailable")
        current = next(
            (row for row in self.repository.list_scoped_beliefs(scope, limit=10000) if int(row.get("id", -1)) == int(belief_id)),
            None,
        )
        if current is None:
            raise LookupError("scoped_object_not_found")
        if action == "approve":
            provenance = current.get("provenance") if isinstance(current.get("provenance"), dict) else {}
            if current.get("status") not in {"pending", "quarantined"}:
                raise ValueError("invalid_belief_transition")
            if not current.get("source_memory_id"):
                raise ValueError("belief_anchor_required")
            if not is_activation_eligible(provenance):
                raise ValueError("belief_evidence_incomplete")
            candidate = provenance.get("candidate") if isinstance(provenance.get("candidate"), dict) else None
            relation = str(candidate.get("relation") or "") if candidate else ""
            if candidate and relation in {"reinforce", "challenge"}:
                merger = getattr(self.repository, "merge_scoped_belief_candidate", None)
                if not callable(merger):
                    raise ValueError("candidate_merge_unavailable")
                try:
                    return dict(merger(
                        scope,
                        int(belief_id),
                        expected_target_revision=candidate.get("target_revision_at_capture"),
                    ))
                except Exception as exc:
                    code = str(getattr(exc, "reason_code", None) or getattr(exc, "code", None) or str(exc))
                    if code in {"candidate_target_unavailable", "candidate_relation_unsupported"}:
                        resolver = getattr(self.repository, "resolve_scoped_belief_candidate", None)
                        if callable(resolver):
                            resolver(scope, int(belief_id), resolution="rejected", reason_code=code)
                    raise ValueError(code) from exc
            if candidate and relation not in {"", "new"}:
                raise ValueError("candidate_relation_unsupported")
            target_status = "active"
        else:
            if current.get("status") == "archived":
                raise ValueError("invalid_belief_transition")
            target_status = "archived"
        provenance = dict(current.get("provenance") or {})
        provenance.update({"lifecycle_action": action, "lifecycle_actor": "webui"})
        candidate = provenance.get("candidate")
        if action == "approve" and isinstance(candidate, dict):
            candidate = dict(candidate)
            candidate.update({"resolution": "promoted", "resolution_reason": "manual_approved"})
            provenance["candidate"] = candidate
        self.repository.upsert_scoped_belief(
            scope,
            belief_key=current["belief_key"],
            content=current["content"],
            belief_type=current["belief_type"],
            strength=float(current.get("strength") or 0.0),
            status=target_status,
            source_memory_id=current.get("source_memory_id"),
            provenance=provenance,
        )
        return {"id": int(belief_id), "status": target_status}


__all__ = ["BeliefLifecycleService"]
