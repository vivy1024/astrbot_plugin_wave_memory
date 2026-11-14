"""v6.0 person_scene_stats 仓储与 user_profiles 迁移测试。"""

from __future__ import annotations

import sqlite3

from engine.db.connection import ConnectionManager
from engine.db.person_scene_stats_repo import PersonSceneStatsRepository


def test_touch_list_sum_and_latest_nickname(tmp_path):
    cm = ConnectionManager(str(tmp_path / "stats.db"))
    try:
        repo = PersonSceneStatsRepository(cm)
        repo.touch(bot_id="yushu", group_id="g1", user_id="u1", nickname="老王", increment=1, seen_at=100.0)
        repo.touch(bot_id="yushu", group_id="g1", user_id="u1", nickname="", increment=2, seen_at=150.0)
        repo.touch(bot_id="yushu", group_id="g2", user_id="u1", nickname="王哥", increment=3, seen_at=200.0)
        repo.touch(bot_id="yushu", group_id="private:u1", user_id="u1", nickname="私聊王", increment=10, seen_at=300.0)

        row = repo.get(bot_id="yushu", group_id="g1", user_id="u1")
        assert row is not None
        assert row["nickname"] == "老王"
        assert row["interaction_count"] == 3
        assert row["first_seen"] == 100.0
        assert row["last_seen"] == 150.0

        assert repo.sum_interactions(bot_id="yushu", user_ids=["u1"]) == 6
        assert repo.sum_interactions(bot_id="yushu", user_ids=["u1"], exclude_private=False) == 16
        assert repo.latest_nickname(bot_id="yushu", user_ids=["u1"]) == "王哥"
        assert [r["user_id"] for r in repo.list_group(bot_id="yushu", group_id="g1")] == ["u1"]
    finally:
        cm.close()


def test_migrate_from_user_profiles_is_idempotent(tmp_path):
    db_path = tmp_path / "mig.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        """CREATE TABLE user_profiles (
               user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT,
               affection REAL DEFAULT 0, interaction_count INTEGER DEFAULT 0,
               first_seen REAL, last_seen REAL, metadata TEXT,
               PRIMARY KEY (user_id, group_id, bot_id)
           )"""
    )
    raw.executemany(
        "INSERT INTO user_profiles (user_id, group_id, bot_id, nickname, interaction_count, first_seen, last_seen, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        [
            ("10001", "398291136", "yushu", "长乐", 12, 1000.0, 2000.0),
            ("10002", "398291136", "", "无主旧号", 5, 500.0, 900.0),
        ],
    )
    raw.commit()
    raw.close()

    cm = ConnectionManager(str(db_path))
    try:
        repo = PersonSceneStatsRepository(cm)
        repo.migrate_from_user_profiles()
        repo.migrate_from_user_profiles()  # 幂等

        r1 = repo.get(bot_id="yushu", group_id="398291136", user_id="10001")
        r2 = repo.get(bot_id="yushu", group_id="398291136", user_id="10002")
        assert r1 and r1["nickname"] == "长乐" and r1["interaction_count"] == 12
        assert r2 and r2["nickname"] == "无主旧号" and r2["interaction_count"] == 5
    finally:
        cm.close()
