"""WaveMemory 梗百科查询工具：看不懂梗、或想接梗时查。

只读静态资产（精选词条、扩充词条、神人包知识与语录、``神言.txt``），不读写任何会话数据，
所以不需要作用域；``神言.txt`` 只能经这个工具查到。返回前统一做安全过滤与身份污染检查，
被过滤时如实说明“有结果但因安全规则未返回”。
"""

from __future__ import annotations

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
    from ..services.identity_safety import is_identity_contamination
    from ..services.jargon.meme_encyclopedia import LOOKUP_MAX_CHARS, get_meme_knowledge_base
except ImportError:  # pragma: no cover
    from services.identity_safety import is_identity_contamination
    from services.jargon.meme_encyclopedia import LOOKUP_MAX_CHARS, get_meme_knowledge_base


@dataclass
class WaveMemoryMemeLookupTool(FunctionTool[AstrAgentContext]):
    """按关键词查梗百科，返回释义和至多 2 条例句/相关文案。"""

    name: str = "wave_memory_meme_lookup"
    description: str = (
        "查梗百科。群友说的梗、黑话、抽象话看不懂时，或者想接梗但拿不准意思和用法时调用。"
        "按关键词在精选梗、扩充词条、神人包知识与语录、神言语料里检索，返回释义和至多 2 条例句"
        "（总长约 600 字）。结果只供理解：想接梗可以用，不是必须用；标注“仅供理解”的词别主动用。"
        "查不到或因安全规则未返回时，不要自行编造释义。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要查的梗、黑话或关键词，例如「赢学」「做局」「v我50」「不是哥们」；只写梗本身，不要整段复制聊天记录",
            },
        },
        "required": ["query"],
    })

    knowledge_base: Any = field(default=None, repr=False)

    def _kb(self) -> Any:
        return self.knowledge_base if self.knowledge_base is not None else get_meme_knowledge_base()

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "请提供要查的梗或关键词（query）。"
        try:
            result = self._kb().lookup(query)
            text = result.render(max_chars=LOOKUP_MAX_CHARS)
        except Exception as exc:
            return f"梗百科暂时不可用：{type(exc).__name__}。不要自行编造释义。"
        # render 前每段都已单独过滤；整段再兜底检查一次，任何命中都不返回内容。
        if is_identity_contamination(text):
            return f"查到与「{result.query}」相关的内容，但因安全规则（身份污染）未返回。不要自行编造释义。"
        return text


__all__ = ["WaveMemoryMemeLookupTool"]
