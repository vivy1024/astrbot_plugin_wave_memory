"""WaveMemory fact proposal tool — proposes newly discovered facts from conversational context."""

from __future__ import annotations

import time
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

try:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover
    from typing import Generic, TypeVar
    _T = TypeVar("_T")
    class FunctionTool(Generic[_T]): pass
    class ContextWrapper(Generic[_T]): pass
    class AstrAgentContext: pass

try:
    from ..domain.scope import RuntimeScope
    from ..services.identity_safety import is_identity_contamination
    from ..services.fact_policy import get_fact_policy, valid_subject
    from ..engine.db.fact_policy_repo import FactPolicyRepo, source_memory, resolve_subject
    from .scope_boundary import require_memory_runtime_scope, resolve_source_memory_id, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from services.identity_safety import is_identity_contamination
    from services.fact_policy import get_fact_policy, valid_subject
    from engine.db.fact_policy_repo import FactPolicyRepo, source_memory, resolve_subject
    from tools.scope_boundary import require_memory_runtime_scope, resolve_source_memory_id, scope_error_message


def fact_importance(value: Any) -> int:
    """事实重要性 1-5（默认 3）：只用于想起时排序，不作入库门槛。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))


def _auto_reviewer():
    try:
        from ..services.auto_review import get_auto_reviewer
    except ImportError:  # pragma: no cover
        from services.auto_review import get_auto_reviewer
    return get_auto_reviewer()


_IRONY_MARKERS = ("irony", "sarcasm", "ironic", "反串", "阴阳")


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _explicit_irony(payload: Any) -> bool:
    blob = payload
    if isinstance(payload, dict):
        blob = " ".join(str(item) for item in payload.values())
    text = str(blob or "").strip().casefold()
    return any(marker in text for marker in _IRONY_MARKERS)


def collect_jargon_hits(quote: str, runtime_scope: Any, jargon_service: Any, repo: Any) -> list[dict[str, Any]]:
    """Read-only jargon matches for a source quote. Never mines or classifies irony."""
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    text = str(quote or "")
    if not text:
        return hits

    if repo is not None and hasattr(repo, "list_scoped_jargon"):
        try:
            rows = repo.list_scoped_jargon(runtime_scope, limit=200) or []
        except Exception:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip()
            word = str(row.get("word") or "").strip()
            if status not in {"active", "approved", "confirmed", ""} or not word or word in seen or word not in text:
                continue
            seen.add(word)
            meaning = str(row.get("meaning") or "").strip()
            hits.append({
                "word": word,
                "meaning": meaning,
                "source": "scoped",
                "status": status or "active",
                "irony": _explicit_irony(row.get("provenance")) or _explicit_irony(meaning),
            })

    holyman = getattr(jargon_service, "_holyman", None)
    matcher = getattr(holyman, "match_text", None)
    if callable(matcher):
        try:
            matched = matcher(text, max_items=8) or []
        except Exception:
            matched = []
        for item in matched:
            row = _as_mapping(item)
            word = str(row.get("term") or row.get("word") or "").strip()
            if not word or word in seen:
                continue
            seen.add(word)
            meaning = str(row.get("explanation") or row.get("meaning") or "").strip()
            hits.append({
                "word": word,
                "meaning": meaning,
                "source": "holyman",
                "status": str(row.get("classification") or "global_abstract"),
                "irony": _explicit_irony(row) or _explicit_irony(meaning),
            })
    return hits


@dataclass
class WaveMemoryProposeFactTool(FunctionTool[AstrAgentContext]):
    """在对话或反思中提审关于某个群友或群实体的明确客观事实。"""

    name: str = "wave_memory_propose_fact"
    description: str = (
        "在对话互动中发现了关于群友或事物的明确、高价值客观事实（如工作、专业、居住地、计划等）时调用。"
        "必须提供 subject, predicate, object, source_quote, context_evidence。"
        "原话里的黑话只用于理解，不得当成事实内容。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "明确的人名、可解析的平台账号或实体名；不能填空主体、代词或未知占位符",
            },
            "predicate": {
                "type": "string",
                "description": "关系或动作谓词（例如 '住在'、'本科学历是'、'正在备考'、'擅长'）",
            },
            "object": {
                "type": "string",
                "description": (
                    "事实客体或属性值（例如 '上海'、'计算机'、'考研'、'Python'）。"
                    "必须是 source_quote 里**逐字连续出现**的一段：不许概括、改写、补全或换语序。"
                    "摘不出逐字片段就不要提这条事实"
                ),
            },
            "source_quote": {
                "type": "string",
                "description": "支撑该事实的原话摘录，必须逐字照抄，不能只写模型意译",
            },
            "context_evidence": {
                "type": "string",
                "description": "当面交流中确认该事实的依据或前因后果（1句话，确保真实有据）",
            },
            "source_memory_id": {
                "type": "integer",
                "description": "可选。当轮或近期记忆 id",
            },
            "valid_from": {
                "type": "string",
                "description": "可选，原话明确给出的生效日期或 ISO 8601 时间；未知时省略，不推测日期",
            },
            "valid_until": {
                "type": "string",
                "description": "可选，原话明确给出的截止日期或 ISO 8601 时间；日期含当天，时间为排他上界",
            },
            "importance": {
                "type": "integer",
                "description": "可选，1-5：这件事对了解这个人有多重要。5=身份/长期（职业、生日、家人、重大经历），"
                "3=一般喜好习惯，1=一时的琐事。只影响以后想起它的先后，不会因为不重要就不记。默认 3",
            },
        },
        "required": ["subject", "predicate", "object", "source_quote", "context_evidence"],
    })

    db: Any = field(default=None, repr=False)
    jargon_service: Any = field(default=None, repr=False)
    fact_policy: Any = field(default=None, repr=False)
    write_gateway: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db:
            return "数据库未初始化"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        runtime_scope, error_code = require_memory_runtime_scope(ctx, "fact_proposal.propose")
        if error_code:
            return scope_error_message("事实提审", error_code)
        assert runtime_scope is not None

        subject = str(kwargs.get("subject") or "").strip()
        predicate = str(kwargs.get("predicate") or "").strip()
        obj = str(kwargs.get("object") or "").strip()
        source_quote = str(kwargs.get("source_quote") or "").strip()
        context_evidence = str(kwargs.get("context_evidence") or "").strip()
        raw_memory_id = kwargs.get("source_memory_id")

        if not subject or not predicate or not obj or not source_quote or not context_evidence:
            return "subject、predicate、object、source_quote 与 context_evidence 均为必填项"
        if not valid_subject(subject):
            return "事实提审被拒绝：需要明确有效的主体（invalid_fact_subject）"

        if (
            is_identity_contamination(subject)
            or is_identity_contamination(predicate)
            or is_identity_contamination(obj)
            or is_identity_contamination(source_quote)
            or is_identity_contamination(context_evidence)
        ):
            return "事实提审被拒绝：内容或证据检测到身份角色扮演污染"

        repo = getattr(self.db, "scoped_knowledge", None)
        if repo is None or not hasattr(repo, "upsert_scoped_fact"):
            return "scoped_knowledge 仓储不可用"

        jargon_hits = collect_jargon_hits(
            source_quote,
            runtime_scope,
            self.jargon_service,
            repo,
        )
        ironic_hits = [hit for hit in jargon_hits if hit.get("irony")]
        if ironic_hits:
            words = "、".join(str(hit.get("word") or "") for hit in ironic_hits if hit.get("word"))
            return (
                f"事实提审被拒绝：原话中的「{words}」已标记为反串/阴阳，不能升格为认真事实。"
                "如需记录梗本身，请改用 wave_memory_mark_cultural_moment。"
            )

        source_memory_id = resolve_source_memory_id(
            self.db,
            runtime_scope,
            quote=source_quote,
            explicit_id=raw_memory_id,
        )
        cm = getattr(repo, "cm", None)
        if cm is None:
            return "事实专用仓储不可用"
        source = source_memory(cm.conn, runtime_scope, source_memory_id, source_quote)
        # The legacy resolver may return an unrelated latest message. Keep an
        # unverified proposal pending instead of presenting that ID as evidence.
        if source is None:
            source_memory_id = None
        policy = self.fact_policy or get_fact_policy()
        try:
            validity = policy.validity(predicate=predicate, object=obj, source_quote=source_quote,
                                       source_timestamp=(source or {}).get("timestamp"),
                                       valid_from=kwargs.get("valid_from"), valid_until=kwargs.get("valid_until"))
        except ValueError as exc:
            return f"事实提审被拒绝：无效的有效期（{exc}）"

        needs_jargon_review = any(
            hit.get("word") and not str(hit.get("meaning") or "").strip()
            for hit in jargon_hits
        )
        provenance = {
            "source": "llm_reflection",
            "evidence": context_evidence,
            "source_quote": source_quote,
            "jargon_hits": [
                {key: hit[key] for key in ("word", "meaning", "source", "status") if key in hit}
                for hit in jargon_hits
            ],
            "needs_jargon_review": needs_jargon_review,
            "proposed_at": time.time(),
            "source_timestamp": (source or {}).get("timestamp"),
            "resolved_subject": resolve_subject(cm.conn, runtime_scope, subject, source, source_quote),
            "temporal_policy": {**validity.metadata(), "timezone": policy.timezone},
            "importance": fact_importance(kwargs.get("importance")),
        }

        try:
            fact_repo = FactPolicyRepo(cm)
            reviewer = _auto_reviewer()
            if reviewer is not None and getattr(reviewer, "db", None) is not self.db:
                reviewer = None
            writer = self.write_gateway or getattr(self.db, "write_gateway", None)
            if writer is None and reviewer is not None and reviewer.db is self.db:
                writer = reviewer.write_gateway
            proposal = dict(
                subject=subject,
                predicate=predicate,
                object=obj,
                confidence=0.85,
                source_memory_id=source_memory_id,
                provenance=provenance,
                # Infer relative dates again with evidence verified inside the writer.
                valid_from=kwargs.get("valid_from"),
                valid_until=kwargs.get("valid_until"),
            )
            if writer is not None:
                try:
                    from ..services.scoped_knowledge_mutations import ScopedKnowledgeMutationGateway
                except ImportError:  # pragma: no cover
                    from services.scoped_knowledge_mutations import ScopedKnowledgeMutationGateway
                result = await ScopedKnowledgeMutationGateway(writer, fact_policy=policy).propose_fact(
                    scope=runtime_scope, **proposal)
                fact_id = result.locator
            else:
                # Compatibility for standalone repositories without a runtime writer.
                fact_id = fact_repo.propose(runtime_scope, **proposal, policy=policy)
        except Exception as e:
            return f"事实提审写入失败: {e}"
        status = await reviewer.review_fact(runtime_scope, fact_id) if reviewer is not None else None
        if status is None:
            status = (fact_repo.get(runtime_scope, fact_id) or {}).get("status")
        if status == "active":
            return f"已记录事实：[{subject}] {predicate} [{obj}]（已审核，按有效期生效，ID: {fact_id}）"
        if status in {"deleted", "superseded", "rejected"}:
            return f"已保存新的事实观察（ID: {fact_id}，原事实状态 {status} 保持不变，需人工复核历史）"
        if status == "conflict":
            return f"已提审事实：[{subject}] {predicate} [{obj}]，与已有事实冲突，留待人工裁决 (ID: {fact_id})"
        return f"已成功提审事实：[{subject}] {predicate} [{obj}] (待审ID: {fact_id})"
