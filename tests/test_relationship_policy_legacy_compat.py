from __future__ import annotations

import sqlite3

import pytest

from domain.relationship_policy import (
    cap_automatic_delta,
    compute_affinity,
    is_noisy_relationship_event,
    validate_event,
)
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_relationship_calibration import (
    ensure_scoped_relationship_calibration_schema,
)


def test_passing_noise_is_rejected_as_formal_event():
    assert is_noisy_relationship_event("message_seen", "看见一条群友消息") is True
    assert is_noisy_relationship_event("joke", "消息带来趣味感") is True
    assert is_noisy_relationship_event("direct_reply", "连续直接互动后更熟了") is False
    with pytest.raises(ValueError, match="invalid_relationship_event_type"):
        validate_event("message_seen", "familiarity", "看见一条群友消息", 0.05)
    with pytest.raises(ValueError, match="invalid_relationship_event_type"):
        validate_event("joke", "fun", "消息带来趣味感", 1.0)


def test_legacy_five_dimension_snapshot_recomputes_original_affinity():
    snapshot = {
        "familiarity": 100.0,
        "trust": 96.58,
        "fun": 22.05,
        "hostility": 7.72,
        "depth": 80.0,
    }

    assert compute_affinity(snapshot) == 74


def test_hostility_positive_event_keeps_legacy_single_event_cap():
    assert cap_automatic_delta(dimension="hostility", requested_delta=20, daily_total=0) == 8
    assert cap_automatic_delta(dimension="hostility", requested_delta=-20, daily_total=0) == -5


def test_existing_four_dimension_value_table_is_upgraded_without_losing_rows(tmp_path):
    path = tmp_path / "four-dimension-values.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE scoped_soul_relationship_values (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility = 'group'),
            subject_principal_id TEXT NOT NULL,
            dimension TEXT NOT NULL CHECK (dimension IN ('familiarity', 'trust', 'fun', 'depth')),
            automatic_value REAL NOT NULL,
            manual_adjustment REAL,
            manual_override REAL,
            effective_value REAL NOT NULL,
            relationship_revision INTEGER NOT NULL,
            evidence TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL,
            PRIMARY KEY (bot_id, session_id, visibility, subject_principal_id, dimension)
        );
        INSERT INTO scoped_soul_relationship_values(
            bot_id, session_id, visibility, subject_principal_id, dimension,
            automatic_value, manual_adjustment, manual_override, effective_value,
            relationship_revision, evidence, updated_at
        ) VALUES ('bot-a', 'qq:group:g1', 'group', 'qq:user:u1', 'trust', 12, 2, NULL, 14, 3, '[]', 10);
        """
    )
    connection.commit()
    connection.close()

    manager = ConnectionManager(str(path))
    try:
        ensure_scoped_relationship_calibration_schema(manager)
        row = manager.execute_read(
            """SELECT automatic_value, manual_adjustment, effective_value
                 FROM scoped_soul_relationship_values
                WHERE dimension='trust'"""
        ).fetchone()
        assert tuple(row) == (12.0, 2.0, 14.0)
        with manager.write_transaction() as tx:
            tx.execute(
                """INSERT INTO scoped_soul_relationship_values(
                       bot_id, session_id, visibility, subject_principal_id, dimension,
                       automatic_value, manual_adjustment, manual_override, effective_value,
                       relationship_revision, evidence, updated_at
                   ) VALUES ('bot-a', 'qq:group:g1', 'group', 'qq:user:u1', 'hostility', 8, NULL, NULL, 8, 4, '[]', 20)"""
            )
        assert manager.execute_read(
            "SELECT COUNT(*) FROM scoped_soul_relationship_values WHERE dimension='hostility'"
        ).fetchone()[0] == 1
    finally:
        manager.close()
