from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from services.legacy_relationship_migration import (
    CONFIRMATION,
    LegacyRelationshipMigrationError,
    compute_legacy_affection,
    preview,
    stage,
)


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _scope(group_id: str = "g1", bot_id: str = "bot-a") -> dict[str, str]:
    return {
        "bot_id": bot_id,
        "session_id": f"qq:group:{group_id}",
        "visibility": "group",
        "group_id": group_id,
    }


def _schema(path: Path, *, values: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE user_profiles (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            affection INTEGER NOT NULL,
            metadata TEXT NOT NULL,
            last_seen REAL
        );
        CREATE TABLE relationship_events (
            id INTEGER PRIMARY KEY,
            bot_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            dimension TEXT NOT NULL,
            delta REAL NOT NULL,
            reason TEXT NOT NULL,
            source_episode_id INTEGER,
            source_memory_id INTEGER,
            created_at REAL
        );
        CREATE TABLE scoped_soul_relationships (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            subject_principal_id TEXT NOT NULL,
            affinity INTEGER NOT NULL,
            state TEXT,
            dimensions TEXT NOT NULL,
            revision INTEGER,
            evidence TEXT,
            updated_at REAL,
            UNIQUE(bot_id, session_id, visibility, subject_principal_id)
        );
        CREATE TABLE scoped_soul_relationship_events (
            id INTEGER PRIMARY KEY,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            subject_principal_id TEXT,
            dimension TEXT,
            delta REAL
        );
        CREATE TABLE scoped_soul_revisions (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            component TEXT NOT NULL,
            subject_principal_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(bot_id, session_id, visibility, component, subject_principal_id)
        );
        """
    )
    if values:
        conn.executescript(
            """
            CREATE TABLE scoped_soul_relationship_values (
                bot_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                visibility TEXT NOT NULL,
                subject_principal_id TEXT NOT NULL,
                dimension TEXT NOT NULL CHECK(dimension IN ('familiarity','trust','fun','hostility','depth')),
                automatic_value REAL NOT NULL,
                manual_adjustment REAL,
                manual_override REAL,
                effective_value REAL,
                relationship_revision INTEGER,
                evidence TEXT,
                updated_at REAL,
                UNIQUE(bot_id, session_id, visibility, subject_principal_id, dimension)
            );
            """
        )
    conn.close()


def _seed(path: Path, *, group_id: str = "g1", bot_id: str = "bot-a", user_id: str = "u1") -> dict[str, float]:
    dimensions = {"familiarity": 40.0, "trust": 50.0, "fun": 20.0, "hostility": 10.0, "depth": 30.0}
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO user_profiles VALUES (1,?,?,?,?,?,?)",
        (user_id, group_id, bot_id, compute_legacy_affection(dimensions), json.dumps({"dimensions": dimensions}), 100.0),
    )
    conn.execute(
        """INSERT INTO relationship_events
           VALUES (10,?,?,?,?,?,?,?,?,?,?)""",
        (bot_id, group_id, user_id, "gift_or_feed", "fun", 3.0, "legacy evidence", 8, 9, 101.0),
    )
    conn.commit()
    conn.close()
    return dimensions


def test_legacy_formula_matches_weights_truncation_and_clamp():
    values = {"familiarity": 40, "trust": 50, "fun": 20, "hostility": 10, "depth": 30}
    assert compute_legacy_affection(values) == 31
    assert compute_legacy_affection({"familiarity": 1000, "trust": 1000, "fun": 0, "hostility": 0, "depth": 0}) == 100
    assert compute_legacy_affection({"familiarity": 0, "trust": 0, "fun": 0, "hostility": 1000, "depth": 0}) == -100


def test_preview_exact_mapping_and_unmapped_or_incomplete_profiles(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    _schema(source)
    dimensions = _seed(source)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO user_profiles VALUES (2,'u2','unknown','bot-a',10,?,NULL)",
        (json.dumps({"dimensions": dimensions}),),
    )
    conn.execute("INSERT INTO user_profiles VALUES (3,'u3','g1','bot-a',0,'{}',NULL)")
    conn.commit()
    report = preview(conn, [_scope()])
    conn.close()

    outcomes = {item["legacy_id"]: (item["disposition"], item.get("reason")) for item in report["profiles"]}
    assert outcomes["1"] == ("migrate", None)
    assert outcomes["2"] == ("review", "target_scope_missing")
    assert outcomes["3"] == ("review", "legacy_five_dimensions_incomplete")
    assert report["events"][0]["disposition"] == "audit"


def test_preview_keeps_formal_shallow_relationship_instead_of_inflating_legacy(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    _schema(source)
    _seed(source)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "bot-a",
            "qq:group:g1",
            "group",
            "qq:user:u1",
            7,
            "neutral",
            json.dumps({"familiarity": 15, "trust": 0.9, "fun": 0, "hostility": 0, "depth": 15}),
            3,
            "[]",
            1.0,
        ),
    )
    conn.commit()
    report = preview(conn, [_scope()])
    conn.close()

    item = next(row for row in report["profiles"] if row.get("legacy_id") == "1")
    assert item["disposition"] == "keep_formal"
    assert item["reason"] == "formal_shallow_relationship_preferred"
    assert report["summary"]["profiles_keep_formal"] == 1
    assert report["events"][0]["disposition"] == "audit"


def test_stage_preserves_source_writes_five_dimension_baseline_and_audits_events(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "staged.sqlite3"
    _schema(source)
    dimensions = _seed(source)
    before = source.read_bytes()

    report = stage(source, output, tmp_path / "runs", [_scope()], _hash(source), CONFIRMATION)

    assert source.read_bytes() == before
    assert report["quick_check"] == "ok"
    assert report.get("mode", "full") == "full"
    assert report["legacy_table_state"]["user_profiles"]["count"] == 1
    assert report["legacy_table_state"]["relationship_events"]["count"] == 1
    assert report["legacy_table_state"]["user_profiles"]["sha256"].startswith("sha256:")
    assert Path(report["source_backup_path"]).exists()
    assert Path(report["report_path"]).is_file()
    conn = sqlite3.connect(output)
    relationship = conn.execute("SELECT affinity, dimensions FROM scoped_soul_relationships").fetchone()
    assert relationship[0] == 31
    assert json.loads(relationship[1]) == dimensions
    values = dict(conn.execute("SELECT dimension, automatic_value FROM scoped_soul_relationship_values").fetchall())
    assert values == dimensions
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_legacy_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_events").fetchone()[0] == 0
    assert conn.execute(
        "SELECT revision FROM scoped_soul_revisions WHERE component='relationship' AND subject_principal_id='qq:user:u1'"
    ).fetchone()[0] == 1
    item = conn.execute(
        "SELECT original_formal_json, original_formal_hash FROM legacy_relationship_migration_items WHERE source_table='user_profiles'"
    ).fetchone()
    assert item == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM relationship_events").fetchone()[0] == 1
    conn.close()


def test_event_audit_only_does_not_change_formal_affinity(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "staged-event-only.sqlite3"
    _schema(source)
    _seed(source)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "bot-a",
            "qq:group:g1",
            "group",
            "qq:user:u1",
            12,
            "neutral",
            json.dumps({"familiarity": 1, "trust": 0, "fun": 0, "hostility": 0, "depth": 0}),
            2,
            "[]",
            1.0,
        ),
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationship_values
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("bot-a", "qq:group:g1", "group", "qq:user:u1", "familiarity", 1, None, None, 1, 2, "[]", 1.0),
    )
    conn.commit()
    conn.close()
    before = source.read_bytes()

    report = stage(
        source,
        output,
        tmp_path / "runs-event-only",
        [_scope()],
        _hash(source),
        CONFIRMATION,
        mode="event_audit_only",
    )

    assert source.read_bytes() == before
    assert report["mode"] == "event_audit_only"
    assert report["profile_result"]["skipped"] is True
    assert report["event_result"]["audited"] == 1
    assert report["formal_fingerprint_before"] == report["formal_fingerprint_after"]
    conn = sqlite3.connect(output)
    assert conn.execute("SELECT affinity FROM scoped_soul_relationships").fetchone()[0] == 12
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_legacy_events").fetchone()[0] == 1
    # no new formal live events
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_events").fetchone()[0] == 0
    conn.close()


def test_manual_calibration_conflict_is_review_and_never_overwritten(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "staged.sqlite3"
    _schema(source)
    _seed(source)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bot-a", "qq:group:g1", "group", "qq:user:u1", 77, "friendly", "{}", 4, "[]", 1.0),
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationship_values
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("bot-a", "qq:group:g1", "group", "qq:user:u1", "trust", 1, None, 88, 88, 4, "[]", 1.0),
    )
    conn.commit()
    conn.close()

    report = stage(source, output, tmp_path / "runs", [_scope()], _hash(source), CONFIRMATION)
    assert report["profile_result"]["migrated"] == 0
    conn = sqlite3.connect(output)
    assert conn.execute("SELECT affinity FROM scoped_soul_relationships").fetchone()[0] == 77
    assert conn.execute("SELECT manual_override FROM scoped_soul_relationship_values").fetchone()[0] == 88
    reason = conn.execute(
        "SELECT reason FROM legacy_relationship_migration_items WHERE source_table='user_profiles'"
    ).fetchone()[0]
    assert reason == "manual_relationship_calibration_conflict"
    conn.close()


def test_existing_unadjusted_formal_state_is_preserved_as_live_overlay(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "staged.sqlite3"
    _schema(source)
    _seed(source)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bot-a", "qq:group:g1", "group", "qq:user:u1", 2, "neutral", '{"trust":2}', 3, "[]", 1.0),
    )
    conn.commit()
    preview_connection = sqlite3.connect(source)
    preview_report = preview(preview_connection, [_scope()])
    preview_connection.close()
    assert preview_report["profiles"][0]["disposition"] == "keep_formal"
    conn.close()

    report = stage(source, output, tmp_path / "runs", [_scope()], _hash(source), CONFIRMATION)
    assert report["profile_result"]["already_migrated"] == 1
    conn = sqlite3.connect(output)
    affinity, dimensions_json, revision = conn.execute(
        "SELECT affinity, dimensions, revision FROM scoped_soul_relationships"
    ).fetchone()
    assert affinity == 2
    assert json.loads(dimensions_json) == {"trust": 2}
    assert revision == 3
    conn.close()


def test_existing_live_overlay_clamps_after_legacy_baseline(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "staged.sqlite3"
    _schema(source)
    dimensions = _seed(source)
    dimensions["familiarity"] = 100.0
    conn = sqlite3.connect(source)
    conn.execute(
        "UPDATE user_profiles SET affection=?, metadata=?",
        (compute_legacy_affection(dimensions), json.dumps({"dimensions": dimensions})),
    )
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bot-a", "qq:group:g1", "group", "qq:user:u1", 5, "neutral", '{\"familiarity\":5}', 3, "[]", 1.0),
    )
    conn.commit()
    conn.close()

    stage(source, output, tmp_path / "runs", [_scope()], _hash(source), CONFIRMATION)

    conn = sqlite3.connect(output)
    dimensions_json, evidence_json = conn.execute(
        "SELECT dimensions, evidence FROM scoped_soul_relationships"
    ).fetchone()
    assert json.loads(dimensions_json)["familiarity"] == 5
    conn.close()


def test_stage_is_idempotent_for_existing_audit_and_profile_items(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    _schema(source)
    _seed(source)
    stage(source, first, tmp_path / "run1", [_scope()], _hash(source), CONFIRMATION)
    second_report = stage(first, second, tmp_path / "run2", [_scope()], _hash(first), CONFIRMATION)

    assert second_report["profile_result"]["already_migrated"] == 1
    assert second_report["event_result"]["already_audited"] == 1
    conn = sqlite3.connect(second)
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_legacy_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationships").fetchone()[0] == 1
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()


def test_stage_rejects_in_place_or_wrong_confirmation(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    _schema(source)
    _seed(source)
    with pytest.raises(LegacyRelationshipMigrationError, match="source_and_output"):
        stage(source, source, tmp_path / "runs", [_scope()], _hash(source), CONFIRMATION)
    with pytest.raises(LegacyRelationshipMigrationError, match="confirmation"):
        stage(source, tmp_path / "out.sqlite3", tmp_path / "runs", [_scope()], _hash(source), "")
