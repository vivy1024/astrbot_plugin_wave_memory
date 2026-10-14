"""Unit tests for fanout→shared_memory_grants dry-run planner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.fanout_to_shared_grants_dryrun import (
    apply_grants,
    choose_owner,
    filter_candidates,
    plan_grants,
)


def _mk_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_posix())
    conn.executescript(
        """
        CREATE TABLE memories(
            id INTEGER PRIMARY KEY,
            content TEXT,
            group_id TEXT,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            resolution_state TEXT,
            provenance TEXT
        );
        CREATE TABLE scope_recovery_memory_map(
            legacy_memory_id INTEGER NOT NULL,
            target_scope_key TEXT NOT NULL,
            target_memory_id INTEGER NOT NULL,
            origin_key TEXT,
            run_id TEXT,
            PRIMARY KEY (legacy_memory_id, target_scope_key)
        );
        """
    )
    # legacy owner in g1, fanout copies in g2/g3
    conn.execute(
        "INSERT INTO memories VALUES (10,'owned','g1','yushu','qq:group:g1','group','resolved',NULL)"
    )
    conn.execute(
        "INSERT INTO memories VALUES (20,'copy2','g2','yushu','qq:group:g2','group','resolved',"
        "'{\"projection_kind\":\"fanout_duplicate\"}')"
    )
    conn.execute(
        "INSERT INTO memories VALUES (30,'copy3','g3','yushu','qq:group:g3','group','resolved',"
        "'{\"projection_kind\":\"fanout_duplicate\"}')"
    )
    for sk, mid in (
        ("yushu|qq:group:g1|group", 20),  # map sometimes points only at copies; legacy still owner
        ("yushu|qq:group:g2|group", 20),
        ("yushu|qq:group:g3|group", 30),
    ):
        conn.execute(
            "INSERT INTO scope_recovery_memory_map VALUES (10,?,?, 'o','r')",
            (sk, mid),
        )
    # second family: no legacy row, one unmarked target + one marked
    conn.execute(
        "INSERT INTO memories VALUES (100,'keep','ga','yushu','qq:group:ga','group','resolved',NULL)"
    )
    conn.execute(
        "INSERT INTO memories VALUES (101,'dup','gb','yushu','qq:group:gb','group','resolved',"
        "'{\"projection_kind\":\"fanout_duplicate\"}')"
    )
    conn.execute(
        "INSERT INTO scope_recovery_memory_map VALUES (99,'yushu|qq:group:ga|group',100,'o','r')"
    )
    conn.execute(
        "INSERT INTO scope_recovery_memory_map VALUES (99,'yushu|qq:group:gb|group',101,'o','r')"
    )
    conn.commit()
    return conn


def test_choose_owner_prefers_legacy_formal_unmarked():
    meta = {
        10: {
            "id": 10,
            "bot_id": "yushu",
            "session_id": "qq:group:g1",
            "visibility": "group",
            "group_id": "g1",
            "is_fanout_duplicate": False,
        },
        20: {
            "id": 20,
            "bot_id": "yushu",
            "session_id": "qq:group:g2",
            "visibility": "group",
            "group_id": "g2",
            "is_fanout_duplicate": True,
        },
    }
    oid, reason = choose_owner(10, [20, 30], meta)
    assert oid == 10
    assert reason == "legacy_formal_unmarked"


def test_choose_owner_skips_unscoped_legacy_uses_preferred_keeper():
    meta = {
        10: {
            "id": 10,
            "bot_id": "",
            "session_id": "",
            "visibility": "",
            "group_id": "115",
            "is_fanout_duplicate": False,
        },
        20: {
            "id": 20,
            "bot_id": "baizz",
            "session_id": "x:group:1",
            "visibility": "group",
            "group_id": "1",
            "is_fanout_duplicate": True,
        },
        21: {
            "id": 21,
            "bot_id": "yushu",
            "session_id": "羽书:group:100000001",
            "visibility": "group",
            "group_id": "100000001",
            "is_fanout_duplicate": True,
        },
    }
    oid, reason = choose_owner(10, [20, 21], meta, preferred_group_id="100000001")
    assert oid == 21
    assert reason == "preferred_scope_fanout_keeper"


def test_plan_grants_dryrun_candidates(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = _mk_db(db)
    plan = plan_grants(conn, sample_output=50)
    conn.close()
    assert plan["phase2_promote_allowed"] is False
    assert plan["writes_memories"] is False
    assert plan["grant_candidates"] >= 2
    # family 10: consumers g2,g3 for owner 10
    owners = {c["owner_memory_id"] for c in plan["candidates"]}
    assert 10 in owners
    assert 100 in owners
    consumers = {
        (c["owner_memory_id"], c["consumer_scope"]["group_id"])
        for c in plan["candidates"]
    }
    assert (10, "g2") in consumers
    assert (10, "g3") in consumers
    assert (100, "gb") in consumers
    # owner never grants to self
    assert all(
        c["owner_scope"]["session_id"] != c["consumer_scope"]["session_id"]
        for c in plan["candidates"]
    )


def test_same_bot_only_filter_and_staged_apply(tmp_path: Path):
    db = tmp_path / "src.db"
    conn = _mk_db(db)
    plan = plan_grants(conn, same_bot_only=True, sample_output=50)
    conn.close()
    assert plan["same_bot_only"] is True
    assert plan["cross_bot_candidates"] == 0
    assert all(not c.get("cross_bot") for c in plan["candidates"])
    # filter helper
    mixed = plan_grants(sqlite3.connect(db.as_posix()), same_bot_only=False)["candidates"]
    filtered = filter_candidates(mixed, same_bot_only=True)
    assert len(filtered) < len(mixed) or all(not c.get("cross_bot") for c in mixed)
    assert all(not c.get("cross_bot") for c in filtered)

    # staged apply into empty/other file
    staged = tmp_path / "staged_grants.db"
    # create empty file DB for ConnectionManager
    sqlite3.connect(staged.as_posix()).close()
    result = apply_grants(staged, plan["candidates"], limit=10)
    assert result["applied"] is True
    assert result["created"] + result["reactivated"] + result["skipped"] == result["batch"]
    assert result["created"] >= 1

    # verify table has rows and no memories table pollution required
    c2 = sqlite3.connect(staged.as_posix())
    n = c2.execute("SELECT COUNT(*) FROM shared_memory_grants WHERE status='active'").fetchone()[0]
    c2.close()
    assert int(n) >= 1
