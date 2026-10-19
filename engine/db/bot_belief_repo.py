"""信念 v2 仓储：Bot 本人的看法、状态变化历史与反思进度（自有小表，不经 DomainCommand）。

写入全部经 ``apply``：一次反思或一次管理台操作里的所有看法改动、历史事件与反思进度在同一个
事务里提交；带 ``expected_revision`` 的改动在版本对不上时整体回滚并抛 ``BeliefRevisionConflict``。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .connection import ConnectionManager
from .migrations.bot_beliefs import ensure_bot_beliefs_schema

_JSON_LIST_COLUMNS = frozenset({"triggers", "support_dates", "session_ids"})
_WRITABLE_COLUMNS = frozenset({
    "content", "category", "status", "strength", "triggers", "support_dates", "session_ids",
    "confirm_runs", "challenge_count", "challenge_streak", "first_seen_at", "last_confirmed_at",
    "last_challenged_at", "last_decay_at", "status_changed_at",
})
_BELIEF_COLUMNS = (
    "id, bot_id, content, category, status, strength, triggers, support_dates, session_ids, "
    "confirm_runs, challenge_count, challenge_streak, first_seen_at, last_confirmed_at, "
    "last_challenged_at, last_decay_at, status_changed_at, revision, created_at, updated_at"
)
_EVENT_COLUMNS = (
    "id, bot_id, belief_id, event_type, from_status, to_status, strength_before, strength_after, "
    "reason, evidence, run_id, actor, occurred_at"
)
_RUN_COLUMNS = (
    "id, bot_id, run_date, status, attempts, diary_cursor, diary_watermark, diary_watermark_id, diary_ids, " \
    "counts, error, started_at, finished_at"
)


class BeliefRevisionConflict(ValueError):
    """看法已被别处改过（版本号对不上）；本次改动整体未写入。"""

    code = "belief_revision_conflict"
    reason_code = "belief_revision_conflict"

    def __init__(self, belief_id: int) -> None:
        self.belief_id = int(belief_id)
        super().__init__(self.code)


@dataclass
class BeliefWrite:
    """一条看法的新建（``belief_id is None``）或更新，连同要追加的历史事件。"""

    belief_id: int | None
    fields: dict[str, Any]
    expected_revision: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def _loads_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _loads_dict(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _belief_row(row: Sequence[Any]) -> dict[str, Any]:
    names = [name.strip() for name in _BELIEF_COLUMNS.split(",")]
    item = dict(zip(names, row))
    for column in _JSON_LIST_COLUMNS:
        item[column] = _loads_list(item.get(column))
    return item


def _event_row(row: Sequence[Any]) -> dict[str, Any]:
    names = [name.strip() for name in _EVENT_COLUMNS.split(",")]
    item = dict(zip(names, row))
    item["evidence"] = _loads_list(item.get("evidence"))
    return item


def _run_row(row: Sequence[Any]) -> dict[str, Any]:
    names = [name.strip() for name in _RUN_COLUMNS.split(",")]
    item = dict(zip(names, row))
    item["diary_ids"] = _loads_list(item.get("diary_ids"))
    item["counts"] = _loads_dict(item.get("counts"))
    return item


def _encode(column: str, value: Any) -> Any:
    if column in _JSON_LIST_COLUMNS:
        return _dumps(list(value or []))
    return value


class BotBeliefRepository:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True):
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm
        if ensure_schema:
            ensure_bot_beliefs_schema(cm)

    # ─── 读 ───

    def list_beliefs(
        self, bot_id: str, *, statuses: Iterable[str] | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        bot = str(bot_id or "").strip()
        if not bot:
            return []
        params: list[Any] = [bot]
        where = "bot_id=?"
        wanted = [str(s) for s in (statuses or []) if str(s)]
        if statuses is not None:
            if not wanted:
                return []
            where += f" AND status IN ({','.join('?' for _ in wanted)})"
            params.extend(wanted)
        rows = self.cm.execute_read(
            f"SELECT {_BELIEF_COLUMNS} FROM bot_beliefs WHERE {where} "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 5000))),
        ).fetchall()
        return [_belief_row(row) for row in rows]

    def get_belief(self, bot_id: str, belief_id: int) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            f"SELECT {_BELIEF_COLUMNS} FROM bot_beliefs WHERE id=? AND bot_id=?",
            (int(belief_id), str(bot_id or "")),
        ).fetchone()
        return _belief_row(row) if row else None

    def list_events(
        self,
        bot_id: str,
        *,
        belief_ids: Iterable[int] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [str(bot_id or "")]
        where = "bot_id=?"
        if belief_ids is not None:
            ids = sorted({int(value) for value in belief_ids})
            if not ids:
                return []
            where += f" AND belief_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        rows = self.cm.execute_read(
            f"SELECT {_EVENT_COLUMNS} FROM bot_belief_events WHERE {where} "
            "ORDER BY occurred_at ASC, id ASC LIMIT ?",
            (*params, max(1, min(int(limit), 10000))),
        ).fetchall()
        return [_event_row(row) for row in rows]

    def list_recent_changes(self, bot_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """最近的状态变化（新萌芽、形成、稳固、动摇、淡出、否决、恢复），附看法原文。"""
        rows = self.cm.execute_read(
            """SELECT e.id, e.bot_id, e.belief_id, e.event_type, e.from_status, e.to_status,
                       e.strength_before, e.strength_after, e.reason, e.evidence, e.run_id, e.actor,
                       e.occurred_at, b.content, b.category, b.status
                  FROM bot_belief_events e JOIN bot_beliefs b ON b.id=e.belief_id AND b.bot_id=e.bot_id
                 WHERE e.bot_id=? AND e.from_status != e.to_status
                 ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?""",
            (str(bot_id or ""), max(1, min(int(limit), 100))),
        ).fetchall()
        items = []
        for row in rows:
            item = _event_row(row[:13])
            item.update({"content": row[13], "category": row[14], "current_status": row[15]})
            items.append(item)
        return items

    def list_bot_ids(self) -> list[str]:
        rows = self.cm.execute_read("SELECT DISTINCT bot_id FROM bot_beliefs ORDER BY bot_id").fetchall()
        return [str(row[0]) for row in rows if row[0]]

    def last_succeeded_run(self, bot_id: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            f"SELECT {_RUN_COLUMNS} FROM bot_belief_reflection_runs WHERE bot_id=? AND status='succeeded' "
            "ORDER BY run_date DESC, id DESC LIMIT 1",
            (str(bot_id or ""),),
        ).fetchone()
        return _run_row(row) if row else None

    def diary_cursor(self, bot_id: str) -> int:
        """已读日记的最大 id（旧口径，仅供管理台展示；判重不再用它）。"""
        row = self.cm.execute_read(
            "SELECT COALESCE(MAX(diary_cursor), 0) FROM bot_belief_reflection_runs WHERE bot_id=? AND status='succeeded'",
            (str(bot_id or ""),),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def diary_watermark(self, bot_id: str) -> tuple[float, int]:
        """已纳入反思的日记位置 ``(created_at, id)``；该位置及之前的日记都已读过。

        用 ``created_at`` 而不是 ``id`` 判重，是为了不受「补记历史日记」影响：
        补记出来的时间线行 ``occurred_at`` 在过去、``id`` 却是最新的，
        按 id 推进的游标会把这类行当成「已经读过的更老的行」永久跳过。
        同一天多个群的日记共用同一个写入时刻（23:30），所以还要用 ``id`` 定位同刻内的边界。
        """
        row = self.cm.execute_read(
            "SELECT COALESCE(MAX(diary_watermark), 0), COALESCE(MAX(diary_watermark_id), 0) "
            "FROM bot_belief_reflection_runs WHERE bot_id=? AND status='succeeded'",
            (str(bot_id or ""),),
        ).fetchone()
        if not row:
            return 0.0, 0
        return float(row[0] or 0.0), int(row[1] or 0)

    def get_run(self, bot_id: str, run_date: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            f"SELECT {_RUN_COLUMNS} FROM bot_belief_reflection_runs WHERE bot_id=? AND run_date=?",
            (str(bot_id or ""), str(run_date)),
        ).fetchone()
        return _run_row(row) if row else None

    def list_runs(self, bot_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.cm.execute_read(
            f"SELECT {_RUN_COLUMNS} FROM bot_belief_reflection_runs WHERE bot_id=? "
            "ORDER BY run_date DESC, id DESC LIMIT ?",
            (str(bot_id or ""), max(1, min(int(limit), 100))),
        ).fetchall()
        return [_run_row(row) for row in rows]

    # ─── 反思进度 ───

    def claim_run(
        self,
        bot_id: str,
        run_date: str,
        *,
        now: float | None = None,
        stale_after: float = 1800.0,
        max_attempts: int = 3,
    ) -> int | None:
        """占用当天的反思：没有记录就新建；失败未超次数、或进行中已超时（进程中途退出）可以重来。"""
        stamp = float(now if now is not None else time.time())
        bot = str(bot_id or "").strip()
        if not bot:
            return None
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                "SELECT id, status, attempts, started_at FROM bot_belief_reflection_runs WHERE bot_id=? AND run_date=?",
                (bot, str(run_date)),
            ).fetchone()
            if row is None:
                cursor = tx.execute(
                    """INSERT INTO bot_belief_reflection_runs (bot_id, run_date, status, attempts, started_at)
                       VALUES (?, ?, 'running', 1, ?)""",
                    (bot, str(run_date), stamp),
                )
                return int(cursor.lastrowid)
            run_id, status, attempts, started_at = row
            if status == "succeeded":
                return None
            if status == "running" and stamp - float(started_at or 0.0) < float(stale_after):
                return None
            if int(attempts or 0) >= int(max_attempts):
                return None
            tx.execute(
                """UPDATE bot_belief_reflection_runs
                      SET status='running', attempts=attempts+1, started_at=?, error='', finished_at=NULL
                    WHERE id=?""",
                (stamp, int(run_id)),
            )
            return int(run_id)

    def fail_run(self, run_id: int, *, error: str, now: float | None = None) -> None:
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            tx.execute(
                "UPDATE bot_belief_reflection_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                (str(error or "")[:300], stamp, int(run_id)),
            )

    # ─── 写 ───

    def apply(
        self,
        bot_id: str,
        writes: Sequence[BeliefWrite],
        *,
        finish_run: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> list[int]:
        """在一个事务里写入全部改动；返回每条改动对应的看法 id。"""
        stamp = float(now if now is not None else time.time())
        bot = str(bot_id or "").strip()
        if not bot:
            raise ValueError("bot_id_required")
        ids: list[int] = []
        with self.cm.write_transaction() as tx:
            for write in writes:
                unknown = set(write.fields) - _WRITABLE_COLUMNS
                if unknown:
                    raise ValueError(f"unknown_belief_columns:{sorted(unknown)}")
                if write.belief_id is None:
                    fields = dict(write.fields)
                    if not str(fields.get("content") or "").strip():
                        raise ValueError("belief_content_required")
                    fields.setdefault("first_seen_at", stamp)
                    fields.setdefault("status_changed_at", stamp)
                    columns = sorted(fields)
                    cursor = tx.execute(
                        f"""INSERT INTO bot_beliefs (bot_id, {', '.join(columns)}, revision, created_at, updated_at)
                            VALUES (?, {', '.join('?' for _ in columns)}, 1, ?, ?)""",
                        (bot, *[_encode(column, fields[column]) for column in columns], stamp, stamp),
                    )
                    belief_id = int(cursor.lastrowid)
                else:
                    belief_id = int(write.belief_id)
                    columns = sorted(write.fields)
                    assignments = "".join(f"{column}=?, " for column in columns)
                    sql = (
                        f"UPDATE bot_beliefs SET {assignments}revision=revision+1, updated_at=? "
                        "WHERE id=? AND bot_id=?"
                    )
                    params: list[Any] = [*[_encode(c, write.fields[c]) for c in columns], stamp, belief_id, bot]
                    if write.expected_revision is not None:
                        sql += " AND revision=?"
                        params.append(int(write.expected_revision))
                    cursor = tx.execute(sql, params)
                    if cursor.rowcount == 0:
                        raise BeliefRevisionConflict(belief_id)
                for event in write.events:
                    tx.execute(
                        """INSERT INTO bot_belief_events (bot_id, belief_id, event_type, from_status, to_status,
                               strength_before, strength_after, reason, evidence, run_id, actor, occurred_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            bot, belief_id, str(event.get("event_type") or ""),
                            str(event.get("from_status") or ""), str(event.get("to_status") or ""),
                            event.get("strength_before"), event.get("strength_after"),
                            str(event.get("reason") or "")[:500], _dumps(list(event.get("evidence") or [])),
                            event.get("run_id"), str(event.get("actor") or "reflection"),
                            float(event.get("occurred_at") or stamp),
                        ),
                    )
                ids.append(belief_id)
            if finish_run is not None:
                tx.execute(
                    """UPDATE bot_belief_reflection_runs
                          SET status='succeeded', diary_cursor=?, diary_watermark=?, diary_watermark_id=?,
                              diary_ids=?, counts=?, error='', finished_at=?
                        WHERE id=? AND bot_id=?""",
                    (
                        int(finish_run.get("diary_cursor") or 0),
                        float(finish_run.get("diary_watermark") or 0.0),
                        int(finish_run.get("diary_watermark_id") or 0),
                        _dumps(list(finish_run.get("diary_ids") or [])),
                        _dumps(dict(finish_run.get("counts") or {})), stamp, int(finish_run["run_id"]), bot,
                    ),
                )
        return ids


__all__ = ["BeliefRevisionConflict", "BeliefWrite", "BotBeliefRepository"]
