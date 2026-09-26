"""v5.1 存储瘦身：旧全文索引只在索引列变化时重写、中文索引不存正文副本、去掉重复索引。"""

from __future__ import annotations

import sqlite3
import sys
import types
from types import SimpleNamespace

import pytest

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from engine.database import WaveMemoryDB
from engine.db import fts_cjk


def _names(conn, kind):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def test_legacy_fts_update_trigger_is_upgraded_and_skips_non_text_updates(tmp_path):
    path = tmp_path / "w.db"
    db = WaveMemoryDB(str(path), dimension=4)
    db.close()
    # 模拟旧库：换回「任何 UPDATE 都重写」的触发器
    conn = sqlite3.connect(path)
    conn.executescript("""
        DROP TRIGGER fts_memories_au;
        CREATE TRIGGER fts_memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO fts_memories(fts_memories, rowid, content, sender_name, group_id)
            VALUES ('delete', old.id, old.content, old.sender_name, old.group_id);
            INSERT INTO fts_memories(rowid, content, sender_name, group_id)
            VALUES (new.id, new.content, new.sender_name, new.group_id);
        END;
    """)
    conn.close()

    db = WaveMemoryDB(str(path), dimension=4)
    try:
        sql = db.conn.execute("SELECT sql FROM sqlite_master WHERE name='fts_memories_au'").fetchone()[0]
        assert "UPDATE OF content, sender_name, group_id" in sql
        db.conn.execute("INSERT INTO memories(id, group_id, content, timestamp) VALUES (1, 'g', '原来的内容', 1.0)")
        db.conn.commit()
        db.conn.execute("UPDATE memories SET access_count = access_count + 1 WHERE id = 1")
        db.conn.execute("UPDATE memories SET content = '改过的内容' WHERE id = 1")
        db.conn.commit()
        hits = db.conn.execute("SELECT rowid FROM fts_memories WHERE fts_memories MATCH '改过的内容'").fetchall()
        assert hits == [(1,)]
        assert db.conn.execute("SELECT rowid FROM fts_memories WHERE fts_memories MATCH '原来的内容'").fetchall() == []
    finally:
        db.close()


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 43, 0), reason="contentless_delete 需要 SQLite 3.43+")
def test_cjk_index_is_contentless_and_supports_delete(tmp_path):
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, quarantine INTEGER, memory_type TEXT)")
    fts_cjk.ensure_schema_committed(conn)
    assert f"{fts_cjk.TABLE}_content" not in _names(conn, "table")
    conn.execute("INSERT INTO memories VALUES (1, '张羽师兄最近怎样', 0, 'message')")
    assert fts_cjk.sync_memory(conn, 1) == "indexed"
    assert fts_cjk.query_ids(conn, ["张羽"]) == [1]
    conn.execute("UPDATE memories SET content='换了话题' WHERE id=1")
    fts_cjk.sync_memory(conn, 1)
    assert fts_cjk.query_ids(conn, ["张羽"]) == []
    assert fts_cjk.query_ids(conn, ["话题"]) == [1]


def test_redundant_prefix_indexes_are_dropped(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "i.db"), dimension=4)
    try:
        indexes = _names(db.conn, "index")
        assert "idx_scoped_memory_tags_scope_memory" not in indexes
        assert "idx_scoped_tags_scope_name" not in indexes
        assert "idx_scoped_memory_tags_scope_tag" in indexes
    finally:
        db.close()
