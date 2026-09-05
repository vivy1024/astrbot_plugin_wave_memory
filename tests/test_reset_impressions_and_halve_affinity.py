from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from domain.relationship_policy import clamp_dimension, compute_affinity
from engine.db.migrations.scoped_relationship_calibration import (
    ensure_scoped_relationship_calibration_schema_connection,
)
from engine.db.migrations.scoped_soul import _SCOPED_SOUL_SCHEMA
from scripts.reset_impressions_and_halve_affinity import (
    BATCH_ID,
    CONFIRMATION,
    main,
    run,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE user_profiles (
               user_id TEXT,
               group_id TEXT,
               bot_id TEXT,
               metadata TEXT,
               PRIMARY KEY (user_id, group_id, bot_id)
           )"""
    )
    for statement in (part.strip() for part in _SCOPED_SOUL_SCHEMA.split(";") if part.strip()):
        conn.execute(statement)
    ensure_scoped_relationship_calibration_schema_connection(conn)
    conn.commit()


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO user_profiles VALUES ('u1', 'g1', 'bot-a', ?)",
        (json.dumps({"impression": "旧印象", "tags": {"geek": 1}}, ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO user_profiles VALUES ('u2', 'g1', 'bot-a', ?)",
        (json.dumps({"impression": ""}, ensure_ascii=False),),
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationships(
               bot_id, session_id, visibility, subject_principal_id, affinity, state,
               dimensions, revision, evidence, updated_at)
           VALUES ('bot-a', 'qq:group:g1', 'group', 'qq:user:u1', 40, 'friendly', ?, 3, '[]', 1000)""",
        (json.dumps({"familiarity": 40, "trust": 60, "fun": 20, "hostility": 10, "depth": 40}),),
    )
    values = {
        "familiarity": (40.0, None, None, 40.0),
        "trust": (50.0, 10.0, None, 60.0),
        "fun": (20.0, None, None, 20.0),
        "hostility": (10.0, None, None, 10.0),
        "depth": (20.0, None, 40.0, 40.0),
    }
    for dimension, (automatic, adjustment, override, effective) in values.items():
        conn.execute(
            """INSERT INTO scoped_soul_relationship_values(
                   bot_id, session_id, visibility, subject_principal_id, dimension,
                   automatic_value, manual_adjustment, manual_override, effective_value,
                   relationship_revision, evidence, updated_at)
               VALUES ('bot-a', 'qq:group:g1', 'group', 'qq:user:u1', ?, ?, ?, ?, ?, 3, '[]', 1000)""",
            (dimension, automatic, adjustment, override, effective),
        )
    conn.execute(
        """INSERT INTO scoped_soul_revisions(
               bot_id, session_id, visibility, component, subject_principal_id, revision, updated_at)
           VALUES ('bot-a', 'qq:group:g1', 'group', 'relationship', 'qq:user:u1', 3, 1000)"""
    )
    conn.commit()


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "copy.db"
    conn = sqlite3.connect(path)
    try:
        _create_schema(conn)
        _seed(conn)
    finally:
        conn.close()
    return path


def test_dry_run_does_not_write(tmp_path):
    path = _db(tmp_path)
    report = run(path, apply=False, now=2000.0)
    assert report["mode"] == "dry-run"
    assert report["impressions"]["clear_count"] == 1
    assert report["relationships"]["count"] == 0
    assert report["halve_affinity"] is False
    conn = sqlite3.connect(path)
    try:
        metadata = json.loads(conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()[0])
        affinity = conn.execute("SELECT affinity, revision FROM scoped_soul_relationships").fetchone()
        assert metadata["impression"] == "旧印象"
        assert affinity == (40, 3)
        assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_calibration_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_clears_impression_without_halving_affinity(tmp_path):
    path = _db(tmp_path)
    report = run(path, apply=True, now=2000.0)
    assert report["mode"] == "apply"
    assert report["applied_impressions"] == 1
    assert report["applied_relationships"] == 0
    assert report["applied_dimensions"] == 0
    assert Path(report["backup_path"]).exists()
    conn = sqlite3.connect(path)
    try:
        metadata = json.loads(conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()[0])
        affinity, revision = conn.execute("SELECT affinity, revision FROM scoped_soul_relationships").fetchone()
        assert metadata["impression"] == ""
        assert metadata["impression_history"][-1]["text"] == "旧印象"
        assert affinity == 40
        assert revision == 3
        assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_calibration_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_clears_impression_and_halves_effective_values(tmp_path):
    path = _db(tmp_path)
    report = run(path, apply=True, now=2000.0, halve_affinity=True)
    assert report["mode"] == "apply"
    assert report["applied_impressions"] == 1
    assert report["applied_relationships"] == 1
    assert report["applied_dimensions"] == 5
    assert Path(report["backup_path"]).exists()

    expected = {
        "familiarity": clamp_dimension("familiarity", 40 * 0.5),
        "trust": clamp_dimension("trust", 60 * 0.5),
        "fun": clamp_dimension("fun", 20 * 0.5),
        "hostility": clamp_dimension("hostility", 10 * 0.5),
        "depth": clamp_dimension("depth", 40 * 0.5),
    }
    conn = sqlite3.connect(path)
    try:
        metadata = json.loads(conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()[0])
        assert metadata["impression"] == ""
        assert metadata["tags"] == {"geek": 1}
        assert metadata["impression_history"][-1]["text"] == "旧印象"
        assert metadata["impression_history"][-1]["actor"] == "maintenance"
        empty = json.loads(conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u2'").fetchone()[0])
        assert empty == {"impression": ""}

        rows = {
            str(row[0]): row[1:]
            for row in conn.execute(
                """SELECT dimension, automatic_value, manual_adjustment, manual_override, effective_value, relationship_revision
                     FROM scoped_soul_relationship_values"""
            )
        }
        assert rows["trust"][0] == 50
        assert rows["trust"][1] == 10
        assert rows["depth"][2] == expected["depth"]
        for dimension, value in expected.items():
            assert rows[dimension][2] == value
            assert rows[dimension][3] == value
            assert rows[dimension][4] == 4

        affinity, state, dimensions, revision = conn.execute(
            "SELECT affinity, state, dimensions, revision FROM scoped_soul_relationships"
        ).fetchone()
        loaded = json.loads(dimensions)
        assert loaded == expected
        assert affinity == compute_affinity(expected)
        assert revision == 4
        assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_calibration_events").fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM scoped_soul_timeline WHERE event_type='relationship.manual_calibration'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT revision FROM scoped_soul_revisions WHERE component='relationship'"
        ).fetchone()[0] == 4
        evidence = json.loads(conn.execute("SELECT evidence FROM scoped_soul_relationships").fetchone()[0])
        assert evidence[0]["kind"] == "maintenance_batch"
        assert evidence[0]["id"] == BATCH_ID
    finally:
        conn.close()


def test_production_apply_requires_confirmation(tmp_path, capsys):
    prod = tmp_path / "plugin_data" / "wave_memory.db"
    prod.parent.mkdir()
    src = _db(tmp_path)
    prod.write_bytes(src.read_bytes())
    try:
        main(["--db", str(prod), "--apply"])
        raise AssertionError("production apply must fail closed")
    except SystemExit as exc:
        assert "allow-production" in str(exc)
    conn = sqlite3.connect(prod)
    try:
        assert conn.execute("SELECT affinity FROM scoped_soul_relationships").fetchone()[0] == 40
    finally:
        conn.close()
    assert CONFIRMATION
