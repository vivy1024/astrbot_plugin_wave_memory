"""Evidence summary dry-run: machine evidence + audit → summary, no affinity write."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.relationship_evidence_summary_dryrun import (
    _machine_evidence,
    apply_summaries,
    plan,
)


def test_machine_evidence_detection():
    assert _machine_evidence(None)
    assert _machine_evidence("[]")
    assert _machine_evidence(json.dumps([{"relationship_event_id": 1, "dimension": "familiarity"}]))
    assert not _machine_evidence(json.dumps([{"summary": "叙事证据"}]))
    assert not _machine_evidence("not-json")


def test_plan_proposes_summary_without_affinity_change(tmp_path: Path):
    db = tmp_path / "r.db"
    conn = sqlite3.connect(db.as_posix())
    conn.executescript(
        """
        CREATE TABLE scoped_soul_relationships(
            bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, affinity INTEGER, state TEXT,
            dimensions TEXT, revision INTEGER, evidence TEXT, updated_at REAL
        );
        CREATE TABLE scoped_soul_relationship_legacy_events(
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, event_type TEXT, reason TEXT, occurred_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "yushu",
            "羽书:group:100000001",
            "group",
            "羽书:user:1",
            12,
            "friendly",
            "{}",
            1,
            json.dumps([{"relationship_event_id": 9}]),
            1.0,
        ),
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO scoped_soul_relationship_legacy_events VALUES (?,?,?,?,?,?,?,?)",
            (
                i + 1,
                "yushu",
                "羽书:group:100000001",
                "group",
                "羽书:user:1",
                "direct_reply",
                "看见一条群友消息",
                float(i),
            ),
        )
    conn.commit()
    report = plan(conn, session_id="羽书:group:100000001", limit=10)
    conn.close()
    assert report["writes_affinity"] is False
    assert report["summary_candidates"] == 1
    c = report["sample"][0]
    assert c["affinity"] == 12
    assert c["proposed_evidence_append"]["affects_affinity"] is False
    assert "历史审计事件 3 条" in c["proposed_evidence_append"]["summary"]


def test_apply_summaries_updates_evidence_keeps_affinity(tmp_path: Path):
    db = tmp_path / "apply.db"
    conn = sqlite3.connect(db.as_posix())
    conn.executescript(
        """
        CREATE TABLE scoped_soul_relationships(
            bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, affinity INTEGER, state TEXT,
            dimensions TEXT, revision INTEGER, evidence TEXT, updated_at REAL
        );
        CREATE TABLE scoped_soul_relationship_legacy_events(
            id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject_principal_id TEXT, event_type TEXT, reason TEXT, occurred_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO scoped_soul_relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "yushu",
            "羽书:group:100000001",
            "group",
            "羽书:user:1",
            12,
            "friendly",
            "{}",
            1,
            json.dumps([{"relationship_event_id": 9}]),
            1.0,
        ),
    )
    conn.execute(
        "INSERT INTO scoped_soul_relationship_legacy_events VALUES (1,?,?,?,?,?,?,?)",
        (
            "yushu",
            "羽书:group:100000001",
            "group",
            "羽书:user:1",
            "direct_reply",
            "看见一条群友消息",
            1.0,
        ),
    )
    conn.commit()
    report = plan(conn, session_id="羽书:group:100000001", limit=10)
    conn.close()
    result = apply_summaries(
        db,
        report["candidates"],
        bot_id="yushu",
        session_id="羽书:group:100000001",
        limit=10,
    )
    assert result["updated"] == 1
    assert result["affinity_mismatch"] == 0
    assert result["writes_affinity"] is False
    conn = sqlite3.connect(db.as_posix())
    aff, evidence = conn.execute(
        "SELECT affinity, evidence FROM scoped_soul_relationships WHERE subject_principal_id=?",
        ("羽书:user:1",),
    ).fetchone()
    conn.close()
    assert aff == 12
    payload = json.loads(evidence)
    assert any(x.get("kind") == "historical_audit_summary" for x in payload)
    # idempotent second apply
    result2 = apply_summaries(
        db,
        report["candidates"],
        bot_id="yushu",
        session_id="羽书:group:100000001",
    )
    assert result2["updated"] == 0
    assert result2["skipped"] >= 1
