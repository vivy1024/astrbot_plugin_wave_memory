"""关切驱动的主动跟进：关切对象在群里出现时，可按配置主动跟进。

过去关切记下后什么也不影响：主动回复整条链是死代码，关切只出现在记账提醒的候选里。这里改成
事件驱动——不另起定时发消息，而是在群消息进来、说话人正是某条未了关切的对象时，把这条消息标记为
需要 Bot 回应（AstrBot 的 ``is_at_or_wake_command``），并在请求里附一段「你惦记的事」提示。回复走
正常链路：完整记忆注入、人设、身份防线都照常生效。

门控（任一不满足都不开口）：配置开关 ``Learning_Settings.proactive_reply_enabled``（默认关闭；
关闭时不主动开口，但 Bot 本来就要回复这个人时仍会附上提示）；关切处于 active / progressing；同一条关切 ``cooldown_hours``
内只跟进一次；同一群两次主动至少间隔 ``group_min_gap_seconds``、每小时最多 ``group_max_per_hour`` 次；
不在静默时段；说话内容至少 2 个字。

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
            cutoff = self.clock() - self.cooldown_seconds
            self._followed = {int(key): float(value) for key, value in dict(raw).items() if float(value) >= cutoff}
        except Exception as exc:
            logger.warning(f"[WaveMemory] 关切跟进记录读取失败（按空处理）: {exc!r}")
            self._followed = {}

    def _save_followed(self, now: float) -> None:
        if not self.state_path:
            return
        cutoff = now - self.cooldown_seconds
        self._followed = {key: value for key, value in self._followed.items() if value >= cutoff}
        partial = f"{self.state_path}.partial"
        try:
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump({str(key): value for key, value in self._followed.items()}, handle)
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

    def claim_followup(
        self, scope: RuntimeScope, sender_id: str, sender_name: str, message: str, *, now: float | None = None,
        allowed: bool = True, accept: Callable[[dict[str, Any]], bool] | None = None,
        on_skip: Callable[[str, float], None] | None = None,
    ) -> dict[str, Any] | None:
        """全部门控通过才占用持久冷却；accept 是零模型规则筛选，不是发送效果反馈。"""
        if allowed is not True or not self.enabled or scope.visibility != "group" or scope.session is None:
            return None
        if len(str(message or "").strip()) < 2:
            return None
        stamp = float(now if now is not None else self.clock())
        try:
            candidates = self.open_concerns_for(scope, sender_id, sender_name)
        except Exception as exc:
            logger.debug(f"[WaveMemory] 读取关切失败: {exc!r}")
            return None
        # 还没到日子的事（明天才考试）不主动去问结果
        today = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
        candidates = [item for item in candidates if not item.get("due") or item["due"] <= today]
        cooling = [self._followed[item["id"]] + self.cooldown_seconds - stamp
                   for item in candidates if self.recently_followed(item["id"], now=stamp)]
        candidates = [item for item in candidates if not self.recently_followed(item["id"], now=stamp)]
        if not candidates:
            if cooling and on_skip:
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
        if history and stamp - history[-1] < self.group_min_gap:
            self._count("skip_group_gap")
            if on_skip:
                on_skip("concern_group_gap", history[-1] + self.group_min_gap - stamp)
            return None
        if len(history) >= self.group_max_per_hour:
            self._count("skip_group_hourly")
            if on_skip:
                on_skip("concern_hourly_limit", max(0.0, history[0] + 3600 - stamp))
            return None
        chosen = candidates[0]
        if accept is not None and not accept(chosen):
            self._count("skip_reply_rule")
            return None
        self._followed[chosen["id"]] = stamp
        self._save_followed(stamp)
        history.append(stamp)
        self._count("followup")
        return chosen

    def mark_followed(self, concern_id: int, *, now: float | None = None) -> None:
        """本来就要回复这个人时顺带跟进，也计入冷却，避免同一件事反复追问。"""
        stamp = float(now if now is not None else self.clock())
        self._followed[int(concern_id)] = stamp
        self._save_followed(stamp)

    def recently_followed(self, concern_id: int, *, now: float | None = None) -> bool:
        stamp = float(now if now is not None else self.clock())
        previous = self._followed.get(int(concern_id))
        return previous is not None and stamp - previous < self.cooldown_seconds

    def reaction_summary(self, scope: RuntimeScope, sender_id: str, *, days: float = 30.0) -> str:
        """这个人最近怎么回应她的话（回复效果反馈记录的承接 / 纠正 / 拒绝）；没有记录返回空串。"""
        conn = getattr(self.db, "conn", None)
        if conn is None or scope.session is None or not sender_id:
            return ""
        try:
            rows = conn.execute(
                """SELECT outcome, source_quote FROM reply_feedback_events
                    WHERE bot_id=? AND session_id=? AND visibility=? AND speaker_id=? AND outcome != 'unknown'
                      AND created_at >= ? ORDER BY id DESC LIMIT 20""",
                (scope.bot_id, scope.session.id, scope.visibility, str(sender_id), self.clock() - days * 86400),
            ).fetchall()
        except Exception:
            return ""  # 回复效果反馈没开过时没有这张表
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
                f"觉得现在不合适就只回复「{SILENCE_MARK}」四个字符，这句话不会发出去。"
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
