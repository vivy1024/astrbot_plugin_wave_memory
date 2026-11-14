"""Wave Memory 扩展工具：scoped facts 三元组查询。

好感查询见 ``affinity_update.WaveMemoryAffinityTool``；旧的 legacy 占位工具已移除。
"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

try:
    from .scope_boundary import require_read_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover - direct tools imports in isolated tests
    from tools.scope_boundary import require_read_runtime_scope, scope_error_message


@dataclass
class WaveMemoryFactsTool(FunctionTool[AstrAgentContext]):
    """查询 scoped facts 三元组（知识图谱）。"""

    name: str = "wave_memory_facts"
    description: str = (
        "搜索当前群聊作用域内的事实三元组（知识图谱）。"
        "可用关键词匹配主体、谓语、客体。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，会匹配主体、谓语、客体",
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数量，默认 10",
                "default": 10,
            },
        },
        "required": ["query"],
    })

    db: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query", "") or "").strip()
        try:
            limit = max(1, min(int(kwargs.get("limit", 10)), 50))
        except (TypeError, ValueError):
            limit = 10

        if not query:
            return "请提供搜索关键词"

        scope, error_code = require_read_runtime_scope(ctx, "fact.read")
        if error_code:
            return scope_error_message("事实查询", error_code)
        assert scope is not None

        if not self.db:
            return "数据库未初始化"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        repository = getattr(self.db, "scoped_knowledge", None)
        if repository is None:
            return "Scoped Knowledge Repository 未初始化，已拒绝事实查询"

        # 只通过 canonical RuntimeScope 读取，避免 tools 直接访问 legacy facts 表。
        rows = repository.list_scoped_facts(scope, limit=limit * 5)
        needle = query.casefold()
        rows = [
            row for row in rows
            if needle in str(row.get("subject", "")).casefold()
            or needle in str(row.get("predicate", "")).casefold()
            or needle in str(row.get("object", "")).casefold()
        ]
        rows = sorted(rows, key=lambda row: float(row.get("confidence") or 0.0), reverse=True)[:limit]

        if not rows:
            return f"没有找到与「{query}」相关的事实"

        lines = [f"找到 {len(rows)} 条事实:"]
        for row in rows:
            subject = row.get("subject", "")
            predicate = row.get("predicate", "")
            object_ = row.get("object", "")
            confidence = float(row.get("confidence") or 0.0)
            lines.append(f"  {subject} → {predicate} → {object_} (置信度:{confidence:.1f})")
        return "\n".join(lines)


__all__ = [
    "WaveMemoryFactsTool",
]
