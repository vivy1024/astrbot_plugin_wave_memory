"""Wave Memory LLM Tool — 让模型主动搜索和存储记忆"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

try:
    from .scope_boundary import (
        extract_memory_runtime_scope,
        require_memory_runtime_scope,
        scope_error_message,
    )
except ImportError:  # 兼容插件顶级加载
    from tools.scope_boundary import (
        extract_memory_runtime_scope,
        require_memory_runtime_scope,
        scope_error_message,
    )


def _extract_memory_scope(context: ContextWrapper[AstrAgentContext]):
    """返回基础 WaveMemory 工具允许的 group/private Scope。"""
    return extract_memory_runtime_scope(context)


# 在命中前后这么多个 id 里找同会话邻居：多个群、多个 Bot 交错写入，id 相邻的往往不是同一段对话。
_WINDOW_SPAN = 200
_WINDOW_SIDE = 2
_INACTIVE_TYPES = ("archived", "evicted", "deleted", "noise")


def _context_window(conn, memory_id: int, *, bot_id: str) -> list[tuple]:
    """命中记忆所在会话里、属于当前 Bot（或无归属旧行）的前后各 2 条，含命中本身。

    命中本身不属于当前 Bot 或已失效时返回空，调用方只显示命中片段。
    """
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        wanted = [name for name in ("session_id", "group_id", "bot_id") if name in columns]
        if not wanted:
            return []
        anchor = conn.execute(f"SELECT {', '.join(wanted)} FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if anchor is None:
            return []
        values = dict(zip(wanted, anchor))
        if str(values.get("bot_id") or "") not in {"", str(bot_id or "")}:
            return []
        if values.get("session_id"):
            same_conversation, key = "COALESCE(session_id, '') = ?", values["session_id"]
        elif "group_id" in values:
            same_conversation, key = "COALESCE(group_id, '') = ?", values.get("group_id") or ""
        else:
            return []
        conditions = [same_conversation]
        params: list = [key]
        if "bot_id" in columns:
            conditions.append("COALESCE(bot_id, '') IN (?, '')")
            params.append(str(bot_id or ""))
        if "quarantine" in columns:
            conditions.append("COALESCE(quarantine, 0) = 0")
        if "memory_type" in columns:
            conditions.append(f"COALESCE(memory_type, 'message') NOT IN ({', '.join('?' for _ in _INACTIVE_TYPES)})")
            params.extend(_INACTIVE_TYPES)
        rows = conn.execute(
            f"""SELECT id, sender_name, content FROM memories
                 WHERE id BETWEEN ? AND ? AND {' AND '.join(conditions)}
                 ORDER BY id ASC""",
            (memory_id - _WINDOW_SPAN, memory_id + _WINDOW_SPAN, *params),
        ).fetchall()
    except Exception:
        return []
    ids = [int(row[0]) for row in rows]
    if memory_id not in ids:
        return []
    at = ids.index(memory_id)
    return rows[max(0, at - _WINDOW_SIDE): at + _WINDOW_SIDE + 1]


@dataclass
class WaveMemorySearchTool(FunctionTool[AstrAgentContext]):
    """让模型主动搜索记忆的统一工具。"""

    name: str = "wave_memory_search"
    description: str = (
        "搜索当前对话范围内的历史记忆与对话记录。既支持自然语言模糊回忆，也支持精确关键词查找；"
        "自动返回命中记忆及前后上下文窗口，确认历史事实、事件经过与谁说过什么。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问题描述，自然语言或精确词均可"
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数量，默认 5",
                "default": 5
            },
            "include_context": {
                "type": "boolean",
                "description": "是否展开命中消息前后的聊天上下文（默认 true）",
                "default": True
            }
        },
        "required": ["query"]
    })

    # 运行时注入
    query_engine: Any = field(default=None, repr=False)
    db: Any = field(default=None, repr=False)
    # 与注入 fts5 通道一致的跨群设置（由工具注册表按插件配置传入）
    cross_group_enabled: bool = True
    shared_memory_grants_enabled: bool = False

    def _exact_channel(self):
        try:
            from ..services.injection.channels.fts5 import FTS5Channel
        except ImportError:  # 仓库测试直接导入
            from services.injection.channels.fts5 import FTS5Channel
        return FTS5Channel(
            db=self.db,
            cross_group_enabled=self.cross_group_enabled,
            shared_memory_grants_enabled=self.shared_memory_grants_enabled,
        )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query") or kwargs.get("keywords") or "").strip()
        limit = int(kwargs.get("limit") or kwargs.get("top_k") or kwargs.get("max_results") or 5)
        include_context = bool(kwargs.get("include_context", True))

        if not query:
            return "请提供搜索关键词"

        # db 存活检测
        if self.db and getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "记忆数据库连接异常"

        scope, error_code = require_memory_runtime_scope(context, "memory.message.read")
        if error_code:
            return scope_error_message("记忆搜索", error_code)
        assert scope is not None

        memories: list[dict] = []
        if self.query_engine:
            try:
                memories = await self.query_engine.query(
                    text=query,
                    group_id=scope.session.conversation_id,
                    top_k=limit,
                    scope=scope,
                )
            except Exception:
                memories = []

        # 语义检索为空时退回原词检索：与注入的 fts5 通道同一套作用域（本 Bot、可见性、跨群开关）
        # 和索引（中文两字切词索引就绪后用它）。
        if not memories and self.db and getattr(self.db, "conn", None) is not None:
            try:
                memories = self._exact_channel().search_scoped(scope, query, top_k=limit)
            except Exception:
                memories = []

        if not memories:
            return f"没有找到关于「{query}」的相关记忆"

        # 如果无需上下文展开或无数据库连接，直接使用 query_engine 格式化
        if not include_context or not self.db:
            if self.query_engine:
                return self.query_engine.format_injection(
                    memories,
                    current_group_id=scope.session.conversation_id,
                )
            return "\n".join(f"- {m.get('sender_name', '某人')}: {m.get('content', '')}" for m in memories)

        # 展开对话上下文窗口（严格受限于当前会话作用域，严防跨群泄露）
        conn = getattr(self.db, "conn", None) or getattr(self.db, "_conn", None)
        if not conn:
            if self.query_engine:
                return self.query_engine.format_injection(
                    memories,
                    current_group_id=scope.session.conversation_id,
                )
            return "\n".join(f"- {m.get('sender_name', '某人')}: {m.get('content', '')}" for m in memories)

        output_sections = []
        seen_msg_ids = set()
        for idx, m in enumerate(memories[:limit], 1):
            mid = m.get("id")
            if not mid or not isinstance(mid, int):
                output_sections.append(f"【记忆片段 {idx}】 {m.get('sender_name', '某人')}: {m.get('content', '')}")
                continue

            if mid in seen_msg_ids:
                continue

            context_rows = _context_window(conn, mid, bot_id=scope.bot_id)
            if not context_rows:
                output_sections.append(f"【记忆片段 {idx}】 {m.get('sender_name', '某人')}: {m.get('content', '')}")
            else:
                block_lines = [f"【对话切片 {idx}】"]
                for cid, csender, ctext in context_rows:
                    seen_msg_ids.add(cid)
                    marker = "▶ " if cid == mid else "  "
                    block_lines.append(f"{marker}{csender or '某人'}: {ctext}")
                output_sections.append("\n".join(block_lines))

        return "\n\n".join(output_sections)


@dataclass
class WaveMemoryRememberTool(FunctionTool[AstrAgentContext]):
    """让模型主动存储重要信息的工具。"""

    name: str = "wave_memory_remember"
    description: str = "主动记住一条重要信息。当用户告诉你需要记住的事情、或你判断某个信息值得长期保存时使用。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要记住的内容，用简洁的陈述句"
            },
            "importance": {
                "type": "number",
                "description": "重要性 (0.1-2.0)，默认 1.5 表示主动记忆比普通消息更重要",
                "default": 1.5
            }
        },
        "required": ["content"]
    })

    # 运行时注入
    writer: Any = field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        content = kwargs.get("content", "")
        importance = kwargs.get("importance", 1.5)

        if not content:
            return "请提供要记住的内容"

        if not self.writer:
            return "记忆系统未初始化"

        scope, error_code = require_memory_runtime_scope(context, "memory.message.write")
        if error_code:
            return scope_error_message("记忆写入", error_code)
        assert scope is not None

        import time
        event = getattr(getattr(context, "context", None), "event", None)
        await self.writer.enqueue({
            "scope": scope,
            "group_id": scope.session.conversation_id,
            "sender_id": "bot_remember",
            "sender_name": "主动记忆",
            "content": content,
            "timestamp": time.time(),
            "event_id": getattr(event, "message_id", None) if event else None,
            "importance": importance,
            "metadata": {"origin_kind": "agent_remember"},
        })

        return f"已记住：{content[:50]}..."
