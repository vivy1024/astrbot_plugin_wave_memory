"""跨平台身份关联：同一个人在不同平台上的账号属于同一个"人"。

关联按 Bot 保存——谁是谁同样是 Bot 自己的认识，羽书知道的不自动成为其他 Bot
知道的。关联只由管理员显式建立：模型或用户自称"我就是某某"无法核实，
自动关联会让任何人冒领别人的交情。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from .connection import ConnectionManager

# 平台前缀可以是中文（线上羽书的会话前缀就是「羽书」），只排除空白和冒号。
PRINCIPAL_PATTERN = re.compile(r"^[^\s:]+:user:[^\s:]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_identity_links (
    bot_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    person_key TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (bot_id, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_person_identity_links_person
    ON person_identity_links (bot_id, person_key);
"""


class PersonIdentityError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _require_principal(value: Any) -> str:
    principal = str(value or "").strip()
    if not PRINCIPAL_PATTERN.fullmatch(principal):
        raise PersonIdentityError("invalid_principal", f"principal must look like '<platform>:user:<id>': {principal!r}")
    return principal


def _require_bot(value: Any) -> str:
    bot_id = str(value or "").strip()
    if not bot_id:
        raise PersonIdentityError("bot_id_required")
    return bot_id


def local_user_id(principal: str) -> str:
    """``platform:user:id`` → ``id``；格式不符时返回空串。"""
    parts = str(principal or "").split(":user:", 1)
    return parts[1] if len(parts) == 2 and parts[0] and parts[1] else ""


def linked_principals_via(connection: Any, bot_id: str, principal: str) -> list[str]:
    """同一 Bot 认定的同一个人的全部账号（含自身）；表不存在或无关联时只返回自身。"""
    principal = str(principal or "")
    try:
        rows = connection.execute(
            """SELECT principal_id FROM person_identity_links
                WHERE bot_id=? AND person_key=(
                    SELECT person_key FROM person_identity_links WHERE bot_id=? AND principal_id=?
                )
                ORDER BY principal_id""",
            (str(bot_id or ""), str(bot_id or ""), principal),
        ).fetchall()
    except Exception:
        rows = []
    linked = [str(row[0]) for row in rows if row and row[0]]
    return linked if principal in linked else [principal, *linked]


class PersonIdentityRepo:
    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        with cm.migration_transaction() as tx:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    tx.execute(statement)

    def linked_principals(self, bot_id: str, principal: str) -> list[str]:
        return linked_principals_via(self.cm.conn, bot_id, principal)

    def link(self, bot_id: str, principals: list[str], *, note: str = "", created_by: str = "") -> str:
        """把多个账号认定为同一个人；已属于不同人的账号会合并成一个人。返回 person_key。"""
        bot_id = _require_bot(bot_id)
        cleaned = list(dict.fromkeys(_require_principal(item) for item in principals or []))
        if len(cleaned) < 2:
            raise PersonIdentityError("at_least_two_principals_required")
        now = time.time()
        with self.cm.write_transaction() as tx:
            placeholders = ",".join("?" for _ in cleaned)
            existing = [
                str(row[0])
                for row in tx.execute(
                    f"""SELECT DISTINCT person_key FROM person_identity_links
                         WHERE bot_id=? AND principal_id IN ({placeholders}) ORDER BY person_key""",
                    (bot_id, *cleaned),
                ).fetchall()
            ]
            person_key = existing[0] if existing else f"person:{uuid.uuid4().hex}"
            for other in existing[1:]:
                tx.execute(
                    "UPDATE person_identity_links SET person_key=? WHERE bot_id=? AND person_key=?",
                    (person_key, bot_id, other),
                )
            for principal in cleaned:
                tx.execute(
                    """INSERT INTO person_identity_links(bot_id, principal_id, person_key, note, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bot_id, principal_id) DO UPDATE SET person_key=excluded.person_key""",
                    (bot_id, principal, person_key, str(note or "")[:200], str(created_by or "")[:80], now),
                )
        return person_key

    def unlink(self, bot_id: str, principal: str) -> bool:
        bot_id = _require_bot(bot_id)
        principal = _require_principal(principal)
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                "SELECT person_key FROM person_identity_links WHERE bot_id=? AND principal_id=?",
                (bot_id, principal),
            ).fetchone()
            if row is None:
                return False
            tx.execute("DELETE FROM person_identity_links WHERE bot_id=? AND principal_id=?", (bot_id, principal))
            # 只剩一个账号的"人"不再构成关联。
            remaining = tx.execute(
                "SELECT COUNT(*) FROM person_identity_links WHERE bot_id=? AND person_key=?",
                (bot_id, row[0]),
            ).fetchone()[0]
            if int(remaining) < 2:
                tx.execute("DELETE FROM person_identity_links WHERE bot_id=? AND person_key=?", (bot_id, row[0]))
        return True

    def list_people(self, bot_id: str) -> list[dict[str, Any]]:
        rows = self.cm.execute_read(
            """SELECT person_key, principal_id, note, created_by, created_at
                 FROM person_identity_links WHERE bot_id=? ORDER BY person_key, principal_id""",
            (_require_bot(bot_id),),
        ).fetchall()
        people: dict[str, dict[str, Any]] = {}
        for person_key, principal, note, created_by, created_at in rows:
            person = people.setdefault(str(person_key), {
                "person_key": str(person_key), "principals": [], "note": "", "created_at": created_at,
            })
            person["principals"].append(str(principal))
            if note and not person["note"]:
                person["note"] = str(note)
            person["created_by"] = str(created_by or "")
        return list(people.values())


__all__ = [
    "PRINCIPAL_PATTERN",
    "PersonIdentityError",
    "PersonIdentityRepo",
    "linked_principals_via",
    "local_user_id",
]
