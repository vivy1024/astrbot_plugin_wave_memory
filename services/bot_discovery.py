"""Bot 标签读写与协同防互聊服务。

四级标签状态（存在 person_registry.entity_kind，兼容缺失列）：
- ``human``          : 默认普通群友
- ``verified_human`` : 管理员确认为真人（羽书的判断永不覆盖）
- ``suspected_bot``  : 羽书在对话里觉得对方像 Bot，顺手标了一笔（wave_memory_social mode=bot_check）
- ``confirmed_bot``  : 管理员确认是 Bot，或本系统已注册的 Bot 账号

这里没有任何关键词、正则或行为统计规则：谁是 Bot 由羽书在对话里自己判断，系统只负责存下来、
在被标为 Bot 的人说话时不主动接、被他 @ 时限制来回轮数，避免两个 Bot 互相刷屏。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

BOT_ENTITY_KINDS = ("human", "verified_human", "suspected_bot", "confirmed_bot")
BOT_DETECTED_KINDS = frozenset({"suspected_bot", "confirmed_bot"})


def normalize_entity_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in BOT_ENTITY_KINDS else "human"


class BotDiscoveryService:
    """人物 Bot 标签读写与协同轮数控制。"""

    def __init__(
        self,
        db: Any = None,
        *,
        system_bot_ids: Iterable[str] = (),
        clock=time.time,
    ) -> None:
        self.db = db
        self.clock = clock
        self._lock = threading.RLock()
        self._system_bot_ids: set[str] = {str(x).strip() for x in system_bot_ids if str(x or "").strip()}
        # 协同限流：(bot_id, session_id, sender_id) -> deque[timestamp]
        self._collab_turns: dict[tuple[str, str, str], deque[float]] = {}
        # 进程内缓存标签，避免热路径每条消息都查库
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def sync_system_bots(self, bot_ids: Iterable[str]) -> None:
        with self._lock:
            self._system_bot_ids = {str(x).strip() for x in bot_ids if str(x or "").strip()}

    def _conn(self) -> Any:
        if self.db is None:
            return None
        return getattr(self.db, "conn", self.db)

    def _ensure_columns(self, conn: Any) -> bool:
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(person_registry)").fetchall()}
        except Exception:
            return False
        if not cols:
            return False
        needed = (
            ("entity_kind", "TEXT DEFAULT 'human'"),
            ("bot_tag_source", "TEXT DEFAULT ''"),
            ("bot_tag_reason", "TEXT DEFAULT ''"),
            ("bot_tag_updated_at", "REAL DEFAULT 0"),
        )
        changed = False
        for name, ddl in needed:
            if name not in cols:
                try:
                    conn.execute(f"ALTER TABLE person_registry ADD COLUMN {name} {ddl}")
                    changed = True
                except Exception:
                    return False
        if changed:
            try:
                conn.commit()
            except Exception:
                pass
        return True

    def get_tag(self, user_id: str, *, display_name: str = "") -> dict[str, Any]:
        """读取某人已存的 Bot 标签（带短时缓存；本系统注册的账号恒为 confirmed_bot）。

        只读，不做任何判断。``display_name`` 仅为兼容旧调用方保留，不参与判定。
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {"is_bot": False, "entity_kind": "human", "source": "", "reason": "", "updated_at": 0.0}
        with self._lock:
            if uid in self._system_bot_ids:
                return {
                    "is_bot": True,
                    "entity_kind": "confirmed_bot",
                    "source": "system_self",
                    "reason": "本系统已注册的 Bot 账号",
                    "updated_at": 0.0,
                }
            now = float(self.clock())
            cached = self._cache.get(uid)
            if cached and now - cached[0] < 15.0:
                return dict(cached[1])

        conn = self._conn()
        tag = {"is_bot": False, "entity_kind": "human", "source": "", "reason": "", "updated_at": 0.0}
        if conn is not None and self._ensure_columns(conn):
            try:
                row = conn.execute(
                    "SELECT entity_kind, bot_tag_source, bot_tag_reason, bot_tag_updated_at, display_name "
                    "FROM person_registry WHERE qq_id=?",
                    (uid,),
                ).fetchone()
                if row:
                    kind = normalize_entity_kind(row[0])
                    tag = {
                        "is_bot": kind in BOT_DETECTED_KINDS,
                        "entity_kind": kind,
                        "source": str(row[1] or ""),
                        "reason": str(row[2] or ""),
                        "updated_at": float(row[3] or 0.0),
                    }
                    if not display_name and row[4]:
                        display_name = str(row[4])
            except Exception:
                pass

        with self._lock:
            self._cache[uid] = (float(self.clock()), dict(tag))
            if len(self._cache) > 2048:
                self._cache.pop(next(iter(self._cache)))
        return tag

    def set_tag(
        self,
        user_id: str,
        *,
        entity_kind: str,
        source: str = "admin",
        reason: str = "",
        display_name: str = "",
    ) -> dict[str, Any]:
        """写入人物 Bot 标签；``verified_human`` 不可被自动检测覆盖。"""
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id_required")
        kind = normalize_entity_kind(entity_kind)
        now = float(self.clock())
        clean_source = str(source or "admin").strip()[:40]
        clean_reason = str(reason or "").strip()[:200]

        conn = self._conn()
        if conn is not None and self._ensure_columns(conn):
            existing = conn.execute(
                "SELECT entity_kind FROM person_registry WHERE qq_id=?",
                (uid,),
            ).fetchone()
            if existing:
                cur_kind = normalize_entity_kind(existing[0])
                # 管理员已经标定为真人或确认 Bot 时，自动启发式不得反向覆盖
                if clean_source != "admin" and cur_kind in {"verified_human", "confirmed_bot"}:
                    return self.get_tag(uid, display_name=display_name)
                conn.execute(
                    """UPDATE person_registry
                          SET entity_kind=?, bot_tag_source=?, bot_tag_reason=?, bot_tag_updated_at=?
                        WHERE qq_id=?""",
                    (kind, clean_source, clean_reason, now, uid),
                )
            else:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(person_registry)").fetchall()}
                insert_cols = ["qq_id", "display_name"]
                insert_vals: list[Any] = [uid, display_name or uid]
                optional_defaults = (
                    ("aliases", "[]"),
                    ("tag_ids", "[]"),
                    ("first_seen", now),
                    ("last_seen", now),
                    ("message_count", 0),
                    ("groups", "[]"),
                    ("entity_kind", kind),
                    ("bot_tag_source", clean_source),
                    ("bot_tag_reason", clean_reason),
                    ("bot_tag_updated_at", now),
                )
                for col, val in optional_defaults:
                    if col in cols:
                        insert_cols.append(col)
                        insert_vals.append(val)
                placeholders = ", ".join("?" for _ in insert_cols)
                conn.execute(
                    f"INSERT INTO person_registry ({', '.join(insert_cols)}) VALUES ({placeholders})",
                    tuple(insert_vals),
                )
            conn.commit()

        result = {
            "is_bot": kind in BOT_DETECTED_KINDS,
            "entity_kind": kind,
            "source": clean_source,
            "reason": clean_reason,
            "updated_at": now,
        }
        with self._lock:
            self._cache[uid] = (now, dict(result))
        return result

    def check_and_record_collab_turn(
        self,
        *,
        bot_id: str,
        session_id: str,
        sender_id: str,
        max_turns: int,
        window_seconds: float = 600.0,
        record: bool = False,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """检查与被标定 Bot 的协同对话是否超过防死循环上限；返回 (是否允许, 当前窗口轮数)。"""
        stamp = float(self.clock() if now is None else now)
        limit = max(0, int(max_turns))
        key = (str(bot_id), str(session_id), str(sender_id))
        with self._lock:
            bucket = self._collab_turns.setdefault(key, deque(maxlen=64))
            while bucket and stamp - bucket[0] > window_seconds:
                bucket.popleft()
            count = len(bucket)
            if count >= limit:
                return False, count
            if record:
                bucket.append(stamp)
                count = len(bucket)
            return True, count


__all__ = [
    "BOT_DETECTED_KINDS",
    "BOT_ENTITY_KINDS",
    "BotDiscoveryService",
    "normalize_entity_kind",
]
