"""Wave Memory DeepSearch Tool — FTS5 全文搜索 + 上下文窗口"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from ..engine.database import WaveMemoryDB


@dataclass
class WaveMemoryDeepSearchTool(FunctionTool[AstrAgentContext]):
    """深度搜索工具：FTS5 全文搜索 + 上下文窗口扩展。

    适用于精确关键词搜索、查找特定对话片段、追溯历史事件。
    """

    name: str = "wave_memory_deep_search"
    description: str = (
        "深度搜索历史对话记录。使用关键词精确匹配，并返回匹配消息前后的上下文。"
        "适合查找特定话题、追溯事件经过、确认谁说过什么。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "string",
                "description": "搜索关键词，支持多个词用空格分隔（AND 逻辑）"
            },
            "window_size": {
                "type": "integer",
                "description": "上下文窗口大小（命中消息前后各扩展几条），默认 3",
                "default": 3,
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回片段数，默认 5",
                "default": 5,
            },
        },
        "required": ["keywords"],
    })

    db: Any = None

    async def execute(self, ctx: ContextWrapper, **kwargs) -> str:
        keywords = kwargs.get("keywords", "").strip()
        window_size = int(kwargs.get("window_size", 3))
        max_results = int(kwargs.get("max_results", 5))

        if not keywords:
            return "请提供搜索关键词。"

        if not self.db:
            return "记忆数据库未初始化。"

        try:
            # FTS5 搜索
            fts_query = " AND ".join(keywords.split())
            hits = self.db.conn.execute("""
                SELECT rowid, snippet(fts_memories, 0, '【', '】', '...', 32) as snippet,
                       rank
                FROM fts_memories
                WHERE fts_memories MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, max_results * 2)).fetchall()

            if not hits:
                # 尝试 OR 搜索
                fts_query_or = " OR ".join(keywords.split())
                hits = self.db.conn.execute("""
                    SELECT rowid, snippet(fts_memories, 0, '【', '】', '...', 32) as snippet,
                           rank
                    FROM fts_memories
                    WHERE fts_memories MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query_or, max_results * 2)).fetchall()

            if not hits:
                return f"未找到包含「{keywords}」的记忆。"

            # 上下文窗口扩展
            fragments = []
            seen_ids = set()

            for hit in hits[:max_results]:
                memory_id = hit[0]
                if memory_id in seen_ids:
                    continue

                # 获取命中记忆的 group_id
                mem_row = self.db.conn.execute(
                    "SELECT group_id FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
                if not mem_row:
                    continue

                group_id = mem_row[0]

                # 获取上下文窗口
                window = self.db.conn.execute("""
                    SELECT id, sender_name, content, created_at
                    FROM memories
                    WHERE group_id = ? AND id BETWEEN ? AND ?
                    ORDER BY id ASC
                """, (group_id, memory_id - window_size, memory_id + window_size)).fetchall()

                if not window:
                    continue

                # 格式化片段
                lines = []
                for row in window:
                    mid, sender, content, ts = row
                    seen_ids.add(mid)
                    marker = "→ " if mid == memory_id else "  "
                    sender_str = sender or "unknown"
                    lines.append(f"{marker}{sender_str}: {content}")

                fragments.append("\n".join(lines))

            if not fragments:
                return f"未找到包含「{keywords}」的记忆。"

            # 组装输出
            output_parts = [f"找到 {len(fragments)} 个相关片段：\n"]
            for i, frag in enumerate(fragments, 1):
                output_parts.append(f"[片段{i}]\n{frag}")

            return "\n\n".join(output_parts)

        except Exception as e:
            logger.warning(f"[WaveMemory] DeepSearch failed: {e}")
            return f"搜索出错：{e}"
