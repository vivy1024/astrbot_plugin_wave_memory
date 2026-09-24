"""Bot Profile 持久化：``bot_profiles`` 表与修改历史。

Profile 整体以 JSON 保存（字段会随版本增加，避免每加一个字段就改表），
``db_id`` 是主键，``version`` 做乐观锁：保存时带上读到的版本号，不一致就拒绝，
防止两个页面同时编辑互相覆盖。每次保存把旧内容写进 ``bot_profile_history``，
误改可以回看。
"""

from __future__ import annotations

import json
import time
from typing import Any

try:
    from ...domain.bot_profile import BotProfile, BotProfileError
except ImportError:  # top-level import in isolated tests
    from domain.bot_profile import BotProfile, BotProfileError
from .connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_profiles (
    db_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bot_profile_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    db_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    changed_at REAL NOT NULL,
    changed_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bot_profile_history_bot
    ON bot_profile_history (db_id, version);
"""

HISTORY_KEEP_PER_BOT = 50


class BotProfileConflict(BotProfileError):
    def __init__(self, db_id: str, expected: int, actual: int) -> None:
        super().__init__("version_conflict", f"{db_id} 已被修改（期望版本 {expected}，当前 {actual}），请刷新后重试")
        self.expected = expected
        self.actual = actual


def _row_to_profile(row: Any) -> BotProfile:
    payload = json.loads(row[1])
    payload["db_id"] = row[0]
    payload["enabled"] = bool(row[2])
    payload["version"] = int(row[3])
    return BotProfile.from_dict(payload)


class BotProfileRepo:
    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        with cm.migration_transaction() as tx:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    tx.execute(statement)

    def count(self) -> int:
        row = self.cm.execute("SELECT COUNT(*) FROM bot_profiles").fetchone()
        return int(row[0] if row else 0)

    def list(self, *, include_disabled: bool = True) -> list[BotProfile]:
        sql = "SELECT db_id, payload, enabled, version FROM bot_profiles"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY created_at, db_id"
        profiles: list[BotProfile] = []
        for row in self.cm.execute(sql).fetchall():
            try:
                profiles.append(_row_to_profile(row))
            except (BotProfileError, ValueError, TypeError) as exc:
                # 坏行不拖垮整个注册表；调用方通过 invalid_rows() 在页面上提示。
                self._last_invalid = getattr(self, "_last_invalid", {})
                self._last_invalid[str(row[0])] = str(exc)
        return profiles

    def invalid_rows(self) -> dict[str, str]:
        return dict(getattr(self, "_last_invalid", {}))

    def get(self, db_id: str) -> BotProfile | None:
        row = self.cm.execute(
            "SELECT db_id, payload, enabled, version FROM bot_profiles WHERE db_id=?",
            (str(db_id or "").strip(),),
        ).fetchone()
        return _row_to_profile(row) if row else None

    def save(
        self,
        profile: BotProfile,
        *,
        expected_version: int | None = None,
        changed_by: str = "",
        reason: str = "",
    ) -> BotProfile:
        """新增或更新。``expected_version`` 为 None 时不做乐观锁检查（迁移、导入用）。"""
        payload = profile.to_dict()
        for volatile in ("version", "enabled", "db_id"):
            payload.pop(volatile, None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                "SELECT payload, version FROM bot_profiles WHERE db_id=?",
                (profile.db_id,),
            ).fetchone()
            if row is None:
                if expected_version not in (None, 0):
                    raise BotProfileConflict(profile.db_id, int(expected_version), 0)
                version = 1
                tx.execute(
                    """INSERT INTO bot_profiles(db_id, payload, enabled, version, created_at, updated_at, updated_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (profile.db_id, encoded, 1 if profile.enabled else 0, version, now, now, str(changed_by)[:80]),
                )
            else:
                current = int(row[1])
                if expected_version is not None and int(expected_version) != current:
                    raise BotProfileConflict(profile.db_id, int(expected_version), current)
                tx.execute(
                    """INSERT INTO bot_profile_history(db_id, version, payload, changed_at, changed_by, reason)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (profile.db_id, current, row[0], now, str(changed_by)[:80], str(reason)[:200]),
                )
                version = current + 1
                tx.execute(
                    """UPDATE bot_profiles SET payload=?, enabled=?, version=?, updated_at=?, updated_by=?
                       WHERE db_id=?""",
                    (encoded, 1 if profile.enabled else 0, version, now, str(changed_by)[:80], profile.db_id),
                )
                tx.execute(
                    """DELETE FROM bot_profile_history WHERE db_id=? AND id NOT IN (
                           SELECT id FROM bot_profile_history WHERE db_id=? ORDER BY id DESC LIMIT ?)""",
                    (profile.db_id, profile.db_id, HISTORY_KEEP_PER_BOT),
                )
        saved = self.get(profile.db_id)
        assert saved is not None
        return saved

    def history(self, db_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.cm.execute(
            """SELECT version, payload, changed_at, changed_by, reason FROM bot_profile_history
                WHERE db_id=? ORDER BY id DESC LIMIT ?""",
            (str(db_id or ""), max(1, min(int(limit), HISTORY_KEEP_PER_BOT))),
        ).fetchall()
        return [
            {
                "version": int(row[0]),
                "payload": json.loads(row[1]),
                "changed_at": float(row[2]),
                "changed_by": row[3],
                "reason": row[4],
            }
            for row in rows
        ]


__all__ = ["BotProfileConflict", "BotProfileRepo", "HISTORY_KEEP_PER_BOT"]
