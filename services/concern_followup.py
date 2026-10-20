"""关切驱动的主动跟进：关切对象在群里出现时，可按配置主动跟进。

过去关切记下后什么也不影响：主动回复整条链是死代码，关切只出现在记账提醒的候选里。这里改成
事件驱动——不另起定时发消息，而是在群消息进来、说话人正是某条未了关切的对象时，把这条消息标记为
需要 Bot 回应（AstrBot 的 ``is_at_or_wake_command``），并在请求里附一段「你惦记的事」提示。回复走
正常链路：完整记忆注入、人设、身份防线都照常生效。

门控（任一不满足都不开口）：配置开关 ``Learning_Settings.proactive_reply_enabled``（默认关闭；
关闭时不主动开口，但 Bot 本来就要回复这个人时仍会附上提示）；关切处于 active / progressing；同一条关切 ``cooldown_hours``
内只跟进一次；同一群两次主动至少间隔 ``group_min_gap_seconds``、每小时最多 ``group_max_per_hour`` 次；
不在静默时段；说话内容至少 2 个字。

占用是两段式的：``claim_followup`` 只把这条关切临时占住，避免同一轮里重复追问；真的说出口
（``confirm_followup(spoke=True)``）才写完整冷却与群频率记录。模型选择沉默、调用 hold_peace、
平台否决或请求根本没走到发送时，只留一小段「考虑过」的冷却，不把一次没发生的跟进记成发生过。

另外负责关切的整理：过了预期结案时间、或 ``stale_days`` 天没有任何进展的关切标记为 expired。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

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


def concern_due_date(evidence: Any) -> str:
    """证据里记的「哪天发生/出结果」（YYYY-MM-DD），没有返回空串。"""
    try:
        items = json.loads(evidence) if isinstance(evidence, str) else evidence
    except (TypeError, ValueError):
        return ""
    if not isinstance(items, list):
        return ""
    for item in items:
        if isinstance(item, dict) and item.get("kind") == "due" and item.get("date"):
            return str(item["date"])[:10]
    return ""


# 主动开口时她可以选择不说：只回复这个标记，发送前会被丢弃（app/ingress 的装饰阶段）
SILENCE_MARK = "[沉默]"

# 一次请求从占用到发送的最长等待；超过就当这次没发生，允许下一轮重新考虑。
PENDING_TTL_SECONDS = 180.0
# 她考虑过但选择不说：短时间内不反复拿同一件事去问她，也不消耗完整跟进冷却。
CONSIDERED_COOLDOWN_SECONDS = 900.0


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
        state_path: str | None = None,
        clock: Callable[[], float] = time.time,
        pending_ttl_seconds: float = PENDING_TTL_SECONDS,
        considered_cooldown_seconds: float = CONSIDERED_COOLDOWN_SECONDS,
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
        self._considered: dict[int, float] = {}
        # 只在本进程内有效：请求结束前不重复占用，进程重启就当作这次没发生。
        self._pending: dict[int, dict[str, Any]] = {}
        self.pending_ttl_seconds = max(1.0, float(pending_ttl_seconds))
        self.considered_cooldown_seconds = max(0.0, float(considered_cooldown_seconds))
        self._group_history: dict[tuple[str, str], deque] = {}
        self.stats: dict[str, int] = {}
        self.clock = clock
        # 跟进冷却落盘：只在内存里时，重启后同一条关切会在冷却期内再被追问一次
        self.state_path = state_path
        self._load_followed()

    def _load_followed(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("state must be an object")
            # 兼容旧格式：整个文件就是 {concern_id: timestamp}
            followed_raw = raw.get("followed") if "followed" in raw or "considered" in raw else raw
            considered_raw = raw.get("considered") if isinstance(raw.get("considered"), dict) else {}
            now = self.clock()
            followed_cutoff = now - self.cooldown_seconds
            considered_cutoff = now - self.considered_cooldown_seconds
            self._followed = {
                int(key): float(value) for key, value in dict(followed_raw or {}).items()
                if float(value) >= followed_cutoff
            }
            self._considered = {
                int(key): float(value) for key, value in dict(considered_raw).items()
                if float(value) >= considered_cutoff
            }
        except Exception as exc:
            logger.warning(f"[WaveMemory] 关切跟进记录读取失败（按空处理）: {exc!r}")
            self._followed = {}
            self._considered = {}

    def _save_followed(self, now: float) -> None:
        if not self.state_path:
            return
        followed_cutoff = now - self.cooldown_seconds
        considered_cutoff = now - self.considered_cooldown_seconds
        self._followed = {key: value for key, value in self._followed.items() if value >= followed_cutoff}
        self._considered = {
            key: value for key, value in self._considered.items() if value >= considered_cutoff
        }
        partial = f"{self.state_path}.partial"
        try:
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "followed": {str(key): value for key, value in self._followed.items()},
                        "considered": {str(key): value for key, value in self._considered.items()},
                    },
                    handle,
                )
            os.replace(partial, self.state_path)
        except Exception as exc:
            logger.debug(f"[WaveMemory] 关切跟进记录保存失败: {exc!r}")

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
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(scoped_soul_concerns)").fetchall()}
        concern_type_expr = "concern_type" if "concern_type" in columns else "''"
        rows = conn.execute(
            f"""SELECT id, topic, intensity, status, evidence, created_at, last_triggered, last_progress_at
                         , {concern_type_expr} AS concern_type
                  FROM scoped_soul_concerns
                 WHERE bot_id=? AND session_id=? AND visibility=? AND status IN ({','.join('?' * len(OPEN_STATUSES))})
                 ORDER BY intensity DESC, id DESC LIMIT 50""",
            (scope.bot_id, scope.session.id, scope.visibility, *OPEN_STATUSES),
        ).fetchall()
        name = str(sender_name or "").strip()
        found = []
        for row in rows:
            if str(row[8] or "") == "social_anchor":
                continue
            subjects = concern_subject_ids(row[4])
            topic = str(row[1] or "")
            matched = str(sender_id) in subjects if subjects else bool(name) and topic.startswith(f"{name}：")
            if matched:
                found.append({
                    "id": int(row[0]), "topic": topic, "intensity": float(row[2] or 0), "status": row[3],
                    "created_at": row[5], "last_progress_at": row[7] or row[6],
                    "concern_type": str(row[8] or ""), "due": concern_due_date(row[4]),
                })
        return found

    def _prune_pending(self, now: float) -> None:
        stale = [
            concern_id for concern_id, pending in self._pending.items()
            if now - float(pending.get("stamp") or 0.0) > self.pending_ttl_seconds
        ]
        for concern_id in stale:
            self._pending.pop(concern_id, None)

    def is_pending(self, concern_id: int, *, now: float | None = None) -> bool:
        stamp = float(now if now is not None else self.clock())
        self._prune_pending(stamp)
        return int(concern_id) in self._pending

    def claim_followup(
        self, scope: RuntimeScope, sender_id: str, sender_name: str, message: str, *, now: float | None = None,
        allowed: bool = True, accept: Callable[[dict[str, Any]], bool] | None = None,
        on_skip: Callable[[str, float], None] | None = None,
    ) -> dict[str, Any] | None:
        """全部门控通过才临时占住一条关切；说不说得出口，要等 ``confirm_followup``。"""
        if allowed is not True or not self.enabled or scope.visibility != "group" or scope.session is None:
            return None
        if len(str(message or "").strip()) < 2:
            return None
        stamp = float(now if now is not None else self.clock())
        self._prune_pending(stamp)
        try:
            candidates = self.open_concerns_for(scope, sender_id, sender_name)
        except Exception as exc:
            logger.debug(f"[WaveMemory] 读取关切失败: {exc!r}")
            return None
        # 还没到日子的事（明天才考试）不主动去问结果
        today = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
        candidates = [item for item in candidates if not item.get("due") or item["due"] <= today]
        pending_remaining = [
            float(self._pending[item["id"]]["stamp"]) + self.pending_ttl_seconds - stamp
            for item in candidates if item["id"] in self._pending
        ]
        candidates = [item for item in candidates if item["id"] not in self._pending]
        cooling = [
            remaining for item in candidates
            if (remaining := self.cooldown_remaining(item["id"], now=stamp)) > 0
        ]
        candidates = [item for item in candidates if not self.recently_followed(item["id"], now=stamp)]
        if not candidates:
            if pending_remaining and on_skip:
                on_skip("concern_pending", min(pending_remaining))
            elif cooling and on_skip:
                on_skip("concern_cooldown", min(cooling))
            return None
        if self._quiet(stamp):
            self._count("skip_quiet_hours")
            if on_skip:
                on_skip("concern_quiet_hours", 0.0)
            return None
        history = self._group_history.setdefault((scope.bot_id, scope.session.id), deque(maxlen=self.group_max_per_hour))
        while history and stamp - history[0] > 3600:
            history.popleft()
        # 还在等模型回答的主动占用也计入群频率：否则同一群两条消息可以同时通过门控，
        # 然后在发送阶段各自 confirm，实际发出去的条数超过上限。
        pending_stamps = [
            float(pending.get("stamp") or 0.0)
            for pending in self._pending.values()
            if pending.get("proactive") and pending.get("group") == (scope.bot_id, scope.session.id)
        ]
        last_open = max([history[-1] if history else 0.0, *pending_stamps])
        if last_open and stamp - last_open < self.group_min_gap:
            self._count("skip_group_gap")
            if on_skip:
                on_skip("concern_group_gap", last_open + self.group_min_gap - stamp)
            return None
        if len(history) + len(pending_stamps) >= self.group_max_per_hour:
            self._count("skip_group_hourly")
            if on_skip:
                on_skip("concern_hourly_limit", max(0.0, history[0] + 3600 - stamp) if history else 0.0)
            return None
        chosen = candidates[0]
        if accept is not None and not accept(chosen):
            self._count("skip_reply_rule")
            return None
        self._pending[chosen["id"]] = {
            "stamp": stamp,
            "group": (scope.bot_id, scope.session.id),
            "proactive": True,
        }
        self._count("claimed")
        return chosen

    def reserve_followup(
        self, scope: RuntimeScope, concern: dict[str, Any], *, now: float | None = None,
    ) -> bool:
        """本来就要回复这个人时顺带想起这件事：只占位，不写冷却、不计主动频率。"""
        if scope.session is None:
            return False
        try:
            concern_id = int(concern.get("id") or 0)
        except (TypeError, ValueError):
            return False
        if concern_id <= 0:
            return False
        stamp = float(now if now is not None else self.clock())
        self._prune_pending(stamp)
        if concern_id in self._pending:
            return False
        self._pending[concern_id] = {
            "stamp": stamp,
            "group": (scope.bot_id, scope.session.id),
            "proactive": False,
        }
        return True

    def confirm_followup(self, concern_id: int, *, spoke: bool, now: float | None = None) -> bool:
        """请求走到发送阶段后结算：说出口才记完整冷却；选择沉默只记一小段「考虑过」。"""
        pending = self._pending.pop(int(concern_id), None)
        if pending is None:
            return False
        stamp = float(now if now is not None else self.clock())
        if spoke:
            self._considered.pop(int(concern_id), None)
            self._followed[int(concern_id)] = stamp
            self._save_followed(stamp)
            if pending.get("proactive"):
                group = pending.get("group")
                if isinstance(group, tuple) and len(group) == 2:
                    history = self._group_history.setdefault(group, deque(maxlen=self.group_max_per_hour))
                    history.append(stamp)
            self._count("followup")
        else:
            self._considered[int(concern_id)] = stamp
            self._save_followed(stamp)
            self._count("chose_silence")
        return True

    def release_followup(self, concern_id: int) -> bool:
        """请求被平台/框架否决或根本没走到模型：这次不算发生过，下一轮可以重新考虑。"""
        released = self._pending.pop(int(concern_id), None) is not None
        if released:
            self._count("released")
        return released

    def mark_followed(self, concern_id: int, *, now: float | None = None) -> None:
        """本来就要回复这个人时顺带跟进，也计入冷却，避免同一件事反复追问。"""
        stamp = float(now if now is not None else self.clock())
        self._considered.pop(int(concern_id), None)
        self._followed[int(concern_id)] = stamp
        self._save_followed(stamp)

    def cooldown_remaining(self, concern_id: int, *, now: float | None = None) -> float:
        stamp = float(now if now is not None else self.clock())
        remaining = 0.0
        previous = self._followed.get(int(concern_id))
        if previous is not None:
            remaining = max(remaining, previous + self.cooldown_seconds - stamp)
        considered = self._considered.get(int(concern_id))
        if considered is not None:
            remaining = max(remaining, considered + self.considered_cooldown_seconds - stamp)
        return max(0.0, remaining)

    def recently_followed(self, concern_id: int, *, now: float | None = None) -> bool:
        return self.cooldown_remaining(concern_id, now=now) > 0.0

    def _reaction_rows(
        self, scope: RuntimeScope, sender_id: str, *, days: float = 30.0,
    ) -> list[tuple[str, str]]:
        conn = getattr(self.db, "conn", None)
        if conn is None or scope.session is None or not sender_id:
            return []
        try:
            return conn.execute(
                """SELECT outcome, source_quote FROM reply_feedback_events
                    WHERE bot_id=? AND session_id=? AND visibility=? AND speaker_id=? AND outcome != 'unknown'
                      AND created_at >= ? ORDER BY id DESC LIMIT 20""",
                (scope.bot_id, scope.session.id, scope.visibility, str(sender_id), self.clock() - days * 86400),
            ).fetchall()
        except Exception:
            return []  # 回复效果反馈没开过时没有这张表

    def reaction_counts(
        self, scope: RuntimeScope, sender_id: str, *, days: float = 30.0,
    ) -> dict[str, int]:
        """这个人最近怎么回应她的话，只回计数，供「心事」判断她会不会犹豫。"""
        counts: dict[str, int] = {}
        for outcome, _quote in self._reaction_rows(scope, sender_id, days=days):
            counts[str(outcome)] = counts.get(str(outcome), 0) + 1
        return counts

    def reaction_summary(self, scope: RuntimeScope, sender_id: str, *, days: float = 30.0) -> str:
        """这个人最近怎么回应她的话（回复效果反馈记录的承接 / 纠正 / 拒绝）；没有记录返回空串。"""
        rows = self._reaction_rows(scope, sender_id, days=days)
        if not rows:
            return ""
        labels = {"accepted": "接住了你的话", "corrected": "纠正过你", "rejected": "不想理你 / 怼回来"}
        counts: dict[str, int] = {}
        for outcome, _quote in rows:
            counts[outcome] = counts.get(outcome, 0) + 1
        parts = [f"{labels[key]} {counts[key]} 次" for key in ("accepted", "corrected", "rejected") if counts.get(key)]
        latest = next((str(quote) for _outcome, quote in rows if quote), "")
        return f"最近一个月 ta 对你说的话：{'，'.join(parts)}" + (f"（最近一次 ta 说「{latest[:40]}」）" if latest else "")

    @staticmethod
    def hint(concern: dict[str, Any], sender_name: str, *, proactive: bool, reactions: str = "") -> str:
        since = ""
        try:
            since = datetime.fromtimestamp(float(concern.get("created_at") or 0)).strftime("%m-%d")
        except (TypeError, ValueError, OSError):
            since = ""
        who = sender_name or "这位群友"
        opener = f"{who} 刚在群里说话，你没被点名，是你自己想开口" if proactive else f"你正在回复 {who}"
        due = f"，说是 {concern['due']} 的事" if concern.get("due") else ""
        text = (
            f"【你惦记的事】{opener}。你之前记挂着：{concern.get('topic')}"
            f"（concern:{concern.get('id')}{'，' + since + ' 记下' if since else ''}{due}）。"
            "合适就自然地问一句后来怎么样、或兑现答应过的事；不合适就正常说话，别硬转话题。"
            f"这件事已经了结就调用 wave_memory_note_concern(action=resolve, concern_id={concern.get('id')}, note=怎么了结的)。"
        )
        if proactive:
            text += (
                (f"{reactions}。" if reactions else "")
                + f"要不要开口由你自己决定：看看 ta 正在聊什么、现在插话会不会打扰或冒犯人，"
                f"觉得现在不合适就调用 wave_memory_hold_peace(reason=一句话) 保有沉默，这条回复不会发出去。"
                f"（旧做法：只回复四个字「{SILENCE_MARK}」同样有效，但工具更能记清你当时为什么这么判断。）"
            )
        return text

    # ─── 整理 ───

    async def tidy(self, *, now: float | None = None) -> int:
        """过期或长期没有进展的关切标记为 expired。返回处理条数。"""
        gateway = self.write_gateway
        conn = getattr(self.db, "conn", None)
        if gateway is None or conn is None or not callable(getattr(gateway, "transition_concern", None)):
            return 0
        stamp = float(now if now is not None else self.clock())
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


__all__ = ["SILENCE_MARK", "ConcernFollowupService", "concern_due_date", "concern_subject_ids"]
