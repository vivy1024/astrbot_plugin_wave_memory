"""中文全文索引 fts_memories_cjk：切词、增量同步、回填与 fts5 通道切换。"""

from __future__ import annotations

import asyncio
import sqlite3

from engine.db import fts_cjk

from test_fts5_channel import DBBox, FTS5ChannelTest


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY, content TEXT, memory_type TEXT, quarantine INTEGER
        )"""
    )
    fts_cjk.ensure_schema(conn)
    return conn


def test_tokenize_splits_cjk_into_bigrams_and_lowercases_words():
    assert fts_cjk.tokenize("张羽师兄 OK") == ["张羽", "羽师", "师兄", "ok"]
    assert fts_cjk.tokenize("羽") == ["羽"]
    assert fts_cjk.tokenize("") == []


def test_match_expr_uses_adjacent_phrases_and_skips_single_chars():
    assert fts_cjk.match_expr(["张羽", "灵石矿", "的", "a"]) == '"张羽" OR "灵石 石矿"'
    assert fts_cjk.match_expr(["张羽", "张羽"]) == '"张羽"'
    assert fts_cjk.match_expr([]) == ""


def test_query_matches_word_inside_sentence_but_not_split_characters():
    conn = _conn()
    conn.executemany(
        "INSERT INTO memories(id, content, memory_type, quarantine) VALUES (?, ?, 'message', 0)",
        [(1, "张羽师兄最近怎样"), (2, "张某羽来过"), (3, "今天挖到灵石矿了")],
    )
    for memory_id in (1, 2, 3):
        fts_cjk.sync_memory(conn, memory_id)

    assert fts_cjk.query_ids(conn, ["张羽"]) == [1]
    assert fts_cjk.query_ids(conn, ["灵石矿"]) == [3]
    assert fts_cjk.query_ids(conn, ["张羽", "灵石矿"]) and set(fts_cjk.query_ids(conn, ["张羽", "灵石矿"])) == {1, 3}


def test_sync_removes_rows_that_are_no_longer_indexable():
    conn = _conn()
    conn.execute("INSERT INTO memories VALUES (1, '张羽来了', 'message', 0)")
    assert fts_cjk.sync_memory(conn, 1) == "indexed"
    conn.execute("UPDATE memories SET quarantine=1 WHERE id=1")
    assert fts_cjk.sync_memory(conn, 1) == "removed"
    assert fts_cjk.query_ids(conn, ["张羽"]) == []
    conn.execute("DELETE FROM memories WHERE id=1")
    assert fts_cjk.sync_memory(conn, 1) == "removed"


def test_backfill_batches_skip_noise_and_mark_ready():
    conn = _conn()
    conn.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, 0)",
        [(1, "张羽一", "message"), (2, "张羽二", "noise"), (3, "张羽三", "message"), (4, "", "message")],
    )
    assert not fts_cjk.is_ready(conn)
    cursor, written = fts_cjk.backfill_batch(conn, after_id=0, limit=1)
    assert (cursor, written) == (1, 1)
    cursor, written = fts_cjk.backfill_batch(conn, after_id=cursor, limit=10)
    assert (cursor, written) == (3, 1)
    assert fts_cjk.backfill_batch(conn, after_id=cursor) == (3, 0)
    fts_cjk.mark_ready(conn)
    state = fts_cjk.status(conn)
    assert state["ready"] and state["rows"] == 2 and state["backfill_cursor"] == 3
    assert sorted(fts_cjk.query_ids(conn, ["张羽"])) == [1, 3]
    fts_cjk.reset(conn)
    assert not fts_cjk.is_ready(conn) and fts_cjk.status(conn)["rows"] == 0


def test_status_without_schema():
    assert fts_cjk.status(sqlite3.connect(":memory:")) == {"exists": False, "ready": False, "rows": 0}


class FTS5ChannelCjkTest(FTS5ChannelTest):
    """fts5 通道：中文索引就绪后按两字片段召回，未就绪时保持旧行为。"""

    def _cjk_db(self, *, ready: bool) -> DBBox:
        db = self._db()
        fts_cjk.ensure_schema(db.conn)
        # 旧索引里「张羽」和后文连成一个词，旧路径只能靠 LIKE 兜底
        self._insert_memory(db, (1, "昨天张羽师兄带我去挖灵石矿", "u1", "用户", 1000.0, 1.0, "live", "g1", "message"))
        self._insert_memory(db, (2, "别的群里张羽也出现过", "u2", "路人", 900.0, 1.0, "live", "g2", "message"))
        for memory_id in (1, 2):
            fts_cjk.sync_memory(db.conn, memory_id)
        if ready:
            fts_cjk.mark_ready(db.conn)
        db.conn.commit()
        return db

    def test_ready_index_recalls_word_inside_sentence_with_scope_filter(self):
        from services.injection.channels.fts5 import FTS5Channel

        db = self._cjk_db(ready=True)
        result = asyncio.run(FTS5Channel(db=db, cross_group_enabled=False).build(self._ctx(message="张羽 灵石矿")))

        self.assertEqual(result.status, "hit")
        self.assertEqual([item["id"] for item in result.items], [1])

    def test_not_ready_index_keeps_legacy_path(self):
        from services.injection.channels import fts5

        db = self._cjk_db(ready=False)
        self.assertEqual(fts5._cjk_match_expr(db.conn, ["张羽"]), "")
        result = asyncio.run(fts5.FTS5Channel(db=db, cross_group_enabled=False).build(self._ctx(message="张羽 灵石矿")))
        # 旧路径靠作用域内 LIKE 兜底，仍然只返回本群
        self.assertEqual([item["id"] for item in result.items], [1])

    def test_long_unsegmented_keyword_is_split_into_bigrams(self):
        from services.injection.channels import fts5

        db = self._cjk_db(ready=True)
        expr = fts5._cjk_match_expr(db.conn, ["师兄带我去挖"])
        self.assertIn('"师兄"', expr)
        self.assertIn(" OR ", expr)
