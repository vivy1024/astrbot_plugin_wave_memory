"""Bot 级（广域）黑话仓储。

与 ``ScopedKnowledgeRepo`` 的关键区别：本仓储**不接受 RuntimeScope**，只按 ``bot_id``
归属。这是有意为之——广域黑话的定义就是「该 Bot 的所有群共享」，如果接受 group
RuntimeScope，调用方就可能误用某个群的身份去读写跨群数据。

对应表 ``bot_jargon`` 没有 session/visibility 列，因此这里不做 group 校验，而是对
``bot_id`` 做 fail-closed 校验。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from .connection import ConnectionManager

_BOT_JARGON_STATUSES = frozenset({"active", "inactive"})
_BOT_JARGON_SOURCES = frozenset({"manual", "promoted", "holyman_import", "manual_deleted"})


class BotJargonScopeError(ValueError):
    """Bot 级黑话的稳定 fail-closed 拒绝。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message or code)


def _require_bot_id(bot_id: Any) -> str:
    if not isinstance(bot_id, str) or not bot_id or bot_id != bot_id.strip():
        raise BotJargonScopeError(
            "bot_scope_required",
            "a non-empty exact bot_id is required for bot-level jargon",
        )
    return bot_id


def _exact_nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty exact string")
    return value


class BotJargonRepository:
    """广域黑话的唯一正式读写 API。"""

    def __init__(self, cm: ConnectionManager) -> None:
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm

    def list_bot_jargon(self, bot_id: Any, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        bot_id = _require_bot_id(bot_id)
        if status is not None and status not in _BOT_JARGON_STATUSES:
            raise ValueError("invalid bot jargon status")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        conditions = ["bot_id=?"]
        params: list[Any] = [bot_id]
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        rows = self.cm.execute_read(
            f"""SELECT id, bot_id, word, meaning, status, is_jargon, confidence, source,
                       origin_scope, reference_key, provenance, created_at, updated_at
                  FROM bot_jargon WHERE {' AND '.join(conditions)}
                 ORDER BY updated_at DESC, id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def load_all_bot_jargon_overlay(self, bot_id: Any) -> list[dict[str, Any]]:
        """全量读取该 Bot 的全部覆盖层与墓碑记录。

        用于注入层与管理台合并内置资产。分批遍历确保不会因数量上限导致早期墓碑丢失、
        进而导致被删除词意外复活。
        """
        bot_id = _require_bot_id(bot_id)
        results: list[dict[str, Any]] = []
        last_id = 0
        batch_size = 500
        while True:
            rows = self.cm.execute_read(
                """SELECT id, bot_id, word, meaning, status, is_jargon, confidence, source,
                          origin_scope, reference_key, provenance, created_at, updated_at
                     FROM bot_jargon
                    WHERE bot_id = ? AND id > ?
                    ORDER BY id ASC LIMIT ?""",
                (bot_id, last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                results.append(self._row_to_dict(row))
                last_id = int(row[0])
            if len(rows) < batch_size:
                break
        return results

    def find_bot_jargon(self, bot_id: Any, *, word: str) -> dict[str, Any] | None:
        bot_id = _require_bot_id(bot_id)
        word = _exact_nonempty(word, "word")
        row = self.cm.execute_read(
            """SELECT id, bot_id, word, meaning, status, is_jargon, confidence, source,
                      origin_scope, reference_key, provenance, created_at, updated_at
                 FROM bot_jargon WHERE bot_id=? AND word=?""",
            (bot_id, word),
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def upsert_bot_jargon(
        self,
        bot_id: Any,
        *,
        word: str,
        meaning: str = "",
        status: str = "active",
        source: str = "manual",
        confidence: float = 0.0,
        origin_scope: str | None = None,
        reference_key: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        overwrite_meaning: bool = True,
    ) -> int:
        """写入或更新一条广域黑话。

        ``overwrite_meaning=False`` 用于内置资产导入：只补缺失的释义，绝不覆盖用户在
        WebUI 手工维护过的释义，也绝不改动 ``status``。
        """
        bot_id = _require_bot_id(bot_id)
        word = _exact_nonempty(word, "word")
        if status not in _BOT_JARGON_STATUSES:
            raise ValueError("invalid bot jargon status")
        if source not in _BOT_JARGON_SOURCES:
            raise ValueError("invalid bot jargon source")
        meaning = str(meaning or "")
        if len(meaning) > 2000:
            raise ValueError("meaning is too long")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number") from None
        payload = json.dumps(
            dict(provenance or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        now = time.time()
        existing = self.find_bot_jargon(bot_id, word=word)

        if existing is not None and not overwrite_meaning:
            self.cm.execute_write(
                """UPDATE bot_jargon
                      SET meaning = CASE WHEN COALESCE(meaning,'') = '' THEN ? ELSE meaning END,
                          confidence = CASE WHEN confidence < ? THEN ? ELSE confidence END,
                          reference_key = COALESCE(reference_key, ?),
                          updated_at = ?
                    WHERE bot_id=? AND word=?""",
                (meaning, confidence, confidence, reference_key, now, bot_id, word),
            )
            self.cm.commit()
            return int(existing["id"])

        self.cm.execute_write(
            """INSERT INTO bot_jargon(
                   bot_id, word, meaning, status, is_jargon, confidence, source,
                   origin_scope, reference_key, provenance, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_id, word) DO UPDATE SET
                   meaning=excluded.meaning, status=excluded.status,
                   confidence=excluded.confidence, source=excluded.source,
                   origin_scope=COALESCE(excluded.origin_scope, bot_jargon.origin_scope),
                   reference_key=COALESCE(excluded.reference_key, bot_jargon.reference_key),
                   provenance=excluded.provenance, updated_at=excluded.updated_at""",
            (bot_id, word, meaning, status, confidence, source, origin_scope, reference_key, payload, now, now),
        )
        self.cm.commit()
        row = self.cm.execute_read(
            "SELECT id FROM bot_jargon WHERE bot_id=? AND word=?", (bot_id, word)
        ).fetchone()
        if row is None:
            raise BotJargonScopeError("bot_jargon_write_failed")
        return int(row[0])

    def set_bot_jargon_status(self, bot_id: Any, *, word: str, status: str) -> dict[str, Any]:
        bot_id = _require_bot_id(bot_id)
        word = _exact_nonempty(word, "word")
        if status not in _BOT_JARGON_STATUSES:
            raise ValueError("invalid bot jargon status")
        current = self.find_bot_jargon(bot_id, word=word)
        if current is None:
            raise LookupError("scoped_object_not_found")
        self.cm.execute_write(
            "UPDATE bot_jargon SET status=?, updated_at=? WHERE bot_id=? AND word=?",
            (status, time.time(), bot_id, word),
        )
        self.cm.commit()
        return {"id": int(current["id"]), "word": word, "status": status}

    def delete_bot_jargon(self, bot_id: Any, *, word: str, tombstone: bool = True) -> dict[str, Any]:
        """删除广域黑话。

        默认留墓碑（``source='manual_deleted'`` + ``status='inactive'``）而不是物理删除：
        否则下一次「从内置资产导入」会把这个词原样复活，用户的删除动作等于无效。
        """
        bot_id = _require_bot_id(bot_id)
        word = _exact_nonempty(word, "word")
        current = self.find_bot_jargon(bot_id, word=word)
        if current is None:
            raise LookupError("scoped_object_not_found")
        now = time.time()
        if tombstone:
            self.cm.execute_write(
                """UPDATE bot_jargon
                      SET status='inactive', source='manual_deleted', updated_at=?
                    WHERE bot_id=? AND word=?""",
                (now, bot_id, word),
            )
            self.cm.commit()
        else:
            self.cm.execute_write("DELETE FROM bot_jargon WHERE bot_id=? AND word=?", (bot_id, word))
            self.cm.commit()
        return {"id": int(current["id"]), "word": word, "status": "inactive", "removed": True}

    def list_active_for_prompt(self, bot_id: Any, *, limit: int = 50) -> list[dict[str, Any]]:
        """注入口径：只取 active 行，且必须有非空释义。"""
        rows = self.list_bot_jargon(bot_id, status="active", limit=limit)
        return [row for row in rows if str(row.get("meaning") or "").strip()]

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row[0], "bot_id": row[1], "word": row[2], "meaning": row[3],
            "status": row[4], "is_jargon": None if row[5] is None else bool(row[5]),
            "confidence": row[6], "source": row[7], "origin_scope": row[8],
            "reference_key": row[9], "provenance": json.loads(row[10] or "{}"),
            "created_at": row[11], "updated_at": row[12],
        }


__all__ = ["BotJargonRepository", "BotJargonScopeError"]
