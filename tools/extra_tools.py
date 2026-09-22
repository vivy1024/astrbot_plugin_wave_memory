"""Wave Memory 扩展工具。

事实查询已经迁移到 scoped repository。好感度与标签图谱仍依赖无
canonical scope 的 legacy read-model，因此在完成数据面迁移前必须拒绝。
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
class WaveMemoryAffinityTool(FunctionTool[AstrAgentContext]):
    """已隔离：legacy social read-model 不能证明 canonical RuntimeScope。"""

    name: str = "wave_memory_affinity"
    description: str = "互动关系查询正在进行 Scope 数据面迁移，当前不可用。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "迁移完成后可查询的群友"},
            "mode": {
                "type": "string", "enum": ["single", "ranking", "active"]},
        },
        "required": [],
    })

    db: Any = field(default=None, repr=False)
    bot_db_ids: dict[str, str] = field(default_factory=dict, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        # user_profiles/memories legacy read-model 不能完整表示 platform+bot+session。
        return scope_error_message("互动关系查询", "scope_migration_required")


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


@dataclass
class WaveMemoryTagGraphTool(FunctionTool[AstrAgentContext]):
    """已隔离：legacy tag graph 不能证明 canonical RuntimeScope。"""

    name: str = "wave_memory_tag_graph"
    description: str = "标签关系图正在进行 Scope 数据面迁移，当前不可用。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "tag_name": {"type": "string", "description": "迁移完成后可查询的标签名"},
        },
        "required": ["tag_name"],
    })

    db: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        # tags/memory_tags/tag_relations 均是无完整 scope 的 legacy 投影。
        return scope_error_message("标签关系图查询", "scope_migration_required")


__all__ = [
    "WaveMemoryAffinityTool",
    "WaveMemoryFactsTool",
    "WaveMemoryTagGraphTool",
]
