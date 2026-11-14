"""v6.0 群友分场景活跃统计表（替代 user_profiles 的花名册/互动计数/昵称角色）。

无 JSON metadata 泥潭、无旧版假好感字段；好感、印象与人情锚点由各自的正式仓储管理。
全部 ``IF NOT EXISTS``，重复执行无副作用。
"""

from __future__ import annotations

from ..connection import ConnectionManager

_PERSON_SCENE_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_scene_stats (
    bot_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_person_scene_stats_bot_user
    ON person_scene_stats (bot_id, user_id);

CREATE INDEX IF NOT EXISTS idx_person_scene_stats_bot_group_last
    ON person_scene_stats (bot_id, group_id, last_seen DESC);
"""


def ensure_person_scene_stats_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    statements = tuple(s.strip() for s in _PERSON_SCENE_STATS_SCHEMA.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)


__all__ = ["ensure_person_scene_stats_schema"]
