"""Unit test for readonly status verifier with tiny sqlite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.verify_phase2_production_readonly_status import verify


def test_verify_ok_without_grants_or_summary(tmp_path: Path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db.as_posix())
    conn.executescript(
        """
        CREATE TABLE memories(id INTEGER, provenance TEXT);
        CREATE TABLE scoped_soul_relationships(
            bot_id TEXT, session_id TEXT, visibility TEXT, evidence TEXT
        );
        CREATE TABLE scoped_soul_relationship_legacy_events(id INTEGER);
        INSERT INTO memories VALUES (1, NULL);
        INSERT INTO scoped_soul_relationships VALUES
          ('yushu','羽书:group:100000001','group','[]');
        INSERT INTO scoped_soul_relationship_legacy_events VALUES (1);
        """
    )
    conn.commit()
    conn.close()
    report = verify(db, vacuumed=None)
    assert report["ok"] is True
    assert report["prod"]["shared_memory_grants"] == -1
    assert report["prod"]["evidence_historical_summary_rows"] == 0
    assert report["invariants"]["phase2_promote_allowed"] is False
