from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.fanout_cutover_apply import CONFIRMATION, CutoverError, apply_cutover, main


def _mini_db(path: Path, *, marked: int = 0, audit: int = 2, formal: int = 2) -> None:
    conn = sqlite3.connect(path.as_posix())
    conn.executescript(
        """
        CREATE TABLE memories(
            id INTEGER PRIMARY KEY,
            content TEXT,
            provenance TEXT,
            group_id TEXT,
            vector BLOB,
            timestamp REAL
        );
        CREATE TABLE scoped_soul_relationships(
            bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, affinity INTEGER, state TEXT,
            dimensions TEXT, revision INTEGER, evidence TEXT, updated_at REAL
        );
        CREATE TABLE scoped_soul_relationship_legacy_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_event_id TEXT, scope_key TEXT, bot_id TEXT, session_id TEXT,
            visibility TEXT, group_id TEXT, subject_principal_id TEXT,
            event_type TEXT, dimension TEXT, delta REAL, reason TEXT,
            occurred_at REAL, source_episode_id INTEGER, source_memory_id INTEGER,
            source_hash TEXT, event_hash TEXT, run_id TEXT, created_at REAL
        );
        CREATE TABLE scope_recovery_memory_map(
            legacy_memory_id INTEGER, target_scope_key TEXT, target_memory_id INTEGER,
            origin_key TEXT, run_id TEXT
        );
        """
    )
    # one clean memory with fake vector (1024 float32 zeros)
    vec = (b"\x00\x00\x00\x00") * 1024
    prov = None if marked == 0 else json.dumps({"projection_kind": "fanout_duplicate"})
    conn.execute(
        "INSERT INTO memories(id, content, provenance, group_id, vector, timestamp) VALUES (1,?,?,?,?,?)",
        ("hello", prov, "100000001", vec, 1.0),
    )
    for i in range(formal):
        conn.execute(
            "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "yushu",
                "羽书:group:100000001",
                "group",
                f"羽书:user:{i}",
                1,
                "neutral",
                "{}",
                1,
                "[]",
                1.0,
            ),
        )
    for i in range(audit):
        conn.execute(
            """INSERT INTO scoped_soul_relationship_legacy_events(
                legacy_event_id, scope_key, bot_id, session_id, visibility, group_id,
                subject_principal_id, event_type, dimension, delta, reason, occurred_at,
                source_episode_id, source_memory_id, source_hash, event_hash, run_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(i),
                "k",
                "yushu",
                "羽书:group:100000001",
                "group",
                "100000001",
                f"羽书:user:{i}",
                "direct_reply",
                "familiarity",
                0.5,
                "x",
                1.0,
                None,
                None,
                "h",
                f"e{i}",
                "r",
                1.0,
            ),
        )
    conn.commit()
    conn.close()


def test_apply_cutover_default_dry_run_does_not_switch(tmp_path: Path):
    prod = tmp_path / "plugin_data" / "astrbot_plugin_wave_memory" / "wave_memory.db"
    vac = tmp_path / "vac.sqlite3"
    idx = tmp_path / "indexes"
    prod.parent.mkdir(parents=True)
    idx.mkdir()
    _mini_db(prod, marked=5, audit=3, formal=2)
    _mini_db(vac, marked=0, audit=3, formal=2)
    # fake index assets
    (idx / "memory.hnsw.manifest.json").write_text(
        json.dumps(
            {
                "kind": "memory",
                "generation": 1,
                "dimension": 1024,
                "db_watermark": 0,
                "count": 1,
                "checksum": "x",
                "created_at": "t",
            }
        ),
        encoding="utf-8",
    )
    (idx / "memory.hnsw.g00000000000000000001").write_bytes(b"fake")

    # dry-run should not rename prod
    report = apply_cutover(
        prod=prod,
        vacuumed=vac,
        index_dir=idx,
        confirmation="",
        writers_stopped=False,
        do_checkpoint=False,
        dry_run=True,
    )
    assert report["switched"] is False
    assert prod.exists()
    assert report["mode"] == "dry-run-apply"


def test_apply_without_confirmation_fails(tmp_path: Path):
    prod = tmp_path / "wave_memory.db"
    vac = tmp_path / "vac.sqlite3"
    idx = tmp_path / "indexes"
    idx.mkdir()
    _mini_db(prod)
    _mini_db(vac, marked=0)
    (idx / "memory.hnsw.manifest.json").write_text("{}", encoding="utf-8")
    try:
        apply_cutover(
            prod=prod,
            vacuumed=vac,
            index_dir=idx,
            confirmation="wrong",
            writers_stopped=True,
            do_checkpoint=True,
            dry_run=False,
        )
        assert False, "expected CutoverError"
    except CutoverError as exc:
        assert "confirmation_required" in str(exc)


def test_cli_dry_run_exits_zero(tmp_path: Path, capsys):
    # CLI against missing files should still return structured output via preflight paths;
    # use real mini layout under tmp and monkeypatch defaults via argv.
    prod = tmp_path / "wave_memory.db"
    vac = tmp_path / "vac.sqlite3"
    idx = tmp_path / "indexes"
    idx.mkdir()
    _mini_db(prod, marked=1, audit=1, formal=1)
    _mini_db(vac, marked=0, audit=1, formal=1)
    (idx / "memory.hnsw.manifest.json").write_text(
        json.dumps(
            {
                "kind": "memory",
                "generation": 1,
                "dimension": 1024,
                "db_watermark": 0,
                "count": 1,
                "checksum": "x",
                "created_at": "t",
            }
        ),
        encoding="utf-8",
    )
    (idx / "memory.hnsw.g1").write_bytes(b"x")
    rc = main(
        [
            "--prod-db",
            str(prod),
            "--vacuumed-db",
            str(vac),
            "--index-dir",
            str(idx),
        ]
    )
    assert rc == 0
    assert CONFIRMATION == "cutover-fanout-cleaned-db"
