"""v2.1 清理迁移：Bot 身份全部来自注册表，不写死任何部署的号码。

旧表 ``facts`` 已废除，迁移里与之相关的步骤一并移除；本测试只覆盖剩余职责
（bot_id 统一、互动计数清零、jargon 字段补齐）。
"""

from __future__ import annotations

import sqlite3

from engine.db.migrations.v2_1_cleanup import run_migration


def _legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE beliefs (id INTEGER PRIMARY KEY, bot_id TEXT, content TEXT);
        CREATE TABLE user_profiles (id INTEGER PRIMARY KEY, interaction_count INTEGER);
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, phrase TEXT);
        INSERT INTO beliefs (bot_id, content) VALUES ('1000000005', '旧号记的'), ('1000000009', '别人的');
        INSERT INTO user_profiles (interaction_count) VALUES (7), (3);
        """
    )
    conn.commit()
    conn.close()


def test_without_bot_identifiers_skips_identity_step(tmp_path):
    db = tmp_path / "wm.db"
    _legacy_db(db)
    assert run_migration(str(db)) is True
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT bot_id FROM beliefs ORDER BY id")] == ["1000000005", "1000000009"]
    # 互动计数清零与 bot_identifiers 无关，照常执行
    assert [r[0] for r in conn.execute("SELECT interaction_count FROM user_profiles")] == [0, 0]
    conn.close()


def test_bot_identifiers_from_registry_drive_cleanup(tmp_path):
    db = tmp_path / "wm.db"
    _legacy_db(db)
    ok = run_migration(str(db), {
        "qq_ids": ["1000000005"], "db_ids": ["bot-a"], "names": ["示例甲"],
        "qq_to_db": {"1000000005": "bot-a"},
    })
    assert ok is True
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT bot_id FROM beliefs ORDER BY id")] == ["bot-a", "1000000009"]
    conn.close()


def test_migration_does_not_touch_facts_table(tmp_path):
    """迁移不得再读写旧表 facts —— 该表已废除，迁移里残留 ALTER 会在新装上炸。"""
    db = tmp_path / "wm.db"
    _legacy_db(db)
    conn = sqlite3.connect(db)
    # 故意建一张 facts 表并塞一行：迁移若还碰它，要么改列要么删行，本断言即可发现
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT)")
    conn.execute("INSERT INTO facts (subject) VALUES ('哨兵')")
    conn.commit()
    conn.close()

    assert run_migration(str(db), {"qq_ids": [], "db_ids": [], "names": [], "qq_to_db": {}}) is True

    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT subject FROM facts")] == ["哨兵"]
    assert [r[1] for r in conn.execute("PRAGMA table_info(facts)")] == ["id", "subject"]
    conn.close()
