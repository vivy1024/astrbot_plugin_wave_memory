"""Wave Memory LLM Tool — 让模型主动搜索和存储记忆"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


@dataclass
class WaveMemorySearchTool(FunctionTool[AstrAgentContext]):
    """让模型主动搜索记忆的工具。"""

    name: str = "wave_memory_search"
    description: str = "搜索历史记忆和对话记录。当需要回忆之前聊过的内容、查找群友说过的话、或确认历史事实时使用。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问题，用自然语言描述你想找的记忆"
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量，默认 5",
                "default": 5
            }
        },
        "required": ["query"]
    })

    # 运行时注入
    query_engine: Any = field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 5)

        if not query:
            return "请提供搜索关键词"

        if not self.query_engine:
            return "记忆系统未初始化"

        event = context.context.event
        group_id = event.get_group_id() if event else None

        memories = await self.query_engine.query(text=query, group_id=group_id, top_k=top_k)

        if not memories:
            return "没有找到相关记忆"

        return self.query_engine.format_injection(memories)


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

        event = context.context.event
        group_id = event.get_group_id() if event else "global"

        import time
        await self.writer.enqueue({
            "group_id": group_id,
            "sender_id": "bot_remember",
            "sender_name": "主动记忆",
            "content": content,
            "timestamp": time.time(),
            "importance": importance,
        })

        return f"已记住：{content[:50]}..."
