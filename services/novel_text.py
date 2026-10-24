"""原著原文检索：按关键词或章节号直读小说 .txt，不经过向量库。

书设笔记（``book_lore.db``）是 GraphRAG 与工作台抽取出来的摘要，只有第 901 章之后
才有逐章笔记；笔记检索又依赖 embedding provider，provider 一挂就查不到任何东西。
但原著全文就在插件数据目录里，查原句、核对话、确认某个配角第几章出场这类问题
根本不需要语义搜索：直接扫全文即可。

本模块只做三件事：
- 把原文按「第N章」切成带偏移的章节表，按 (mtime, size) 缓存，13 MB 全文只切一次；
- 关键词命中映射回章节，返回命中处上下文；
- 按章节号取正文（可给区间）。

不涉及任何会话数据、作用域或网络调用，纯本地只读。
"""

from __future__ import annotations

import bisect
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from .book_lore_workbench import (
        _CHAPTER_RE,
        chinese_number,
        list_text_sources,
        read_source_text,
    )
except ImportError:  # pragma: no cover - 仓库测试直接导入
    from services.book_lore_workbench import (
        _CHAPTER_RE,
        chinese_number,
        list_text_sources,
        read_source_text,
    )

# 单次返回的正文预算：够看清一处对话的来龙去脉，又不会把上下文窗口灌满。
SNIPPET_CONTEXT_CHARS = 260
SEARCH_OUTPUT_BUDGET = 1600
CHAPTER_OUTPUT_BUDGET = 2400


@dataclass(frozen=True)
class ChapterSpan:
    number: int
    title: str
    start: int  # 正文起点（标题行之后）
    end: int  # 正文终点（下一章标题之前）


@dataclass
class _Parsed:
    text: str
    chapters: list[ChapterSpan]
    starts: list[int]  # 与 chapters 对齐的正文起点，供 bisect 定位
    stamp: tuple[float, int]


def chapter_spans(text: str) -> list[ChapterSpan]:
    """按「第N章 标题」切出带偏移的章节；同号重复（目录+正文）时取正文最长的那次。"""

    matches = list(_CHAPTER_RE.finditer(text or ""))
    best: dict[int, ChapterSpan] = {}
    for i, match in enumerate(matches):
        number = chinese_number(match.group(1))
        if number is None:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = f"第{number}章 {match.group(2).strip()}".strip()
        span = ChapterSpan(number=number, title=title, start=match.end(), end=end)
        current = best.get(number)
        if current is None or (span.end - span.start) > (current.end - current.start):
            best[number] = span
    spans = [best[n] for n in sorted(best)]
    return spans


class NovelTextIndex:
    """一本原著的章节表与关键词检索。线程安全，按文件指纹失效。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._parsed: _Parsed | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 载入

    def _current(self) -> _Parsed | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime, stat.st_size)
        cached = self._parsed
        if cached is not None and cached.stamp == stamp:
            return cached
        with self._lock:
            cached = self._parsed
            if cached is not None and cached.stamp == stamp:
                return cached
            text = read_source_text(self.path)
            spans = chapter_spans(text)
            parsed = _Parsed(
                text=text,
                chapters=spans,
                starts=[span.start for span in spans],
                stamp=stamp,
            )
            self._parsed = parsed
            return parsed

    # ------------------------------------------------------------------ 检索

    def _locate(self, parsed: _Parsed, offset: int) -> ChapterSpan | None:
        if not parsed.chapters:
            return None
        index = bisect.bisect_right(parsed.starts, offset) - 1
        if index < 0:
            return None
        span = parsed.chapters[index]
        if offset > span.end:  # 落在最后一章的尾部之外
            return None
        return span

    def _excerpt(self, text: str, start: int, end: int, offset: int) -> str:
        left = max(start, offset - SNIPPET_CONTEXT_CHARS)
        right = min(end, offset + SNIPPET_CONTEXT_CHARS)
        body = re.sub(r"[ \t\u3000]*\r?\n[\s\u3000]*", " ", text[left:right]).strip()
        prefix = "…" if left > start else ""
        suffix = "…" if right < end else ""
        return f"{prefix}{body}{suffix}"

    def search(self, keyword: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """返回关键词命中的章节与上下文；同一章只出一条，保证命中跨章分散。"""

        needle = str(keyword or "").strip()
        if not needle:
            return []
        parsed = self._current()
        if parsed is None:
            return []
        text = parsed.text
        wanted = max(1, int(limit))
        hits: list[dict[str, Any]] = []
        offset = text.find(needle)
        # 命中一处后直接跳到该章末尾再找：像主角名这种每章出现几十次的词，
        # 否则前 N 次全落在开头几章，后面几百章永远轮不到。
        while offset >= 0 and len(hits) < wanted:
            span = self._locate(parsed, offset)
            if span is None:
                offset = text.find(needle, offset + len(needle))
                continue
            hits.append({
                "chapter": span.number,
                "title": span.title,
                "excerpt": self._excerpt(text, span.start, span.end, offset),
            })
            offset = text.find(needle, span.end)
        return hits

    def chapter(self, number: int) -> dict[str, Any] | None:
        parsed = self._current()
        if parsed is None:
            return None
        for span in parsed.chapters:
            if span.number == int(number):
                body = parsed.text[span.start:span.end]
                body = re.sub(r"\r\n?", "\n", body).strip()
                return {"chapter": span.number, "title": span.title, "body": body}
        return None

    def chapter_numbers(self) -> list[int]:
        parsed = self._current()
        return [span.number for span in parsed.chapters] if parsed else []


def _candidate_paths(
    source_roots: Sequence[str], *, book_name: str = ""
) -> list[Path]:
    """允许目录下的小说原文，书名匹配的排前面，其余按体积降序。"""

    found = list(list_text_sources(source_roots))
    wanted = str(book_name or "").strip()

    def rank(path: Path) -> tuple[int, int]:
        name_hit = 0 if wanted and wanted in path.stem else 1
        try:
            size = -path.stat().st_size
        except OSError:
            size = 0
        return (name_hit, size)

    return sorted(found, key=rank)


def open_novel(
    source_roots: Sequence[str], *, book_name: str = "", path: str = ""
) -> NovelTextIndex | None:
    """打开一本原著：显式给 path 就用它，否则在允许目录里挑最匹配的 .txt。"""

    if path:
        candidate = Path(str(path))
        if candidate.is_file() and candidate.suffix.lower() == ".txt":
            return NovelTextIndex(candidate)
        return None
    for candidate in _candidate_paths(source_roots, book_name=book_name):
        return NovelTextIndex(candidate)
    return None


def parse_chapter_range(value: Any) -> tuple[int, int] | None:
    """把 ``"950"`` / ``"950-952"`` / ``950`` 解析成闭区间；解析不了返回 None。"""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return (value, value) if value > 0 else None
    raw = str(value).strip().replace("～", "-").replace("~", "-").replace("—", "-")
    if not raw:
        return None
    parts = [part for part in raw.split("-") if part.strip()]
    if not parts or len(parts) > 2:
        return None
    try:
        numbers = [int(part.strip()) for part in parts]
    except ValueError:
        return None
    if any(number <= 0 for number in numbers):
        return None
    start, end = numbers[0], numbers[-1]
    return (min(start, end), max(start, end))


__all__ = [
    "CHAPTER_OUTPUT_BUDGET",
    "ChapterSpan",
    "NovelTextIndex",
    "SEARCH_OUTPUT_BUDGET",
    "chapter_spans",
    "open_novel",
    "parse_chapter_range",
]
