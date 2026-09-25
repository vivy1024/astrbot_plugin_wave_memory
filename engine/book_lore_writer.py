"""书设库的写入端：书设工作台新增、修改、删除笔记与别名，并记改动日志。

插件读取书设一直走 :class:`engine.external_book_lore.ExternalBookLoreStore`（只读）；这里是唯一
的写入口，只给 9876 书设工作台用。书设是独立知识库（``book_lore.db``），不走主库的
WriteCoordinator，也不进 outbox。每次写入一个短事务，WAL 模式下不挡读取。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

EDIT_LOG_TABLE = "book_lore_edits"
NOTE_FIELDS = ("id", "book_name", "arc", "category", "title", "content", "source_file")


class BookLoreWriteError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BookLoreWriter:
    def __init__(self, db_path: str | Path, *, dimension: int = 1024) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        self.dimension = int(dimension)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            if not Path(self.db_path).is_file():
                raise BookLoreWriteError("book_lore_database_unavailable", f"书设库不存在: {self.db_path}")
            conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {EDIT_LOG_TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        at REAL NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
                        actor TEXT NOT NULL DEFAULT 'webui', detail TEXT NOT NULL DEFAULT '{{}}')"""
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ 读

    def _vector_blob(self, vector: Any) -> bytes | None:
        if vector is None:
            return None
        arr = np.asarray(vector, dtype=np.float32).reshape(-1)
        if arr.size != self.dimension:
            raise BookLoreWriteError("vector_dimension_mismatch", f"向量维度 {arr.size}，书设库要求 {self.dimension}")
        return arr.tobytes()

    def vector_of(self, blob: Any) -> np.ndarray | None:
        if blob is None or len(blob) != self.dimension * 4:
            return None
        return np.frombuffer(blob, dtype=np.float32).copy()

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection().execute(
                f"SELECT {', '.join(NOTE_FIELDS)}, vector IS NOT NULL AS has_vector FROM book_notes WHERE id = ?",
                (str(note_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_notes(
        self,
        *,
        query: str = "",
        category: str = "",
        arc: str = "",
        note_ids: Iterable[str] | None = None,
        exclude_ids: Iterable[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        where, params = ["1=1"], []
        if query:
            where.append("(title LIKE ? OR content LIKE ? OR id LIKE ?)")
            params += [f"%{query}%"] * 3
        if category:
            where.append("category = ?")
            params.append(category)
        if arc:
            where.append("arc = ?")
            params.append(arc)
        if note_ids is not None:
            ids = list(note_ids)
            if not ids:
                return {"items": [], "total": 0}
            where.append(f"id IN ({','.join('?' for _ in ids)})")
            params += ids
        if exclude_ids is not None:
            excluded = list(exclude_ids)
            if excluded:
                where.append(f"id NOT IN ({','.join('?' for _ in excluded)})")
                params += excluded
        sql_where = " AND ".join(where)
        with self._lock:
            conn = self._connection()
            total = conn.execute(f"SELECT COUNT(*) FROM book_notes WHERE {sql_where}", params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT id, book_name, arc, category, title, substr(content, 1, 160) AS preview,
                           length(content) AS length, vector IS NOT NULL AS has_vector
                      FROM book_notes WHERE {sql_where}
                     ORDER BY CASE WHEN id LIKE 'note_ch%' THEN 0 ELSE 1 END,
                              CAST(substr(id, 8) AS INTEGER) DESC, id
                     LIMIT ? OFFSET ?""",
                (*params, int(limit), int(offset)),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": int(total)}

    def facets(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            categories = conn.execute("SELECT category, COUNT(*) FROM book_notes GROUP BY category ORDER BY 2 DESC").fetchall()
            arcs = conn.execute("SELECT arc, COUNT(*) FROM book_notes GROUP BY arc ORDER BY arc").fetchall()
            books = [r[0] for r in conn.execute("SELECT DISTINCT book_name FROM book_notes").fetchall()]
        return {
            "categories": [{"name": r[0] or "", "count": int(r[1])} for r in categories],
            "arcs": [{"name": r[0] or "", "count": int(r[1])} for r in arcs],
            "books": books,
        }

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            conn = self._connection()
            for tier, table in (("entities", "book_entities"), ("communities", "book_communities"), ("notes", "book_notes")):
                try:
                    total, vectors = conn.execute(
                        f"SELECT COUNT(*), SUM(vector IS NOT NULL) FROM {table}"
                    ).fetchone()
                except sqlite3.Error:
                    total, vectors = 0, 0
                out[tier] = {"rows": int(total or 0), "with_vector": int(vectors or 0)}
        return out

    def note_ids_all(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._connection().execute("SELECT id FROM book_notes").fetchall()]

    def notes_for_indexing(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        out: list[dict[str, Any]] = []
        with self._lock:
            conn = self._connection()
            for start in range(0, len(ids), 500):
                chunk = list(ids[start:start + 500])
                rows = conn.execute(
                    f"SELECT id, title, content, vector FROM book_notes WHERE id IN ({','.join('?' for _ in chunk)})",
                    chunk,
                ).fetchall()
                out.extend({"id": r["id"], "title": r["title"], "content": r["content"], "vector": r["vector"]} for r in rows)
        return out

    def chapter_numbers(self) -> set[int]:
        with self._lock:
            rows = self._connection().execute("SELECT id FROM book_notes WHERE id LIKE 'note_ch%'").fetchall()
        numbers: set[int] = set()
        for (note_id,) in rows:
            tail = str(note_id)[len("note_ch"):]
            if tail.isdigit():
                numbers.add(int(tail))
        return numbers

    def recent_edits(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection().execute(
                f"SELECT id, at, action, target, actor, detail FROM {EDIT_LOG_TABLE} ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except ValueError:
                item["detail"] = {}
            items.append(item)
        return items

    # ------------------------------------------------------------------ 写

    def _log(self, conn: sqlite3.Connection, action: str, target: str, actor: str, detail: dict[str, Any]) -> None:
        conn.execute(
            f"INSERT INTO {EDIT_LOG_TABLE}(at, action, target, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), action, target, actor or "webui", json.dumps(detail, ensure_ascii=False)),
        )

    def upsert_notes(self, notes: Sequence[dict[str, Any]], *, actor: str = "webui", action: str = "save_note") -> list[str]:
        """写入笔记（同 id 覆盖）。每条可带 ``vector``；不带时清空旧向量，等补齐索引时再算。"""
        saved: list[str] = []
        with self._lock:
            conn = self._connection()
            try:
                for note in notes:
                    note_id = str(note.get("id") or "").strip()
                    title = str(note.get("title") or "").strip()
                    content = str(note.get("content") or "").strip()
                    if not note_id or not title or not content:
                        raise BookLoreWriteError("note_incomplete", "笔记需要 id、标题和正文")
                    blob = self._vector_blob(note.get("vector"))
                    conn.execute(
                        """INSERT INTO book_notes(id, book_name, arc, category, title, content, source_file, vector)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET book_name=excluded.book_name, arc=excluded.arc,
                               category=excluded.category, title=excluded.title, content=excluded.content,
                               source_file=excluded.source_file, vector=excluded.vector""",
                        (note_id, str(note.get("book_name") or ""), str(note.get("arc") or ""),
                         str(note.get("category") or ""), title, content, str(note.get("source_file") or "workbench"), blob),
                    )
                    saved.append(note_id)
                detail = {"count": len(saved)} if len(saved) > 1 else {"title": str(notes[0].get("title") or "")[:60]} if notes else {}
                self._log(conn, action, saved[0] if len(saved) == 1 else f"{len(saved)} 条", actor, detail)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return saved

    def set_vectors(self, vectors: dict[str, Any]) -> int:
        """只回填向量（补齐索引用），不记改动日志。"""
        with self._lock:
            conn = self._connection()
            try:
                for note_id, vector in vectors.items():
                    conn.execute("UPDATE book_notes SET vector = ? WHERE id = ?", (self._vector_blob(vector), note_id))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return len(vectors)

    def delete_note(self, note_id: str, *, actor: str = "webui") -> bool:
        with self._lock:
            conn = self._connection()
            try:
                row = conn.execute("SELECT title FROM book_notes WHERE id = ?", (note_id,)).fetchone()
                if row is None:
                    return False
                conn.execute("DELETE FROM book_notes WHERE id = ?", (note_id,))
                self._log(conn, "delete_note", note_id, actor, {"title": row["title"]})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return True


__all__ = ["BookLoreWriteError", "BookLoreWriter", "EDIT_LOG_TABLE"]
