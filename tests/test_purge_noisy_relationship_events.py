from __future__ import annotations

import json
import sqlite3

from scripts.purge_noisy_relationship_events import plan_purge, run


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE scoped_soul_revisions (
            bot_id TEXT, session_id TEXT, visibility TEXT, component TEXT,
            subject_principal_id TEXT, revision INTEGER, updated_at REAL,
            PRIMARY KEY (bot_id, session_id, visibility, component, subject_principal_id)
        );
        CREATE TABLE scoped_soul_relationships (
            bot_id TEXT, session_id TEXT, visibility TEXT, subject_principal_id TEXT,
            affinity INTEGER, state TEXT, dimensions TEXT, revision INTEGER,
            evidence TEXT, updated_at REAL,
            PRIMARY KEY (bot_id, session_id, visibility, subject_principal_id)
        );
        CREATE TABLE scoped_soul_relationship_values (
            bot_id TEXT, session_id TEXT, visibility TEXT, subject_principal_id TEXT,
            dimension TEXT, automatic_value REAL, manual_adjustment REAL, manual_override REAL,
            effective_value REAL, relationship_revision INTEGER, evidence TEXT, updated_at REAL,
            PRIMARY KEY (bot_id, session_id, visibility, subject_principal_id, dimension)
        );
        CREATE TABLE scoped_soul_relationship_events (
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, event_type TEXT, dimension TEXT, delta REAL,
            reason TEXT, created_at REAL
        );
        CREATE TABLE relationship_events (
            id INTEGER PRIMARY KEY, bot_id TEXT, group_id TEXT, user_id TEXT,
            event_type TEXT, dimension TEXT, delta REAL, reason TEXT, created_at REAL
        );
        CREATE TABLE user_profiles (
            user_id TEXT, group_id TEXT, bot_id TEXT, affection INTEGER, metadata TEXT,
            PRIMARY KEY (user_id, group_id, bot_id)
        );
        """
    )


def test_purge_keeps_real_events_and_drops_passing_noise(tmp_path):
    db = tmp_path / "wave.db"
    conn = sqlite3.connect(db)
    _schema(conn)
    conn.execute(
        """INSERT INTO scoped_soul_relationships
           VALUES ('yushu','羽书:group:1','group','羽书:user:1', 1, 'neutral',
                   '{"familiarity":0.6}', 1, '[]', 1)"""
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationships
           VALUES ('yushu','羽书:group:1','group','羽书:user:2', 2, 'neutral',
                   '{"familiarity":3.0,"trust":1.5}', 2, '[]', 1)"""
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationship_values
           VALUES ('yushu','羽书:group:1','group','羽书:user:1','familiarity',0.6,NULL,NULL,0.6,1,'[]',1)"""
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationship_values
           VALUES ('yushu','羽书:group:1','group','羽书:user:2','familiarity',1.5,NULL,NULL,1.5,2,'[]',1)"""
    )
    conn.execute(
        """INSERT INTO scoped_soul_relationship_values
           VALUES ('yushu','羽书:group:1','group','羽书:user:2','trust',1.5,NULL,NULL,1.5,2,'[]',1)"""
    )
    conn.executemany(
        """INSERT INTO scoped_soul_relationship_events
           (bot_id, session_id, visibility, subject_principal_id, event_type, dimension, delta, reason, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("yushu", "羽书:group:1", "group", "羽书:user:1", "message_seen", "familiarity", 0.05, "看见一条群友消息", 10),
            ("yushu", "羽书:group:1", "group", "羽书:user:1", "message_seen", "familiarity", 0.05, "看见一条群友消息", 11),
            ("yushu", "羽书:group:1", "group", "羽书:user:2", "message_seen", "familiarity", 0.05, "看见一条群友消息", 12),
            ("yushu", "羽书:group:1", "group", "羽书:user:2", "direct_reply", "trust", 1.5, "连续直接互动后更熟了", 13),
            ("yushu", "羽书:group:1", "group", "羽书:user:2", "joke", "fun", 1.0, "消息带来趣味感", 14),
        ],
    )
    conn.execute(
        """INSERT INTO relationship_events
           (bot_id, group_id, user_id, event_type, dimension, delta, reason, created_at)
           VALUES ('yushu','1','1','message_seen','familiarity',0.05,'看见一条群友消息',10)"""
    )
    conn.execute(
        """INSERT INTO user_profiles VALUES ('1','1','yushu',0,'{"dimensions":{"familiarity":0.6}}')"""
    )
    conn.commit()

    planned = plan_purge(conn)
    assert planned["scoped_noise_events"] == 4
    assert planned["legacy_noise_events"] == 1
    assert planned["subjects_deleted"] == 1
    assert planned["subjects_rebuilt"] == 1
    conn.close()

    report = run(db, apply=True, now=99.0)
    assert report["applied"]["deleted_scoped_events"] == 4
    assert report["applied"]["subjects_deleted"] == 1
    assert report["applied"]["subjects_rebuilt"] == 1

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationship_events").fetchone()[0] == 1
    remaining = conn.execute("SELECT event_type, reason FROM scoped_soul_relationship_events").fetchone()
    assert remaining == ("direct_reply", "连续直接互动后更熟了")
    assert conn.execute("SELECT COUNT(*) FROM scoped_soul_relationships").fetchone()[0] == 1
    dims = json.loads(conn.execute("SELECT dimensions FROM scoped_soul_relationships").fetchone()[0])
    assert dims["trust"] == 1.5
    assert "fun" not in dims
    meta = json.loads(conn.execute("SELECT metadata FROM user_profiles").fetchone()[0])
    assert meta["dimensions"]["familiarity"] == 0.5
    conn.close()
