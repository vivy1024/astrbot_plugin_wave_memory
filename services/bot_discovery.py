"""Bot 自动发现、标签判定与协同防互聊服务。

四级标签状态（存在 person_registry.entity_kind，兼容缺失列）：
- ``human``          : 默认普通群友
- ``verified_human`` : 管理员确认为真人（自动检测永不覆盖）
- ``suspected_bot``  : 系统自动发现疑似 Bot（昵称标记 / 机械秒回 / 复读对刷 / 框架报错泄漏 / 群友点破）
- ``confirmed_bot``  : 管理员确认是 Bot（或系统自身注册的 Bot 账号）
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

BOT_ENTITY_KINDS = ("human", "verified_human", "suspected_bot", "confirmed_bot")
BOT_DETECTED_KINDS = frozenset({"suspected_bot", "confirmed_bot"})

# 昵称里常见 Bot / AI 后缀或标记
#
# 结尾的 ``bot`` 只在前面不是英文字母时才算（「小冰Bot」「xx_bot」算，Abbot / robot / Talkbot 是真词，不算）。
_BOT_NAME_RE = re.compile(
    r"(?:\[(?:bot|ai|机器人|自动)\]|\((?:bot|ai|机器人)\)|【(?:bot|ai|机器人)】|(?<![a-z])bot$)",
    re.IGNORECASE,
)

# 其他机器人/框架报错或提示词泄漏特征。
# 只收「真人几乎不会这样说」的强信号：AstrBot 外发的「LLM 响应错误」模板、工具调用失败、模型自称。
# 不收 astrbot / API Error / Rate limit / token exceeded 这类词：群友讨论 AI 与插件时天天在说，会把真人误标成 Bot。
_BOT_ERROR_LEAK_RE = re.compile(
    r"(?:LLM\s*响应错误|调用工具失败|context_length_exceeded|"
    r"\[系统提示\]|\[System\s*Error\]|作为一个人工智能|我只是一个\s*(?:AI|语言模型))",
    re.IGNORECASE,
)

# 群友在群里点破或质疑某人为 Bot 的表达
_CROWD_BOT_CALLOUT_RE = re.compile(
    r"(?:是\s*(?:bot|机器人|ai|人机)|别跟\s*(?:bot|机器人|人机)\s*聊|两[个只]\s*(?:bot|机器人|ai)\s*互聊|人机互刷)",
    re.IGNORECASE,
)


def normalize_entity_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in BOT_ENTITY_KINDS else "human"


class BotDiscoveryService:
    """人物 Bot 自动发现、标签读写与协同轮数控制。"""

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
        # 群内最近消息环，用于检测 30s 内机械秒回与复读对刷
        self._recent_msgs: dict[str, deque[dict[str, Any]]] = {}
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
        """读取某人的 Bot 标签状态（带短时缓存与系统 Bot 自动识别）。"""
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

        # 兜底：名字显式含 [Bot] 且尚未被管理员确认为真人时，直接标疑
        if tag["entity_kind"] == "human" and display_name and _BOT_NAME_RE.search(display_name):
            tag = {
                "is_bot": True,
                "entity_kind": "suspected_bot",
                "source": "auto_name",
                "reason": f"昵称包含 Bot 标识：{display_name[:30]}",
                "updated_at": float(self.clock()),
            }

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

    def observe_group_message(
        self,
        *,
        session_id: str,
        sender_id: str,
        sender_name: str,
        message: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """分析一条群消息是否暴露出他方 Bot 特征，并返回最新标签。"""
        uid = str(sender_id or "").strip()
        if not uid:
            return {"is_bot": False, "entity_kind": "human", "source": "", "reason": ""}
        current = self.get_tag(uid, display_name=sender_name)
        if current["entity_kind"] in {"verified_human", "confirmed_bot"}:
            return current

        stamp = float(self.clock() if now is None else now)
        text = str(message or "").strip()
        detected_reason = ""
        detected_source = ""

        # 1. 昵称含 Bot 标识
        if sender_name and _BOT_NAME_RE.search(sender_name):
            detected_source = "auto_name"
            detected_reason = f"昵称包含 Bot 标识（{sender_name[:24]}）"
        # 2. 框架报错 / 模型模板泄密
        elif text and _BOT_ERROR_LEAK_RE.search(text):
            detected_source = "auto_leak"
            detected_reason = "发言包含疑似 AI/框架报错或身份模板词"

        # 3. 连续机械秒回 / 复读车轱辘话检测
        with self._lock:
            ring = self._recent_msgs.setdefault(session_id or "_", deque(maxlen=24))
            sender_history = [item for item in ring if item["sender_id"] == uid]
            if not detected_reason and text and len(sender_history) >= 2:
                last = sender_history[-1]
                prev = sender_history[-2]
                gap1 = stamp - last["ts"]
                gap2 = last["ts"] - prev["ts"]
                # 连续 3 条间隔在 1~8 秒且间隔方差极小、或内容高度相似复读
                if 0.3 <= gap1 <= 8.0 and 0.3 <= gap2 <= 8.0 and abs(gap1 - gap2) <= 1.2 and len(text) >= 12:
                    detected_source = "auto_rhythm"
                    detected_reason = "短时间出现等间隔机械连发"
                elif len(text) >= 10:
                    # 真人也会复读、连发「哈哈哈哈哈哈哈哈哈哈」，两条相近不算；
                    # 要求连续三条近乎相同、且都在 120 秒内，才认为是模板话术。
                    sim_last = difflib.SequenceMatcher(None, text[:80], last["text"][:80]).ratio()
                    sim_prev = difflib.SequenceMatcher(None, text[:80], prev["text"][:80]).ratio()
                    if sim_last >= 0.86 and sim_prev >= 0.86 and stamp - prev["ts"] <= 120.0:
                        detected_source = "auto_repeat"
                        detected_reason = "短时间内连续三次重复近似模板话术"

            # 4. 群友明确指出「是 bot / 机器人别聊了」→ 标记上一位高频发言的非己方成员
            if not detected_reason and text and _CROWD_BOT_CALLOUT_RE.search(text):
                for item in reversed(ring):
                    other_id = item["sender_id"]
                    if other_id != uid and other_id not in self._system_bot_ids and stamp - item["ts"] <= 45.0:
                        other_tag = self.get_tag(other_id)
                        if other_tag["entity_kind"] == "human":
                            self.set_tag(
                                other_id,
                                entity_kind="suspected_bot",
                                source="crowd_callout",
                                reason="群友反馈疑似机器人刷屏",
                            )
                        break

            ring.append({"sender_id": uid, "text": text, "ts": stamp})

        if detected_reason:
            return self.set_tag(
                uid,
                entity_kind="suspected_bot",
                source=detected_source,
                reason=detected_reason,
                display_name=sender_name,
            )
        return current

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
