"""Experience episode service — records bot-lived events.

写入必须经 WriteCoordinator（唯一事务入口）；读取为只读路径，不要求写入器，
避免只读聚合器（Reflection Trigger、信念涌现）被写入门禁误伤。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .identity_safety import quarantine_episode_kwargs, is_identity_contamination


def _row_to_dict(row) -> dict[str, Any]:
    try:
        source_ids = json.loads(row[11] or "[]")
    except Exception:
        source_ids = []
    return {
        "id": row[0],
        "bot_id": row[1],
        "group_id": row[2],
        "user_id": row[3],
        "episode_type": row[4],
        "trigger_text": row[5],
        "bot_inner_thought": row[6],
        "bot_action": row[7],
        "bot_reply": row[8],
        "user_reaction": row[9],
        "outcome": row[10],
        "source_memory_ids": source_ids,
        "emotional_weight": row[12],
        "created_at": row[13],
    }


def fetch_recent_episodes(
    conn,
    *,
    bot_id: str,
    user_id: str | None = None,
    group_id: str | None = None,
    limit: int = 20,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """只读取最近经历；不构造写入器，因此不受 coordinator 门禁影响。"""
    conditions = ["bot_id = ?"]
    params: list[Any] = [bot_id]
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if group_id is not None:
        conditions.append("group_id = ?")
        params.append(group_id)
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(float(since))
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"""SELECT id, bot_id, group_id, user_id, episode_type, trigger_text,
                   bot_inner_thought, bot_action, bot_reply, user_reaction, outcome,
                   source_memory_ids, emotional_weight, created_at
            FROM experience_episodes
            WHERE {where}
            ORDER BY created_at DESC LIMIT ?""",
        params + [int(limit)],
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def fetch_last_bot_reply(
    conn, *, bot_id: str, group_id: str, user_id: str | None = None
) -> str:
    """只读取该 bot 在本群最近一次回复。

    优先取与当前用户关联的回复，再退回 bot 在群内的最近回复。刻意不读
    memories.sender_id='bot'，那会把不同 bot 身份的回复混在一起。
    """
    bot_id = (bot_id or "").strip()
    group_id = (group_id or "").strip()
    user_id = (user_id or "").strip() if user_id is not None else ""
    if not bot_id or not group_id:
        return ""

    if user_id:
        row = conn.execute(
            """SELECT bot_reply FROM experience_episodes
               WHERE bot_id=? AND group_id=? AND user_id=?
                 AND episode_type='bot_reply' AND COALESCE(bot_reply, '') != ''
                 AND COALESCE(outcome, '') != 'quarantined_roleplay'
               ORDER BY created_at DESC LIMIT 1""",
            (bot_id, group_id, user_id),
        ).fetchone()
        if row and row[0] and not is_identity_contamination(row[0]):
            return row[0]

    row = conn.execute(
        """SELECT bot_reply FROM experience_episodes
           WHERE bot_id=? AND group_id=?
             AND episode_type='bot_reply' AND COALESCE(bot_reply, '') != ''
             AND COALESCE(outcome, '') != 'quarantined_roleplay'
           ORDER BY created_at DESC LIMIT 1""",
        (bot_id, group_id),
    ).fetchone()
    return row[0] if row and row[0] and not is_identity_contamination(row[0]) else ""


class ExperienceEpisodeService:
    """experience_episodes 的小型读写辅助。

    ``record_episode`` 是唯一写入口，必须持有 coordinator；缺少写入器时
    fail closed，绝不退化成裸 ``conn.execute + commit``。
    """

    _EPISODE_SQL = """INSERT INTO experience_episodes
               (bot_id, group_id, user_id, episode_type, trigger_text, bot_inner_thought,
                bot_action, bot_reply, user_reaction, outcome, source_memory_ids,
                emotional_weight, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

    def __init__(self, conn, coordinator=None):
        self.conn = conn
        self.coordinator = coordinator

    def _require_coordinator(self):
        coordinator = self.coordinator
        if coordinator is None or not callable(getattr(coordinator, "transaction_blocking", None)):
            raise ValueError("episode_writer_unavailable")
        return coordinator

    def record_episode(
        self,
        *,
        bot_id: str,
        group_id: str,
        episode_type: str,
        user_id: str | None = None,
        trigger_text: str | None = None,
        bot_inner_thought: str | None = None,
        bot_action: str | None = None,
        bot_reply: str | None = None,
        user_reaction: str | None = None,
        outcome: str | None = None,
        source_memory_ids: list[int] | None = None,
        emotional_weight: float = 0,
        created_at: float | None = None,
    ) -> int:
        coordinator = self._require_coordinator()
        bot_id = (bot_id or "").strip()
        group_id = (group_id or "").strip()
        episode_type = (episode_type or "").strip()
        if not bot_id or not group_id or not episode_type:
            raise ValueError("bot_id, group_id and episode_type are required")

        payload = quarantine_episode_kwargs({
            "trigger_text": trigger_text,
            "bot_inner_thought": bot_inner_thought,
            "bot_action": bot_action,
            "bot_reply": bot_reply,
            "user_reaction": user_reaction,
            "outcome": outcome,
            "emotional_weight": emotional_weight,
        })
        now = float(created_at or time.time())
        values = (
            bot_id,
            group_id,
            user_id,
            episode_type,
            payload.get("trigger_text"),
            payload.get("bot_inner_thought"),
            payload.get("bot_action"),
            payload.get("bot_reply"),
            payload.get("user_reaction"),
            payload.get("outcome"),
            json.dumps(source_memory_ids or [], ensure_ascii=False),
            float(payload.get("emotional_weight") or 0),
            now,
        )

        def persist(connection):
            cur = connection.execute(self._EPISODE_SQL, values)
            return int(getattr(cur, "lastrowid", 0) or 0)

        return int(coordinator.transaction_blocking(persist))

    def recent_episodes(
        self,
        *,
        bot_id: str,
        user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 20,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        return fetch_recent_episodes(
            self.conn,
            bot_id=bot_id,
            user_id=user_id,
            group_id=group_id,
            limit=limit,
            since=since,
        )

    def last_bot_reply(self, *, bot_id: str, group_id: str, user_id: str | None = None) -> str:
        return fetch_last_bot_reply(
            self.conn, bot_id=bot_id, group_id=group_id, user_id=user_id
        )

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        return _row_to_dict(row)


__all__ = [
    "ExperienceEpisodeService",
    "fetch_last_bot_reply",
    "fetch_recent_episodes",
]
