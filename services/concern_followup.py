"""关切驱动的主动跟进：羽书惦记的人在群里出现时，她会主动开口问一句。

过去关切记下后什么也不影响：主动回复整条链是死代码，关切只出现在记账提醒的候选里。这里改成
事件驱动——不另起定时发消息，而是在群消息进来、说话人正是某条未了关切的对象时，把这条消息标记为
需要羽书回应（AstrBot 的 ``is_at_or_wake_command``），并在请求里附一段「你惦记的事」提示。回复走
正常链路：完整记忆注入、人设、身份防线都照常生效。

门控（任一不满足都不开口）：配置开关 ``Learning_Settings.proactive_reply_enabled``（默认关闭；
关闭时不主动开口，但羽书本来就要回复这个人时仍会附上提示）；关切处于 active / progressing；同一条关切 ``cooldown_hours``
内只跟进一次；同一群两次主动至少间隔 ``group_min_gap_seconds``、每小时最多 ``group_max_per_hour`` 次；
不在静默时段；说话内容至少 2 个字。

另外负责关切的整理：过了预期结案时间、或 ``stale_days`` 天没有任何进展的关切标记为 expired。
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope

OPEN_STATUSES = ("active", "progressing")


def concern_subject_ids(evidence: Any) -> set[str]:
    try:
        items = json.loads(evidence) if isinstance(evidence, str) else evidence
    except (TypeError, ValueError):
        return set()
    if not isinstance(items, list):
        return set()
    return {
        str(item.get("user_id")) for item in items
        if isinstance(item, dict) and item.get("kind") == "subject" and item.get("user_id")
    }


class ConcernFollowupService:
    def __init__(
        self,
        db: Any,
        write_gateway: Any = None,
        *,
        enabled: bool = False,
        cooldown_hours: float = 12.0,
        group_min_gap_seconds: float = 600.0,
        group_max_per_hour: int = 3,
        quiet_hours: tuple[int, int] = (1, 7),
        stale_days: float = 30.0,
    ):
        self.db = db
        self.write_gateway = write_gateway
        self.enabled = bool(enabled)
        self.cooldown_seconds = max(0.0, float(cooldown_hours)) * 3600
        self.group_min_gap = max(0.0, float(group_min_gap_seconds))
        self.group_max_per_hour = max(1, int(group_max_per_hour))
        self.quiet_hours = quiet_hours
        self.stale_seconds = max(1.0, float(stale_days)) * 86400
        self._followed: dict[int, float] = {}
        self._group_history: dict[str, deque] = {}
        self.stats: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def _quiet(self, now: float) -> bool:
        start, end = self.quiet_hours
        if start == end:
            return False
        hour = datetime.fromtimestamp(now).hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def open_concerns_for(self, scope: RuntimeScope, sender_id: str, sender_name: str = "") -> list[dict[str, Any]]:
        """当前说话人作为对象的未了关切（证据里记了对象 id；旧关切按「昵称：」前缀兜底匹配）。"""
        conn = getattr(self.db, "conn", None)
        if conn is None or scope.session is None or not sender_id:
            return []
        rows = conn.execute(
            f"""SELECT id, topic, intensity, status, evidence, created_at, last_triggered, last_progress_at
                  FROM scoped_soul_concerns
                 WHERE bot_id=? AND session_id=? AND visibility=? AND status IN ({','.join('?' * len(OPEN_STATUSES))})
                 ORDER BY intensity DESC, id DESC LIMIT 50""",
            (scope.bot_id, scope.session.id, scope.visibility, *OPEN_STATUSES),
        ).fetchall()
        name = str(sender_name or "").strip()
        found = []
        for row in rows:
            subjects = concern_subject_ids(row[4])
            topic = str(row[1] or "")
            matched = str(sender_id) in subjects if subjects else bool(name) and topic.startswith(f"{name}：")
            if matched:
                found.append({
                    "id": int(row[0]), "topic": topic, "intensity": float(row[2] or 0), "status": row[3],
                    "created_at": row[5], "last_progress_at": row[7] or row[6],
                })
        return found

    def claim_followup(
        self, scope: RuntimeScope, sender_id: str, sender_name: str, message: str, *, now: float | None = None,
    ) -> dict[str, Any] | None:
        """满足全部门控时返回要跟进的关切，并记下本次跟进；否则返回 None。"""
        if not self.enabled or scope.visibility != "group" or scope.session is None:
            return None
        if len(str(message or "").strip()) < 2:
            return None
        stamp = float(now if now is not None else time.time())
        try:
            candidates = self.open_concerns_for(scope, sender_id, sender_name)
        except Exception as exc:
            logger.debug(f"[WaveMemory] 读取关切失败: {exc!r}")
            return None
        candidates = [item for item in candidates if stamp - self._followed.get(item["id"], 0.0) >= self.cooldown_seconds]
        if not candidates:
            return None
        if self._quiet(stamp):
            self._count("skip_quiet_hours")
            return None
        history = self._group_history.setdefault(scope.session.id, deque(maxlen=self.group_max_per_hour))
        while history and stamp - history[0] > 3600:
            history.popleft()
        if history and stamp - history[-1] < self.group_min_gap:
            self._count("skip_group_gap")
            return None
        if len(history) >= self.group_max_per_hour:
            self._count("skip_group_hourly")
            return None
        chosen = candidates[0]
        self._followed[chosen["id"]] = stamp
        history.append(stamp)
        self._count("followup")
        return chosen

    def mark_followed(self, concern_id: int, *, now: float | None = None) -> None:
        """羽书本来就要回复这个人时顺带跟进，也计入冷却，避免同一件事反复追问。"""
        self._followed[int(concern_id)] = float(now if now is not None else time.time())

    def recently_followed(self, concern_id: int, *, now: float | None = None) -> bool:
        stamp = float(now if now is not None else time.time())
        return stamp - self._followed.get(int(concern_id), 0.0) < self.cooldown_seconds

    @staticmethod
    def hint(concern: dict[str, Any], sender_name: str, *, proactive: bool) -> str:
        since = ""
        try:
            since = datetime.fromtimestamp(float(concern.get("created_at") or 0)).strftime("%m-%d")
        except (TypeError, ValueError, OSError):
            since = ""
        who = sender_name or "这位群友"
        opener = f"{who} 刚在群里说话，你没被点名，是你自己想开口" if proactive else f"你正在回复 {who}"
        return (
            f"【你惦记的事】{opener}。你之前记挂着：{concern.get('topic')}"
            f"（concern:{concern.get('id')}{'，' + since + ' 记下' if since else ''}）。"
            "合适就自然地问一句后来怎么样、或兑现答应过的事；不合适就正常说话，别硬转话题。"
            f"这件事已经了结就调用 wave_memory_note_concern(action=resolve, concern_id={concern.get('id')}, note=怎么了结的)。"
        )

    # ─── 整理 ───

    async def tidy(self, *, now: float | None = None) -> int:
        """过期或长期没有进展的关切标记为 expired。返回处理条数。"""
        gateway = self.write_gateway
        conn = getattr(self.db, "conn", None)
        if gateway is None or conn is None or not callable(getattr(gateway, "transition_concern", None)):
            return 0
        stamp = float(now if now is not None else time.time())
        rows = conn.execute(
            """SELECT id, bot_id, session_id, visibility, expected_resolution_at,
                      COALESCE(last_progress_at, last_triggered, created_at)
                 FROM scoped_soul_concerns WHERE status IN ('active', 'progressing', 'dormant')"""
        ).fetchall()
        expired = 0
        for concern_id, bot_id, session_id, visibility, due, last in rows:
            overdue = due is not None and stamp > float(due)
            stale = last is not None and stamp - float(last) > self.stale_seconds
            if not (overdue or stale):
                continue
            scope = _scope_from_row(bot_id, session_id, visibility)
            if scope is None:
                continue
            try:
                await gateway.transition_concern(
                    scope=scope, action="expire", concern_id=int(concern_id),
                    note="过了预期结案时间" if overdue else f"{int(self.stale_seconds // 86400)} 天没有进展",
                    idempotency_hint=f"concern_tidy:{concern_id}:{int(stamp // 86400)}",
                    actor="concern_followup",
                )
                expired += 1
            except Exception as exc:
                logger.debug(f"[WaveMemory] 关切整理失败 concern:{concern_id}: {exc!r}")
        if expired:
            logger.info(f"[WaveMemory] 关切整理：{expired} 条过期或长期无进展，已标记 expired")
        return expired


def _scope_from_row(bot_id: str, session_id: str, visibility: str) -> RuntimeScope | None:
    try:
        from ..domain.scope import SessionRef
    except ImportError:  # pragma: no cover
        from domain.scope import SessionRef
    parts = str(session_id or "").split(":", 2)
    if len(parts) != 3 or visibility not in {"group", "private"}:
        return None
    platform_id, kind, conversation_id = parts
    try:
        return RuntimeScope(
            bot_id=str(bot_id), visibility=visibility,
            session=SessionRef(id=str(session_id), platform_id=platform_id, kind=kind, conversation_id=conversation_id),
        )
    except Exception:
        return None


__all__ = ["ConcernFollowupService", "concern_subject_ids"]
