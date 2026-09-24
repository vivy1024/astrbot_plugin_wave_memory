from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.scope_recovery import (
    SCOPE_RECOVERY_PREVIEW_KIND,
    SCOPE_RECOVERY_RULE_VERSION,
    ScopeRecoveryError,
    ScopeRecoveryPreviewJobs,
    build_recovery_request,
    plan_snapshot,
)
from services.migrations_archive.approved_scope_recovery import (
    ApprovedScopeRecoveryError,
    apply_approved_scope_recovery,
    build_approved_scope_recovery_plan,
    create_approved_scope_snapshot,
    verify_approved_scope_recovery,
    write_approved_scope_recovery_plan,
)
from services.migrations_archive.scope_recovery_migration import (
    ScopeRecoveryMigrationError,
    apply_classified_scope_recovery,
    apply_staged_migration,
)
from webui.blueprints import maintenance


_FORMAL_SCOPE = {
    "group_id": "g1",
    "bot_id": "bot-a",
    "session_id": "qq:group:g1",
    "visibility": "group",
}


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            content TEXT,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            resolution_state TEXT,
            quarantine INTEGER,
            source TEXT,
            sender_id TEXT,
            timestamp REAL
        );
        CREATE TABLE user_profiles (
            user_id TEXT,
            group_id TEXT,
            bot_id TEXT,
            affection INTEGER,
            interaction_count INTEGER,
            metadata TEXT
        );
        CREATE TABLE relationship_events (
            id INTEGER PRIMARY KEY,
            bot_id TEXT,
            group_id TEXT,
            user_id TEXT,
            event_type TEXT,
            dimension TEXT,
            delta REAL,
            reason TEXT,
            created_at REAL
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            source_memory_id INTEGER,
            confidence REAL,
            fact_type TEXT
        );
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE memory_tags (memory_id INTEGER, tag_id INTEGER);
        CREATE TABLE tag_relations (source_tag_id INTEGER, target_tag_id INTEGER);
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, word TEXT, meaning TEXT, group_id TEXT, bot_id TEXT, status TEXT);
        CREATE TABLE beliefs (id INTEGER PRIMARY KEY, content TEXT, group_id TEXT, bot_id TEXT, status TEXT);
        CREATE TABLE concerns (id INTEGER PRIMARY KEY, group_id TEXT, bot_id TEXT, topic TEXT, event_summary TEXT);
        CREATE TABLE bot_mood (id INTEGER PRIMARY KEY, group_id TEXT, mood_type TEXT, intensity REAL, description TEXT);
        CREATE TABLE book_communities (id INTEGER PRIMARY KEY, title TEXT, summary TEXT);
        """
    )
    connection.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "g1", "formal", "bot-a", "qq:group:g1", "group", "resolved", 0, "live", "u1", 1),
            (2, "g2", "explicit mapping", "", "", "", "", 0, "live", "u2", 2),
            (3, "g3", "ambiguous mapping", "", "", "", "", 0, "live", "u3", 3),
            (4, "g4", "unresolved", "", "", "", "unresolved_legacy", 1, "live", "u4", 4),
        ],
    )
    connection.execute("INSERT INTO user_profiles (user_id, group_id, bot_id, affection, interaction_count, metadata) VALUES ('u2', 'g2', 'bot-b', 12, 3, '{}')")
    connection.executemany(
        "INSERT INTO relationship_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(1, "bot-b", "g2", "u2", "positive", "trust", 2, "helpful", 10), (2, "", "g3", "u3", "negative", "trust", -1, "ambiguous", 11)],
    )
    connection.executemany(
        "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(1, "u2", "likes", "books", 2, 0.9, "FACTUAL"), (2, "u4", "likes", "noise", 4, 0.4, "FACTUAL")],
    )
    connection.executemany("INSERT INTO tags VALUES (?, ?)", [(1, "books"), (2, "noise")])
    connection.executemany("INSERT INTO memory_tags VALUES (?, ?)", [(2, 1), (4, 2)])
    connection.execute("INSERT INTO tag_relations VALUES (1, 2)")
    connection.executemany("INSERT INTO jargon VALUES (?, ?, ?, ?, ?, ?)", [(1, "册", "book", "g2", "bot-b", "approved"), (2, "歧义", "review", "g3", "", "pending")])
    connection.execute("INSERT INTO beliefs VALUES (1, 'books matter', 'g2', 'bot-b', 'active')")
    connection.execute("INSERT INTO concerns VALUES (1, 'g2', 'bot-b', 'books', 'follow up')")
    connection.execute("INSERT INTO bot_mood VALUES (1, 'g2', 'calm', 0.7, 'steady')")
    connection.execute("INSERT INTO book_communities VALUES (1, 'Library', 'reviewed catalog needed')")
    connection.commit()
    return connection


def test_recovery_request_is_dry_run_and_rejects_unsafe_modes():
    request = build_recovery_request({"idempotency_key": "preview-1", "sample_limit": "not-a-number"})
    assert request["kind"] == SCOPE_RECOVERY_PREVIEW_KIND
    assert request["payload"] == {
        "mode": "dry_run",
        "rule_version": SCOPE_RECOVERY_RULE_VERSION,
        "scope_mappings": [],
        "target_scopes": [],
        "migration_policy": "shared_generic_v2",
        "sample_limit": 50,
    }

    with pytest.raises(ScopeRecoveryError, match="dry_run_required"):
        build_recovery_request({"idempotency_key": "preview-1", "mode": "apply"})
    with pytest.raises(ScopeRecoveryError, match="scope_mapping_not_canonical"):
        build_recovery_request({"idempotency_key": "preview-1", "scope_mappings": [{"group_id": "g2", "bot_id": "bot-b", "session_id": "bad", "visibility": "group"}]})


def test_plan_snapshot_requires_explicit_scope_evidence_and_reports_coverage():
    connection = _connection()
    try:
        result = plan_snapshot(
            connection,
            {
                "scope_mappings": [
                    {"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"},
                    {"group_id": "g3", "bot_id": "bot-c", "session_id": "qq:group:g3", "visibility": "group"},
                    {"group_id": "g3", "bot_id": "bot-d", "session_id": "qq:group:g3", "visibility": "group"},
                ],
                "sample_limit": 10,
            },
        )
    finally:
        connection.close()

    counts = result["counts"]
    assert result["mode"] == "dry_run"
    assert result["source_business_mutated"] is False
    assert counts["memories_scanned"] == 4
    assert counts["formal_ready"] == 1
    assert counts["recoverable"] == 1
    assert counts["scope_mapping_required"] == 1
    assert counts["ambiguous_group_mapping"] == 1
    assert counts["quarantined_or_unresolved"] == 1
    assert counts["relationship_profiles_recoverable"] == 1
    assert result["coverage"]["ratio"] == 0.5
    assert result["samples"]["scope_mapping_required"][0]["legacy_memory_id"] == 4

    domains = result["domains"]
    assert domains["memories"]["source_tables"] == ["memories"]
    assert domains["relationships"]["scanned"] == 2
    assert domains["relationships"]["recoverable"] == 1
    assert domains["relationships"]["ambiguous_group_mapping"] == 1
    assert domains["affinity"]["scanned"] == 1
    assert domains["affinity"]["recoverable"] == 1
    assert domains["facts"]["recoverable"] == 1
    assert domains["facts"]["scope_mapping_required"] == 1
    assert domains["tags"]["recoverable"] == 1
    assert domains["tags"]["review_required"] == 1
    assert domains["jargon"]["recoverable"] == 1
    assert domains["beliefs"]["recoverable"] == 1
    assert domains["soul"]["recoverable"] == 2
    assert domains["book_lore"]["status"] == "review_required"
    assert domains["book_lore"]["review_required"] == 1
    assert domains["jargon"]["disposition"] == "purge"
    assert domains["beliefs"]["disposition"] == "purge"
    assert domains["soul"]["disposition"] == "purge"
    assert domains["soul"]["skipped"] == 0
    assert result["migration"]["target_scope_notice"] == "explicit_two_target_scopes_required_for_shared_migration"


def test_preview_reports_shared_targets_and_purge_counts():
    connection = _connection()
    try:
        result = plan_snapshot(
            connection,
            {
                "target_scopes": [
                    {"group_id": "g1", "bot_id": "bot-a", "session_id": "qq:group:g1", "visibility": "group"},
                    {"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"},
                ],
            },
        )
    finally:
        connection.close()

    assert result["migration"]["target_scope_count"] == 2
    assert result["migration"]["shared"]["target_memory_rows"] == 4
    assert result["migration"]["affinity"]["profiles_targetable"] == 1
    assert result["migration"]["affinity"]["relationship_event_targetable"] == 1
    assert result["domains"]["jargon"]["to_delete"] == 2
    assert result["domains"]["beliefs"]["to_delete"] == 1
    assert result["domains"]["soul"]["to_delete"] == 2
    assert result["domains"]["soul"]["skipped"] == 0


def test_staged_migration_copies_shared_data_then_purges_selected_legacy_rows(tmp_path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "migrated.sqlite3"
    connection = sqlite3.connect(source)
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT, content TEXT,
            vector BLOB, timestamp REAL, importance REAL, memory_type TEXT, source TEXT, summary TEXT,
            bot_id TEXT, session_id TEXT, visibility TEXT, origin_fingerprint TEXT, provenance TEXT,
            version TEXT, quarantine INTEGER DEFAULT 0, resolution_state TEXT
        );
        CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, confidence REAL, source_memory_id INTEGER, status TEXT);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, tag_type TEXT, description TEXT, confidence REAL);
        CREATE TABLE memory_tags (memory_id INTEGER, tag_id INTEGER, position INTEGER, relevance REAL);
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, word TEXT);
        CREATE TABLE group_jargon (id INTEGER PRIMARY KEY, word TEXT);
        CREATE TABLE beliefs (id INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE belief_system (id INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE bot_mood (id INTEGER PRIMARY KEY, group_id TEXT);
        CREATE TABLE mood_snapshots (id INTEGER PRIMARY KEY, group_id TEXT);
        CREATE TABLE concerns (id INTEGER PRIMARY KEY, group_id TEXT);
        CREATE TABLE time_anchors (id INTEGER PRIMARY KEY, group_id TEXT);
        CREATE TABLE user_profiles (id INTEGER PRIMARY KEY, user_id TEXT, group_id TEXT, bot_id TEXT, affection INTEGER, metadata TEXT);
        CREATE TABLE relationship_events (id INTEGER PRIMARY KEY, bot_id TEXT, group_id TEXT, user_id TEXT, event_type TEXT, dimension TEXT, delta REAL, reason TEXT, created_at REAL);
        CREATE TABLE tag_catalog (id INTEGER PRIMARY KEY, normalized_name TEXT UNIQUE, display_name TEXT, tag_type TEXT, description TEXT, embedding BLOB, status TEXT, created_at REAL, updated_at REAL);
        CREATE TABLE scoped_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT, subject TEXT, predicate TEXT, object TEXT, confidence REAL, status TEXT, source_memory_id INTEGER, provenance TEXT, created_at REAL, updated_at REAL, revision INTEGER DEFAULT 1, UNIQUE(bot_id, session_id, visibility, subject, predicate, object));
        CREATE TABLE scoped_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id INTEGER, bot_id TEXT, session_id TEXT, visibility TEXT, name TEXT, tag_type TEXT, description TEXT, confidence REAL, metadata TEXT, created_at REAL, updated_at REAL, UNIQUE(bot_id, session_id, visibility, name));
        CREATE TABLE scoped_memory_tags (bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER, position INTEGER, relevance REAL, created_at REAL, UNIQUE(bot_id, session_id, visibility, memory_id, tag_id));
        CREATE TABLE scoped_soul_relationships (bot_id TEXT, session_id TEXT, visibility TEXT, subject_principal_id TEXT, affinity INTEGER, state TEXT, dimensions TEXT, revision INTEGER, evidence TEXT, updated_at REAL, PRIMARY KEY(bot_id, session_id, visibility, subject_principal_id));
        CREATE TABLE scoped_soul_relationship_events (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT, subject_principal_id TEXT, event_type TEXT, dimension TEXT, delta REAL, reason TEXT, source_episode_id INTEGER, source_memory_id INTEGER, revision INTEGER, created_at REAL);
        INSERT INTO memories(id, group_id, sender_id, sender_name, content, timestamp, importance, memory_type, source, quarantine, resolution_state) VALUES (1, 'legacy', 'u1', 'User', '通用历史', 10, 1, 'message', 'legacy', 0, 'resolved');
        INSERT INTO facts VALUES (1, 'u1', 'likes', 'books', .9, 1, 'pending');
        INSERT INTO tags VALUES (1, 'books', 'keyword', '', .8);
        INSERT INTO memory_tags VALUES (1, 1, 1, .9);
        INSERT INTO jargon VALUES (1, '低质黑话');
        INSERT INTO group_jargon VALUES (1, '旧黑话');
        INSERT INTO beliefs VALUES (1, '低质信念');
        INSERT INTO belief_system VALUES (1, '旧信念');
        INSERT INTO bot_mood VALUES (1, 'g1');
        INSERT INTO mood_snapshots VALUES (1, 'g1');
        INSERT INTO concerns VALUES (1, 'g1');
        INSERT INTO time_anchors VALUES (1, 'g1');
        INSERT INTO user_profiles VALUES (1, 'u1', 'g1', 'bot-a', 42, '{}');
        INSERT INTO user_profiles VALUES (2, 'u2', 'g1', NULL, 55, '{}');
        INSERT INTO relationship_events VALUES (1, 'bot-a', 'g1', 'u1', 'help', 'trust', 2, 'legacy event', 11);
        """
    )
    connection.commit()
    connection.close()

    targets = [
        {"group_id": "g1", "bot_id": "bot-a", "session_id": "qq:group:g1", "visibility": "group"},
        {"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"},
    ]
    source_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    report = apply_staged_migration(source, output, tmp_path / "runs", targets, plan_hash="plan:test", expected_source_hash=source_hash, confirmation="migrate")

    assert report["migrated"] == {"memories": 2, "facts": 2, "tags": 2, "relationship_events": 1, "affinity": 1}
    assert report["review"]["affinity"] == 1
    assert report["deleted"] == {"jargon": 1, "group_jargon": 1, "beliefs": 1, "belief_system": 1, "bot_mood": 1, "mood_snapshots": 1, "concerns": 1}
    assert report["indexes_status"] == "pending"
    assert Path(report["source_backup_path"]).is_file()

    source_check = sqlite3.connect(source)
    try:
        assert source_check.execute("SELECT COUNT(*) FROM jargon").fetchone()[0] == 1
        assert source_check.execute("SELECT COUNT(*) FROM time_anchors").fetchone()[0] == 1
    finally:
        source_check.close()

    migrated = sqlite3.connect(output)
    try:
        assert migrated.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
        assert migrated.execute("SELECT COUNT(*) FROM scoped_facts").fetchone()[0] == 2
        assert migrated.execute("SELECT COUNT(*) FROM scoped_memory_tags").fetchone()[0] == 2
        assert migrated.execute("SELECT COUNT(*) FROM scoped_soul_relationships").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM scoped_soul_relationship_events").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM time_anchors").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM jargon").fetchone()[0] == 0
        assert migrated.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == 0
        assert {row[0] for row in migrated.execute("SELECT group_id FROM memories WHERE source='legacy_shared'").fetchall()} == {"g1", "g2"}
        assert {row[0] for row in migrated.execute("SELECT source_memory_id FROM scoped_facts").fetchall()} == {2, 3}
    finally:
        migrated.close()

    with pytest.raises(ScopeRecoveryMigrationError, match="exactly_two_target_scopes_required"):
        apply_staged_migration(source, tmp_path / "invalid.sqlite3", tmp_path / "runs-invalid", targets[:1], plan_hash="plan:test", expected_source_hash=source_hash, confirmation="migrate")
    with pytest.raises(ScopeRecoveryMigrationError, match="migration_confirmation_required"):
        apply_staged_migration(source, tmp_path / "unconfirmed.sqlite3", tmp_path / "runs-unconfirmed", targets, plan_hash="plan:test", expected_source_hash=source_hash)


