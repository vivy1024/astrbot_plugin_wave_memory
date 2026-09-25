"""书设工作台：书设的实时维护。

v5 的书设是 6 月 GraphRAG 离线跑出来的快照（整本 10.5 小时），之后的新章节靠手工往
``book_lore.db`` 插笔记；插进去的笔记既没算向量、也没进笔记索引，所以检索和注入都看不到
（线上 2497 条笔记只有 2020 条在索引里，第 950 章以后全部缺失）。

这里把书设改成「改了就能用」：
- 保存笔记时当场算向量、写库、增量更新笔记索引（同一个 BookLoreIndex 实例，注入立即生效）；
- 「补齐索引」把库里没向量、或没进索引的笔记补上；
- 扫描小说原文（插件数据目录或 novel_docs 下的 .txt），列出库里还没有的章节，一键导入；
  也可以直接粘贴新章节文本导入。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Sequence

import numpy as np

try:
    from ..engine.book_lore_writer import BookLoreWriteError, BookLoreWriter
except ImportError:  # pragma: no cover - 仓库测试直接导入
    from engine.book_lore_writer import BookLoreWriteError, BookLoreWriter

DEFAULT_BOOK = "没钱修什么仙"
CHAPTER_CATEGORY = "章节事件"
# 注入与检索时不当作世界观书设用的类别（白真真的个人经历另有记忆通道）
PERSONAL_CATEGORIES = ("白真真经历",)
EMBED_TEXT_CHARS = 3000
EMBED_BATCH = 16
MAX_IMPORT_CHAPTERS = 200

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CHAPTER_RE = re.compile(r"^[ \t　]*第([0-9０-９零〇一二两三四五六七八九十百千万]+)章[ \t　]*(.*?)[ \t　]*$", re.MULTILINE)


def chinese_number(text: str) -> int | None:
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if text.isdigit():
        return int(text)
    total, section, number = 0, 0, 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit == 10000:
                total += (section + number) * unit
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


def split_chapters(text: str) -> list[dict[str, Any]]:
    """按「第N章 标题」切章。同一章号出现多次（目录 + 正文）时取正文最长的那次。"""
    matches = list(_CHAPTER_RE.finditer(text or ""))
    chapters: dict[int, dict[str, Any]] = {}
    for i, match in enumerate(matches):
        number = chinese_number(match.group(1))
        if number is None:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        title = f"第{number}章 {match.group(2).strip()}".strip()
        current = chapters.get(number)
        if current is None or len(body) > len(current["body"]):
            chapters[number] = {"number": number, "title": title, "body": body}
    return [chapters[n] for n in sorted(chapters)]


def chapter_note(chapter: dict[str, Any], *, arc: str, book_name: str, source_file: str) -> dict[str, Any]:
    """沿用已有章节笔记的格式：id=note_chN，正文前面带【标题】。"""
    title = chapter["title"]
    return {
        "id": f"note_ch{chapter['number']}",
        "book_name": book_name,
        "arc": arc,
        "category": CHAPTER_CATEGORY,
        "title": title,
        "content": f"【{title}】\n{title}\n\n{chapter['body']}",
        "source_file": source_file,
    }


def _embed_text(note: dict[str, Any]) -> str:
    return f"{note.get('title') or ''}\n{str(note.get('content') or '')[:EMBED_TEXT_CHARS]}"


class BookLoreWorkbench:
    def __init__(
        self,
        *,
        lore_db_path: str,
        index: Any,
        embedding_service: Any,
        source_roots: Sequence[str],
        dimension: int = 1024,
        default_book: str = DEFAULT_BOOK,
    ) -> None:
        self.writer = BookLoreWriter(lore_db_path, dimension=dimension)
        self.index = index
        self.embedding = embedding_service
        self.source_roots = [str(Path(root).resolve()) for root in source_roots if root]
        self.default_book = default_book
        self._lock = asyncio.Lock()
        # 原文切章结果按 (修改时间, 大小) 缓存：13 MB 的原文每次概览都重切太慢
        self._scan_cache: dict[str, tuple[float, int, list[int], str]] = {}

    # ------------------------------------------------------------------ 概览

    async def overview(self) -> dict[str, Any]:
        counts = await asyncio.to_thread(self.writer.counts)
        facets = await asyncio.to_thread(self.writer.facets)
        db_note_ids = set(await asyncio.to_thread(self.writer.note_ids_all))
        indexed = self.index.note_ids() if self.index is not None and hasattr(self.index, "note_ids") else set()
        not_indexed = sorted(db_note_ids - indexed)
        index_counts = {
            "entities": int(getattr(self.index, "entity_count", 0) or 0) if self.index is not None else 0,
            "communities": int(getattr(self.index, "community_count", 0) or 0) if self.index is not None else 0,
            "notes": len(db_note_ids & indexed),
        }
        chapters = await asyncio.to_thread(self.writer.chapter_numbers)
        return {
            "available": self.index is not None and self.embedding is not None,
            "counts": counts,
            "indexed": index_counts,
            "notes_not_indexed": len(not_indexed),
            "notes_not_indexed_sample": not_indexed[:20],
            "stale_index_entries": len(indexed - db_note_ids),
            "facets": facets,
            "latest_chapter": max(chapters) if chapters else None,
            "chapter_count": len(chapters),
            "sources": await asyncio.to_thread(self.scan_sources),
            "recent_edits": await asyncio.to_thread(self.writer.recent_edits, 30),
        }

    # ------------------------------------------------------------------ 原文扫描

    def _safe_path(self, path: str) -> Path:
        resolved = Path(path).resolve()
        if not any(str(resolved) == root or str(resolved).startswith(root + os.sep) for root in self.source_roots):
            raise BookLoreWriteError("path_not_allowed", "只能读取插件数据目录或小说目录下的文件")
        if resolved.suffix.lower() != ".txt" or not resolved.is_file():
            raise BookLoreWriteError("not_a_text_file", f"不是可读的 .txt 文件: {resolved.name}")
        return resolved

    def _read(self, path: Path) -> str:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def scan_sources(self) -> list[dict[str, Any]]:
        """列出允许目录下的小说原文，以及它们比书设库多出来的章节。"""
        known = self.writer.chapter_numbers()
        found: list[dict[str, Any]] = []
        for root in self.source_roots:
            base = Path(root)
            if not base.is_dir():
                continue
            for path in sorted(base.glob("*.txt")):
                try:
                    stat = path.stat()
                    if stat.st_size < 50_000:  # 小说原文至少几十 KB；跳过零碎文本
                        continue
                    cached = self._scan_cache.get(str(path))
                    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                        numbers, latest_title = cached[2], cached[3]
                    else:
                        chapters = split_chapters(self._read(path))
                        numbers = [c["number"] for c in chapters]
                        latest_title = chapters[-1]["title"] if chapters else ""
                        self._scan_cache[str(path)] = (stat.st_mtime, stat.st_size, numbers, latest_title)
                except OSError:
                    continue
                if len(numbers) < 3:
                    continue
                # 「新章节」只算比库里最新一章更靠后的。早期章节由 GraphRAG 社区摘要覆盖，没有逐章笔记，
                # 不能当成未入库（线上第 1–900 章就是这种情况，一键导入会重复收录并白耗向量调用）。
                latest_known = max(known) if known else 0
                missing = [n for n in numbers if n > latest_known]
                earlier_gaps = [n for n in numbers if n <= latest_known and n not in known]
                found.append({
                    "path": str(path),
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "chapters": len(numbers),
                    "latest_chapter": max(numbers),
                    "latest_title": latest_title,
                    "new_chapters": len(missing),
                    "new_range": [min(missing), max(missing)] if missing else None,
                    "earlier_without_notes": len(earlier_gaps),
                })
        return found

    def preview_text(self, text: str) -> list[dict[str, Any]]:
        known = self.writer.chapter_numbers()
        return [
            {"number": c["number"], "title": c["title"], "length": len(c["body"]), "exists": c["number"] in known}
            for c in split_chapters(text)
        ]

    # ------------------------------------------------------------------ 写入

    async def _embed(self, notes: Sequence[dict[str, Any]]) -> list[np.ndarray | None]:
        vectors: list[np.ndarray | None] = []
        for start in range(0, len(notes), EMBED_BATCH):
            batch = [_embed_text(note) for note in notes[start:start + EMBED_BATCH]]
            got = await self.embedding.get_embeddings(batch)
            for vector in got:
                if vector is None:
                    vectors.append(None)
                    continue
                arr = np.asarray(vector, dtype=np.float32)
                norm = float(np.linalg.norm(arr))
                vectors.append(arr / norm if norm > 0 else arr)
        return vectors

    def _require_ready(self) -> None:
        if self.index is None or self.embedding is None:
            raise BookLoreWriteError("book_lore_unavailable", "书设索引或向量服务未就绪")

    async def save_notes(self, notes: Sequence[dict[str, Any]], *, action: str = "save_note", actor: str = "webui") -> dict[str, Any]:
        """写库并当场更新笔记索引。向量算不出来时仍写库，留到「补齐索引」。"""
        self._require_ready()
        prepared = []
        for note in notes:
            item = {field: note.get(field) for field in ("id", "book_name", "arc", "category", "title", "content", "source_file")}
            item["book_name"] = item.get("book_name") or self.default_book
            prepared.append(item)
        async with self._lock:
            vectors = await self._embed(prepared)
            for item, vector in zip(prepared, vectors):
                item["vector"] = vector
            saved = await asyncio.to_thread(self.writer.upsert_notes, prepared, actor=actor, action=action)
            ready = [(item["id"], item["vector"]) for item in prepared if item["vector"] is not None]
            if ready:
                await asyncio.to_thread(self._index_upsert, ready)
        return {"saved": saved, "indexed": len(ready), "pending_vectors": len(saved) - len(ready)}

    def _index_upsert(self, pairs: Sequence[tuple[str, np.ndarray]]) -> None:
        self.index.upsert_notes([pid for pid, _ in pairs], np.stack([vec for _, vec in pairs]))
        self.index.save_notes()

    async def delete_note(self, note_id: str, *, actor: str = "webui") -> bool:
        self._require_ready()
        async with self._lock:
            removed = await asyncio.to_thread(self.writer.delete_note, note_id, actor=actor)
            if removed:
                await asyncio.to_thread(lambda: (self.index.remove_notes([note_id]), self.index.save_notes()))
        return removed

    async def sync_index(self, *, limit: int = 2000) -> dict[str, Any]:
        """补齐：库里有、索引里没有的笔记进索引；没向量的先算向量；索引里多出来的（库里已删）移除。"""
        self._require_ready()
        async with self._lock:
            db_ids = set(await asyncio.to_thread(self.writer.note_ids_all))
            indexed = self.index.note_ids()
            missing = sorted(db_ids - indexed)[: int(limit)]
            stale = sorted(indexed - db_ids)
            rows = await asyncio.to_thread(self.writer.notes_for_indexing, missing)
            with_vector = [(r["id"], self.writer.vector_of(r["vector"])) for r in rows]
            need_embed = [r for r, (_, vec) in zip(rows, with_vector) if vec is None]
            fresh = await self._embed(need_embed) if need_embed else []
            fresh_map = {row["id"]: vec for row, vec in zip(need_embed, fresh) if vec is not None}
            if fresh_map:
                await asyncio.to_thread(self.writer.set_vectors, fresh_map)
            pairs = [(nid, vec if vec is not None else fresh_map.get(nid)) for nid, vec in with_vector]
            pairs = [(nid, vec) for nid, vec in pairs if vec is not None]
            if pairs or stale:
                def _apply():
                    if stale:
                        self.index.remove_notes(stale)
                    if pairs:
                        self.index.upsert_notes([p for p, _ in pairs], np.stack([v for _, v in pairs]))
                    self.index.save_notes()
                await asyncio.to_thread(_apply)
        return {
            "missing": len(missing),
            "indexed": len(pairs),
            "embedded": len(fresh_map),
            "failed": len(missing) - len(pairs),
            "removed_stale": len(stale),
            "remaining": max(0, len(db_ids - indexed) - len(missing)),
        }

    async def import_chapters(
        self,
        *,
        text: str | None = None,
        path: str | None = None,
        numbers: Sequence[int] | None = None,
        arc: str = "",
        overwrite: bool = False,
        actor: str = "webui",
    ) -> dict[str, Any]:
        """从原文文件或粘贴的文本导入章节，默认只导入库里没有的章。"""
        if path:
            source = self._safe_path(path)
            text = await asyncio.to_thread(self._read, source)
            source_name = source.name
        else:
            source_name = "workbench_paste"
        chapters = split_chapters(text or "")
        if not chapters:
            raise BookLoreWriteError("no_chapters", "没有找到「第N章」开头的章节")
        known = await asyncio.to_thread(self.writer.chapter_numbers)
        wanted = set(int(n) for n in numbers) if numbers else None
        # 从原文文件导入且没指定章号时，只取比库里最新一章更新的（与扫描结果一致）；
        # 粘贴的文本是用户挑好的，库里没有的都导入。
        latest_known = max(known) if known else 0
        newer_only = bool(path) and wanted is None and not overwrite
        selected = [
            c for c in chapters
            if (wanted is None or c["number"] in wanted)
            and (overwrite or c["number"] not in known)
            and (not newer_only or c["number"] > latest_known)
        ]
        if len(selected) > MAX_IMPORT_CHAPTERS:
            raise BookLoreWriteError("too_many_chapters", f"一次最多导入 {MAX_IMPORT_CHAPTERS} 章，请分批")
        if not selected:
            return {"saved": [], "indexed": 0, "pending_vectors": 0, "skipped_existing": len(chapters)}
        arc = arc or await asyncio.to_thread(self._latest_arc)
        notes = [chapter_note(c, arc=arc, book_name=self.default_book, source_file=source_name) for c in selected]
        result = await self.save_notes(notes, action="import_chapters", actor=actor)
        result["chapters"] = [c["number"] for c in selected]
        result["arc"] = arc
        return result

    def _latest_arc(self) -> str:
        chapters = sorted(self.writer.chapter_numbers())
        if chapters:
            note = self.writer.get_note(f"note_ch{chapters[-1]}")
            if note and note.get("arc"):
                return str(note["arc"])
        return ""


__all__ = [
    "BookLoreWorkbench",
    "CHAPTER_CATEGORY",
    "PERSONAL_CATEGORIES",
    "chapter_note",
    "chinese_number",
    "split_chapters",
]
