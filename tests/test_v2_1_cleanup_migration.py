"""v2.1 清理迁移：Bot 身份全部来自注册表，不写死任何部署的号码。"""

from __future__ import annotations

import sqlite3

from engine.db.migrations.v2_1_cleanup import run_migration


def _legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, created_at REAL);
        CREATE TABLE beliefs (id INTEGER PRIMARY KEY, bot_id TEXT, content TEXT);
        CREATE TABLE user_profiles (id INTEGER PRIMARY KEY, interaction_count INTEGER);
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, phrase TEXT);
        INSERT INTO facts (subject, predicate, object, created_at) VALUES
            ('示例甲', '是', '机器人', 1.0), ('1000000005', '在', '群里', 1.0), ('阿一', '喜欢', '吉他', 1.0);
        INSERT INTO beliefs (bot_id, content) VALUES ('1000000005', '旧号记的'), ('1000000009', '别人的');
        """
    )
    conn.commit()
    conn.close()


def test_without_bot_identifiers_skips_identity_steps(tmp_path):
    db = tmp_path / "wm.db"
    _legacy_db(db)
    assert run_migration(str(db)) is True
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 3
    assert [r[0] for r in conn.execute("SELECT bot_id FROM beliefs ORDER BY id")] == ["1000000005", "1000000009"]
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
    assert [r[0] for r in conn.execute("SELECT subject FROM facts")] == ["阿一"]
    assert [r[0] for r in conn.execute("SELECT bot_id FROM beliefs ORDER BY id")] == ["bot-a", "1000000009"]
    conn.close()
