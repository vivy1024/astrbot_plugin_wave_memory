"""关系小传仓储：按 (bot_id, user_id) 保存 Bot 为每个人写的关系小传（自有小表，不经 DomainCommand）。

- ``version`` 做乐观锁：写入时带上读到的版本号，不一致就拒绝（管理台两处同时编辑、后台生成期间
  管理员改过），与 Bot Profile 的写法相同；新建按 0。
- 每次正文被替换，旧正文写进 ``person_relationship_portrait_history``，每人只留最近
  ``HISTORY_KEEP_PER_PERSON`` 条。
- ``status``：``auto`` 为模型生成；``edited`` 为管理员手改过，后台不会自动覆盖，只标记
  ``pending_update``（有新记录待更新），由管理员选择重新生成或保留。
- ``reviewed_through_event_id`` / ``reviewed_at`` 是「上次小传之后」的判定起点：自动生成时等于依据的
  最大事件编号与生成时间；管理员选择「保留」时推进到当时的最新事件。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from .connection import ConnectionManager
from .migrations.relationship_portrait import ensure_relationship_portrait_schema

HISTORY_KEEP_PER_PERSON = 10
PORTRAIT_STATUSES = frozenset({"auto", "edited"})

_COLUMNS = (
    "bot_id, user_id, content, version, status, source_max_event_id, source_event_count, provider, "
    "edited_by, generated_at, reviewed_through_event_id, reviewed_at, pending_update, pending_since, "
    "last_error, last_error_at, last_attempt_at, created_at, updated_at"
)
_HISTORY_COLUMNS = (
    "id, bot_id, user_id, version, content, status, source_max_event_id, source_event_count, provider, "
    "edited_by, generated_at, replaced_at, replaced_by"
)


class PortraitError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.reason_code = code


class PortraitConflict(PortraitError):
    """版本号不一致，或自动生成撞上了管理员手改过的小传。"""

    def __init__(self, code: str, *, expected: int | None = None, actual: int | None = None, message: str = "") -> None:
        super().__init__(code, message or f"{code}（期望版本 {expected}，当前 {actual}）")
        self.expected = expected
        self.actual = actual


def _row_to_item(row: Any) -> dict[str, Any]:
    return {
        "bot_id": str(row[0] or ""),
        "user_id": str(row[1] or ""),
        "content": str(row[2] or ""),
        "version": int(row[3] or 0),
        "status": str(row[4] or "auto"),
        "source_max_event_id": int(row[5] or 0),
        "source_event_count": int(row[6] or 0),
        "provider": str(row[7] or ""),
        "edited_by": str(row[8] or ""),
        "generated_at": float(row[9]) if row[9] is not None else None,
        "reviewed_through_event_id": int(row[10] or 0),
        "reviewed_at": float(row[11]) if row[11] is not None else None,
        "pending_update": bool(row[12]),
        "pending_since": float(row[13]) if row[13] is not None else None,
        "last_error": str(row[14] or ""),
        "last_error_at": float(row[15]) if row[15] is not None else None,
        "last_attempt_at": float(row[16]) if row[16] is not None else None,
        "created_at": float(row[17] or 0.0),
        "updated_at": float(row[18] or 0.0),
    }


def _history_to_item(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "bot_id": str(row[1] or ""),
        "user_id": str(row[2] or ""),
        "version": int(row[3] or 0),
        "content": str(row[4] or ""),
        "status": str(row[5] or "auto"),
        "source_max_event_id": int(row[6] or 0),
        "source_event_count": int(row[7] or 0),
        "provider": str(row[8] or ""),
        "edited_by": str(row[9] or ""),
        "generated_at": float(row[10]) if row[10] is not None else None,
        "replaced_at": float(row[11] or 0.0),
        "replaced_by": str(row[12] or ""),
    }


def _key(bot_id: Any, user_id: Any) -> tuple[str, str]:
    bot = str(bot_id or "").strip()
    user = str(user_id or "").strip()
    if not bot or not user:
        raise PortraitError("portrait_person_required", "bot_id 与 user_id 必填")
    return bot, user


class RelationshipPortraitRepo:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True):
        self.cm = cm
        if ensure_schema:
            ensure_relationship_portrait_schema(cm)

    # ─── 读取 ───

    def _read(self, sql: str, params: tuple[Any, ...], connection=None):
        reader = connection.execute if connection is not None else self.cm.execute_read
        return reader(sql, params)

    def get(self, *, bot_id: str, user_id: str, connection=None) -> dict[str, Any] | None:
        bot, user = _key(bot_id, user_id)
        row = self._read(
            f"SELECT {_COLUMNS} FROM person_relationship_portraits WHERE bot_id=? AND user_id=?",
            (bot, user), connection,
        ).fetchone()
        return _row_to_item(row) if row else None

    def find(self, *, bot_id: str, user_ids: Iterable[str], connection=None) -> dict[str, Any] | None:
        """同一个人的几个关联账号里取一行：优先有正文的、版本新的、最近更新的。"""
        ids = [str(item).strip() for item in dict.fromkeys(user_ids or ()) if str(item or "").strip()]
        bot = str(bot_id or "").strip()
        if not bot or not ids:
            return None
        marks = ",".join("?" for _ in ids)
        row = self._read(
            f"""SELECT {_COLUMNS} FROM person_relationship_portraits
                 WHERE bot_id=? AND user_id IN ({marks})
                 ORDER BY (content <> '') DESC, version DESC, updated_at DESC, user_id
                 LIMIT 1""",
            (bot, *ids), connection,
        ).fetchone()
        return _row_to_item(row) if row else None

    def list_for_bot(self, bot_id: str, *, connection=None) -> list[dict[str, Any]]:
        rows = self._read(
            f"SELECT {_COLUMNS} FROM person_relationship_portraits WHERE bot_id=? ORDER BY user_id",
            (str(bot_id or "").strip(),), connection,
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    def history(self, *, bot_id: str, user_id: str, limit: int = HISTORY_KEEP_PER_PERSON, connection=None) -> list[dict[str, Any]]:
        bot, user = _key(bot_id, user_id)
        rows = self._read(
            f"""SELECT {_HISTORY_COLUMNS} FROM person_relationship_portrait_history
                 WHERE bot_id=? AND user_id=? ORDER BY version DESC, id DESC LIMIT ?""",
            (bot, user, max(1, min(int(limit), HISTORY_KEEP_PER_PERSON))), connection,
        ).fetchall()
        return [_history_to_item(row) for row in rows]

    # ─── 写入 ───

    @staticmethod
    def _current(tx, bot: str, user: str) -> dict[str, Any] | None:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM person_relationship_portraits WHERE bot_id=? AND user_id=?",
            (bot, user),
        ).fetchone()
        return _row_to_item(row) if row else None

    @staticmethod
    def _check_version(current: Mapping[str, Any] | None, expected_version: int | None) -> None:
        if expected_version is None:
            return
        actual = int((current or {}).get("version") or 0)
        if int(expected_version) != actual:
            raise PortraitConflict("portrait_version_conflict", expected=int(expected_version), actual=actual)

    def save_content(
        self,
        *,
        bot_id: str,
        user_id: str,
        content: str,
        status: str,
        expected_version: int | None,
        provider: str = "",
        edited_by: str = "",
        source_max_event_id: int | None = None,
        source_event_count: int | None = None,
        allow_overwrite_edited: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """写入新正文，版本 +1，旧正文留档。

        ``status='auto'``（模型生成）会推进判定起点并清掉待更新标记；遇到管理员手改过的小传时，
        除非 ``allow_overwrite_edited``（管理员在管理台点了重新生成），否则拒绝。
        ``status='edited'``（管理员编辑）不改依据范围与判定起点。
        """
        bot, user = _key(bot_id, user_id)
        text = str(content or "").strip()
        if not text:
            raise PortraitError("portrait_content_required", "小传正文不能为空")
        if status not in PORTRAIT_STATUSES:
            raise PortraitError("invalid_portrait_status", f"status 只能是 auto 或 edited：{status!r}")
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            current = self._current(tx, bot, user)
            self._check_version(current, expected_version)
            if (
                status == "auto"
                and current is not None
                and current.get("status") == "edited"
                and current.get("content")
                and not allow_overwrite_edited
            ):
                raise PortraitConflict(
                    "portrait_edited_by_admin", expected=expected_version, actual=int(current["version"]),
                    message="管理员手改过的小传不自动覆盖",
                )
            version = int((current or {}).get("version") or 0) + 1
            replaced_by = "admin" if status == "edited" else "model"
            if current is not None and current.get("content"):
                tx.execute(
                    """INSERT INTO person_relationship_portrait_history (
                           bot_id, user_id, version, content, status, source_max_event_id, source_event_count,
                           provider, edited_by, generated_at, replaced_at, replaced_by
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bot, user, int(current["version"]), current["content"], current["status"],
                        int(current["source_max_event_id"]), int(current["source_event_count"]),
                        current["provider"], current["edited_by"], current["generated_at"], stamp, replaced_by,
                    ),
                )
                tx.execute(
                    """DELETE FROM person_relationship_portrait_history WHERE bot_id=? AND user_id=? AND id NOT IN (
                           SELECT id FROM person_relationship_portrait_history WHERE bot_id=? AND user_id=?
                            ORDER BY version DESC, id DESC LIMIT ?)""",
                    (bot, user, bot, user, HISTORY_KEEP_PER_PERSON),
                )
            if status == "auto":
                source_max = int(source_max_event_id or 0)
                values = {
                    "content": text, "version": version, "status": "auto",
                    "source_max_event_id": source_max, "source_event_count": int(source_event_count or 0),
                    "provider": str(provider or "")[:120], "edited_by": "", "generated_at": stamp,
                    "reviewed_through_event_id": source_max, "reviewed_at": stamp,
                    "pending_update": 0, "pending_since": None, "last_error": "", "last_error_at": None,
                    "last_attempt_at": stamp, "updated_at": stamp,
                }
            else:
                values = {
                    "content": text, "version": version, "status": "edited",
                    "edited_by": str(edited_by or "webui")[:80], "generated_at": stamp,
                    "pending_update": 0, "pending_since": None, "updated_at": stamp,
                }
                if current is None:
                    values.update({"source_max_event_id": int(source_max_event_id or 0),
                                   "source_event_count": int(source_event_count or 0)})
            self._upsert(tx, bot, user, values, exists=current is not None, now=stamp)
            saved = self._current(tx, bot, user)
        assert saved is not None
        return saved

    @staticmethod
    def _upsert(tx, bot: str, user: str, values: Mapping[str, Any], *, exists: bool, now: float) -> None:
        if exists:
            assignments = ", ".join(f"{name}=?" for name in values)
            tx.execute(
                f"UPDATE person_relationship_portraits SET {assignments} WHERE bot_id=? AND user_id=?",
                (*values.values(), bot, user),
            )
            return
        payload = {"created_at": now, "updated_at": now, **values}
        names = ", ".join(("bot_id", "user_id", *payload))
        marks = ", ".join("?" for _ in range(len(payload) + 2))
        tx.execute(
            f"INSERT INTO person_relationship_portraits ({names}) VALUES ({marks})",
            (bot, user, *payload.values()),
        )

    def record_error(self, *, bot_id: str, user_id: str, error: str, now: float | None = None) -> dict[str, Any]:
        """记下最近一次失败（不改正文与版本号）；还没有小传时建一行空正文占位。"""
        bot, user = _key(bot_id, user_id)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            current = self._current(tx, bot, user)
            self._upsert(tx, bot, user, {
                "last_error": str(error or "unknown_error")[:300],
                "last_error_at": stamp,
                "last_attempt_at": stamp,
                "updated_at": stamp,
            }, exists=current is not None, now=stamp)
            saved = self._current(tx, bot, user)
        assert saved is not None
        return saved

    def mark_pending(self, *, bot_id: str, user_id: str, now: float | None = None) -> dict[str, Any] | None:
        """管理员手改过的小传有了新记录：只打「待更新」标记，不动正文。"""
        bot, user = _key(bot_id, user_id)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            current = self._current(tx, bot, user)
            if current is None:
                return None
            if not current.get("pending_update"):
                tx.execute(
                    """UPDATE person_relationship_portraits SET pending_update=1, pending_since=?, updated_at=?
                        WHERE bot_id=? AND user_id=?""",
                    (stamp, stamp, bot, user),
                )
            return self._current(tx, bot, user)

    def keep_current(
        self,
        *,
        bot_id: str,
        user_id: str,
        expected_version: int | None,
        reviewed_through_event_id: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        """管理员选择保留当前小传：清掉待更新标记，把判定起点推进到当前最新事件。"""
        bot, user = _key(bot_id, user_id)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            current = self._current(tx, bot, user)
            if current is None or not current.get("content"):
                raise PortraitError("portrait_not_found", "还没有关系小传")
            self._check_version(current, expected_version)
            tx.execute(
                """UPDATE person_relationship_portraits
                      SET pending_update=0, pending_since=NULL, reviewed_through_event_id=?, reviewed_at=?, updated_at=?
                    WHERE bot_id=? AND user_id=?""",
                (max(int(current["reviewed_through_event_id"]), int(reviewed_through_event_id or 0)), stamp, stamp, bot, user),
            )
            saved = self._current(tx, bot, user)
        assert saved is not None
        return saved


__all__ = [
    "HISTORY_KEEP_PER_PERSON",
    "PORTRAIT_STATUSES",
    "PortraitConflict",
    "PortraitError",
    "RelationshipPortraitRepo",
]
