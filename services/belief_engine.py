"""BeliefEngine — 信念系统核心

从 consolidation 摘要中提取稳定判断，维护信念的强化/动摇生命周期，
查询时注入相关信念作为 bot 的"底色"。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Optional

from astrbot.api import logger

try:
    from ..domain.scope import RuntimeScope
    from ..engine.database import WaveMemoryDB
except ImportError:  # pragma: no cover - direct service imports in focused tests
    from domain.scope import RuntimeScope
    from engine.database import WaveMemoryDB
from .belief_confidence import POLICY_VERSION, calculate_confidence, is_activation_eligible
from .belief_gating import (
    RelationshipSnapshot,
    canonical_subject_principal,
    candidate_fingerprint,
    evaluate_relationship_gate,
    interaction_policy,
    snapshot_from_relationship,
)
from .llm_fallback import LLMFallbackClient
from .identity_safety import is_identity_contamination


class BeliefLifecycleService:
    """正式 scoped Belief 生命周期服务；不读取或写入 legacy beliefs。"""

    def __init__(self, repository):
        self.repository = repository

    def transition(self, scope: RuntimeScope, belief_id: int, action: str) -> dict:
        if action not in {"approve", "archive"}:
            raise ValueError("belief_transition_unavailable")
        current = next(
            (row for row in self.repository.list_scoped_beliefs(scope, limit=10000) if int(row.get("id", -1)) == int(belief_id)),
            None,
        )
        if current is None:
            raise LookupError("scoped_object_not_found")
        if action == "approve":
            if current.get("status") != "pending":
                raise ValueError("invalid_belief_transition")
            if not current.get("source_memory_id"):
                raise ValueError("belief_anchor_required")
            target_status = "active"
        else:
            if current.get("status") == "archived":
                raise ValueError("invalid_belief_transition")
            target_status = "archived"
        provenance = dict(current.get("provenance") or {})
        provenance.update({"lifecycle_action": action, "lifecycle_actor": "webui"})
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


EXTRACT_PROMPT = """分析以下群聊经历窗口，提取 0-2 条**高质量**稳定判断（宁缺毋滥，没有就返回 []）。

稳定判断 = 反复出现的模式、对某人/某事的一致性看法、或对自己的认知。
不是事实陈述（"今天下雨"不是），是主观判断（"这个人说话不可信"是）。

【严格排除以下情况，命中则不要提取】
1. 跑团/角色扮演/小说情节：TRPG、COC、DND、模组、剧透、"角色""设定""模组"等虚构内容。
2. 实体边界错误：主语必须是清晰的真实人物/群体名，不能把定语黏进昵称，也不能把群名/书名当人。
3. 琐碎偏好：口味、零食、表情等无意义细节一律不提取。
4. 来自小说/书籍内化的世界观，不是真实社交判断。

【只提取】对真实群友的稳定社交判断、bot 真实的自我认知、反复验证的真实世界观。
每条结论必须用下面的 message ID 引用实际消息证据；不得引用未提供的 ID。

记忆摘要：
{summary}

可引用的经历消息：
{evidence_messages}

已有信念（避免重复或与之矛盾的也列出来）：
{existing_beliefs}

输出格式（JSON 数组，没有合格的就返回 []）：
[{{
  "content": "一句话判断（主语是真实人物/自己）",
  "type": "person_judgment|world_view|self_identity|preference",
  "evidence_memory_ids": [12, 15],
  "challenge_memory_ids": [],
  "match_id": null,
  "relation": "new|reinforce|challenge",
  "challenges": [],
  "anchor_sentence": "来自实际消息的短句"
}}]

- evidence_memory_ids：支持该判断的实际 message ID，至少一个。
- challenge_memory_ids：反证某条已有信念的实际 message ID；没有则 []。
- match_id：若该结论对应已有信念，填写其 ID；无则 null。
- relation：new 表示新候选；reinforce 表示支持 match_id；challenge 表示反证 match_id。
- challenges：本条候选反证的其他已有信念 ID；仅填实际矛盾项。

