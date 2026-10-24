"""原著全文检索：章节切分、关键词定位与工具渲染。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.novel_text import (  # noqa: E402
    NovelTextIndex,
    chapter_spans,
    open_novel,
    parse_chapter_range,
)
from domain.scope import CatalogScope  # noqa: E402
from tools.novel_source import WaveMemoryNovelSourceTool  # noqa: E402

SAMPLE = """书名：测试之书

第1章 面试

张羽站在门口，心里想着手术费的事情，思绪乱成一团。

第2章 学校

他在学校里第一次听见「羽书」这个名字，据说是个修仙的天才。
老师又说了一遍羽书，他记住了。

第3章 体育

跑步。第九百五十章还没到。
"""

SCOPE = CatalogScope(catalog_id="catalog-1", corpus_id="corpus-1", version="v1")


def _tool(roots, **kwargs):
    """原文文件在测试里只有几 KB，低于目录扫描阈值，因此显式给 novel_path。"""

    novel_path = str(next(Path(roots[0]).glob("*.txt")))
    return WaveMemoryNovelSourceTool(
        source_roots=[str(r) for r in roots],
        novel_path=novel_path,
        catalog_scope=SCOPE,
        **kwargs,
    )


@pytest.fixture()
def novel_file(tmp_path: Path) -> Path:
    path = tmp_path / "测试之书.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_chapter_spans_use_headings_and_offsets(novel_file):
    text = novel_file.read_text(encoding="utf-8")
    spans = chapter_spans(text)

    assert [span.number for span in spans] == [1, 2, 3]
    assert spans[0].title == "第1章 面试"
    # 偏移必须落在正文里，不能把标题行算进正文
    assert "手术费" in text[spans[0].start : spans[0].end]
    assert "第2章" not in text[spans[0].start : spans[0].end]


def test_search_maps_hits_to_chapters_and_dedups(novel_file):
    index = NovelTextIndex(novel_file)

    hits = index.search("羽书", limit=5)

    # 第2章出现两次，只算一处
    assert [hit["chapter"] for hit in hits] == [2]
    assert "羽书" in hits[0]["excerpt"]
    assert hits[0]["title"] == "第2章 学校"


def test_search_spreads_hits_across_chapters(tmp_path: Path):
    """主角名这种每章都出现的词，不能把前 N 次命中全挤在同一章。"""

    body = "\n\n".join(
        f"第{n}章 章名{n}\n\n张羽在这里出现了，张羽又出现了。" for n in range(1, 6)
    )
    path = tmp_path / "重复之书.txt"
    path.write_text(body, encoding="utf-8")

    hits = NovelTextIndex(path).search("张羽", limit=3)

    assert [hit["chapter"] for hit in hits] == [1, 2, 3]


def test_search_returns_empty_for_absent_keyword(novel_file):
    index = NovelTextIndex(novel_file)

    assert index.search("不存在的词") == []


def test_chapter_lookup_and_numbers(novel_file):
    index = NovelTextIndex(novel_file)

    chapter = index.chapter(3)

    assert chapter is not None
    assert chapter["title"] == "第3章 体育"
    assert "跑步" in chapter["body"]
    assert index.chapter_numbers() == [1, 2, 3]
    assert index.chapter(99) is None


def test_index_reloads_when_file_changes(novel_file):
    index = NovelTextIndex(novel_file)
    assert index.chapter_numbers() == [1, 2, 3]

    novel_file.write_text(SAMPLE + "\n第4章 新章\n\n新内容。\n", encoding="utf-8")

    assert index.chapter_numbers() == [1, 2, 3, 4]


def test_parse_chapter_range_accepts_single_range_and_noise():
    assert parse_chapter_range("950") == (950, 950)
    assert parse_chapter_range(950) == (950, 950)
    assert parse_chapter_range("950-952") == (950, 952)
    assert parse_chapter_range("952~950") == (950, 952)
    assert parse_chapter_range(" 950 — 951 ") == (950, 951)
    assert parse_chapter_range("") is None
    assert parse_chapter_range("abc") is None
    assert parse_chapter_range("0") is None
    assert parse_chapter_range(None) is None
    assert parse_chapter_range(True) is None


def test_open_novel_prefers_matching_book_name(tmp_path: Path):
    # list_text_sources 会跳过 50 KB 以下的零碎文本，所以两份都要够大
    (tmp_path / "别的书.txt").write_text("x" * 80_000, encoding="utf-8")
    wanted = tmp_path / "测试之书.txt"
    wanted.write_text(SAMPLE + "\n" * 60_000, encoding="utf-8")

    novel = open_novel([str(tmp_path)], book_name="测试之书")

    assert novel is not None and novel.path.name == "测试之书.txt"


@pytest.mark.asyncio
async def test_tool_renders_keyword_hits(novel_file):
    tool = _tool([novel_file.parent])

    out = await tool.call(None, keyword="羽书")

    assert "<novel_source>" in out
    assert "第2章 学校" in out
    assert "羽书" in out


@pytest.mark.asyncio
async def test_tool_renders_chapter_range(novel_file):
    tool = _tool([novel_file.parent])

    out = await tool.call(None, chapter="1-2")

    assert "第1章 面试" in out and "第2章 学校" in out
    assert "手术费" in out
    assert "跑步" not in out  # 第3章不在区间里


@pytest.mark.asyncio
async def test_tool_reports_missing_chapter_and_absent_keyword(novel_file):
    tool = _tool([novel_file.parent])

    too_far = await tool.call(None, chapter="99")
    absent = await tool.call(None, keyword="不存在的词")
    neither = await tool.call(None)

    assert "只到第 3 章" in too_far
    assert "没有找到含" in absent
    assert "keyword" in neither


@pytest.mark.asyncio
async def test_tool_fails_closed_without_source_file(tmp_path: Path):
    tool = WaveMemoryNovelSourceTool(
        source_roots=[str(tmp_path)], catalog_scope=SCOPE
    )

    out = await tool.call(None, keyword="羽书")

    assert "没有找到可读的原著" in out
