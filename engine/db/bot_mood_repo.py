"""Bot 心情 v2 仓储：一个 Bot 一份心情状态 + 情绪事件流水（自有小表，不经 DomainCommand）。

- ``bot_mood_events``：每条情绪事件带来源（source）、原因、涉及的人、发生场合（session/visibility）与时间；
  ``source_ref`` 非空时幂等（同一关系事件、同一次记账只记一次）。
- ``bot_mood_state``：最近一次合成的心情快照与关系事件游标，只用于观测与增量导入，
  真正的当前心情总是由事件流水按衰减现算（见 services/bot_mood.py）。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator, Mapping
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


class MoodReadBudgetExceeded(RuntimeError):
    """超出单次合成预算；不能拿不完整流水冒充完整心情。"""


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
        merge_seconds: float = 0.0,
        relationship_origin: Mapping[str, Any] | None = None,
    ) -> int | None:
        """写入一条情绪事件；``source_ref`` 重复时不写并返回 None。"""
        bot = str(bot_id or "").strip()
        if not bot:
            raise ValueError("bot_id_required")
        now = float(created_at if created_at is not None else time.time())
        valence, arousal, intensity = float(valence), float(arousal), float(intensity)
        if not all(math.isfinite(value) for value in (now, valence, arousal, intensity)):
            raise ValueError("mood_event_must_be_finite")
        values = (
            bot, str(source or "unknown"), max(-1.0, min(1.0, valence)),
            max(-1.0, min(1.0, arousal)), max(0.0, min(1.0, intensity)),
            str(cause or "")[:200], str(subject_principal_id or ""), str(subject_name or "")[:40],
            str(session_id or ""), str(visibility or "group"), str(source_ref or ""),
            json.dumps(dict(evidence or {}), ensure_ascii=False, sort_keys=True), now,
        )
        with self.cm.write_transaction() as tx:
            if relationship_origin:
                # 升级前以关系行 ID 导入的记录保持原样；新维度不能再导入同一源事件。
                column = "source_memory_id" if relationship_origin.get("source_memory_id") is not None else "operation_id"
                origin = relationship_origin.get(column)
                if origin is not None and tx.execute(
                    f"""SELECT 1 FROM scoped_soul_relationship_events r
                        JOIN bot_mood_events m ON m.bot_id=r.bot_id
                          AND m.source_ref='relationship_event:' || r.id
                        WHERE r.bot_id=? AND r.session_id=? AND r.visibility=?
                          AND r.subject_principal_id=? AND r.event_type=? AND r.{column}=? LIMIT 1""",
                    (bot, session_id, visibility, subject_principal_id, relationship_origin["event_type"], origin),
                ).fetchone():
                    return None
            # 有来源键的事件逐条幂等；窗口合并只用于没有来源键的原话冲击。
            # 不能覆盖带键事件，否则重放旧来源时会再写一次，或绕过 INSERT 的唯一约束。
            if merge_seconds > 0 and subject_principal_id and not source_ref:
                # 查找与替换处于同一个写事务；不同场合不相互覆盖原因与私密属性。
                last = tx.execute(
                    """SELECT id, valence FROM bot_mood_events
                       WHERE bot_id=? AND source=? AND subject_principal_id=? AND source_ref=''
                         AND session_id=? AND visibility=? AND created_at BETWEEN ? AND ?
                         AND (valence>=0)=? ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (bot, source, subject_principal_id, session_id, visibility,
                     now - merge_seconds, now, valence >= 0),
                ).fetchone()
                if last is not None:
                    if abs(last[1]) >= abs(values[2]):
                        return None
                    tx.execute(
                        """UPDATE bot_mood_events SET source=?, valence=?, arousal=?, intensity=?, cause=?,
                           subject_principal_id=?, subject_name=?, session_id=?, visibility=?, source_ref=?,
                           evidence=?, created_at=? WHERE bot_id=? AND id=?""",
                        (*values[1:], bot, last[0]),
                    )
                    return int(last[0])
            cursor = tx.execute(
                """INSERT OR IGNORE INTO bot_mood_events
                       (bot_id, source, valence, arousal, intensity, cause, subject_principal_id,
                        subject_name, session_id, visibility, source_ref, evidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
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

    def iter_events(
        self, bot_id: str, *, since: float, until: float, page_size: int = 256, max_pages: int = 128,
    ) -> Iterator[MoodEventView]:
        """固定 ID 上界、时间窗口、每页和总页数；键集分页不会漏掉同时间戳事件。"""
        page_size = max(1, min(1024, int(page_size)))
        max_pages = max(1, min(128, int(max_pages)))
        high_id = self.cm.execute_read("SELECT COALESCE(MAX(id), 0) FROM bot_mood_events").fetchone()[0]
        cursor: tuple[float, int] | None = None
        for page in range(max_pages + 1):
            after = " AND (created_at, id) < (?, ?)" if cursor else ""
            rows = self.cm.execute_read(
                f"""SELECT {_EVENT_COLUMNS} FROM bot_mood_events
                    WHERE bot_id=? AND created_at>=? AND created_at<=? AND id<=?{after}
                    ORDER BY created_at DESC, id DESC LIMIT ?""",
                (str(bot_id), float(since), float(until), high_id, *(cursor or ()),
                 page_size if page < max_pages else 1),
            ).fetchall()
            if page == max_pages and rows:
                raise MoodReadBudgetExceeded("mood_event_read_budget_exceeded")
            for row in rows:
                yield _event(row)
            if len(rows) < page_size:
                return
            cursor = (float(rows[-1][5]), int(rows[-1][0]))

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

    def prune(self, bot_id: str, *, before: float, limit: int = 512) -> int:
        with self.cm.write_transaction() as tx:
            cursor = tx.execute(
                """DELETE FROM bot_mood_events WHERE id IN (
                     SELECT id FROM bot_mood_events WHERE bot_id=? AND created_at<?
                     ORDER BY created_at ASC LIMIT ?)""",
                (str(bot_id), float(before), max(1, min(1024, int(limit)))),
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
                """SELECT id, session_id, visibility, subject_principal_id, event_type, delta, reason, created_at,
                          source_memory_id, operation_id
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
                "source_memory_id": row[8], "operation_id": row[9],
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

    def bot_reply_timestamps(self, bot_id: str, *, since: float, limit: int = 2000, until: float | None = None) -> list[float]:
        """窗口内 Bot 自己说过的话的时间戳（只走 timestamp 索引，``+`` 禁用 sender/bot 索引）。"""
        try:
            rows = self.cm.execute_read(
                """SELECT timestamp FROM memories
                    WHERE timestamp>=? AND timestamp<=? AND +sender_id='bot' AND +bot_id=?
                    ORDER BY timestamp DESC LIMIT ?""",
                (float(since), float(until if until is not None else time.time()), str(bot_id),
                 max(1, min(20000, int(limit)))),
            ).fetchall()
        except Exception:
            return []
        return [float(row[0]) for row in reversed(rows) if row[0] is not None]

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


__all__ = ["BotMoodRepository", "MoodReadBudgetExceeded"]