只返回 JSON，不要其他文字。"""


class BeliefEngine:
    """信念系统 — 提取、维护、注入。"""

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

    def _relationship_gate(
        self,
        scope: RuntimeScope,
        memory_ids: list[int],
        memory_by_id: dict[int, dict],
    ) -> dict[str, Any]:
        """Read formal relationships for evidence speakers and apply strict gating."""
        participants: list[str] = []
        unknown_subjects: list[str] = []
        snapshots: list[RelationshipSnapshot] = []
        if self.soul_repository is None:
            return evaluate_relationship_gate([], unknown_subjects=["repository_unavailable"])
        for memory_id in memory_ids:
            memory = memory_by_id.get(memory_id) or {}
            principal = canonical_subject_principal(scope, memory.get("sender_id"))
            if principal is None:
                if "speaker_unknown" not in unknown_subjects:
                    unknown_subjects.append("speaker_unknown")
                continue
            if principal in participants:
                continue
            participants.append(principal)
            try:
                rows = self.soul_repository.list_relationships(
                    scope,
                    subject_principal_id=principal,
                )
                row = next((item for item in rows if isinstance(item, dict) and item.get("subject_principal_id") == principal), None)
            except Exception:
                row = None
            if row is None:
                unknown_subjects.append(principal)
            else:
                snapshot = snapshot_from_relationship(principal, row)
                snapshots.append(snapshot)
                if not snapshot.known_for_gating:
                    unknown_subjects.append(principal)
        gate = evaluate_relationship_gate(snapshots, unknown_subjects=unknown_subjects)
        gate["subjects"] = list(dict.fromkeys(participants + list(gate.get("unknown_subjects") or [])))
        if not participants and "speaker_unknown" not in gate["unknown_subjects"]:
            gate["unknown_subjects"] = ["speaker_unknown"]
            gate["decision"] = "pending"
            gate["reason_code"] = "belief_speaker_unknown"
            gate["review_required"] = True
        return gate

    @staticmethod
    def _belief_revision(belief: Mapping[str, Any]) -> int:
        try:
            value = float(belief.get("updated_at") or belief.get("created_at") or 1)
        except (TypeError, ValueError):
            value = 1.0
        return max(1, int(value * 1000))

    async def extract_from_summary(
        self,
        summary: str,
        scope: RuntimeScope,
        source_memory_ids: list[int] | None = None,
        query_trace_id: str | None = None,
        trace_store: object | None = None,
    ) -> list[dict]:
        """从同 Scope 的 consolidation 窗口提取可审计的 pending 信念候选。

        每条 LLM 候选都必须点名本窗口中真实、已解析且未隔离的消息 ID。
        分数来自持久化经历观察，而不是固定初始值、摘要长度或 LLM 自报置信度。
        ``query_trace_id`` 保留为兼容参数，但后台 consolidation 的合法性不依赖
        一个不存在的前台 query trace。
        """
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            logger.warning("[BeliefEngine] Scoped extraction rejected: RuntimeScope required")
            return []
        if not summary or len(summary) < 20 or is_identity_contamination(summary) or self.llm is None:
            return []
        if (
            not isinstance(source_memory_ids, list)
            or not source_memory_ids
            or any(isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0 for memory_id in source_memory_ids)
        ):
            logger.warning("[BeliefEngine] Scoped extraction rejected: verified source ids required")
            return []
        requested_ids = list(dict.fromkeys(source_memory_ids))
        scoped_memories = self.db.get_memories_by_ids(requested_ids, scope=scope)
        memory_by_id = {
            int(memory.get("id")): memory
            for memory in scoped_memories
            if isinstance(memory, dict) and isinstance(memory.get("id"), int)
        }
        if set(memory_by_id) != set(requested_ids):
            logger.warning("[BeliefEngine] Scoped extraction rejected: source ids are absent or cross-scope")
            return []

        existing = self.db.list_scoped_beliefs(scope, limit=100)
        existing_by_id = {
            int(belief["id"]): belief
            for belief in existing
            if isinstance(belief, dict) and isinstance(belief.get("id"), int)
        }
        existing_text = "\n".join(
            f"[ID:{belief['id']}] {belief['content']} (type={belief['belief_type']}, support={float(belief.get('strength') or 0.0):.0%})"
            for belief in existing
        ) or "（暂无）"
        evidence_messages = "\n".join(
            f"[memory_id:{memory_id}] {str(memory_by_id[memory_id].get('sender_name') or memory_by_id[memory_id].get('sender_id') or 'unknown')}: "
            f"{str(memory_by_id[memory_id].get('content') or '')[:180]}"
            for memory_id in requested_ids
        )
        prompt = EXTRACT_PROMPT.format(
            summary=summary[:1500],
            evidence_messages=evidence_messages[:9000],
            existing_beliefs=existing_text[:6000],
        )
        window_key = self._window_key(scope, requested_ids)

        try:
            response = await self.llm.text_chat(prompt=prompt)
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            beliefs_data = json.loads(text)
            if not isinstance(beliefs_data, list):
                return []

            created: list[dict] = []
            for item in beliefs_data[:2]:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                belief_type = str(item.get("type") or "world_view").strip()
                evidence_ids = self._valid_evidence_ids(item.get("evidence_memory_ids"), requested_ids)
                challenge_ids = self._valid_evidence_ids(item.get("challenge_memory_ids"), requested_ids)
                if not content or len(content) < 5 or is_identity_contamination(content) or not evidence_ids:
                    continue
                if belief_type not in ("person_judgment", "world_view", "self_identity", "preference"):
                    belief_type = "world_view"

                anchor_sentence = self._safe_anchor_sentence(item.get("anchor_sentence"), evidence_ids, memory_by_id)
                relation = str(item.get("relation") or "new").strip().lower()
                if relation not in {"new", "reinforce", "challenge"}:
                    relation = "new"
                explicit_match = self._matching_existing_belief(item.get("match_id"), existing_by_id)
                target = explicit_match if relation in {"reinforce", "challenge"} else None
                if target is None and relation == "challenge":
                    target = next(
                        (
                            candidate
                            for raw_target_id in (item.get("challenges") or [])
                            for candidate in [self._matching_existing_belief(raw_target_id, existing_by_id)]
                            if candidate is not None
                        ),
                        None,
                    )
                if target is None and relation != "challenge":
                    target = self._find_similar(content, existing)
                    if target is None:
                        target = self._find_open_new_candidate(content, existing)
                    relation = "reinforce" if target is not None and not self._is_open_new_candidate(target) else "new"
                target_candidate_meta = (
                    target.get("provenance", {}).get("candidate")
                    if isinstance(target, dict) and isinstance(target.get("provenance"), dict)
                    else None
                )
                if target is not None and (target.get("status") == "archived" or (
                    isinstance(target_candidate_meta, dict)
                    and target_candidate_meta.get("resolution") == "open"
                    and target_candidate_meta.get("relation") != "new"
                )):
                    target = None
                    relation = "new"
                elif target is not None and self._is_open_new_candidate(target) and relation in {"reinforce", "challenge"}:
                    relation = "new"

                candidate_memory_ids = list(dict.fromkeys([*evidence_ids, *challenge_ids]))
                gate = self._relationship_gate(scope, candidate_memory_ids, memory_by_id)
                gate["scope"] = {
                    "bot_id": scope.bot_id,
                    "session_id": scope.session.id if scope.session else None,
                    "visibility": scope.visibility,
                }
                is_candidate = gate["decision"] != "direct"
                if is_candidate:
                    candidate_relation = relation if relation in {"reinforce", "challenge"} and target is not None else "new"
                    target_belief_id = int(target["id"]) if target is not None and candidate_relation != "new" else None
                    belief_key = candidate_fingerprint(
                        scope,
                        relation=candidate_relation,
                        target_belief_id=target_belief_id,
                        content=content,
                    )
                    candidate_row = next((row for row in existing if row.get("belief_key") == belief_key), None)
                    if candidate_row is not None and candidate_row.get("status") == "archived":
                        existing_candidate_meta = (candidate_row.get("provenance") or {}).get("candidate") if isinstance(candidate_row.get("provenance"), dict) else None
                        if isinstance(existing_candidate_meta, dict) and existing_candidate_meta.get("resolution") != "open":
                            continue
                    captured_target_revision = self._belief_revision(target) if target_belief_id is not None else None
                    if candidate_row is not None:
                        existing_candidate_meta = (candidate_row.get("provenance") or {}).get("candidate") if isinstance(candidate_row.get("provenance"), dict) else None
                        if isinstance(existing_candidate_meta, dict) and existing_candidate_meta.get("target_revision_at_capture") is not None:
                            captured_target_revision = existing_candidate_meta.get("target_revision_at_capture")
                    provenance = {
                        "producer": "consolidation",
                        "confidence_policy_version": POLICY_VERSION,
                        "window_memory_ids": requested_ids,
                        "anchor_sentence": anchor_sentence,
                        "gating": gate,
                        "candidate": {
                            "kind": "belief_candidate",
                            "fingerprint": belief_key,
                            "target_belief_id": target_belief_id,
                            "target_revision_at_capture": captured_target_revision,
                            "relation": candidate_relation,
                            "resolution": "open",
                        },
                    }
                    candidate_id = self.db.upsert_scoped_belief(
                        scope,
                        belief_key=belief_key,
                        content=content,
                        belief_type=belief_type,
                        strength=float(candidate_row.get("strength") or 0.0) if candidate_row else 0.0,
                        status="quarantined" if gate["decision"] == "quarantine" else "pending",
                        source_memory_id=(challenge_ids or evidence_ids)[0],
                        provenance=provenance,
                    )
                    candidate = {
                        "id": candidate_id,
                        "belief_key": belief_key,
                        "content": content,
                        "belief_type": belief_type,
                        "strength": 0.0,
                        "status": "quarantined" if gate["decision"] == "quarantine" else "pending",
                        "source_memory_id": (challenge_ids or evidence_ids)[0],
                        "provenance": provenance,
                    }
                    self._replace_existing(existing, existing_by_id, candidate)
                    refreshed = self._record_and_refresh(
                        scope,
                        candidate,
                        # Candidate evidence always measures support for the candidate itself;
                        # a challenge candidate is translated to target ``challenge`` only
                        # during the guarded merge.
                        polarity="support",
                        window_key=window_key,
                        memory_ids=challenge_ids or evidence_ids,
                        memory_by_id=memory_by_id,
                        anchor_sentence=anchor_sentence,
                        query_trace_id=query_trace_id,
                    )
                    self._replace_existing(existing, existing_by_id, refreshed)
                    created.append({
                        "id": refreshed["id"], "content": refreshed["content"],
                        "type": refreshed["belief_type"], "confidence": refreshed["strength"],
                        "status": refreshed.get("status"), "gate": gate,
                        "candidate": True,
                    })
                    continue

                changed_targets: set[int] = set()
                existing_open_candidate = self._is_open_new_candidate(target)
                if target is not None and relation == "challenge":
                    if is_candidate:
                        continue
                    refreshed = self._record_and_refresh(
                        scope,
                        target,
                        polarity="challenge",
                        window_key=window_key,
                        memory_ids=challenge_ids or evidence_ids,
                        memory_by_id=memory_by_id,
                        anchor_sentence=anchor_sentence,
                        query_trace_id=query_trace_id,
                    )
                    refreshed["provenance"]["gating"] = gate
                    self.db.upsert_scoped_belief(
                        scope,
                        belief_key=refreshed["belief_key"],
                        content=refreshed["content"],
                        belief_type=refreshed["belief_type"],
                        strength=float(refreshed.get("strength") or 0.0),
                        status=refreshed.get("status") or target.get("status") or "pending",
                        source_memory_id=refreshed.get("source_memory_id"),
                        provenance=refreshed["provenance"],
                    )
                    self._replace_existing(existing, existing_by_id, refreshed)
                    changed_targets.add(int(target["id"]))
                else:
                    is_new = target is None
                    if is_new:
                        belief_key = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:32]
                        initial_status = "quarantined" if gate["decision"] == "quarantine" else "pending"
                        belief_id = self.db.upsert_scoped_belief(
                            scope,
                            belief_key=belief_key,
                            content=content,
                            belief_type=belief_type,
                            strength=0.0,
                            status=initial_status,
                            source_memory_id=evidence_ids[0],
                            provenance={
                                "producer": "consolidation",
                                "confidence_policy_version": POLICY_VERSION,
                                "window_memory_ids": requested_ids,
                                "anchor_sentence": anchor_sentence,
                                "gating": gate,
                            },
                        )
                        target = {
                            "id": belief_id,
                            "belief_key": belief_key,
                            "content": content,
                            "belief_type": belief_type,
                            "strength": 0.0,
                            "status": initial_status,
                            "source_memory_id": evidence_ids[0],
                            "provenance": {"gating": gate},
                        }
                        existing.append(target)
                        existing_by_id[belief_id] = target
                    refreshed = self._record_and_refresh(
                        scope,
                        target,
                        polarity="support",
                        window_key=window_key,
                        memory_ids=evidence_ids,
                        memory_by_id=memory_by_id,
                        anchor_sentence=anchor_sentence,
                        query_trace_id=query_trace_id,
                    )
                    refreshed["provenance"]["gating"] = gate
                    if existing_open_candidate and gate["decision"] == "direct":
                        candidate_meta = refreshed["provenance"].get("candidate")
                        if isinstance(candidate_meta, dict):
                            candidate_meta = dict(candidate_meta)
                            candidate_meta.update({"resolution": "promoted", "resolution_reason": "relationship_direct"})
                            refreshed["provenance"]["candidate"] = candidate_meta
                    # Direct only skips review; it never bypasses evidence-v1.
                    if gate["decision"] == "direct" and is_activation_eligible(refreshed.get("provenance")):
                        refreshed["status"] = "active"
                    elif gate["decision"] == "quarantine":
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
                    self._replace_existing(existing, existing_by_id, refreshed)
                    changed_targets.add(int(refreshed["id"]))
                    if is_new:
                        created.append({
                            "id": refreshed["id"], "content": refreshed["content"],
                            "type": refreshed["belief_type"], "confidence": refreshed["strength"],
                            "status": refreshed.get("status"), "gate": gate,
                        })
                        logger.info("[BeliefEngine] New scoped belief observation: %s... (type=%s, gate=%s)", content[:50], belief_type, gate["decision"])

                for raw_target_id in item.get("challenges") or []:
                    challenged = self._matching_existing_belief(raw_target_id, existing_by_id)
                    if challenged is None or int(challenged["id"]) in changed_targets:
                        continue
                    if gate["decision"] != "direct":
                        continue
                    refreshed = self._record_and_refresh(
                        scope,
                        challenged,
                        polarity="challenge",
                        window_key=window_key,
                        memory_ids=challenge_ids or evidence_ids,
                        memory_by_id=memory_by_id,
                        anchor_sentence=anchor_sentence,
                        query_trace_id=query_trace_id,
                    )
                    self._replace_existing(existing, existing_by_id, refreshed)
                    changed_targets.add(int(challenged["id"]))
            return created
        except json.JSONDecodeError:
            logger.debug("[BeliefEngine] Failed to parse LLM output as JSON")
            return []
        except Exception as exc:
            logger.debug("[BeliefEngine] Scoped extract failed: %s", exc)
            return []

    @staticmethod
    def _window_key(scope: RuntimeScope, memory_ids: list[int]) -> str:
        assert scope.session is not None
        return f"consolidation:{scope.session.id}:{memory_ids[0]}:{memory_ids[-1]}"

    @staticmethod
    def _valid_evidence_ids(raw_ids: Any, allowed_ids: list[int]) -> list[int]:
        if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, list):
            return []
        allowed = set(allowed_ids)
        result: list[int] = []
        for raw_id in raw_ids:
            if isinstance(raw_id, bool):
                continue
            try:
                memory_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if memory_id in allowed and memory_id not in result:
                result.append(memory_id)
        return result

    @staticmethod
    def _matching_existing_belief(raw_id: Any, beliefs_by_id: dict[int, dict]) -> dict | None:
        if isinstance(raw_id, bool):
            return None
        try:
            belief_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        return beliefs_by_id.get(belief_id)

    @staticmethod
    def _safe_anchor_sentence(raw_anchor: Any, evidence_ids: list[int], memory_by_id: dict[int, dict]) -> str:
        source_text = "\n".join(str(memory_by_id[memory_id].get("content") or "") for memory_id in evidence_ids)
        anchor = str(raw_anchor or "").strip()
        if anchor and len(anchor) <= 240 and anchor in source_text:
            return anchor
        return str(memory_by_id[evidence_ids[0]].get("content") or "").strip()[:240]

    @staticmethod
    def _replace_existing(existing: list[dict], beliefs_by_id: dict[int, dict], refreshed: dict) -> None:
        belief_id = int(refreshed["id"])
        beliefs_by_id[belief_id] = refreshed
        for index, row in enumerate(existing):
            if int(row.get("id", -1)) == belief_id:
                existing[index] = refreshed
                break
        else:
            existing.append(refreshed)

    def _record_and_refresh(
        self,
        scope: RuntimeScope,
        belief: dict,
        *,
        polarity: str,
        window_key: str,
        memory_ids: list[int],
        memory_by_id: dict[int, dict],
        anchor_sentence: str,
        query_trace_id: str | None,
    ) -> dict:
        participants = [
            str(memory_by_id[memory_id].get("sender_id") or memory_by_id[memory_id].get("sender_name") or "").strip()
            for memory_id in memory_ids
        ]
        timestamps = [
            float(memory_by_id[memory_id].get("timestamp") or 0.0)
            for memory_id in memory_ids
        ]
        repo = getattr(self.db, "scoped_knowledge", None)
        tag_getter = getattr(repo, "list_scoped_memory_tags", None)
        observation_tags: list[dict] = []
        if callable(tag_getter):
            try:
                observation_tags = list(tag_getter(scope, memory_ids) or [])
            except Exception:
                observation_tags = []
        self.db.record_scoped_belief_observation(
            scope,
            belief_id=int(belief["id"]),
            window_key=window_key,
            polarity=polarity,
            memory_ids=memory_ids,
            participants=participants,
            source_tags=observation_tags,
            metadata={"producer": "consolidation", "anchor_sentence": anchor_sentence},
            window_started_at=min(timestamps) if timestamps else time.time(),
            window_ended_at=max(timestamps) if timestamps else time.time(),
        )
        return self._refresh_from_observations(
            scope,
            belief,
            anchor_sentence=anchor_sentence,
            query_trace_id=query_trace_id,
        )

    def refresh_evidence_after_tags(self, scope: RuntimeScope, belief_id: int) -> dict | None:
        """Recompute evidence-v1 after TagWorker later completes the tag chain.

        Does not create new observations. Quarantine and non-direct gates stay
        isolated; only ``direct + activation_eligible`` may become active.
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
        gating = refreshed.get("provenance", {}).get("gating") if isinstance(refreshed.get("provenance"), dict) else {}
        decision = str(gating.get("decision") or "")
        if decision == "direct" and is_activation_eligible(refreshed.get("provenance")):
            refreshed["status"] = "active"
        elif decision == "quarantine":
            refreshed["status"] = "quarantined"
        else:
            refreshed["status"] = "pending" if status != "quarantined" else "quarantined"
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

        active_beliefs = self.db.list_scoped_beliefs(scope, status="active")
        beliefs: list[dict] = [
            belief for belief in active_beliefs
            if isinstance(belief, dict) and belief.get("status") == "active"
            and self._evidence_ready(belief)
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
        unique_beliefs = [belief for belief in unique_beliefs if self._evidence_ready(belief)]

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

        if formal_relationship_available and sender_id and response_policy:
            lines.append("<relationship_guidance>")
            lines.append(f"- {response_policy.get('boundary')}")
            if response_policy.get("privacy_openness") == "safe_public_topics_only":
                lines.append("- 信任较高时可自然承接对方主动公开的话题，但不要主动泄露私人信息。")
            if response_policy.get("playfulness") == "light":
                lines.append("- 对话氛围允许轻度玩笑，但不要用玩笑替代事实或越过边界。")
            lines.append("</relationship_guidance>")

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

    @staticmethod
    def _evidence_ready(belief: dict) -> bool:
        provenance = belief.get("provenance") if isinstance(belief.get("provenance"), dict) else {}
        return is_activation_eligible(provenance)

    def _is_duplicate(self, content: str, existing: list[dict]) -> bool:
        """简单文本相似度去重。"""
        content_lower = content.lower()
        for b in existing:
            if b.get("status") == "archived":
                continue
            candidate = b.get("provenance", {}).get("candidate") if isinstance(b.get("provenance"), dict) else None
            if isinstance(candidate, dict) and candidate.get("resolution") == "open":
                continue
            existing_lower = b["content"].lower()
            # 简单 Jaccard
            words_new = set(content_lower)
            words_old = set(existing_lower)
            if len(words_new & words_old) / max(len(words_new | words_old), 1) > 0.6:
                return True
        return False

    @staticmethod
    def _is_open_new_candidate(belief: Mapping[str, Any] | None) -> bool:
        if not isinstance(belief, Mapping):
            return False
        provenance = belief.get("provenance") if isinstance(belief.get("provenance"), dict) else {}
        candidate = provenance.get("candidate") if isinstance(provenance.get("candidate"), dict) else {}
        return candidate.get("resolution") == "open" and candidate.get("relation") == "new"

    def _find_open_new_candidate(self, content: str, existing: list[dict]) -> Optional[dict]:
        """Reuse an unresolved ``relation=new`` candidate when trust later improves."""
        content_lower = content.lower()
        for belief in existing:
            if belief.get("status") == "archived":
                continue
            provenance = belief.get("provenance") if isinstance(belief.get("provenance"), dict) else {}
            candidate = provenance.get("candidate") if isinstance(provenance.get("candidate"), dict) else {}
            if candidate.get("resolution") != "open" or candidate.get("relation") != "new":
                continue
            existing_lower = str(belief.get("content") or "").lower()
            words_new = set(content_lower)
            words_old = set(existing_lower)
            score = len(words_new & words_old) / max(len(words_new | words_old), 1)
            if score > 0.6:
                return belief
        return None

    def _find_similar(self, content: str, existing: list[dict]) -> Optional[dict]:
        """找到最相似的已有信念。"""
        content_lower = content.lower()
        best = None
        best_score = 0
        for b in existing:
            if b.get("status") == "archived":
                continue
            candidate = b.get("provenance", {}).get("candidate") if isinstance(b.get("provenance"), dict) else None
            if isinstance(candidate, dict) and candidate.get("resolution") == "open":
                continue
            existing_lower = b["content"].lower()
            words_new = set(content_lower)
            words_old = set(existing_lower)
            score = len(words_new & words_old) / max(len(words_new | words_old), 1)
            if score > best_score:
                best_score = score
                best = b
        return best if best_score > 0.6 else None