def test_classified_recovery_projects_only_live_generic_and_single_bot_group_rows(tmp_path):
    source = tmp_path / "source.sqlite3"
    report_path = tmp_path / "classification.json"
    output = tmp_path / "recovered.sqlite3"
    connection = sqlite3.connect(source)
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, sender_id TEXT, sender_name TEXT,
            content TEXT NOT NULL, vector BLOB, timestamp REAL NOT NULL, importance REAL,
            access_count INTEGER, last_accessed REAL, memory_type TEXT, source TEXT, summary TEXT,
            bot_id TEXT, session_id TEXT, visibility TEXT, origin_fingerprint TEXT, provenance TEXT,
            version INTEGER, quarantine INTEGER, resolution_state TEXT
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            source_memory_id INTEGER, confidence REAL, valid_from REAL, valid_until REAL,
            created_at REAL, last_reinforced REAL, fact_type TEXT
        );
        CREATE TABLE tags (
            id INTEGER PRIMARY KEY, name TEXT, vector BLOB, created_at REAL, tag_type TEXT,
            aliases TEXT, description TEXT, confidence REAL, metadata TEXT, updated_at REAL
        );
        CREATE TABLE memory_tags (memory_id INTEGER, tag_id INTEGER, position INTEGER, relevance REAL, PRIMARY KEY(memory_id, tag_id));
        CREATE TABLE tag_relations (
            id INTEGER PRIMARY KEY, source_tag_id INTEGER, target_tag_id INTEGER,
            relation_type TEXT, weight REAL, confidence REAL, metadata TEXT, created_at REAL
        );
        CREATE TABLE tag_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, normalized_name TEXT, display_name TEXT,
            tag_type TEXT, description TEXT, embedding BLOB, embedding_model TEXT,
            embedding_dim INTEGER, status TEXT, created_at REAL, updated_at REAL,
            UNIQUE(normalized_name, tag_type)
        );
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT,
            name TEXT, tag_type TEXT, description TEXT, confidence REAL, metadata TEXT,
            created_at REAL, updated_at REAL, revision INTEGER DEFAULT 1, status TEXT DEFAULT 'active',
            aliases TEXT DEFAULT '[]', catalog_id INTEGER,
            UNIQUE(bot_id, session_id, visibility, name)
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER,
            position INTEGER, relevance REAL, created_at REAL,
            PRIMARY KEY(bot_id, session_id, visibility, memory_id, tag_id)
        );
        CREATE TABLE scoped_memory_effective_tags (
            bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER,
            position INTEGER, relevance REAL, source TEXT, correction_id TEXT,
            projection_revision INTEGER, updated_at REAL,
            PRIMARY KEY(bot_id, session_id, visibility, memory_id, tag_id)
        );
        CREATE TABLE scoped_tag_projection_state (
            bot_id TEXT, session_id TEXT, visibility TEXT, state TEXT,
            projection_revision INTEGER, cursor_memory_id INTEGER, last_error TEXT, updated_at REAL,
            PRIMARY KEY(bot_id, session_id, visibility)
        );
        CREATE TABLE scoped_tag_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT,
            source_tag_id INTEGER, target_tag_id INTEGER, relation_type TEXT, weight REAL,
            confidence REAL, metadata TEXT, created_at REAL, updated_at REAL, status TEXT,
            valid_until REAL, revision INTEGER,
            UNIQUE(bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type)
        );
        CREATE TABLE scoped_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT,
            subject TEXT, predicate TEXT, object TEXT, confidence REAL, status TEXT,
            source_memory_id INTEGER, provenance TEXT, valid_from REAL, valid_until REAL,
            created_at REAL, updated_at REAL, revision INTEGER,
            UNIQUE(bot_id, session_id, visibility, subject, predicate, object)
        );
        INSERT INTO memories VALUES
            (100,'g1','u','U','formal g1',NULL,100,1,0,NULL,'message','live',NULL,'yushu','qq:group:g1','group',NULL,'{}',2,0,'resolved'),
            (101,'g2','u','U','formal yushu g2',NULL,101,1,0,NULL,'message','live',NULL,'yushu','qq:group:g2','group',NULL,'{}',2,0,'resolved'),
            (102,'g2','u','U','formal baizz g2',NULL,102,1,0,NULL,'message','live',NULL,'baizz','qqb:group:g2','group',NULL,'{}',2,0,'resolved'),
            (1,'legacy','u1','U1','generic history',X'0000803F000000000000000000000000',1,1,0,NULL,'message','core',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL),
            (2,'g1','u2','U2','single bot chat',X'0000803F000000000000000000000000',2,1,0,NULL,'message','chat',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL),
            (3,'g2','u3','U3','multi bot chat',X'0000803F000000000000000000000000',3,1,0,NULL,'message','chat',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL),
            (4,'g1','u4','U4','evicted chat',X'0000803F000000000000000000000000',4,1,0,NULL,'evicted','chat',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL);
        INSERT INTO tags VALUES
            (1,'alpha',X'0000803F000000000000000000000000',1,'topic','[]','',.9,'{}',1),
            (2,'beta',X'000000000000803F0000000000000000',1,'topic','[]','',.8,'{}',1);
        INSERT INTO memory_tags VALUES (1,1,1,.9),(1,2,2,.8),(2,1,1,.9),(4,2,1,.8);
        INSERT INTO tag_relations VALUES (1,1,2,'related',.7,.8,'{}',1);
        INSERT INTO facts VALUES
            (1,'u1','likes','alpha',1,.9,NULL,NULL,1,NULL,'FACTUAL'),
            (2,'u2','likes','chat',2,.8,NULL,NULL,2,NULL,'FACTUAL'),
            (3,'u4','likes','evicted',4,.8,NULL,NULL,3,NULL,'FACTUAL');
        """
    )
    connection.commit()
    connection.close()
    report_path.write_text(
        json.dumps(
            {
                "source_sha256": "historical-source",
                "snapshot_sha256": "historical-snapshot",
                "summary": {"legacy_scope_incomplete_rows": 4},
                "items": [
                    {"memory_id": 1, "category": "generic_shared_candidate"},
                    {"memory_id": 2, "category": "group_chat_candidate"},
                    {"memory_id": 3, "category": "group_chat_candidate"},
                    {"memory_id": 4, "category": "evicted"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = apply_classified_scope_recovery(
        source,
        report_path,
        output,
        tmp_path / "runs",
        confirmation="recover",
    )

    assert result["selected_by_category"] == {
        "generic_shared_candidate": 1,
        "group_chat_candidate": 1,
    }
    assert result["projected_memory_rows"] == 4
    assert result["skipped"]["multi_bot_group_review"] == 1
    assert result["evicted_rows_reactivated"] == 0
    recovered = sqlite3.connect(output)
    try:
        assert recovered.execute(
            "SELECT COUNT(*) FROM memories WHERE provenance LIKE '%classified_legacy_recovery%'"
        ).fetchone()[0] == 4
        assert recovered.execute(
            "SELECT COUNT(*) FROM memories WHERE provenance LIKE '%classified_legacy_recovery%' AND memory_type='evicted'"
        ).fetchone()[0] == 0
        assert recovered.execute("SELECT COUNT(*) FROM scoped_memory_tags").fetchone()[0] == 7
        assert recovered.execute("SELECT COUNT(*) FROM scoped_memory_effective_tags").fetchone()[0] == 7
        assert recovered.execute("SELECT COUNT(*) FROM scoped_tag_relations").fetchone()[0] == 3
        assert recovered.execute("SELECT COUNT(*) FROM scoped_facts").fetchone()[0] == 4
        assert recovered.execute("SELECT COUNT(*) FROM memories WHERE id IN (1,2,3,4)").fetchone()[0] == 4
    finally:
        recovered.close()


def test_approved_group_scope_recovery_rejects_ambiguous_fanout(tmp_path):
    source = tmp_path / "snapshot.sqlite3"
    plan_path = tmp_path / "approved-plan.json"
    output = tmp_path / "staged.sqlite3"
    connection = sqlite3.connect(source)
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, sender_id TEXT, sender_name TEXT,
            content TEXT NOT NULL, vector BLOB, timestamp REAL NOT NULL, importance REAL,
            access_count INTEGER, last_accessed REAL, memory_type TEXT, source TEXT, summary TEXT,
            bot_id TEXT, session_id TEXT, visibility TEXT, origin_fingerprint TEXT, provenance TEXT,
            version INTEGER, quarantine INTEGER, resolution_state TEXT
        );
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, vector BLOB, created_at REAL, tag_type TEXT,
            aliases TEXT, description TEXT, confidence REAL, metadata TEXT, updated_at REAL);
        CREATE TABLE memory_tags (memory_id INTEGER, tag_id INTEGER, position INTEGER, relevance REAL);
        CREATE TABLE tag_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, normalized_name TEXT, display_name TEXT,
            tag_type TEXT, description TEXT, embedding BLOB, embedding_model TEXT,
            embedding_dim INTEGER, status TEXT, created_at REAL, updated_at REAL,
            UNIQUE(normalized_name, tag_type)
        );
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT,
            name TEXT, tag_type TEXT, description TEXT, confidence REAL, metadata TEXT,
            created_at REAL, updated_at REAL, revision INTEGER DEFAULT 1, status TEXT DEFAULT 'active',
            aliases TEXT DEFAULT '[]', catalog_id INTEGER,
            UNIQUE(bot_id, session_id, visibility, name)
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER,
            position INTEGER, relevance REAL, created_at REAL,
            PRIMARY KEY(bot_id, session_id, visibility, memory_id, tag_id)
        );
        CREATE TABLE scoped_memory_effective_tags (
            bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER,
            position INTEGER, relevance REAL, source TEXT, correction_id TEXT,
            projection_revision INTEGER, updated_at REAL,
            PRIMARY KEY(bot_id, session_id, visibility, memory_id, tag_id)
        );
        CREATE TABLE scoped_tag_projection_state (
            bot_id TEXT, session_id TEXT, visibility TEXT, state TEXT,
            projection_revision INTEGER, cursor_memory_id INTEGER, last_error TEXT, updated_at REAL,
            PRIMARY KEY(bot_id, session_id, visibility)
        );
        CREATE TABLE scoped_tag_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, session_id TEXT, visibility TEXT,
            source_tag_id INTEGER, target_tag_id INTEGER, relation_type TEXT, weight REAL,
            confidence REAL, metadata TEXT, created_at REAL, updated_at REAL, status TEXT,
            valid_until REAL, revision INTEGER,
            UNIQUE(bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type)
        );
        CREATE TABLE scoped_beliefs (id INTEGER PRIMARY KEY, source_memory_id INTEGER, content TEXT);
        CREATE TABLE memory_feedback (id INTEGER PRIMARY KEY, memory_id INTEGER, feedback TEXT);
        CREATE TABLE experience_episodes (id INTEGER PRIMARY KEY, source_memory_ids TEXT);
        CREATE TABLE scope_recovery_memory_map (
            legacy_memory_id INTEGER NOT NULL, target_scope_key TEXT NOT NULL,
            target_memory_id INTEGER NOT NULL, origin_key TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
            PRIMARY KEY(legacy_memory_id, target_scope_key)
        );
        """
    )
    formal = [
        (100, "g1", "formal a", "bot-a", "a:group:g1"),
        (101, "g1", "formal b", "bot-b", "b:group:g1"),
        (102, "g2", "formal c", "bot-c", "c:group:g2"),
    ]
    for memory_id, group_id, content, bot_id, session_id in formal:
        connection.execute(
            """INSERT INTO memories(id,group_id,sender_id,sender_name,content,timestamp,importance,memory_type,
                   source,bot_id,session_id,visibility,quarantine,resolution_state,provenance)
               VALUES (?,?, 'u','U',?,1,1,'message','live',?,?, 'group',0,'resolved','{}')""",
            (memory_id, group_id, content, bot_id, session_id),
        )
    connection.executemany(
        """INSERT INTO memories(id,group_id,sender_id,sender_name,content,timestamp,importance,memory_type,
               source,bot_id,session_id,visibility,quarantine,resolution_state)
           VALUES (?,?,'u','U',?,1,1,'message',?,NULL,NULL,NULL,0,'')""",
        [
            (1, "g1", "group-bound core", "core"),
            (2, "g1", "group-bound chat", "chat"),
            (3, "g2", "mapping not approved", "chat"),
            (4, "g1", "noise stays review", "noise"),
        ],
    )
    connection.execute(
        """INSERT INTO memories(id,group_id,sender_id,sender_name,content,timestamp,importance,memory_type,
               source,bot_id,session_id,visibility,quarantine,resolution_state)
           VALUES (5,'g1','u','U','partial Scope evidence',1,1,'message','chat','bot-a',NULL,NULL,0,'')"""
    )
    provenance = json.dumps(
        {
            "legacy_source_table": "memories",
            "legacy_id": 1,
            "source_group_id": "g1",
        },
        ensure_ascii=False,
    )
    historical = [
        (200, "g1", "bot-a", "a:group:g1", "bot-a|a:group:g1|group"),
        (201, "g1", "bot-b", "b:group:g1", "bot-b|b:group:g1|group"),
        (202, "g2", "bot-c", "c:group:g2", "bot-c|c:group:g2|group"),
    ]
    for memory_id, group_id, bot_id, session_id, scope_key in historical:
        connection.execute(
            """INSERT INTO memories(id,group_id,sender_id,sender_name,content,timestamp,importance,memory_type,
                   source,bot_id,session_id,visibility,quarantine,resolution_state,provenance)
               VALUES (?,?,'u','U','group-bound core',1,1,'message','core',?,?, 'group',0,'resolved',?)""",
            (memory_id, group_id, bot_id, session_id, provenance),
        )
        connection.execute(
            """INSERT INTO scope_recovery_memory_map(
                   legacy_memory_id,target_scope_key,target_memory_id,origin_key,run_id
               ) VALUES (1,?,?,?, 'historical')""",
            (scope_key, memory_id, f"historical|1|{scope_key}"),
        )
    connection.execute("INSERT INTO tags VALUES (1,'alpha',NULL,1,'topic','[]','',.9,'{}',1)")
    connection.execute("INSERT INTO memory_tags VALUES (1,1,1,.9)")
    connection.execute(
        "INSERT INTO scoped_memory_tags VALUES ('bot-c','c:group:g2','group',202,1,1,.9,1)"
    )
    connection.execute("INSERT INTO scoped_beliefs VALUES (1,202,'wrong scope belief')")
    connection.execute("INSERT INTO memory_feedback VALUES (1,202,'historical feedback')")
    connection.execute("INSERT INTO experience_episodes VALUES (1,'[202]')")
    connection.commit()
    connection.close()

    mappings = [
        {"group_id": "g1", "bot_id": "bot-a", "session_id": "a:group:g1", "visibility": "group"},
        {"group_id": "g1", "bot_id": "bot-b", "session_id": "b:group:g1", "visibility": "group"},
    ]
    frozen_snapshot = tmp_path / "frozen-snapshot.sqlite3"
    snapshot_result = create_approved_scope_snapshot(source, frozen_snapshot)
    assert snapshot_result["quick_check"] == "ok"
    sidecar = Path(str(frozen_snapshot) + "-wal")
    sidecar.write_bytes(b"must reject non-single-file snapshot")
    with pytest.raises(ApprovedScopeRecoveryError, match="source_snapshot_sidecar_present"):
        build_approved_scope_recovery_plan(frozen_snapshot, mappings)
    sidecar.unlink()

    # Ambiguous group mappings used to produce one copied row per Bot. That
    # fanout model is retired: a legacy row may only be recovered into one
    # explicitly owned formal Scope, never duplicated across matching scopes.
    with pytest.raises(ApprovedScopeRecoveryError, match="no_approved_recoverable_memories"):
        build_approved_scope_recovery_plan(frozen_snapshot, mappings)


