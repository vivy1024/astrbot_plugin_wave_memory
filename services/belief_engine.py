"""BeliefEngine — 信念系统核心

维护信念生命周期，查询时注入已审 active 信念作为 bot 的底色。
信念只能由现场工具提审，不再从 consolidation 摘要抽取。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests
    import logging
    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
    from ..engine.database import WaveMemoryDB
except ImportError:  # pragma: no cover - direct service imports in focused tests
    from domain.scope import RuntimeScope
    from engine.database import WaveMemoryDB
from .belief_confidence import POLICY_VERSION, calculate_confidence, is_activation_eligible
from .belief_gating import (
    canonical_subject_principal,
    interaction_policy,
    snapshot_from_relationship,
)
from .llm_fallback import LLMFallbackClient


APPROVED_FACT_STATUSES = frozenset({"active", "approved"})


def approved_source_fact_ids(repository: Any, scope: RuntimeScope, fact_ids: Any) -> list[int]:
    """Return currently approved fact ids in this scope. Order-preserving, unique."""
    raw_ids: list[int] = []
    seen: set[int] = set()
    values = fact_ids if isinstance(fact_ids, (list, tuple, set)) else []
    for value in values:
        try:
            fact_id = int(value)
        except (TypeError, ValueError):
            continue
        if fact_id <= 0 or fact_id in seen:
            continue
        seen.add(fact_id)
        raw_ids.append(fact_id)
    if len(raw_ids) < 2 or repository is None or not hasattr(repository, "list_scoped_facts"):
        return []
    try:
        rows = repository.list_scoped_facts(scope, limit=200) or []
    except Exception:
        return []
    approved = {
        int(row["id"])
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("status") or "") in APPROVED_FACT_STATUSES
        and row.get("id") is not None
    }
    return [fact_id for fact_id in raw_ids if fact_id in approved]


def is_fact_backed(repository: Any, scope: RuntimeScope, provenance: Any) -> bool:
    """True when provenance currently cites at least two approved facts in this scope."""
    payload = provenance if isinstance(provenance, Mapping) else {}
    return len(approved_source_fact_ids(repository, scope, payload.get("source_fact_ids"))) >= 2


def is_episode_backed(repository: Any, scope: RuntimeScope, provenance: Any) -> bool:
    """经历证据路径：用于事实底座尚未建立时的冷启动升格。

    与 is_fact_backed 是并行的两条准入路径，不是替代关系。要求：
    - provenance 由 belief_emergence 产出，且带 episode_id；
    - 引用的记忆在同 Scope 内去重后 ≥ 2 条，且全部是健康、未隔离的已解析记忆
      （跨 Bot / 跨群 / 已隔离的记忆一律不算）。

    「至少两条」与 evidence-v1 的 ACTIVATION_MIN_SUPPORT_WINDOWS 同量级，但这里是
    独立的证据形态（经历窗口）判定，不复用 confidence_components，避免把需要
    tag 链路完整的证据门槛错误地套到经历上。
    """
    payload = provenance if isinstance(provenance, Mapping) else {}
    if str(payload.get("producer") or "") != "belief_emergence":
        return False
    if payload.get("episode_id") in (None, ""):
        return False
    candidate_ids = _positive_unique_ids(payload.get("source_memory_ids"))
    if len(candidate_ids) < 2:
        return False
    if repository is None:
        return False
    lister = getattr(repository, "list_scoped_memories_for_belief", None)
    if not callable(lister):
        return False
    try:
        rows = lister(scope, memory_ids=candidate_ids) or []
    except Exception:
        return False
    healthy = {
        int(row["id"])
        for row in rows
        if isinstance(row, Mapping)
        and row.get("id") is not None
        and str(row.get("resolution_state") or "") == "resolved"
        and not row.get("quarantine")
    }
    return len([mid for mid in candidate_ids if mid in healthy]) >= 2


def _positive_unique_ids(values: Any) -> list[int]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return []
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            identifier = int(value)
        except (TypeError, ValueError):
            continue
        if identifier > 0 and identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    return result


def first_memory_id_from_episode(repository: Any, scope: RuntimeScope, provenance: Any) -> int | None:
    """经历证据路径的锚点：取第一条同 Scope 内健康记忆的 id。"""
    payload = provenance if isinstance(provenance, Mapping) else {}
    candidate_ids = _positive_unique_ids(payload.get("source_memory_ids"))
    if not candidate_ids:
        return None
    lister = getattr(repository, "list_scoped_memories_for_belief", None)
    if not callable(lister):
        return None
    try:
        rows = lister(scope, memory_ids=candidate_ids) or []
    except Exception:
        return None
    healthy = {
        int(row["id"])
        for row in rows
        if isinstance(row, Mapping)
        and row.get("id") is not None
        and str(row.get("resolution_state") or "") == "resolved"
        and not row.get("quarantine")
    }
    for memory_id in candidate_ids:
        if memory_id in healthy:
            return memory_id
    return None


def first_memory_id_from_facts(repository: Any, scope: RuntimeScope, fact_ids: list[int]) -> int | None:
    if repository is None or not hasattr(repository, "list_scoped_facts"):
        return None
    wanted = set(fact_ids)
    try:
        rows = repository.list_scoped_facts(scope, limit=200) or []
    except Exception:
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            row_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if row_id not in wanted:
            continue
        memory_id = row.get("source_memory_id")
        try:
            parsed = int(memory_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


class BeliefEngine:
    """信念系统 — 维护与注入已审信念。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        llm_client: LLMFallbackClient,
        bot_id: str,
        max_beliefs: int = 50,
        soul_repository: Any | None = None,
    ):
        self.db = db
        self.llm = llm_client
        self.bot_id = bot_id
        self.max_beliefs = max_beliefs
        self.soul_repository = soul_repository or getattr(db, "soul_repository", None)

    def refresh_evidence_after_tags(self, scope: RuntimeScope, belief_id: int) -> dict | None:
        """Recompute evidence-v1 after TagWorker later completes the tag chain.

        Does not create new observations and does not auto-activate. WebUI
        approve still requires at least two currently approved facts.
        """
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return None
        getter = getattr(self.db, "get_scoped_belief", None)
        if not callable(getter):
            return None
        belief = getter(scope, int(belief_id))
        if not isinstance(belief, dict):
            return None
        status = str(belief.get("status") or "")
        if status not in {"pending", "quarantined"}:
            return None
        refreshed = self._refresh_from_observations(
            scope,
            belief,
            anchor_sentence=str((belief.get("provenance") or {}).get("anchor_sentence") or ""),
            query_trace_id=str((belief.get("provenance") or {}).get("query_trace_id") or ""),
        )
        provenance = refreshed.get("provenance") if isinstance(refreshed.get("provenance"), dict) else {}
        gating = provenance.get("gating") if isinstance(provenance.get("gating"), dict) else {}
        decision = str(gating.get("decision") or "")
        if decision == "quarantine" or status == "quarantined":
            refreshed["status"] = "quarantined"
        else:
            refreshed["status"] = "pending"
        self.db.upsert_scoped_belief(
            scope,
            belief_key=refreshed["belief_key"],
            content=refreshed["content"],
            belief_type=refreshed["belief_type"],
            strength=float(refreshed.get("strength") or 0.0),
            status=refreshed["status"],
            source_memory_id=refreshed.get("source_memory_id"),
            provenance=refreshed["provenance"],
        )
        return refreshed

    def _refresh_from_observations(
        self,
        scope: RuntimeScope,
        belief: dict,
        *,
        anchor_sentence: str,
        query_trace_id: str | None,
    ) -> dict:
        repo = getattr(self.db, "scoped_knowledge", None)
        tag_getter = getattr(repo, "list_scoped_memory_tags", None)
        observations = self.db.list_scoped_belief_observations(scope, belief_id=int(belief["id"]), limit=500)
        evaluation = calculate_confidence(observations)
        evidence_ids: list[int] = []
        support_ids: list[int] = []
        challenge_ids: list[int] = []
        for observation in observations:
            target = support_ids if observation.get("polarity") == "support" else challenge_ids
            for raw_id in observation.get("memory_ids") or []:
                try:
                    memory_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if memory_id > 0 and memory_id not in evidence_ids:
                    evidence_ids.append(memory_id)
                if memory_id > 0 and memory_id not in target:
                    target.append(memory_id)
        all_tags: list[dict] = []
        if callable(tag_getter) and evidence_ids:
            try:
                all_tags = list(tag_getter(scope, evidence_ids) or [])
            except Exception:
                all_tags = []
        tagged_ids = {int(tag.get("memory_id")) for tag in all_tags if isinstance(tag, dict) and tag.get("memory_id") is not None}
        tag_chain_status = "complete" if evidence_ids and set(evidence_ids) <= tagged_ids else "empty"
        provenance = dict(belief.get("provenance") or {})
        provenance.update({
            "producer": "consolidation",
            "confidence_policy_version": POLICY_VERSION,
            "confidence_components": evaluation["components"],
            "confidence_evidence": evaluation["summary"],
            "activation_eligible": bool(evaluation["activation_eligible"] and tag_chain_status == "complete"),
            "source_memory_ids": support_ids,
            "source_tags": all_tags,
            "evidence": {
                "memory_ids": evidence_ids,
                "support_memory_ids": support_ids,
                "challenge_memory_ids": challenge_ids,
                "observation_ids": [observation.get("id") for observation in observations],
                "window_keys": [observation.get("window_key") for observation in observations],
            },
            "tag_chain_status": tag_chain_status,
            "anchor_sentence": anchor_sentence or provenance.get("anchor_sentence") or "",
            "query_trace_id": str(query_trace_id or ""),
            "trace_status": "not_required",
            "scope": scope.session.id if scope.session else scope.bot_id,
        })
        gating = provenance.get("gating") if isinstance(provenance.get("gating"), dict) else {}
        if gating.get("decision") == "quarantine":
            provenance["quarantine_reason"] = str(gating.get("reason_code") or "relationship_quarantine")[:160]
        elif "quarantine_reason" in provenance and gating.get("decision") != "quarantine":
            provenance.pop("quarantine_reason", None)
        source_memory_id = support_ids[0] if support_ids else belief.get("source_memory_id")
        self.db.upsert_scoped_belief(
            scope,
            belief_key=belief["belief_key"],
            content=belief["content"],
            belief_type=belief["belief_type"],
            strength=float(evaluation["components"]["confidence"]),
            status=belief.get("status") or "pending",
            source_memory_id=source_memory_id,
            provenance=provenance,
        )
        return {
            **belief,
            "strength": float(evaluation["components"]["confidence"]),
            "source_memory_id": source_memory_id,
            "provenance": provenance,
        }

    def _current_relationship_policy(
        self,
        scope: RuntimeScope,
        sender_id: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Resolve the current subject's formal relationship without widening Scope."""
        if self.soul_repository is None or not sender_id:
            return None, {}
        principal = canonical_subject_principal(scope, sender_id)
        if principal is None:
            return None, interaction_policy(None, subject_available=False)
        if scope.subject_principal_id and scope.subject_principal_id != principal:
            return None, interaction_policy(None, subject_available=False)
        try:
            rows = self.soul_repository.list_relationships(scope, subject_principal_id=principal)
            row = next((item for item in rows if isinstance(item, dict) and item.get("subject_principal_id") == principal), None)
        except Exception:
            row = None
        if row is None:
            return None, interaction_policy(None, subject_available=False)
        snapshot = snapshot_from_relationship(principal, row)
        return snapshot.to_dict(), interaction_policy(snapshot)

    def get_injection_details(
        self,
        scope: RuntimeScope,
        sender_id: str | None = None,
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build safe belief text plus auditable relationship-driven policy metadata."""
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            logger.warning("[BeliefEngine] Scoped injection rejected: group RuntimeScope required")
            return {"text": "", "belief_ids": [], "gating": {}, "interaction_policy": {}}

        repo = getattr(self.db, "scoped_knowledge", None)
        active_beliefs = self.db.list_scoped_beliefs(scope, status="active")
        beliefs: list[dict] = [
            belief for belief in active_beliefs
            if isinstance(belief, dict) and belief.get("status") == "active"
            and self._injectable(belief, scope, repo)
        ]
        relationship_snapshot, response_policy = self._current_relationship_policy(scope, sender_id)
        allow_person_judgment = bool(response_policy.get("allow_person_judgment", True))
        # A formal repository makes unknown relationship fail closed for person
        # judgments.  A repository-less focused fake keeps the historical read-only
        # behavior and cannot manufacture a direct gate decision.
        formal_relationship_available = self.soul_repository is not None
        selected: list[dict] = []
        selected.extend(belief for belief in beliefs if belief.get("belief_type") == "self_identity")
        if sender_id and (allow_person_judgment or not formal_relationship_available):
            selected.extend(
                belief for belief in beliefs
                if belief.get("belief_type") == "person_judgment"
                and (
                    str(sender_id) in str(belief.get("content") or "")
                    or str(relationship_snapshot.get("subject_principal_id")) in str(belief.get("content") or "")
                )
            )

        normalized_keywords = [str(keyword).casefold() for keyword in (keywords or [])[:3] if keyword]
        if normalized_keywords:
            selected.extend(
                belief for belief in beliefs
                if belief.get("belief_type") in {"world_view", "preference"}
                and any(keyword in str(belief.get("content") or "").casefold() for keyword in normalized_keywords)
            )

        seen_ids = set()
        unique_beliefs = []
        for belief in selected:
            belief_id = belief.get("id")
            if belief_id in seen_ids or belief.get("status") != "active":
                continue
            seen_ids.add(belief_id)
            unique_beliefs.append(belief)
        unique_beliefs = [belief for belief in unique_beliefs if self._injectable(belief, scope, repo)]

        lines: list[str] = []
        if unique_beliefs:
            lines.append("<beliefs>")
            for belief in unique_beliefs[:5]:
                try:
                    strength = float(belief.get("strength") or 0.0)
                except (TypeError, ValueError):
                    strength = 0.0
                strength_label = "确信" if strength > 0.7 else "觉得" if strength > 0.4 else "隐约觉得"
                lines.append(f"- {strength_label}：{belief['content']}")
            lines.append("</beliefs>")

        return {
            "text": "\n".join(lines),
            "belief_ids": [int(item.get("id")) for item in unique_beliefs if item.get("id") is not None],
            "gating": {
                "subject_principal_id": relationship_snapshot.get("subject_principal_id") if relationship_snapshot else None,
                "trust": relationship_snapshot.get("trust") if relationship_snapshot else None,
                "hostility": relationship_snapshot.get("hostility") if relationship_snapshot else None,
                "reason_code": response_policy.get("reason_code") if response_policy else None,
                "mode": response_policy.get("mode") if response_policy else None,
            },
            "interaction_policy": response_policy,
        }

    def get_injection(
        self,
        scope: RuntimeScope,
        sender_id: str | None = None,
        keywords: list[str] | None = None,
    ) -> str:
        """获取当前 group RuntimeScope 内的 active 信念注入文本。"""
        return str(self.get_injection_details(scope, sender_id=sender_id, keywords=keywords).get("text") or "")

    def _injectable(self, belief: dict, scope: RuntimeScope, repository: Any) -> bool:
        provenance = belief.get("provenance") if isinstance(belief.get("provenance"), dict) else {}
        if is_fact_backed(repository, scope, provenance):
            return True
        if is_episode_backed(repository, scope, provenance):
            return True
        return is_activation_eligible(provenance)
