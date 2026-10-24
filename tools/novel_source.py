"""原著原文检索工具：忘记细节或不确定时，直接回原文找原句。

书设笔记是摘要，问答里引错的往往不是设定本身，而是具体桥段、原话和章节位置。
这里按关键词或章节号直读 .txt，不依赖 embedding provider，也不依赖笔记索引，
所以向量服务出问题、章节笔记还没生成时它照样能用。

作用域与书设搜索一致（catalog.read）：原文属于书设的一部分。
"""

from __future__ import annotations

import re
from dataclasses import field
from typing import Any, Sequence

from pydantic.dataclasses import dataclass

try:
    from astrbot.api import logger
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover - 仓库测试无 AstrBot
    import logging

    logger = logging.getLogger(__name__)
    from typing import Generic, TypeVar

    _T = TypeVar("_T")

    class FunctionTool(Generic[_T]):  # type: ignore[no-redef]
        pass

    class ContextWrapper(Generic[_T]):  # type: ignore[no-redef]
        pass

    class AstrAgentContext:  # type: ignore[no-redef]
        pass

try:
    from ..domain.scope import CatalogScope
    from ..services.novel_text import (
        CHAPTER_OUTPUT_BUDGET,
        SEARCH_OUTPUT_BUDGET,
        NovelTextIndex,
        open_novel,
        parse_chapter_range,
    )
    from .scope_boundary import require_catalog_scope, scope_error_message
except ImportError:  # pragma: no cover - 仓库测试直接导入
    from domain.scope import CatalogScope
    from services.novel_text import (
        CHAPTER_OUTPUT_BUDGET,
        SEARCH_OUTPUT_BUDGET,
        NovelTextIndex,
        open_novel,
        parse_chapter_range,
    )
    from tools.scope_boundary import require_catalog_scope, scope_error_message


@dataclass
class WaveMemoryNovelSourceTool(FunctionTool[AstrAgentContext]):
    """按关键词或章节号查原著全文，返回原句上下文或章节正文。"""

    name: str = "wave_memory_novel_source"
    description: str = (
        "查原著原文。忘记具体桥段、拿不准某句原话、想知道某个细节出现在第几章时调用。"
        "给 keyword 就返回含该词的原文上下文，给 chapter 就返回该章正文（可写 950 或 950-952）。"
        "这是逐字原文，比书设摘要可靠：与记忆或笔记冲突时以原文为准。"
        "回答里引用原文时要说明是第几章，不要大段复述。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "要搜的原词或原句片段，例如「羽书」「青云宗」「别急」。只写关键词，不要整段复制问题",
            },
            "chapter": {
                "type": "string",
                "description": "章节号，可写单章 950 或区间 950-952；与 keyword 同时给时先按章节取正文",
            },
            "limit": {
                "type": "integer",
                "description": "keyword 搜索最多返回几处命中，默认 4，上限 8",
                "default": 4,
            },
        },
        "required": [],
    })

    source_roots: Sequence[str] = field(default_factory=tuple, repr=False)
    book_name: str = field(default="", repr=False)
    novel_path: str = field(default="", repr=False)
    catalog_scope: CatalogScope | None = field(default=None, repr=False)
    # 懒加载的 NovelTextIndex；pydantic dataclass 不认自定义类型，这里只做缓存槽位。
    _index: Any = field(default=None, repr=False, compare=False)

    def _novel(self) -> NovelTextIndex | None:
        if self._index is None:
            self._index = open_novel(
                list(self.source_roots), book_name=self.book_name, path=self.novel_path
            )
        return self._index

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        _, error_code = require_catalog_scope(self.catalog_scope, "catalog.read")
        if error_code:
            return scope_error_message("原著检索", error_code)

        keyword = str(kwargs.get("keyword") or "").strip()
        chapter_arg = kwargs.get("chapter")
        if not keyword and (chapter_arg is None or str(chapter_arg).strip() == ""):
            return "请提供 keyword（要搜的原词）或 chapter（章节号）之一。"

        novel = self._novel()
        if novel is None:
            return "没有找到可读的原著文本文件。"

        try:
            if chapter_arg is not None and str(chapter_arg).strip():
                return self._render_chapters(novel, chapter_arg)
            return self._render_search(novel, keyword, kwargs.get("limit", 4))
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning(f"[NovelSource] 检索失败: {exc}")
            return f"原著检索出错：{type(exc).__name__}"

    # ------------------------------------------------------------------ 渲染

    def _render_chapters(self, novel: NovelTextIndex, chapter_arg: Any) -> str:
        span_range = parse_chapter_range(chapter_arg)
        if span_range is None:
            return f"章节号看不懂：「{chapter_arg}」。请写单章 950，或区间 950-952。"
        start, end = span_range
        available = novel.chapter_numbers()
        if not available:
            return "原著的章节切分结果为空，无法按章节读取。"
        latest = available[-1]
        if start > latest:
            return f"原著只到第 {latest} 章，没有第 {start} 章。"

        blocks: list[str] = []
        budget = CHAPTER_OUTPUT_BUDGET
        for number in range(start, end + 1):
            chapter = novel.chapter(number)
            if chapter is None:
                continue
            body = chapter["body"]
            header = f"【{chapter['title']}】"
            if len(body) > budget:
                body = body[:budget].rstrip() + "\n…（本章更长，已截断；需要后文请单独取这章的靠后内容）"
            blocks.append(f"{header}\n{body}")
            budget -= len(body)
            if budget <= 0:
                if number < end:
                    blocks.append(f"…（区间内容超出返回上限，第 {number + 1} 章起未展开）")
                break
        if not blocks:
            return f"原著里没有第 {start}-{end} 章。"
        return "<novel_source>\n" + "\n\n".join(blocks) + "\n</novel_source>"

    def _render_search(self, novel: NovelTextIndex, keyword: str, limit: Any) -> str:
        try:
            top_k = max(1, min(int(limit), 8))
        except (TypeError, ValueError):
            top_k = 4
        hits = novel.search(keyword, limit=top_k)
        if not hits:
            return f"原著里没有找到含「{keyword}」的句子。换个更短的词再试，或直接按章节号取正文。"

        lines: list[str] = [f"原著中「{keyword}」的命中（共 {len(hits)} 处）:"]
        budget = SEARCH_OUTPUT_BUDGET
        for hit in hits:
            block = f"· {hit['title']}\n  {hit['excerpt']}"
            if len(block) > budget:
                break
            lines.append(block)
            budget -= len(block)
        return "<novel_source>\n" + re.sub(r"\n{3,}", "\n\n", "\n".join(lines)) + "\n</novel_source>"


__all__ = ["WaveMemoryNovelSourceTool"]
