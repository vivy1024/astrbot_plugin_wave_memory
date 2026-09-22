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

        # 若语义检索为空，且有数据库，尝试 FTS5 全文精准匹配兜底
        if not memories and self.db and hasattr(self.db, "conn"):
            conn = getattr(self.db, "conn", None) or getattr(self.db, "_conn", None)
            if conn:
                try:
                    # 消毒与包裹每个 token 为合法 FTS5 字面量，防止语法解析崩溃
                    clean_terms = [t.replace('"', '""') for t in query.split() if t.strip()]
                    if clean_terms:
                        fts_query = " AND ".join(f'"{t}"' for t in clean_terms)
                        if scope.visibility == "private":
                            clause = "AND (COALESCE(m.session_id, '') = ? OR COALESCE(m.group_id, '') = ?)"
                            params = (scope.session.id, scope.session.conversation_id)
                        else:
                            clause = "AND COALESCE(m.group_id, '') = ?"
                            params = (scope.session.conversation_id,)

                        rows = conn.execute(f"""
                            SELECT m.id, m.sender_name, m.content, m.timestamp
                            FROM fts_memories
                            JOIN memories AS m ON m.id = fts_memories.rowid
                            WHERE fts_memories MATCH ?
                              AND COALESCE(m.quarantine, 0) = 0
                              AND COALESCE(m.memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')
                              {clause}
                            ORDER BY rank
                            LIMIT ?
                        """, (fts_query, *params, limit)).fetchall()
                        for r in rows:
                            memories.append({
                                "id": r[0],
                                "sender_name": r[1],
                                "content": r[2],
                                "timestamp": r[3],
                                "source": "live",
                            })
                except Exception:
                    pass

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

        if scope.visibility == "private":
            scope_window_clause = "AND (COALESCE(session_id, '') = ? OR COALESCE(group_id, '') = ?)"
            scope_window_params = (scope.session.id, scope.session.conversation_id)
        else:
            scope_window_clause = "AND COALESCE(group_id, '') = ?"
            scope_window_params = (scope.session.conversation_id,)

        output_sections = []
        seen_msg_ids = set()
        for idx, m in enumerate(memories[:limit], 1):
            mid = m.get("id")
            if not mid or not isinstance(mid, int):
                output_sections.append(f"【记忆片段 {idx}】 {m.get('sender_name', '某人')}: {m.get('content', '')}")
                continue

            if mid in seen_msg_ids:
                continue

            # 严格作用域下查前后 2 条消息
            try:
                context_rows = conn.execute(f"""
                    SELECT id, sender_name, content
                    FROM memories
                    WHERE id BETWEEN ? AND ?
                      {scope_window_clause}
                      AND COALESCE(quarantine, 0) = 0
                      AND COALESCE(memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')
                    ORDER BY id ASC
                """, (mid - 2, mid + 2, *scope_window_params)).fetchall()
            except Exception:
                context_rows = []

            if not context_rows:
                output_sections.append(f"【记忆片段 {idx}】 {m.get('sender_name', '某人')}: {m.get('content', '')}")
            else:
                block_lines = [f"【对话切片 {idx}】"]
                for crow in context_rows:
                    cid, csender, ctext = crow
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
