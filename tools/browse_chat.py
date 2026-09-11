"""WaveMemory 聊天记录浏览工具 — 供定时任务与日记反思调阅近期真实群聊流水。"""

from __future__ import annotations

import datetime
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
    T = TypeVar("T")
    class FunctionTool(Generic[T]): pass
    class ContextWrapper(Generic[T]): pass
    class AstrAgentContext: pass

try:
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


@dataclass
class WaveMemoryBrowseRecentChatTool(FunctionTool[AstrAgentContext]):
    """调阅当前群过去一段时间内的真实群聊流水，供撰写日记、反思或总结使用。"""

    name: str = "wave_memory_browse_recent_chat"
    description: str = (
        "调阅当前群聊在过去指定小时内（默认24小时）的真实聊天记录流水；"
        "撰写今日日记、心智自省或回顾今天发生过什么时使用。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "hours": {
                "type": "number",
                "description": "查看过去多少小时内的群聊（默认 24.0）",
                "default": 24.0,
            },
            "limit": {
                "type": "integer",
                "description": "最多返回的消息条数（默认 60，最大 150）",
                "default": 60,
            },
        },
        "required": [],
    })
    db: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        scope, error = require_group_runtime_scope(ctx, "chat.browse")
        if error:
            # 允许在群聊中回退使用 episode.note 权限
            scope, error2 = require_group_runtime_scope(ctx, "episode.note")
            if error2:
                return scope_error_message("群聊记录浏览", error)

        if self.db is None or scope is None or scope.session is None:
            return "数据库或群 Scope 未初始化"

        hours = max(0.5, min(72.0, float(kwargs.get("hours", 24.0) or 24.0)))
        limit = max(10, min(150, int(kwargs.get("limit", 60) or 60)))
        now = time.time()
        from_ts = now - hours * 3600.0

        conn = getattr(self.db, "conn", None)
        if conn is None:
            return "数据库连接不可用"

        try:
            sql = """
                SELECT sender_name, sender_id, content, timestamp
                FROM memories
                WHERE group_id = ?
                  AND timestamp >= ?
                  AND COALESCE(quarantine, 0) = 0
                  AND COALESCE(source, '') != 'noise'
                ORDER BY timestamp ASC
                LIMIT ?
            """
            rows = conn.execute(sql, (scope.session.conversation_id, from_ts, limit)).fetchall()
            if not rows:
                return f"过去 {hours:g} 小时内当前群没有记录到有效的群聊消息。"

            lines = [f"=== 当前群过去 {hours:g} 小时内的群聊记录（共 {len(rows)} 条，按时间正序）==="]
            for r in rows:
                dt_str = datetime.datetime.fromtimestamp(r[3]).strftime("%H:%M:%S")
                name = str(r[0] or "").strip() or str(r[1] or "群友")
                content = str(r[2] or "").strip().replace("\n", " ")
                lines.append(f"[{dt_str}] {name}({r[1]}): {content}")

            return "\n".join(lines)
        except Exception as exc:
            return f"读取群聊记录失败: {exc}"


__all__ = ["WaveMemoryBrowseRecentChatTool"]
