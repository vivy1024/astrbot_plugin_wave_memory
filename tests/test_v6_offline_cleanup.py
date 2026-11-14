"""v6.0.0 停服离线数据库清理与对齐脚本测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.v6_offline_db_cleanup import cleanup_database


def _build_legacy_like_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE bot_profiles (
            db_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        INSERT INTO bot_profiles(db_id, payload) VALUES ('yushu', '{"session_prefix": "羽书"}');

        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            sender_id TEXT,
            sender_name TEXT,
            group_id TEXT,
            session_id TEXT,
            visibility TEXT,
            bot_id TEXT,
            source TEXT,
            quarantine INTEGER DEFAULT 0,
            memory_type TEXT DEFAULT 'message',
            timestamp REAL DEFAULT 1000
        );
        CREATE VIRTUAL TABLE fts_memories USING fts5(content, sender_name, group_id, content='memories', content_rowid='id');
        CREATE TRIGGER fts_memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO fts_memories(rowid, content, sender_name, group_id) VALUES (new.id, new.content, new.sender_name, new.group_id);
        END;
        CREATE TRIGGER fts_memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO fts_memories(fts_memories, rowid, content, sender_name, group_id) VALUES ('delete', old.id, old.content, old.sender_name, old.group_id);
        END;

        CREATE VIRTUAL TABLE fts_memories_cjk USING fts5(tokens, content='', contentless_delete=1, tokenize='unicode61');

        CREATE TABLE user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            nickname TEXT DEFAULT '',
            affection INTEGER DEFAULT 0,
            interaction_count INTEGER DEFAULT 0,
            first_seen REAL DEFAULT 0,
            last_seen REAL DEFAULT 0,
            metadata TEXT DEFAULT '{}',
            bot_id TEXT DEFAULT 'yushu',
            UNIQUE(user_id, group_id, bot_id)
        );

        CREATE TABLE person_unsettled_state (
            bot_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL DEFAULT '',
            energy REAL NOT NULL DEFAULT 0,
            interaction_count INTEGER NOT NULL DEFAULT 0,
            traces TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL,
            PRIMARY KEY (bot_id, user_id, group_id)
        );

        CREATE TABLE tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            tag_type TEXT DEFAULT 'keyword',
            aliases TEXT,
            description TEXT,
            confidence REAL DEFAULT 1.0,
            metadata TEXT,
            created_at REAL NOT NULL,
            updated_at REAL
        );
        CREATE TABLE memory_tags (
            memory_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            relevance REAL DEFAULT 1.0,
            PRIMARY KEY (memory_id, tag_id)
        );
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility = 'group'),
            name TEXT NOT NULL,
            tag_type TEXT NOT NULL DEFAULT 'keyword',
            description TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            aliases TEXT NOT NULL DEFAULT '[]',
            UNIQUE (bot_id, session_id, visibility, name)
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility = 'group'),
            memory_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            relevance REAL NOT NULL DEFAULT 1.0,
            created_at REAL NOT NULL,
            PRIMARY KEY (bot_id, session_id, visibility, memory_id, tag_id)
        );

        CREATE TABLE memory_importance_repair_v51 (id INTEGER PRIMARY KEY, importance REAL);
        INSERT INTO memory_importance_repair_v51(id, importance) VALUES (1, 2.5);
        CREATE TABLE scoped_beliefs (id INTEGER PRIMARY KEY, content TEXT);
        INSERT INTO scoped_beliefs(id, content) VALUES (1, '旧归档信念');
        CREATE TABLE background_job_runs (
            run_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at REAL NOT NULL
        );
        INSERT INTO background_job_runs(run_id, status, created_at) VALUES
            ('old', 'succeeded', 100.0),
            ('recent', 'succeeded', 9999999999.0);

        CREATE TABLE domain_outbox (event_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
        CREATE TABLE outbox_deliveries (event_id TEXT NOT NULL, consumer_name TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE write_operations (operation_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at REAL NOT NULL, committed_at REAL);
        INSERT INTO domain_outbox VALUES ('e1', 100.0), ('e2', 9999999999.0);
        INSERT INTO outbox_deliveries VALUES ('e1', 'c1', 'completed'), ('e2', 'c1', 'completed');
        INSERT INTO write_operations VALUES ('op1', 'committed', 100.0, 100.0), ('op2', 'committed', 9999999999.0, 9999999999.0);

        CREATE TABLE injection_traces (trace_id TEXT PRIMARY KEY, timestamp REAL NOT NULL);
        CREATE TABLE injection_trace_channels (id INTEGER PRIMARY KEY, trace_id TEXT NOT NULL);
        INSERT INTO injection_traces VALUES ('t1', 100.0), ('t2', 9999999999.0);
        INSERT INTO injection_trace_channels VALUES (1, 't1'), (2, 't2');
        """
    )

    # 1. user_profiles
    conn.execute(
        """INSERT INTO user_profiles(user_id, group_id, nickname, affection, interaction_count, first_seen, last_seen, bot_id)
           VALUES ('u1', 'g1', '张三', 10, 42, 100, 500, 'yushu')"""
    )

    # 2. unsettled 跨群旧行 + 新池行
    conn.execute(
        """INSERT INTO person_unsettled_state VALUES
           ('yushu', 'u1', '', 0.4, 2, '[{"quote":"好啊","ts":10}]', 10.0),
           ('yushu', 'u1', 'g1', 0.5, 3, '[{"quote":"帮大忙","ts":20}]', 20.0)"""
    )

    # 3. memories: 早期 chat/core (bot_id NULL) + bzz_experience/oni_lore/noise/empty group
    conn.execute(
        """INSERT INTO memories(id, content, sender_id, sender_name, group_id, session_id, visibility, bot_id, source)
           VALUES
           (1, '早期聊天：张羽去买灵石', 'u1', '张三', 'g1', NULL, NULL, NULL, 'chat'),
           (2, '核心记忆：张羽喜欢熬夜', 'u1', '张三', 'g1', NULL, NULL, NULL, 'core'),
           (3, '白真真旧经验冗余', 'bzz', '白真真', 'g1', NULL, NULL, NULL, 'bzz_experience'),
           (4, '缺氧旧语料冗余', 'oni', 'ONI', 'g1', NULL, NULL, NULL, 'oni_lore'),
           (5, '无意义噪声', 'u2', '李四', 'g1', NULL, NULL, NULL, 'noise'),
           (6, 'ONI测试空群', 'bot_remember', '主动记忆', '', NULL, NULL, NULL, 'chat')"""
    )
    conn.execute("INSERT INTO tags(id, name, created_at) VALUES (1, '灵石', 1000.0), (2, '熬夜', 1000.0)")
    conn.execute("INSERT INTO memory_tags(memory_id, tag_id, position, relevance) VALUES (1, 1, 0, 1.0), (2, 2, 0, 0.9), (3, 1, 0, 1.0)")
    conn.commit()
    conn.close()


