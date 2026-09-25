"""书设工作台：切章、保存即入索引、补齐索引、导入新章节、路径限制；注入通道带上笔记。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from services.book_lore_workbench import BookLoreWorkbench, chinese_number, split_chapters

NOVEL = "\n".join(
    [
        "第1章 目录里的第一章", "第2章 目录里的第二章",  # 目录：很短，应被正文覆盖
        "第1章 开局欠债", "张羽睁开眼，发现自己欠了三百灵石。" * 5,
        "第2章 打工还债", "他去坊市打工，一天只挣两块灵币。" * 5,
        "第十一章 仙帝登极", "十一号仙帝登上了昆墟。" * 5,
    ]
)


class _Index:
    """与 BookLoreIndex 相同的笔记接口，内存实现。"""

    def __init__(self, ids=()):
        self.vectors: dict[str, np.ndarray] = {i: np.ones(4, dtype=np.float32) for i in ids}
        self.saves = 0
        self.entity_count = 3
        self.community_count = 2

    def note_ids(self):
        return set(self.vectors)

    def upsert_notes(self, ids, vectors):
        for nid, vec in zip(ids, vectors):
            self.vectors[nid] = np.asarray(vec)

    def remove_notes(self, ids):
        for nid in ids:
            self.vectors.pop(nid, None)
        return len(ids)

    def save_notes(self):
        self.saves += 1

    def search_notes(self, vector, k=5):
        scored = [(nid, float(np.dot(vec, vector) / (np.linalg.norm(vec) * np.linalg.norm(vector) or 1))) for nid, vec in self.vectors.items()]
        return sorted(scored, key=lambda kv: -kv[1])[:k]


class _Embedding:
    def __init__(self, fail_on=""):
        self.calls = 0
        self.fail_on = fail_on

    async def get_embeddings(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            if self.fail_on and self.fail_on in text:
                out.append(None)
            else:
                out.append(np.array([1.0, float(len(text) % 7), 0.5, 0.0], dtype=np.float32))
        return out


def _lore_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE book_notes (id TEXT PRIMARY KEY, book_name TEXT NOT NULL, arc TEXT DEFAULT '', category TEXT DEFAULT '',
            title TEXT NOT NULL, content TEXT, source_file TEXT DEFAULT '', vector BLOB);
        CREATE TABLE book_entities (id TEXT, vector BLOB);
        CREATE TABLE book_communities (id TEXT, vector BLOB);
    """)
    vec = np.ones(4, dtype=np.float32).tobytes()
    conn.executemany("INSERT INTO book_notes VALUES (?, '没钱修什么仙', ?, ?, ?, ?, '', ?)", [
        ("note_ch1", "arc01", "章节事件", "第1章 开局欠债", "旧内容", vec),         # 已入索引
        ("note_lore", "arc01", "世界观", "灵石", "灵石是硬通货", vec),              # 有向量但没进索引
        ("note_novec", "arc01", "人物", "张羽", "主角", None),                     # 没向量
    ])
    conn.commit()
    conn.close()
    return str(path)


def _workbench(tmp_path, *, index=None, embedding=None):
    db = _lore_db(tmp_path / "book_lore.db")
    (tmp_path / "src").mkdir()
    return BookLoreWorkbench(
        lore_db_path=db,
        index=index if index is not None else _Index(["note_ch1", "note_deleted"]),
        embedding_service=embedding or _Embedding(),
        source_roots=[str(tmp_path / "src")],
        dimension=4,
    )


def test_chinese_numbers_and_chapter_split_prefers_body_over_toc():
    assert chinese_number("十一") == 11 and chinese_number("九百六十一") == 961 and chinese_number("１２") == 12
    chapters = split_chapters(NOVEL)
    assert [c["number"] for c in chapters] == [1, 2, 11]
    assert chapters[0]["title"] == "第1章 开局欠债" and "三百灵石" in chapters[0]["body"]


def test_save_note_is_indexed_immediately_and_logged(tmp_path):
    wb = _workbench(tmp_path)
    result = asyncio.run(wb.save_notes([{"id": "note_new", "title": "安监局", "content": "修仙界的安全监管机构", "category": "世界观"}]))
    assert result == {"saved": ["note_new"], "indexed": 1, "pending_vectors": 0}
    assert "note_new" in wb.index.note_ids() and wb.index.saves == 1
    note = wb.writer.get_note("note_new")
    assert note["book_name"] == "没钱修什么仙" and note["has_vector"]
    assert wb.writer.recent_edits()[0]["action"] == "save_note"