def test_tag_links_without_legacy_endpoints_are_review_samples():
    connection = _connection()
    try:
        connection.execute("INSERT INTO tags VALUES (3, 'orphan')")
        result = plan_snapshot(connection, {"scope_mappings": [{"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"}]})
    finally:
        connection.close()

    assert result["domains"]["tags"]["review_required"] == 2
    assert result["domains"]["tags"]["status"] == "review_required"
    assert any(sample["reason"] == "tag_without_memory_evidence_requires_review" for sample in result["domains"]["tags"]["samples"]["review_required"])


@pytest.mark.asyncio
async def test_preview_job_reads_snapshot_and_leaves_source_unchanged(tmp_path):
    source = tmp_path / "source.sqlite3"
    source_connection = sqlite3.connect(source)
    source_connection.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, group_id TEXT, content TEXT);
        INSERT INTO memories VALUES (1, 'g1', 'legacy row');
        """
    )
    source_connection.commit()
    before = source_connection.execute("SELECT * FROM memories").fetchall()
    source_connection.close()

    jobs = ScopeRecoveryPreviewJobs(source_db_path=source, snapshot_dir=tmp_path / "snapshots")
    request = SimpleNamespace(payload={"rule_version": SCOPE_RECOVERY_RULE_VERSION, "scope_mappings": [], "sample_limit": 5})
    result = await jobs.preview(SimpleNamespace(run_id="run-1"), request, None)

    assert result["completed"] is True
    assert result["source_business_mutated"] is False
    assert not list((tmp_path / "snapshots").glob("*"))
    after_connection = sqlite3.connect(source)
    try:
        assert after_connection.execute("SELECT * FROM memories").fetchall() == before
    finally:
        after_connection.close()


@pytest.mark.asyncio
async def test_scope_recovery_route_only_enqueues_registered_dry_run(monkeypatch):
    class Jobs:
        async def enqueue(self, **kwargs):
            assert kwargs["kind"] == SCOPE_RECOVERY_PREVIEW_KIND
            assert kwargs["payload"]["mode"] == "dry_run"
            assert kwargs["payload"]["scope_mappings"] == [{"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"}]
            assert kwargs["payload"]["target_scopes"] == [
                {"group_id": "g1", "bot_id": "bot-a", "session_id": "qq:group:g1", "visibility": "group"},
                {"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"},
            ]
            return SimpleNamespace(
                to_dict=lambda: {
                    "accepted": True,
                    "request_id": "request-1",
                    "job_id": "run-1",
                    "status": "queued",
                    "operation": {"id": "run-1"},
                }
            )

    class Request:
        async def get_json(self):
            return {
                "idempotency_key": "webui-1",
                "sample_limit": 5,
                "scope_mappings": [{"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"}],
                "target_scopes": [
                    {"group_id": "g1", "bot_id": "bot-a", "session_id": "qq:group:g1", "visibility": "group"},
                    {"group_id": "g2", "bot_id": "bot-b", "session_id": "qq:group:g2", "visibility": "group"},
                ],
            }

    monkeypatch.setattr(maintenance, "get_container", lambda: SimpleNamespace(scope_recovery_jobs=object(), durable_jobs=Jobs()))
    monkeypatch.setattr(maintenance, "request", Request())
    monkeypatch.setattr(maintenance, "jsonify", lambda value: value)

    payload, status = await maintenance.schedule_scope_recovery_preview()

    assert status == 202
    assert payload["dry_run"] is True
    assert payload["source_business_mutated"] is False
    assert payload["checkpoint_url"].endswith("/run-1/checkpoint")
