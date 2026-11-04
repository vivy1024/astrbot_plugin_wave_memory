"""全局待审计数：口径与各页「待审」一致、按 Bot 过滤、单项失败只降级该项、30 秒缓存。"""

from __future__ import annotations

import sqlite3

import pytest

from webui.blueprints import review_summary as rs


class _Cm:
    def __init__(self, conn):
        self.conn = conn
        self.calls = 0

    def execute_read(self, sql, params=()):
        self.calls += 1
        return self.conn.execute(sql, params)


class _Db:
    def __init__(self, cm):
        self._cm = cm


class _Container:
    def __init__(self, cm):
        self.db = _Db(cm)


@pytest.fixture
def container():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE scoped_facts (bot_id TEXT, status TEXT);
        CREATE TABLE scoped_jargon (bot_id TEXT, status TEXT);
        CREATE TABLE reply_expression_candidates (bot_id TEXT, status TEXT);
        CREATE TABLE bot_beliefs (bot_id TEXT, status TEXT);
        CREATE TABLE game_knowledge_cards (bot_id TEXT, status TEXT);
        """
    )
    rows = {
        "scoped_facts": [("yushu", "pending"), ("yushu", "quarantined"), ("yushu", "conflict"),
                         ("yushu", "active"), ("yushu", "rejected"), ("baizz", "pending")],
        "scoped_jargon": [("yushu", "pending"), ("yushu", "pending"), ("yushu", "confirmed"), ("baizz", "pending")],
        "reply_expression_candidates": [("yushu", "pending"), ("yushu", "approved"), ("baizz", "pending")],
        "bot_beliefs": [("yushu", "sprout"), ("yushu", "formed"), ("baizz", "sprout")],
        "game_knowledge_cards": [("", "draft"), ("yushu", "draft"), ("baizz", "draft"), ("yushu", "active")],
    }
    for table, values in rows.items():
        conn.executemany(f"INSERT INTO {table} VALUES (?, ?)", values)
    rs.clear_review_summary_cache()
    yield _Container(_Cm(conn))
    rs.clear_review_summary_cache()
    conn.close()


def test_counts_follow_page_review_semantics_and_bot_filter(container):
    payload = rs.build_review_summary(container, "yushu", now=1.0)
    assert payload["counts"] == {"facts": 3, "jargon": 2, "expressions": 1, "beliefs": 1, "game_drafts": 2}
    assert payload["total"] == 9
    assert payload["errors"] == {}
    assert payload["bot_id"] == "yushu" and payload["generated_at"] == 1.0


def test_missing_table_degrades_only_that_count(container):
    container.db._cm.conn.execute("DROP TABLE bot_beliefs")
    payload = rs.build_review_summary(container, "yushu")
    assert payload["counts"]["beliefs"] is None
    assert payload["errors"]["beliefs"] == "OperationalError"
    assert payload["counts"]["facts"] == 3
    assert payload["total"] == 8


def test_no_database_returns_all_none():
    payload = rs.build_review_summary(type("C", (), {"db": None})(), "yushu")
    assert set(payload["counts"].values()) == {None}
    assert payload["total"] == 0


@pytest.mark.asyncio
async def test_route_requires_bot_id_and_returns_counts(container):
    from quart import Quart

    from webui.blueprints import get_blueprints
    from webui.container import get_container

    assert rs.review_summary_bp in get_blueprints(), "可选蓝图要被注册"
    real = get_container()
    saved = (real.db, real.password)
    real.db, real.password = container.db, ""
    try:
        app = Quart(__name__)
        app.register_blueprint(rs.review_summary_bp)
        client = app.test_client()
        res = await client.get("/api/review/summary")
        assert res.status_code == 400
        assert (await res.get_json())["error"]["code"] == "bot_id_required"
        res = await client.get("/api/review/summary?bot_id=yushu")
        data = await res.get_json()
        assert res.status_code == 200 and data["counts"]["facts"] == 3 and data["total"] == 9
    finally:
        real.db, real.password = saved


def test_cache_hits_within_window_and_expires(container):
    cm = container.db._cm
    first = rs.cached_review_summary(container, "yushu", now=100.0)
    calls = cm.calls
    assert rs.cached_review_summary(container, "yushu", now=100.0 + rs.CACHE_SECONDS - 1) is first
    assert cm.calls == calls
    rs.cached_review_summary(container, "yushu", now=100.0 + rs.CACHE_SECONDS + 1)
    assert cm.calls > calls
    # 不同 Bot 各自缓存
    other = rs.cached_review_summary(container, "baizz", now=200.0)
    assert other["counts"]["facts"] == 1
