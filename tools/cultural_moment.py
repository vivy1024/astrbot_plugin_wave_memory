"""WaveMemory cultural moment tool — flags potential group jargon and exemplar character replies."""

from __future__ import annotations

import json
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
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


@dataclass
class WaveMemoryMarkCulturalMomentTool(FunctionTool[AstrAgentContext]):
    """标记群聊中涌现的潜在黑话/梗，或标记自己刚作出的高光风骨回复以供提审。"""

    name: str = "wave_memory_mark_cultural_moment"
    description: str = (
        "标记当前对话中值得关注的群文化元素（潜在新黑话、名场面梗），或自认作出了极具实事求是风骨的高光回答。"
        "必须提供 moment_type, target_phrase_or_snippet, context_note。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "moment_type": {
                "type": "string",
                "enum": ["potential_jargon", "exemplar_reply"],
                "description": "标记类型：potential_jargon(群友抛出的潜在新黑话/梗)/exemplar_reply(自己刚才给出的高质量有风骨回复)",
            },
            "target_phrase_or_snippet": {
                "type": "string",
                "description": "具体的黑话词条，或回复的核心亮点摘要",
            },
            "context_note": {
                "type": "string",
                "description": "简要说明该词在该语境下的含义，或这次回复体现了什么原则",
            },
        },
        "required": ["moment_type", "target_phrase_or_snippet", "context_note"],
    })

    db: Any = field(default=None, repr=False)
    jargon_service: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db:
            return "数据库未初始化"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        runtime_scope, error_code = require_group_runtime_scope(ctx, "cultural_moment.mark")
        if error_code:
            return scope_error_message("文化高光标记", error_code)
        assert runtime_scope is not None

        moment_type = str(kwargs.get("moment_type") or "").strip()
        target_phrase = str(kwargs.get("target_phrase_or_snippet") or "").strip()
        context_note = str(kwargs.get("context_note") or "").strip()

        if not moment_type or not target_phrase or not context_note:
            return "moment_type、target_phrase_or_snippet 与 context_note 均为必填项"

        now = time.time()

        # 1. 潜在黑话 -> 写入 scoped_jargon 候选待审区
        if moment_type == "potential_jargon":
            repo = getattr(self.db, "scoped_knowledge", None)
            if repo is not None and hasattr(repo, "upsert_scoped_jargon"):
                try:
                    repo.upsert_scoped_jargon(
                        runtime_scope,
                        word=target_phrase,
                        meaning=context_note,
                        status="pending",
                        is_jargon=True,
                        confidence=0.85,
                        provenance={
                            "source": "bot_marked_moment",
                            "context_note": context_note,
                            "marked_at": now,
                        },
                    )
                except Exception as e:
                    return f"黑话候选标记失败: {e}"
            return f"已将潜在黑话「{target_phrase}」提交至本群待审候选区：{context_note}"

        # 2. 高光风骨回复 -> 记录为候选 FewShot 提审
        if moment_type == "exemplar_reply":
            try:
                self.db.conn.execute(
                    """CREATE TABLE IF NOT EXISTS exemplar_reply_candidates (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           bot_id TEXT, group_id TEXT,
                           snippet TEXT, context_note TEXT,
                           created_at REAL
                       )"""
                )
                self.db.conn.execute(
                    """INSERT INTO exemplar_reply_candidates (bot_id, group_id, snippet, context_note, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (runtime_scope.bot_id, runtime_scope.session.conversation_id, target_phrase, context_note, now),
                )
                self.db.conn.commit()
            except Exception as e:
                return f"风骨范式标记失败: {e}"
            return f"已标记高光回复范式「{target_phrase[:40]}...」：{context_note}"

        return "未知的标记类型"
