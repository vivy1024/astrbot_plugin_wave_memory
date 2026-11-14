"""Bot 身体仓储：一个 Bot 一副激素状态 + 带来源的身体事件流水（自有小表，不经 DomainCommand）。

- ``bot_body_state``：当前激素向量与时间锚点。时间推进由服务层按需现算，这里只存最近一次结果。
- ``bot_body_events``：每次身体事件（被夸、被凶、重逢……）的实际增量与来源；``source_ref`` 非空时幂等，
  同一个关系事件不会重复作用到身体上。同时作为「习惯化」的计数来源。
- ``bot_body_person_seen``：每个人最后一次出现的时间（跨所有场合），用于「隔了很久才再来」。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from .connection import ConnectionManager
from .migrations.bot_body import ensure_bot_body_schema

EVENT_RETENTION_SECONDS = 30 * 24 * 3600.0


def _loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class BotBodyRepository:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True):
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm
        if ensure_schema:
            ensure_bot_body_schema(cm)

    # ─── 状态 ───

    def load_state(self, bot_id: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            """SELECT hormones, last_tick_at, last_active_at, last_human_at, offline_since,
                      edge_memo, revision, updated_at
                 FROM bot_body_state WHERE bot_id=?""",
            (str(bot_id),),
        ).fetchone()
        if not row:
            return None
        return {
            "hormones": {k: float(v) for k, v in _loads(row[0]).items() if isinstance(v, (int, float))},
            "last_tick_at": float(row[1] or 0.0),
            "last_active_at": float(row[2] or 0.0),
            "last_human_at": float(row[3] or 0.0),
            "offline_since": float(row[4] or 0.0),
            "edge_memo": {k: float(v) for k, v in _loads(row[5]).items() if isinstance(v, (int, float))},
            "revision": int(row[6] or 0),
            "updated_at": float(row[7] or 0.0),
        }

    def save_state(
        self,
        bot_id: str,
        *,
        hormones: Mapping[str, float],
        last_tick_at: float,
        last_active_at: float | None = None,
        last_human_at: float | None = None,
        offline_since: float | None = None,
        edge_memo: Mapping[str, float] | None = None,
        now: float | None = None,
    ) -> None:
        stamp = float(now if now is not None else time.time())
        current = self.load_state(bot_id) or {}
        payload = (
            str(bot_id),
            json.dumps({k: round(float(v), 3) for k, v in hormones.items()}, ensure_ascii=False, sort_keys=True),
            float(last_tick_at),
            float(last_active_at if last_active_at is not None else current.get("last_active_at", 0.0)),
            float(last_human_at if last_human_at is not None else current.get("last_human_at", 0.0)),
            float(offline_since if offline_since is not None else current.get("offline_since", 0.0)),
            json.dumps(dict(edge_memo if edge_memo is not None else current.get("edge_memo", {})), sort_keys=True),
            stamp,
        )
        self.cm.execute_write(
            """INSERT INTO bot_body_state(bot_id, hormones, last_tick_at, last_active_at, last_human_at,
                                          offline_since, edge_memo, revision, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(bot_id) DO UPDATE SET
                   hormones=excluded.hormones, last_tick_at=excluded.last_tick_at,
                   last_active_at=excluded.last_active_at, last_human_at=excluded.last_human_at,
                   offline_since=excluded.offline_since, edge_memo=excluded.edge_memo,
                   revision=bot_body_state.revision + 1, updated_at=excluded.updated_at""",
            payload,
        )
        self.cm.commit()

    def mark_offline(self, bot_id: str, *, at: float | None = None) -> None:
        """关闭时记下离线起点；下次启动时据此从推进时长里扣掉离线这段。"""
        stamp = float(at if at is not None else time.time())
        self.cm.execute_write(
            "UPDATE bot_body_state SET offline_since=?, updated_at=? WHERE bot_id=?",
            (stamp, stamp, str(bot_id)),
        )
        self.cm.commit()

    # ─── 事件 ───

    def add_event(
        self,
        bot_id: str,
        *,
        kind: str,
        deltas: Mapping[str, float],
        principal_id: str = "",
        scene: str = "",
        source_ref: str = "",
        created_at: float | None = None,
    ) -> int | None:
        """写一条身体事件；``source_ref`` 重复时返回 None（幂等）。"""
        stamp = float(created_at if created_at is not None else time.time())
        cur = self.cm.execute_write(
            """INSERT OR IGNORE INTO bot_body_events(bot_id, kind, principal_id, deltas, scene, source_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(bot_id), str(kind), str(principal_id or ""), json.dumps(dict(deltas), sort_keys=True),
             str(scene or ""), str(source_ref or ""), stamp),
        )
        self.cm.commit()
        rowid = int(getattr(cur, "lastrowid", 0) or 0)
        return rowid if rowid and getattr(cur, "rowcount", 1) != 0 else None

    def count_recent(self, bot_id: str, *, kind: str, principal_id: str, since: float) -> int:
        row = self.cm.execute_read(
            """SELECT COUNT(*) FROM bot_body_events
                WHERE bot_id=? AND kind=? AND principal_id=? AND created_at>=?""",
            (str(bot_id), str(kind), str(principal_id or ""), float(since)),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def recent_events(self, bot_id: str, *, since: float, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.cm.execute_read(
            """SELECT id, kind, principal_id, deltas, scene, created_at FROM bot_body_events
                WHERE bot_id=? AND created_at>=? ORDER BY created_at ASC LIMIT ?""",
            (str(bot_id), float(since), max(1, int(limit))),
        ).fetchall()
        return [
            {"id": int(r[0]), "kind": str(r[1]), "principal_id": str(r[2] or ""),
             "deltas": _loads(r[3]), "scene": str(r[4] or ""), "created_at": float(r[5])}
            for r in rows
        ]

    def prune(self, bot_id: str, *, before: float, limit: int = 2000) -> int:
        cur = self.cm.execute_write(
            """DELETE FROM bot_body_events WHERE id IN (
                   SELECT id FROM bot_body_events WHERE bot_id=? AND created_at<? LIMIT ?)""",
            (str(bot_id), float(before), int(limit)),
        )
        self.cm.commit()
        return int(getattr(cur, "rowcount", 0) or 0)

    # ─── 谁什么时候来过 ───

    def last_seen(self, bot_id: str, principal_id: str) -> float:
        row = self.cm.execute_read(
            "SELECT last_seen_at FROM bot_body_person_seen WHERE bot_id=? AND principal_id=?",
            (str(bot_id), str(principal_id)),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def touch_seen(self, bot_id: str, principal_id: str, *, at: float | None = None) -> float:
        """记下此人刚出现过，返回**上一次**出现的时间（0 表示第一次见）。"""
        stamp = float(at if at is not None else time.time())
        previous = self.last_seen(bot_id, principal_id)
        self.cm.execute_write(
            """INSERT INTO bot_body_person_seen(bot_id, principal_id, last_seen_at) VALUES (?, ?, ?)
               ON CONFLICT(bot_id, principal_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (str(bot_id), str(principal_id), stamp),
        )
        self.cm.commit()
        return previous


__all__ = ["BotBodyRepository"]
