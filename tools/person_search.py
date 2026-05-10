"""Wave Memory Person Search Tool — 按人查记忆"""

from __future__ import annotations

import time
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


@dataclass
class WaveMemoryPersonSearchTool(FunctionTool[AstrAgentContext]):
    """按人物搜索记忆：查找某人说过的话、相关事件、社交关系。"""

    name: str = "wave_memory_person_search"
    description: str = (
        "按人物搜索记忆。可以查找某人说过的话、关于某人的记忆、某人的社交关系。"
        "支持用昵称或QQ号查找。当用户问'某某人最近在干嘛'、'谁和谁关系好'时使用。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "要查找的人物（昵称或QQ号）"
            },
            "query_type": {
                "type": "string",
                "enum": ["recent", "about", "social", "profile"],
                "description": "查询类型：recent=最近发言, about=关于此人的记忆, social=社交关系, profile=人物画像",
                "default": "recent"
            },
            "limit": {
                "type": "integer",
                "description": "返回数量，默认 8",
                "default": 8
            }
        },
        "required": ["person"]
    })

    db: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        person = kwargs.get("person", "").strip()
        query_type = kwargs.get("query_type", "recent")
        limit = int(kwargs.get("limit", 8))

        if not person:
            return "请提供要查找的人物名称或QQ号"

        if not self.db:
            return "记忆数据库未初始化"

        try:
            # 解析人物：先尝试 QQ 号，再尝试昵称
            qq_id = None
            if person.isdigit() and len(person) >= 5:
                info = self.db.get_person_by_qq(person)
                if info:
                    qq_id = person

            if not qq_id:
                matches = self.db.find_person_by_name(person)
                if matches:
                    qq_id = matches[0]["qq_id"]

            if not qq_id:
                return f"未找到名为「{person}」的人物"

            person_info = self.db.get_person_by_qq(qq_id)
            display_name = person_info["display_name"] if person_info else qq_id

            if query_type == "profile":
                return self._format_profile(qq_id, display_name)
            elif query_type == "social":
                return self._format_social(qq_id, display_name)
            elif query_type == "about":
                return self._format_about(qq_id, display_name, limit)
            else:  # recent
                return self._format_recent(qq_id, display_name, limit)

        except Exception as e:
            logger.warning(f"[WaveMemory] PersonSearch failed: {e}")
            return f"查询出错：{e}"

    def _format_profile(self, qq_id: str, display_name: str) -> str:
        stats = self.db.get_person_stats(qq_id)
        if not stats:
            return f"未找到 {display_name} 的画像信息"

        parts = [f"【{display_name}】的画像"]
        parts.append(f"QQ: {qq_id}")

        aliases = stats.get("aliases", [])
        if aliases:
            parts.append(f"曾用名: {', '.join(aliases[:5])}")

        parts.append(f"消息数: {stats.get('message_count', 0)}")

        role_counts = stats.get("role_counts", {})
        parts.append(f"发言: {role_counts.get('sender', 0)} | 被提及: {role_counts.get('mentioned', 0)} | 被讨论: {role_counts.get('about', 0)}")

        top_tags = stats.get("top_tags", [])
        if top_tags:
            tag_str = ", ".join(f"{t['name']}({t['count']})" for t in top_tags[:8])
            parts.append(f"特征标签: {tag_str}")

        # 社交
        cooc = self.db.get_person_cooccurrence(qq_id, top_k=5)
        if cooc:
            social_str = ", ".join(f"{c['display_name']}({c['co_count']}次)" for c in cooc)
            parts.append(f"常互动: {social_str}")

        return "\n".join(parts)

    def _format_social(self, qq_id: str, display_name: str) -> str:
        cooc = self.db.get_person_cooccurrence(qq_id, top_k=10)
        if not cooc:
            return f"未找到 {display_name} 的社交关系数据"

        parts = [f"【{display_name}】的社交关系（共现频率）"]
        for i, c in enumerate(cooc, 1):
            parts.append(f"  {i}. {c['display_name']} — 共现 {c['co_count']} 次")
        return "\n".join(parts)

    def _format_about(self, qq_id: str, display_name: str, limit: int) -> str:
        memories = self.db.get_memories_by_person(qq_id, role="about", limit=limit)
        if not memories:
            # fallback to mentioned
            memories = self.db.get_memories_by_person(qq_id, role="mentioned", limit=limit)
        if not memories:
            return f"未找到关于 {display_name} 的记忆"

        parts = [f"关于【{display_name}】的记忆（{len(memories)} 条）"]
        for mem in memories:
            ts = time.strftime("%m-%d %H:%M", time.localtime(mem["timestamp"]))
            content = mem["content"][:120]
            parts.append(f"  [{ts}] {content}")
        return "\n".join(parts)

    def _format_recent(self, qq_id: str, display_name: str, limit: int) -> str:
        memories = self.db.get_memories_by_person(qq_id, role="sender", limit=limit)
        if not memories:
            return f"未找到 {display_name} 的发言记录"

        parts = [f"【{display_name}】最近发言（{len(memories)} 条）"]
        for mem in memories:
            ts = time.strftime("%m-%d %H:%M", time.localtime(mem["timestamp"]))
            content = mem["content"][:120]
            parts.append(f"  [{ts}] {content}")
        return "\n".join(parts)