def test_embedding_failure_still_saves_and_sync_fills_later(tmp_path):
    wb = _workbench(tmp_path, embedding=_Embedding(fail_on="暂时失败"))
    result = asyncio.run(wb.save_notes([{"id": "note_x", "title": "暂时失败", "content": "向量服务挂了"}]))
    assert result["pending_vectors"] == 1 and "note_x" not in wb.index.note_ids()
    wb.embedding.fail_on = ""
    sync = asyncio.run(wb.sync_index())
    # note_lore（有向量没索引）、note_novec（没向量）、note_x（没向量）进索引；note_deleted（库里已删）移出
    assert sync["indexed"] == 3 and sync["embedded"] == 2 and sync["removed_stale"] == 1 and sync["failed"] == 0
    assert wb.index.note_ids() == {"note_ch1", "note_lore", "note_novec", "note_x"}
    assert wb.writer.get_note("note_novec")["has_vector"]


def test_overview_reports_gaps_and_new_chapters_in_source(tmp_path):
    wb = _workbench(tmp_path)
    (tmp_path / "src" / "novel.txt").write_text(NOVEL + "\n" + ("填充" * 30000), encoding="utf-8")
    overview = asyncio.run(wb.overview())
    assert overview["notes_not_indexed"] == 2 and overview["stale_index_entries"] == 1
    assert overview["latest_chapter"] == 1
    source = overview["sources"][0]
    assert source["latest_chapter"] == 11 and source["new_chapters"] == 2 and source["new_range"] == [2, 11]


def test_import_new_chapters_only_and_path_is_restricted(tmp_path):
    wb = _workbench(tmp_path)
    novel = tmp_path / "src" / "novel.txt"
    novel.write_text(NOVEL, encoding="utf-8")
    result = asyncio.run(wb.import_chapters(path=str(novel)))
    assert result["chapters"] == [2, 11] and result["arc"] == "arc01" and result["indexed"] == 2
    note = wb.writer.get_note("note_ch11")
    assert note["category"] == "章节事件" and note["content"].startswith("【第11章 仙帝登极】")
    assert wb.writer.get_note("note_ch1")["content"] == "旧内容"  # 已有章节不覆盖
    outside = tmp_path / "outside.txt"
    outside.write_text(NOVEL, encoding="utf-8")
    with pytest.raises(Exception) as err:
        asyncio.run(wb.import_chapters(path=str(outside)))
    assert getattr(err.value, "code", "") == "path_not_allowed"
    with pytest.raises(Exception):
        asyncio.run(wb.import_chapters(path=str(tmp_path / "src" / ".." / "outside.txt")))


def test_delete_note_removes_from_index(tmp_path):
    wb = _workbench(tmp_path)
    assert asyncio.run(wb.delete_note("note_ch1")) is True
    assert "note_ch1" not in wb.index.note_ids() and wb.writer.get_note("note_ch1") is None
    assert asyncio.run(wb.delete_note("nope")) is False


def test_book_lore_channel_adds_note_hit_and_skips_personal_notes(tmp_path):
    from domain.scope import CatalogScope, RuntimeScope, SessionRef
    from services.injection.channels.book_lore import BookLoreChannel
    from services.injection.context import InjectionContext

    class _Store:
        def communities_by_ids(self, ids, scope):
            return []

        def notes_by_ids(self, ids, scope):
            rows = {
                "note_ch961": {"id": "note_ch961", "title": "第961章 绝望中的希望", "content": "【第961章 绝望中的希望】\n第961章 绝望中的希望\n\n张羽看着加密信息。", "category": "章节事件"},
                "note_bzz": {"id": "note_bzz", "title": "白真真的一天", "content": "私人经历", "category": "白真真经历"},
            }
            return [rows[i] for i in ids if i in rows]

    index = _Index()
    index.vectors = {"note_bzz": np.array([1, 0, 0, 0], dtype=np.float32), "note_ch961": np.array([0.9, 0.1, 0, 0], dtype=np.float32)}
    index.search_communities = lambda vector, k=1: []

    class _Emb:
        async def get_embedding(self, text):
            return np.array([1, 0, 0, 0], dtype=np.float32)

    channel = BookLoreChannel(book_lore_index=index, embedding_service=_Emb(), lore_store=_Store(),
                              catalog_scope=CatalogScope("book-lore", "default", "current"))
    scope = RuntimeScope("yushu", "group", SessionRef("羽书:group:1", "羽书", "group", "1"), subject_principal_id="羽书:user:u")
    ctx = InjectionContext(event=None, req=None, message="最新章节怎么样了", group_id="1", sender_id="u", sender_name="u",
                           bot_id="yushu", bot_profile_id="yushu", scope=scope,
                           config={"channels": {"book_lore": {"top_k": 1, "min_score": 0.35}}})
    result = asyncio.run(channel.build(ctx))
    assert result.status == "hit"
    assert "第961章 绝望中的希望：张羽看着加密信息。" in result.text  # 去掉了重复的标题行
    assert "白真真" not in result.text
    assert result.items[0]["note_id"] == "note_ch961"
