"""Bot 心情 v2 仓储：一个 Bot 一份心情状态 + 情绪事件流水（自有小表，不经 DomainCommand）。

- ``bot_mood_events``：每条情绪事件带来源（source）、原因、涉及的人、发生场合（session/visibility）与时间；
  ``source_ref`` 非空时幂等（同一关系事件、同一次记账只记一次）。
- ``bot_mood_state``：最近一次合成的心情快照与关系事件游标，只用于观测与增量导入，
  真正的当前心情总是由事件流水按衰减现算（见 services/bot_mood.py）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

try:
    from ...domain.mood_policy import POLICY_VERSION, MoodEventView
except ImportError:  # pragma: no cover - top-level repository imports
    from domain.mood_policy import POLICY_VERSION, MoodEventView

from .connection import ConnectionManager
from .migrations.bot_mood import ensure_bot_mood_schema

_EVENT_COLUMNS = (
    "id, source, valence, arousal, intensity, created_at, cause, subject_name, "
    "session_id, visibility, subject_principal_id"
)


def _event(row: Any) -> MoodEventView:
    return MoodEventView(
        id=int(row[0]),
        source=str(row[1] or ""),
        valence=float(row[2] or 0.0),
        arousal=float(row[3] or 0.0),
        intensity=float(row[4] or 0.0),
        created_at=float(row[5] or 0.0),
        cause=str(row[6] or ""),
        subject_name=str(row[7] or ""),
        session_id=str(row[8] or ""),
        visibility=str(row[9] or "group"),
        subject_principal_id=str(row[10] or ""),
    )


class BotMoodRepository:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True):
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm
        if ensure_schema:
            ensure_bot_mood_schema(cm)

    # ─── 事件 ───

    def add_event(
        self,
        bot_id: str,
        *,
        source: str,
        valence: float,
        arousal: float,
        intensity: float = 0.0,
        cause: str = "",
        subject_principal_id: str = "",
        subject_name: str = "",
        session_id: str = "",
        visibility: str = "group",
        source_ref: str = "",
        evidence: Mapping[str, Any] | None = None,
        created_at: float | None = None,
    ) -> int | None:
        """写入一条情绪事件；``source_ref`` 重复时不写并返回 None。"""
        bot = str(bot_id or "").strip()
        if not bot:
            raise ValueError("bot_id_required")
        now = float(created_at or time.time())
        with self.cm.write_transaction() as tx:
            cursor = tx.execute(
                """INSERT OR IGNORE INTO bot_mood_events
                       (bot_id, source, valence, arousal, intensity, cause, subject_principal_id,
                        subject_name, session_id, visibility, source_ref, evidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bot, str(source or "unknown"), max(-1.0, min(1.0, float(valence))),
                    max(-1.0, min(1.0, float(arousal))), max(0.0, min(1.0, float(intensity or 0.0))),
                    str(cause or "")[:200], str(subject_principal_id or ""), str(subject_name or "")[:40],
                    str(session_id or ""), str(visibility or "group"), str(source_ref or ""),
                    json.dumps(dict(evidence or {}), ensure_ascii=False, sort_keys=True), now,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) <= 0:
                return None
            return int(cursor.lastrowid)

    def list_events(self, bot_id: str, *, since: float, limit: int = 400) -> list[MoodEventView]:
        rows = self.cm.execute_read(
            f"""SELECT {_EVENT_COLUMNS} FROM bot_mood_events
                 WHERE bot_id=? AND created_at>=?
                 ORDER BY created_at DESC LIMIT ?""",
            (str(bot_id), float(since), int(limit)),
        ).fetchall()
        return [_event(row) for row in rows]

    def last_subject_event(self, bot_id: str, *, subject_principal_id: str, source: str, since: float) -> MoodEventView | None:
        if not subject_principal_id:
            return None
        row = self.cm.execute_read(
            f"""SELECT {_EVENT_COLUMNS} FROM bot_mood_events
                 WHERE bot_id=? AND created_at>=? AND subject_principal_id=? AND source=?
                 ORDER BY created_at DESC LIMIT 1""",
            (str(bot_id), float(since), str(subject_principal_id), str(source)),
        ).fetchone()
        return _event(row) if row else None

    def count_by_source(self, bot_id: str, *, since: float) -> dict[str, int]:
        rows = self.cm.execute_read(
            """SELECT source, COUNT(*) FROM bot_mood_events
                WHERE bot_id=? AND created_at>=? GROUP BY source""",
            (str(bot_id), float(since)),
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def prune(self, bot_id: str, *, before: float) -> int:
        with self.cm.write_transaction() as tx:
            cursor = tx.execute(
                "DELETE FROM bot_mood_events WHERE bot_id=? AND created_at<?",
                (str(bot_id), float(before)),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

    # ─── 状态快照 ───

    def get_state(self, bot_id: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            """SELECT valence, arousal, fatigue, word, relationship_cursor, revision, policy_version, updated_at
                 FROM bot_mood_state WHERE bot_id=?""",
            (str(bot_id),),
        ).fetchone()
        if not row:
            return None
        return {
            "valence": float(row[0]), "arousal": float(row[1]), "fatigue": float(row[2] or 0.0),
            "word": str(row[3] or ""), "relationship_cursor": int(row[4] or 0),
            "revision": int(row[5] or 0), "policy_version": str(row[6] or ""), "updated_at": float(row[7] or 0.0),
        }

    def save_state(
        self,
        bot_id: str,
        *,
        valence: float,
        arousal: float,
        fatigue: float = 0.0,
        word: str = "",
        relationship_cursor: int | None = None,
        updated_at: float | None = None,
    ) -> int:
        now = float(updated_at or time.time())
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                "SELECT relationship_cursor, revision FROM bot_mood_state WHERE bot_id=?", (str(bot_id),)
            ).fetchone()
            cursor_value = int(relationship_cursor if relationship_cursor is not None else (row[0] if row else 0))
            revision = int(row[1] if row else 0) + 1
            tx.execute(
                """INSERT INTO bot_mood_state
                       (bot_id, valence, arousal, fatigue, word, relationship_cursor, revision, policy_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id) DO UPDATE SET
                       valence=excluded.valence, arousal=excluded.arousal, fatigue=excluded.fatigue,
                       word=excluded.word, relationship_cursor=excluded.relationship_cursor,
                       revision=excluded.revision, policy_version=excluded.policy_version,
                       updated_at=excluded.updated_at""",
                (str(bot_id), float(valence), float(arousal), float(fatigue), str(word or ""),
                 cursor_value, revision, POLICY_VERSION, now),
            )
            return revision

    # ─── 只读输入：关系事件、Bot 回复、昵称 ───

    def relationship_events_after(self, bot_id: str, *, cursor: int, since: float, limit: int = 200) -> list[dict[str, Any]]:
        try:
            rows = self.cm.execute_read(
                """SELECT id, session_id, visibility, subject_principal_id, event_type, delta, reason, created_at
                     FROM scoped_soul_relationship_events
                    WHERE id>? AND bot_id=? AND created_at>=?
                    ORDER BY id ASC LIMIT ?""",
                (int(cursor), str(bot_id), float(since), int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [
            {
                "id": int(row[0]), "session_id": str(row[1] or ""), "visibility": str(row[2] or "group"),
                "subject_principal_id": str(row[3] or ""), "event_type": str(row[4] or ""),
                "delta": float(row[5] or 0.0), "reason": str(row[6] or ""), "created_at": float(row[7] or 0.0),
            }
            for row in rows
        ]

    def max_relationship_event_id(self, bot_id: str) -> int:
        try:
            row = self.cm.execute_read(
                "SELECT COALESCE(MAX(id), 0) FROM scoped_soul_relationship_events WHERE bot_id=?",
                (str(bot_id),),
            ).fetchone()
        except Exception:
            return 0
        return int(row[0] or 0) if row else 0

    def bot_reply_timestamps(self, bot_id: str, *, since: float, limit: int = 2000) -> list[float]:
        """窗口内 Bot 自己说过的话的时间戳（只走 timestamp 索引，``+`` 禁用 sender/bot 索引）。"""
        try:
            rows = self.cm.execute_read(
                """SELECT timestamp FROM memories
                    WHERE timestamp>? AND +sender_id='bot' AND +bot_id=?
                    ORDER BY timestamp ASC LIMIT ?""",
                (float(since), str(bot_id), int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [float(row[0]) for row in rows if row[0] is not None]

    def display_name(self, bot_id: str, user_id: str) -> str:
        if not user_id:
            return ""
        try:
            row = self.cm.execute_read(
                """SELECT sender_name FROM memories
                    WHERE sender_id=? AND bot_id=? AND COALESCE(sender_name, '')<>''
                    ORDER BY id DESC LIMIT 1""",
                (str(user_id), str(bot_id)),
            ).fetchone()
        except Exception:
            return ""
        return str(row[0] or "").strip() if row else ""


__all__ = ["BotMoodRepository"]
