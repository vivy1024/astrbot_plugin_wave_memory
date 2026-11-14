"""群友分场景活跃统计仓储（``person_scene_stats``）：替代 ``user_profiles`` 的非情感部分。

只管花名册、互动次数、最近出现的昵称与活跃时间；严格经 ``ConnectionManager`` 读写锁操作。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from .connection import ConnectionManager
from .migrations.person_scene_stats import ensure_person_scene_stats_schema


def _marks(items: Sequence[str]) -> str:
    return ", ".join("?" for _ in items)


class PersonSceneStatsRepository:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True):
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm
        if ensure_schema:
            ensure_person_scene_stats_schema(cm)

    def touch(
        self,
        *,
        bot_id: str,
        group_id: str,
        user_id: str,
        nickname: str = "",
        increment: int = 0,
        seen_at: float | None = None,
    ) -> None:
        """登记一次出现（可选累计 ``interaction_count``，更新非空昵称与 ``last_seen``）。"""
        bid = str(bot_id or "").strip()
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not bid or not gid or not uid:
            return
        now = float(seen_at if seen_at is not None else time.time())
        nick = str(nickname or "").strip()[:120]
        inc = max(0, int(increment))
        with self.cm.write_transaction() as tx:
            tx.execute(
                """INSERT INTO person_scene_stats
                       (bot_id, group_id, user_id, nickname, interaction_count, first_seen, last_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, group_id, user_id) DO UPDATE SET
                       nickname = CASE WHEN excluded.nickname <> '' THEN excluded.nickname ELSE person_scene_stats.nickname END,
                       interaction_count = person_scene_stats.interaction_count + ?,
                       first_seen = CASE WHEN person_scene_stats.first_seen <= 0 THEN excluded.first_seen ELSE MIN(person_scene_stats.first_seen, excluded.first_seen) END,
                       last_seen = MAX(person_scene_stats.last_seen, excluded.last_seen),
                       updated_at = ?""",
                (bid, gid, uid, nick, inc, now, now, now, inc, now),
            )

    def get(self, *, bot_id: str, group_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            """SELECT bot_id, group_id, user_id, nickname, interaction_count, first_seen, last_seen, updated_at
                 FROM person_scene_stats
                WHERE bot_id=? AND group_id=? AND user_id=?
                LIMIT 1""",
            (str(bot_id or ""), str(group_id or ""), str(user_id or "")),
        ).fetchone()
        if not row:
            return None
        return {
            "bot_id": str(row[0]),
            "group_id": str(row[1]),
            "user_id": str(row[2]),
            "nickname": str(row[3] or ""),
            "interaction_count": int(row[4] or 0),
            "first_seen": float(row[5] or 0.0),
            "last_seen": float(row[6] or 0.0),
            "updated_at": float(row[7] or 0.0),
        }

    def list_group(self, *, bot_id: str, group_id: str) -> list[dict[str, Any]]:
        rows = self.cm.execute_read(
            """SELECT user_id, nickname, interaction_count, first_seen, last_seen
                 FROM person_scene_stats
                WHERE bot_id=? AND group_id=?
                ORDER BY last_seen DESC, user_id ASC""",
            (str(bot_id or ""), str(group_id or "")),
        ).fetchall()
        return [
            {
                "user_id": str(r[0]),
                "group_id": str(group_id or ""),
                "bot_id": str(bot_id or ""),
                "nickname": str(r[1] or ""),
                "interaction_count": int(r[2] or 0),
                "first_seen": float(r[3] or 0.0),
                "last_seen": float(r[4] or 0.0),
            }
            for r in rows
            if r[0]
        ]

    def sum_interactions(
        self,
        *,
        bot_id: str,
        user_ids: Sequence[str],
        exclude_private: bool = True,
    ) -> int:
        ids = [str(x).strip() for x in user_ids if str(x or "").strip()]
        if not ids:
            return 0
        extra = " AND group_id NOT LIKE 'private:%'" if exclude_private else ""
        row = self.cm.execute_read(
            f"""SELECT COALESCE(SUM(interaction_count), 0)
                  FROM person_scene_stats
                 WHERE bot_id=? AND user_id IN ({_marks(ids)}){extra}""",
            (str(bot_id or ""), *ids),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def latest_nickname(
        self,
        *,
        bot_id: str,
        user_ids: Sequence[str],
        exclude_private: bool = True,
    ) -> str:
        ids = [str(x).strip() for x in user_ids if str(x or "").strip()]
        if not ids:
            return ""
        extra = " AND group_id NOT LIKE 'private:%'" if exclude_private else ""
        row = self.cm.execute_read(
            f"""SELECT nickname
                  FROM person_scene_stats
                 WHERE bot_id=? AND user_id IN ({_marks(ids)}){extra}
                   AND COALESCE(nickname, '') <> ''
                 ORDER BY last_seen DESC
                 LIMIT 1""",
            (str(bot_id or ""), *ids),
        ).fetchone()
        return str(row[0] or "").strip() if row and row[0] else ""

    def migrate_from_user_profiles(self) -> int:
        """一次性幂等从遗留 ``user_profiles`` 导入现有花名册与互动统计（不存在旧表则返回 0）。"""
        exists = self.cm.execute_read(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_profiles'"
        ).fetchone()
        if not exists:
            return 0
        now = time.time()
        with self.cm.write_transaction() as tx:
            cur = tx.execute(
                """INSERT INTO person_scene_stats
                       (bot_id, group_id, user_id, nickname, interaction_count, first_seen, last_seen, updated_at)
                   SELECT
                       COALESCE(NULLIF(TRIM(bot_id), ''), 'yushu'),
                       COALESCE(group_id, ''),
                       TRIM(user_id),
                       COALESCE(MAX(nickname), ''),
                       COALESCE(MAX(interaction_count), 0),
                       COALESCE(MIN(NULLIF(first_seen, 0)), 0),
                       COALESCE(MAX(last_seen), 0),
                       ?
                   FROM user_profiles
                   WHERE COALESCE(TRIM(user_id), '') <> ''
                   GROUP BY COALESCE(NULLIF(TRIM(bot_id), ''), 'yushu'), COALESCE(group_id, ''), TRIM(user_id)
                   ON CONFLICT(bot_id, group_id, user_id) DO UPDATE SET
                       nickname = CASE WHEN person_scene_stats.nickname = '' THEN excluded.nickname ELSE person_scene_stats.nickname END,
                       interaction_count = MAX(person_scene_stats.interaction_count, excluded.interaction_count),
                       first_seen = CASE WHEN person_scene_stats.first_seen <= 0 THEN excluded.first_seen ELSE MIN(person_scene_stats.first_seen, excluded.first_seen) END,
                       last_seen = MAX(person_scene_stats.last_seen, excluded.last_seen),
                       updated_at = ?""",
                (now, now),
            )
            return int(cur.rowcount or 0)


__all__ = ["PersonSceneStatsRepository"]
