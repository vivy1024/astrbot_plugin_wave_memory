"""scoped_jargon 标签链回填脚本的行为测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_jargon_tag_chain.py"
BASE_TS = 1_700_000_000.0


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_jargon_tag_chain", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE scoped_jargon (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            word TEXT NOT NULL,
            status TEXT NOT NULL,
            source_memory_id INTEGER,
            provenance TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        );
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            content TEXT NOT NULL DEFAULT '',
            resolution_state TEXT,
            quarantine INTEGER DEFAULT 0
        );
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            name TEXT NOT NULL,
            tag_type TEXT NOT NULL DEFAULT 'keyword',
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            relevance REAL NOT NULL DEFAULT 1.0
        );
        """
    )
    return conn


def _add_jargon(
    conn,
    *,
    word: str = "老吴",
    status: str = "confirmed",
    memory_id: int | None = 1,
    provenance: dict | str | None = None,
    updated_at: float = BASE_TS,
) -> int:
    stored = provenance if isinstance(provenance, str) else json.dumps(
        provenance if provenance is not None else {"tag_chain_status": "missing", "source_tags": []},
        ensure_ascii=False,
    )
    cursor = conn.execute(
        "INSERT INTO scoped_jargon(bot_id,session_id,visibility,word,status,"
        "source_memory_id,provenance,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("yushu", "qq:group:g1", "group", word, status, memory_id, stored, updated_at),
    )
    return int(cursor.lastrowid)


def _add_memory(
    conn,
    *,
    memory_id: int = 1,
    resolution_state: str = "resolved",
    quarantine: int = 0,
) -> int:
    conn.execute(
        "INSERT INTO memories(id,bot_id,session_id,visibility,content,"
        "resolution_state,quarantine) VALUES (?,?,?,?,?,?,?)",
        (memory_id, "yushu", "qq:group:g1", "group", "老吴今天又来了", resolution_state, quarantine),
    )
    return memory_id


def _add_tag(conn, *, tag_id: int, name: str, status: str = "active") -> int:
    conn.execute(
        "INSERT INTO scoped_tags(id,bot_id,session_id,visibility,name,tag_type,status) "
        "VALUES (?,?,?,?,?,?,?)",
        (tag_id, "yushu", "qq:group:g1", "group", name, "keyword", status),
    )
    return tag_id


