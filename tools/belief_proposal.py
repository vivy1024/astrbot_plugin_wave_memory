"""WaveMemory belief proposal tool — proposes stable judgments backed by approved facts."""

from __future__ import annotations

import hashlib
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
    from ..services.belief_engine import approved_source_fact_ids, first_memory_id_from_facts
    from ..services.identity_safety import is_identity_contamination
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover
    from services.belief_engine import approved_source_fact_ids, first_memory_id_from_facts
    from services.identity_safety import is_identity_contamination
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


def _parse_fact_ids(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
        raw = parts
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    ids: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            fact_id = int(item)
        except (TypeError, ValueError):
            continue
        if fact_id <= 0 or fact_id in seen:
            continue
        seen.add(fact_id)
        ids.append(fact_id)
    return ids


@dataclass
class WaveMemoryProposeBeliefTool(FunctionTool[AstrAgentContext]):
    """提审一条由已审事实支撑的稳定判断。"""

    name: str = "wave_memory_propose_belief"
    description: str = (
        "仅当至少两条已审核事实已经撑住同一判断时调用。"
        "用于对人的稳定判断、对事的看法或处事偏好，不是人生感悟。"
        "必须提供 belief_type, content, grounding_evidence, source_fact_ids。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "belief_type": {
                "type": "string",
                "enum": ["person_judgment", "world_view", "preference"],
                "description": "稳定判断类型：person_judgment(对人)/world_view(对事)/preference(处事偏好)",
            },
            "content": {
                "type": "string",
                "description": "一句话稳定判断（例如：'小明嘴上损，但朋友有难处时不会推脱'）",
            },
            "grounding_evidence": {
                "type": "string",
                "description": "为何认为这些已审事实已经够撑住这条判断",
            },
            "source_fact_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "至少两条当前群内已批准事实 id",
            },
        },
        "required": ["belief_type", "content", "grounding_evidence", "source_fact_ids"],
    })

    db: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db:
            return "数据库未初始化"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        runtime_scope, error_code = require_group_runtime_scope(ctx, "belief_proposal.propose")
        if error_code:
            return scope_error_message("信念提审", error_code)
        assert runtime_scope is not None

        belief_type = str(kwargs.get("belief_type") or "").strip()
        content = str(kwargs.get("content") or "").strip()
        grounding_evidence = str(kwargs.get("grounding_evidence") or "").strip()
        requested_ids = _parse_fact_ids(kwargs.get("source_fact_ids"))

        if not belief_type or not content or not grounding_evidence:
            return "belief_type、content 与 grounding_evidence 均为必填项"
        if belief_type not in {"person_judgment", "world_view", "preference"}:
            return "belief_type 必须是 person_judgment, world_view 或 preference 之一"
        if len(requested_ids) < 2:
            return "必须提供至少两条已审核事实 id，请先走事实一审"

        if is_identity_contamination(content) or is_identity_contamination(grounding_evidence):
            return "信念提审被拒绝：内容或证据检测到身份角色扮演污染"

        repo = getattr(self.db, "scoped_knowledge", None)
        if repo is None or not hasattr(repo, "upsert_scoped_belief"):
            return "scoped_knowledge 仓储不可用"

        approved_ids = approved_source_fact_ids(repo, runtime_scope, requested_ids)
        if len(approved_ids) < 2:
            return "信念提审被拒绝：source_fact_ids 必须是当前群内至少两条已批准事实，请先完成事实一审"

        memory_id = first_memory_id_from_facts(repo, runtime_scope, approved_ids)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        belief_key = f"{belief_type}:{content_hash}"
        try:
            belief_id = repo.upsert_scoped_belief(
                runtime_scope,
                belief_key=belief_key,
                content=content,
                belief_type=belief_type,
                strength=0.65,
                status="pending",
                source_memory_id=memory_id,
                provenance={
                    "source": "llm_reflection",
                    "evidence": grounding_evidence[:200],
                    "source_fact_ids": approved_ids,
                    "proposed_at": time.time(),
                },
            )
            return f"已成功提审信念：[{belief_type}] {content} (待审ID: {belief_id})"
        except Exception as e:
            return f"信念提审写入失败: {e}"
