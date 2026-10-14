"""Unit tests for unscoped owned formalize dry-run (no production)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.unscoped_owned_formalize_dryrun import (  # noqa: E402
    CONFIRMATION,
    apply_formalize,
    build_group_scope_map,
    build_group_scope_map_from_soul,
    load_scope_map_json,
    plan_formalize,
)


def _mk_db(path: Path) -> None:
    conn = sqlite3.connect(path.as_posix())
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            resolution_state TEXT
        );
        """
    )
    # formal peer for group 100000001
    conn.execute(
        "INSERT INTO memories VALUES (1,'100000001','u1','A','hi','yushu','羽书:group:100000001','group','resolved')"
    )
    # unscoped human in same group
    conn.execute(
        "INSERT INTO memories VALUES (2,'100000001','135','半桥','聊天内容',NULL,NULL,NULL,NULL)"
    )
    # unscoped bot noise
    conn.execute(
        "INSERT INTO memories VALUES (3,'100000001','bot','羽书','系统句',NULL,NULL,NULL,NULL)"
    )
    # unscoped no formal peer
    conn.execute(
        "INSERT INTO memories VALUES (4,'999999999','u2','B','无peer',NULL,NULL,NULL,NULL)"
    )
    # lore skip
    conn.execute(
        "INSERT INTO memories VALUES (5,'book_lore','u3','C','lore',NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()


def test_plan_only_owned_candidates(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    _mk_db(db)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    plan = plan_formalize(conn, limit=50)
    conn.close()
    ids = {c["id"] for c in plan["candidates"]}
    assert 2 in ids
    assert 3 not in ids  # bot noise
    assert 4 not in ids  # no peer
    assert 5 not in ids  # lore
    assert plan["rules"]["fanout_forbidden"] is True
    assert plan["rules"]["phase2_promote_allowed"] is False
    assert plan["confirmation_for_apply"] == CONFIRMATION


def test_apply_updates_without_new_rows(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    _mk_db(db)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    plan = plan_formalize(conn, limit=50)
    conn.close()
    result = apply_formalize(db, plan["candidates"], limit=0)
    assert result["updated"] == 1
    assert result["writes_new_rows"] is False
    conn = sqlite3.connect(db.as_posix())
    row = conn.execute(
        "SELECT bot_id, session_id, visibility, resolution_state FROM memories WHERE id=2"
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conn.close()
    assert row == (
        "yushu",
        "羽书:group:100000001",
        "group",
        "owned_formalized_from_unscoped",
    )
    assert total == 5


def test_per_group_limit_balances(tmp_path: Path) -> None:
    db = tmp_path / "bal.db"
    conn = sqlite3.connect(db.as_posix())
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT,
            content TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT
        )
        """
    )
    # formal peers (group ids must be 5+ digits for numeric gate)
    conn.execute(
        "INSERT INTO memories VALUES (1,'11111','f','F','p','yushu','羽书:group:11111','group',NULL)"
    )
    conn.execute(
        "INSERT INTO memories VALUES (2,'22222','f','F','p','yushu','羽书:group:22222','group',NULL)"
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL)",
            (100 + i, "11111", f"u{i}", f"N{i}", f"c{i}"),
        )
    for i in range(10):
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL)",
            (200 + i, "22222", f"v{i}", f"M{i}", f"d{i}"),
        )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    plan = plan_formalize(conn, limit=6, per_group_limit=3)
    inv = plan_formalize(conn, limit=0, inventory_only=True)
    conn.close()
    assert plan["by_group"].get("11111") == 3
    assert plan["by_group"].get("22222") == 3
    assert inv["eligible_total"] == 20
    assert inv["eligible_by_group"]["11111"] == 10


def test_explicit_private_scope_map(tmp_path: Path) -> None:
    db = tmp_path / "priv.db"
    conn = sqlite3.connect(db.as_posix())
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT,
            content TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories VALUES (1,'private:12345','u1','A','hi',NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()
    mp = tmp_path / "pmap.json"
    mp.write_text(
        json.dumps(
            {
                "private:12345": {
                    "bot_id": "yushu",
                    "session_id": "羽书:private:12345",
                    "visibility": "private",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    explicit = load_scope_map_json(mp)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    plan = plan_formalize(conn, limit=10, explicit_scope_map=explicit)
    conn.close()
    assert plan["candidate_count"] == 1
    assert plan["candidates"][0]["proposed"]["visibility"] == "private"
    assert plan["candidates"][0]["proposed"]["session_id"] == "羽书:private:12345"


def test_explicit_scope_map_json(tmp_path: Path) -> None:
    db = tmp_path / "ex.db"
    conn = sqlite3.connect(db.as_posix())
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT,
            content TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories VALUES (1,'581158875','u1','A','hi',NULL,NULL,NULL,NULL)"
    )
    # memory peer for another group should not be overridden
    conn.execute(
        "INSERT INTO memories VALUES (2,'100000001','f','F','p','yushu','羽书:group:100000001','group',NULL)"
    )
    conn.execute(
        "INSERT INTO memories VALUES (3,'100000001','u2','B','x',NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()
    mp = tmp_path / "map.json"
    mp.write_text(
        json.dumps(
            {
                "581158875": {
                    "bot_id": "yushu",
                    "session_id": "羽书:group:581158875",
                },
                # would try to override memory peer — ignored for existing peer
                "100000001": {
                    "bot_id": "baizz",
                    "session_id": "白真真:group:100000001",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    explicit = load_scope_map_json(mp)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    plan = plan_formalize(conn, limit=10, explicit_scope_map=explicit)
    conn.close()
    by = {c["id"]: c for c in plan["candidates"]}
    assert 1 in by
    assert by[1]["proposed"]["bot_id"] == "yushu"
    assert by[1]["proposed"]["session_id"] == "羽书:group:581158875"
    assert 3 in by
    assert by[3]["proposed"]["bot_id"] == "yushu"  # memory peer wins
    assert "581158875" in plan["explicit_only_groups"]


def test_soul_scope_map_fills_missing_peer(tmp_path: Path) -> None:
    db = tmp_path / "soul.db"
    conn = sqlite3.connect(db.as_posix())
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT,
            content TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT
        );
        CREATE TABLE scoped_soul_relationships (
            bot_id TEXT, session_id TEXT, visibility TEXT, subject_principal_id TEXT
        );
        """
    )
    # no formal memory peer for 1000000004
    conn.execute(
        "INSERT INTO memories VALUES (1,'1000000004','u1','A','hi',NULL,NULL,NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES ('yushu','羽书:group:1000000004','group','羽书:user:1')"
    )
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES ('yushu','羽书:group:1000000004','group','羽书:user:2')"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    without = plan_formalize(conn, limit=10, include_soul_scope_map=False)
    with_soul = plan_formalize(conn, limit=10, include_soul_scope_map=True)
    soul_map = build_group_scope_map_from_soul(conn)
    conn.close()
    assert without["candidate_count"] == 0
    assert with_soul["candidate_count"] == 1
    assert with_soul["candidates"][0]["proposed"]["session_id"] == "羽书:group:1000000004"
    assert soul_map["1000000004"]["bot_id"] == "yushu"
    assert "1000000004" in with_soul["soul_only_groups"]


def test_scope_map_prefers_yushu(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db.as_posix())
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT,
            content TEXT, bot_id TEXT, session_id TEXT, visibility TEXT, resolution_state TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories VALUES (1,'150727649','a','A','x','baizz','白真真:group:150727649','group',NULL)"
    )
    conn.execute(
        "INSERT INTO memories VALUES (2,'150727649','b','B','y','yushu','羽书:group:150727649','group',NULL)"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    m = build_group_scope_map(conn)
    conn.close()
    assert m["150727649"]["bot_id"] == "yushu"
