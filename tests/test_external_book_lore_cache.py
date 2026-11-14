"""书设库的表结构信息按文件 mtime/size 缓存：文件没变不再每次跑 PRAGMA，文件变了立即失效。"""

from __future__ import annotations

import sqlite3
import time

import pytest

from domain.scope import CatalogScope
from engine.external_book_lore import ExternalBookLoreStore


def _make_db(path, extra_column: bool = False):
    conn = sqlite3.connect(path)
    extra = ", rank REAL" if extra_column else ""
    conn.execute(f"CREATE TABLE book_communities (id TEXT PRIMARY KEY, title TEXT, summary TEXT{extra})")
    conn.execute("INSERT INTO book_communities(id, title, summary) VALUES ('c1', '标题', '摘要')")
    conn.commit()
    conn.close()


def _scope() -> CatalogScope:
    return CatalogScope.for_book_lore("book-a") if hasattr(CatalogScope, "for_book_lore") else CatalogScope("book_lore", "book-a")


class _CountingConn:
    """包一层 sqlite 连接，统计 PRAGMA table_info 被执行的次数。"""

    def __init__(self, conn, counter):
        self._conn, self._counter = conn, counter

    def execute(self, sql, *args):
        if "table_info" in sql:
            self._counter["pragma"] += 1
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "book_lore.db"
    _make_db(path)
    return ExternalBookLoreStore(path), path


def test_columns_are_cached_while_the_file_is_unchanged(store):
    s, path = store
    counter = {"pragma": 0}
    conn = _CountingConn(sqlite3.connect(path), counter)
    first = s._columns(conn, "book_communities")
    second = s._columns(conn, "book_communities")
    third = s._columns(conn, "book_communities")
    assert first == second == third == {"id", "title", "summary"}
    assert counter["pragma"] == 1, "文件没变：只在第一次读表结构"


def test_cache_is_invalidated_when_the_file_changes(store):
    s, path = store
    conn = sqlite3.connect(path)
    assert "rank" not in s._columns(conn, "book_communities")
    conn.close()
    time.sleep(0.02)
    other = sqlite3.connect(path)
    other.execute("ALTER TABLE book_communities ADD COLUMN rank REAL")
    other.commit()
    other.close()
    conn = sqlite3.connect(path)
    assert "rank" in s._columns(conn, "book_communities"), "书库文件变了，缓存必须失效"
    conn.close()


def test_missing_table_is_not_cached_as_present(store):
    s, path = store
    conn = sqlite3.connect(path)
    assert s._columns(conn, "book_notes") == set()
    conn.execute("CREATE TABLE book_notes (id TEXT, title TEXT, content TEXT)")
    conn.commit()
    conn.close()
    time.sleep(0.02)
    conn = sqlite3.connect(path)
    assert "content" in s._columns(conn, "book_notes")
    conn.close()


def test_returned_sets_are_copies(store):
    s, path = store
    conn = sqlite3.connect(path)
    first = s._columns(conn, "book_communities")
    first.add("junk")
    assert "junk" not in s._columns(conn, "book_communities")
    conn.close()
