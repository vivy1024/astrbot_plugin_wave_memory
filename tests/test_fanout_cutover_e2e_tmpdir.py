"""Temp-directory e2e: apply swap + rollback without touching production."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import scripts.fanout_cutover_apply as apply_mod
from scripts.fanout_cutover_apply import CONFIRMATION as APPLY_CONFIRMATION
from scripts.fanout_cutover_apply import apply_cutover
from scripts.fanout_cutover_rollback import CONFIRMATION as ROLLBACK_CONFIRMATION
from scripts.fanout_cutover_rollback import apply_rollback


def _write_db(path: Path, *, marked: bool, tag: str, formal: int = 2, audit: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        CREATE INDEX IF NOT EXISTS idx_legacy_rel_events_subject
            ON scoped_soul_relationship_legacy_events(
                bot_id, session_id, visibility, subject_principal_id, occurred_at DESC, id DESC
            );
        """
    )
    prov = json.dumps({"projection_kind": "fanout_duplicate"}) if marked else None
    vec = (b"\x00\x00\x00\x00") * 1024
    conn.execute(
        "INSERT INTO memories(id, content, provenance, group_id, vector, timestamp) VALUES (1,?,?,?,?,?)",
        (tag, prov, "100000001", vec, 10.0),
    )
    for i in range(formal):
        conn.execute(
            "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "yushu",
                "羽书:group:100000001",
                "group",
                f"羽书:user:{i}",
                3,
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


def test_apply_then_rollback_in_tmpdir(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "plugin_data" / "astrbot_plugin_wave_memory"
    prod = data_dir / "wave_memory.db"
    vac = tmp_path / "vac" / "wave_memory.fanout-cleanup-full.vacuumed.sqlite3"
    idx = tmp_path / "vac" / "indexes"
    idx.mkdir(parents=True)

    _write_db(prod, marked=True, tag="PROD_OLD", formal=2, audit=3)
    # simulate wal
    (data_dir / "wave_memory.db-wal").write_bytes(b"wal-old")
    (data_dir / "memory.hnsw.manifest.json").write_text("prod-index", encoding="utf-8")
    (data_dir / "memory.hnsw.g0001").write_bytes(b"prod-hnsw")

    _write_db(vac, marked=False, tag="VAC_CLEAN", formal=2, audit=3)
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
    (idx / "memory.hnsw.g00000000000000000001").write_bytes(b"vac-hnsw")

    def fake_preflight(**kwargs):
        return {
            "package_safe_for_cutover": True,
            "needs_refresh_before_cutover": False,
            "hard_gates": {"all": True},
            "prod_wal": {"wal_size": 0, "wal_exists": False},
            "drift": {},
            "assets": {},
            "phase2_promote_allowed": False,
        }

    def fake_accept(db, index_base, **kwargs):
        return {
            "passed": True,
            "hard_gates": {"ok": True},
            "checks": {
                "audit_rows": 3,
                "formal": 2,
                "marked": 0,
            },
        }

    monkeypatch.setattr(apply_mod, "preflight", fake_preflight)
    monkeypatch.setattr(apply_mod, "accept", fake_accept)

    # Apply cutover into temp "production"
    report = apply_cutover(
        prod=prod,
        vacuumed=vac,
        index_dir=idx,
        confirmation=APPLY_CONFIRMATION,
        writers_stopped=True,
        do_checkpoint=False,
        dry_run=False,
    )
    assert report["switched"] is True
    assert report["post_checks"]["ok"] is True
    assert report["post_checks"]["marked"] == 0
    assert report["post_checks"]["audit"] == 3
    assert report["post_checks"]["formal"] == 2

    # Production file now has cleaned content marker
    conn = sqlite3.connect(prod.as_posix())
    content = conn.execute("SELECT content FROM memories WHERE id=1").fetchone()[0]
    marked = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE provenance LIKE '%fanout_duplicate%'"
    ).fetchone()[0]
    conn.close()
    assert content == "VAC_CLEAN"
    assert marked == 0
    assert (data_dir / "memory.hnsw.manifest.json").read_text(encoding="utf-8")
    assert "prod-index" not in (data_dir / "memory.hnsw.manifest.json").read_text(encoding="utf-8")

    pre_db = Path(report["rollback"]["db"])
    pre_idx = Path(report["rollback"]["index_dir"])
    assert pre_db.is_file()
    assert pre_idx.is_dir()

    # Rollback to pre-cutover
    rb = apply_rollback(
        prod_db=prod,
        pre_cutover_db=pre_db,
        pre_cutover_index_dir=pre_idx,
        confirmation=ROLLBACK_CONFIRMATION,
        writers_stopped=True,
        dry_run=False,
    )
    assert rb["ok"] is True
    assert rb["switched"] is True

    conn = sqlite3.connect(prod.as_posix())
    content = conn.execute("SELECT content FROM memories WHERE id=1").fetchone()[0]
    marked = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE provenance LIKE '%fanout_duplicate%'"
    ).fetchone()[0]
    conn.close()
    assert content == "PROD_OLD"
    assert marked == 1
    assert (data_dir / "memory.hnsw.manifest.json").read_text(encoding="utf-8") == "prod-index"