def _link(conn, memory_id: int, tag_id: int, *, position: int = 0) -> None:
    conn.execute(
        "INSERT INTO scoped_memory_tags(bot_id,session_id,visibility,memory_id,tag_id,position) "
        "VALUES (?,?,?,?,?,?)",
        ("yushu", "qq:group:g1", "group", memory_id, tag_id, position),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_scan_finds_confirmed_rows_and_skips_pending_or_linked(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    _add_memory(conn)
    _add_tag(conn, tag_id=1, name="群内玩梗")
    _add_tag(conn, tag_id=2, name="停用标签", status="archived")
    _link(conn, 1, 1, position=1)
    _link(conn, 1, 2, position=0)
    target = _add_jargon(conn)
    pending = _add_jargon(conn, word="候选", status="pending")
    already = _add_jargon(
        conn, word="已回填", provenance={"tag_chain_status": "complete", "source_tags": [{"tag_id": 1}]}
    )
    conn.commit()
    conn.close()

    report = script.scan(db)

    assert report.ok, report.error
    assert report.scanned == 2
    assert [row["jargon_id"] for row in report.rows] == [target]
    row = report.rows[0]
    assert row["status"] == "complete"
    assert [tag["name"] for tag in row["tags"]] == ["群内玩梗"]
    assert pending not in {row["jargon_id"] for row in report.rows}
    assert already not in {row["jargon_id"] for row in report.rows}


def test_scan_marks_empty_and_skips_unhealthy_sources(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    _add_memory(conn)
    _add_memory(conn, memory_id=2, quarantine=1)
    empty = _add_jargon(conn)
    quarantined = _add_jargon(conn, word="隔离", memory_id=2)
    missing = _add_jargon(conn, word="缺失", memory_id=99)
    conn.commit()
    conn.close()

    report = script.scan(db)

    assert report.ok, report.error
    statuses = {row["jargon_id"]: row["status"] for row in report.rows}
    assert statuses[empty] == "empty"
    assert statuses[quarantined] == "unhealthy_memory"
    assert statuses[missing] == "missing_memory"
    assert [row["jargon_id"] for row in report.targets] == [empty]


def test_apply_requires_yes_and_leaves_database_untouched(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    _add_memory(conn)
    _add_tag(conn, tag_id=1, name="群内玩梗")
    _link(conn, 1, 1)
    _add_jargon(conn)
    conn.commit()
    conn.close()
    before = _digest(db)

    with pytest.raises(SystemExit) as exc:
        script.main(["--db", str(db), "--apply"])
    assert exc.value.code == 2
    assert _digest(db) == before


def test_apply_writes_tags_and_undo_restores_provenance(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    _add_memory(conn)
    _add_tag(conn, tag_id=1, name="群内玩梗")
    _link(conn, 1, 1)
    original = {"tag_chain_status": "missing", "source_tags": [], "producer": "jargon_mining"}
    jargon_id = _add_jargon(conn, provenance=original)
    conn.commit()
    conn.close()

    outcome = script.apply_backfill(db)

    assert outcome.ok, outcome.error
    assert outcome.planned == 1
    assert outcome.applied == [
        {
            "jargon_id": jargon_id,
            "word": "老吴",
            "source_memory_id": 1,
            "tag_count": 1,
            "tag_chain_status": "complete",
        }
    ]
    assert outcome.undo_log and Path(outcome.undo_log).is_file()

    conn = sqlite3.connect(db)
    try:
        provenance, updated_at = conn.execute(
            "SELECT provenance, updated_at FROM scoped_jargon WHERE id=?", (jargon_id,)
        ).fetchone()
        payload = json.loads(provenance)
        assert payload["producer"] == "jargon_mining"
        assert payload["tag_chain_status"] == "complete"
        assert payload["source_tags"][0]["name"] == "群内玩梗"
        assert payload["source_tags"][0]["memory_id"] == 1
        assert updated_at > BASE_TS

        conn.executescript(Path(outcome.undo_log).read_text(encoding="utf-8"))
        restored, restored_at = conn.execute(
            "SELECT provenance, updated_at FROM scoped_jargon WHERE id=?", (jargon_id,)
        ).fetchone()
        assert json.loads(restored) == original
        assert restored_at == BASE_TS
    finally:
        conn.close()


def test_apply_skips_when_tags_appeared_after_scan(tmp_path, script, monkeypatch):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    _add_memory(conn)
    jargon_id = _add_jargon(conn)
    conn.commit()
    conn.close()

    original = script.scan

    def scan_then_add_tag(*args, **kwargs):
        report = original(*args, **kwargs)
        changer = sqlite3.connect(db)
        try:
            changer.execute(
                "INSERT INTO scoped_tags(id,bot_id,session_id,visibility,name,status) "
                "VALUES (1,'yushu','qq:group:g1','group','新标签','active')"
            )
            changer.execute(
                "INSERT INTO scoped_memory_tags(bot_id,session_id,visibility,memory_id,tag_id,position) "
                "VALUES ('yushu','qq:group:g1','group',1,1,0)"
            )
            changer.commit()
        finally:
            changer.close()
        return report

    monkeypatch.setattr(script, "scan", scan_then_add_tag)
    outcome = script.apply_backfill(db)

    assert outcome.ok, outcome.error
    assert outcome.planned == 1
    assert outcome.applied == []
    assert outcome.skipped == [{"jargon_id": jargon_id, "reason": "tags_changed"}]

    conn = sqlite3.connect(db)
    try:
        provenance = json.loads(
            conn.execute("SELECT provenance FROM scoped_jargon WHERE id=?", (jargon_id,)).fetchone()[0]
        )
        assert provenance["tag_chain_status"] == "missing"
    finally:
        conn.close()