def test_dry_run_does_not_mutate_and_reports_exact_counts(tmp_path):
    db = tmp_path / "offline.db"
    _build_legacy_like_db(db)

    report = cleanup_database(db, apply=False)
    assert report["mode"] == "dry_run"
    assert report["user_profiles"]["source_rows"] == 1
    assert report["unsettled_state"]["legacy_group_rows"] == 1
    assert report["null_bot_memories"]["deleted_obsolete"] == 4
    assert report["null_bot_memories"]["aligned_yushu"] == 2
    assert report["null_bot_memories"]["migrated_scoped_tags"] == 2
    assert report["fts_cjk"]["missing_rows"] == 2
    assert report["auxiliary"]["cleared_v51_backup_rows"] == 1
    assert report["auxiliary"]["cleared_scoped_beliefs"] == 1
    assert report["auxiliary"]["pruned_job_runs"] == 1

    # dry-run 不能修改数据
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE bot_id IS NULL").fetchone()[0] == 6
    conn.close()


def test_apply_migrates_all_data_aligns_tags_and_fts_and_vacuums(tmp_path):
    db = tmp_path / "offline.db"
    _build_legacy_like_db(db)

    report = cleanup_database(db, apply=True, prune_outbox_days=7, prune_traces_days=3)
    assert report["quick_check"] == "ok"
    assert report["auxiliary"]["pruned_outbox_rows"] == 2
    assert report["auxiliary"]["pruned_trace_rows"] == 1

    conn = sqlite3.connect(str(db))
    # 1. user_profiles 迁入 person_scene_stats 并删除旧表
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='user_profiles'").fetchone() is None
    row = conn.execute(
        "SELECT nickname, interaction_count FROM person_scene_stats WHERE bot_id='yushu' AND group_id='g1' AND user_id='u1'"
    ).fetchone()
    assert row == ("张三", 42)

    # 2. person_unsettled_state 合并为 group_id='' 一人一池
    unsettled = conn.execute(
        "SELECT group_id, energy, interaction_count, traces FROM person_unsettled_state WHERE bot_id='yushu' AND user_id='u1'"
    ).fetchall()
    assert len(unsettled) == 1
    assert unsettled[0][0] == ""
    assert abs(unsettled[0][1] - 0.9) < 1e-6
    assert unsettled[0][2] == 5
    assert len(json.loads(unsettled[0][3])) == 2

    # 3. memories: 4 条废弃清理，2 条完整归属 yushu + session_id + visibility
    rows = conn.execute("SELECT id, bot_id, session_id, visibility FROM memories ORDER BY id").fetchall()
    assert rows == [
        (1, "yushu", "羽书:group:g1", "group"),
        (2, "yushu", "羽书:group:g1", "group"),
    ]

    # 4. scoped_tags / scoped_memory_tags 补全，memory_tags 删除冗余记忆关联
    smt = conn.execute(
        """SELECT smt.memory_id, st.name, smt.session_id
           FROM scoped_memory_tags smt JOIN scoped_tags st ON st.id = smt.tag_id
           ORDER BY smt.memory_id"""
    ).fetchall()
    assert smt == [(1, "灵石", "羽书:group:g1"), (2, "熬夜", "羽书:group:g1")]

    # 5. fts_memories_cjk 补齐且能用两字短语搜到
    hits = conn.execute(
        "SELECT rowid FROM fts_memories_cjk WHERE fts_memories_cjk MATCH '\"灵石\"'"
    ).fetchall()
    assert [h[0] for h in hits] == [1]

    # 6. 哨兵表保留为空表结构、scoped_beliefs 清空、job_runs/outbox/traces 只留最近记录
    assert conn.execute("SELECT COUNT(*) FROM memory_importance_repair_v51").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scoped_beliefs").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT run_id FROM background_job_runs").fetchall()] == ["recent"]
    assert [r[0] for r in conn.execute("SELECT event_id FROM domain_outbox").fetchall()] == ["e2"]
    assert [r[0] for r in conn.execute("SELECT trace_id FROM injection_traces").fetchall()] == ["t2"]
    assert [r[0] for r in conn.execute("SELECT trace_id FROM injection_trace_channels").fetchall()] == ["t2"]
    conn.close()
