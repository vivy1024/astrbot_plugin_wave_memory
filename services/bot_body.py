"""Bot 身体服务：一个 Bot 一副激素状态，所有群、私聊、平台共用，零 LLM。

- 状态是**懒推进**的：每次读写前先按「实际在线」的时间把身体推进到现在（扣掉离线时长，封顶 48 小时）。
- 只有**真人事件**改变身体；模型自己不能改。
- 关系事件（被夸、被凶、投喂……）经 ``RELATIONSHIP_EVENT_MAP`` 作用到身体，来源幂等。
- 同一个人的同一类事件 24 小时内重复发生按 0.8ⁿ 习惯化。
- 对外只给边沿事实（刚越过阈值才说一句）与行为系数，不给数值，不写「你很生气」。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

try:
    from ..domain import body_policy as bp
except ImportError:  # pragma: no cover - 顶层导入
    from domain import body_policy as bp

logger = logging.getLogger(__name__)

# 隔多久再见到同一个人算「重逢」（秒）
REUNION_GAP_SECONDS = 2 * 3600.0
# 一次推进的最短间隔：避免每条消息都重算积分
MIN_TICK_SECONDS = 20.0
EVENT_RETENTION_SECONDS = 30 * 24 * 3600.0


class BotBodyService:
    def __init__(
        self,
        db: Any,
        *,
        profile_for: Callable[[str], Any] | None = None,
        closeness_for: Callable[[str, str], float] | None = None,
        enabled: bool = True,
        clock: Callable[[], float] | None = None,
        hour_of: Callable[[float], float] | None = None,
    ):
        self.db = db
        self.profile_for = profile_for or (lambda _bot_id: None)
        # (bot_id, principal_id) -> 0..100 的关系温度；未提供则一律按一般熟
        self.closeness_for = closeness_for or (lambda _bot, _principal: 50.0)
        self.clock = clock or time.time
        self.hour_of = hour_of or self._local_hour
        self.enabled = bool(enabled) and self.repository is not None
        self._lock = threading.RLock()
        self._stats: dict[str, int] = {}
        # 睡眠态下被叫醒的窗口计数：(bot_id, principal) -> [时间戳]
        self._wake_calls: dict[tuple[str, str], list[float]] = {}
        self._awake_until: dict[str, float] = {}
        self._sleep_replied: dict[tuple[str, str], float] = {}
        self._quiet_replied: dict[tuple[str, str], float] = {}
        self._bots: set[str] = set()

    # ─── 基础 ───

    @property
    def repository(self) -> Any:
        return getattr(self.db, "bot_body", None)

    @staticmethod
    def _local_hour(ts: float) -> float:
        lt = time.localtime(ts)
        return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    def _count(self, key: str, amount: int = 1) -> None:
        self._stats[key] = self._stats.get(key, 0) + amount

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def _now(self, now: float | None) -> float:
        return float(now if now is not None else self.clock())

    def _settings(self, bot_id: str) -> Any:
        return getattr(self.profile_for(bot_id), "body", None)

    def active_for(self, bot_id: str) -> bool:
        """这个 Bot 的身体是否开启。没有 Profile（如测试或临时调用）按开启处理。"""
        if not self.enabled:
            return False
        profile = self.profile_for(bot_id)
        if profile is None:
            return True
        body = getattr(profile, "body", None)
        return bool(getattr(body, "enabled", True)) if body is not None else True

    def _gain(self, bot_id: str) -> float:
        try:
            return float(getattr(self._settings(bot_id), "hormone_gain", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def _sleep_window(self, bot_id: str) -> tuple[int | None, int | None]:
        body = self._settings(bot_id)
        start = getattr(body, "sleep_start", None)
        end = getattr(body, "sleep_end", None)
        try:
            return (int(start), int(end)) if start is not None and end is not None else (None, None)
        except (TypeError, ValueError):
            return None, None

    # ─── 模型服务静默（如 antigravity 夜间停服） ───

    def _quiet_window(self, bot_id: str) -> tuple[int | None, int | None]:
        body = self._settings(bot_id)
        start = getattr(body, "service_quiet_start", None)
        end = getattr(body, "service_quiet_end", None)
        try:
            return (int(start), int(end)) if start is not None and end is not None else (None, None)
        except (TypeError, ValueError):
            return None, None

    def in_service_quiet(self, bot_id: str, *, now: float | None = None) -> bool:
        """现在是不是模型服务静默时段。不看 ``body.enabled``：没开激素的 Bot 也能配静默窗口。"""
        if not self.enabled:
            return False
        start, end = self._quiet_window(bot_id)
        if start is None or end is None:
            return False
        return bp.in_quiet_window(self.hour_of(self._now(now)), start, end)

    def quiet_reply(self, bot_id: str, principal_id: str = "", *, now: float | None = None, pick: int = 0) -> str:
        """静默时段被叫到回的话术；同一个人在冷却期内不重复回，返回空串表示这次保持沉默。

        话术里的 ``{end}`` 换成「几点回来」。冷却与睡眠短语共用 ``sleep_reply_cooldown_seconds``，
        但记账独立，所以静默话术不会吃掉睡眠短语的冷却。
        """
        stamp = self._now(now)
        body = self._settings(bot_id)
        cooldown = float(getattr(body, "sleep_reply_cooldown_seconds", 1800.0) or 0.0)
        key = (bot_id, principal_id)
        with self._lock:
            last = self._quiet_replied.get(key, 0.0)
            if cooldown > 0 and stamp - last < cooldown:
                return ""
            self._quiet_replied[key] = stamp
        phrases = [p for p in (getattr(body, "quiet_replies", None) or []) if p]
        if not phrases:
            return ""
        _start, end = self._quiet_window(bot_id)
        text = phrases[int(pick) % len(phrases)].replace("{end}", bp.quiet_end_label(end))
        self._count("quiet_reply")
        return text

    def sleep_reply(self, bot_id: str, principal_id: str = "", *, now: float | None = None, pick: int = 0) -> str:
        """睡着时回的固定短语；同一个人在冷却期内不重复回，返回空串表示这次保持沉默。"""
        stamp = self._now(now)
        body = self._settings(bot_id)
        cooldown = float(getattr(body, "sleep_reply_cooldown_seconds", 1800.0) or 0.0)
        key = (bot_id, principal_id)
        with self._lock:
            last = self._sleep_replied.get(key, 0.0)
            if cooldown > 0 and stamp - last < cooldown:
                return ""
            self._sleep_replied[key] = stamp
        phrases = [p for p in (getattr(body, "sleep_replies", None) or ["……zzz"]) if p]
        self._count("sleep_reply")
        return phrases[int(pick) % len(phrases)] if phrases else ""

    # ─── 状态推进 ───

    def _state(self, bot_id: str, now: float) -> dict[str, Any]:
        """读出并懒推进到 ``now``；首次见到的 Bot 从当前钟点的基线起步。"""
        repo = self.repository
        stored = repo.load_state(bot_id) if repo is not None else None
        hour_now = self.hour_of(now)
        if not stored or not stored.get("hormones"):
            return {
                "hormones": bp.default_state(hour_now),
                "last_tick_at": now,
                "last_active_at": now,
                "last_human_at": 0.0,
                "offline_since": 0.0,
                "edge_memo": {},
                "fresh": True,
            }
        state = dict(stored)
        last_tick = float(stored.get("last_tick_at") or now)
        offline_since = float(stored.get("offline_since") or 0.0)
        elapsed = max(0.0, now - last_tick)
        # 扣掉离线时长：关闭时记下 offline_since，重启后的第一次推进只算「关闭之前」的在线时间
        if offline_since and last_tick <= offline_since <= now:
            elapsed = max(0.0, offline_since - last_tick)
            state["offline_since"] = 0.0
            state["last_active_at"] = now
        quiet = max(0.0, now - float(stored.get("last_active_at") or now))
        if elapsed >= 1.0:
            state["hormones"] = bp.advance(
                stored["hormones"], elapsed_seconds=elapsed, hour_at=self.hour_of, start_ts=last_tick,
                quiet_seconds=max(0.0, quiet - elapsed),
            )
        state["last_tick_at"] = now
        return state

    def _persist(self, bot_id: str, state: Mapping[str, Any], now: float, **extra: Any) -> None:
        repo = self.repository
        if repo is None:
            return
        try:
            repo.save_state(
                bot_id,
                hormones=state["hormones"],
                last_tick_at=now,
                last_active_at=extra.get("last_active_at", state.get("last_active_at")),
                last_human_at=extra.get("last_human_at", state.get("last_human_at")),
                offline_since=extra.get("offline_since", state.get("offline_since")),
                edge_memo=extra.get("edge_memo", state.get("edge_memo")),
                now=now,
            )
        except Exception as exc:
            logger.debug(f"[WaveMemory] 身体状态写入失败 {bot_id}: {exc!r}")

    def tick(self, bot_ids: list[str] | None = None, *, now: float | None = None) -> None:
        """周期维护：推进并落盘，清理过期事件。"""
        if not self.enabled:
            return
        stamp = self._now(now)
        for bot_id in sorted(set(bot_ids or ()) | self._bots):
            if not self.active_for(bot_id):
                continue
            try:
                state = self._state(bot_id, stamp)
                self._persist(bot_id, state, stamp)
                self.repository.prune(bot_id, before=stamp - EVENT_RETENTION_SECONDS)
            except Exception as exc:
                logger.debug(f"[WaveMemory] 身体维护失败 {bot_id}: {exc!r}")

    def mark_offline(self, bot_ids: list[str] | None = None, *, now: float | None = None) -> None:
        """关闭前调用：记下离线起点。"""
        if not self.enabled:
            return
        stamp = self._now(now)
        for bot_id in sorted(set(bot_ids or ()) | self._bots):
            if not self.active_for(bot_id):
                continue
            try:
                state = self._state(bot_id, stamp)
                self._persist(bot_id, state, stamp, offline_since=stamp)
            except Exception as exc:
                logger.debug(f"[WaveMemory] 身体离线标记失败 {bot_id}: {exc!r}")

    # ─── 输入：事件 ───

    def _closeness(self, bot_id: str, principal: str) -> float:
        try:
            return float(self.closeness_for(bot_id, principal))
        except Exception:
            return 50.0

    def note_event(
        self,
        bot_id: str,
        kind: str,
        *,
        principal_id: str = "",
        scene: str = "",
        source_ref: str = "",
        gain: float = 1.0,
        now: float | None = None,
    ) -> dict[str, float] | None:
        """一次真人事件作用到身体上；返回实际增量（幂等重复或未启用返回 None）。"""
        if not self.active_for(bot_id) or kind not in bp.EVENT_TABLE:
            return None
        gain = float(gain) * self._gain(bot_id)
        if gain <= 0:
            return None
        stamp = self._now(now)
        repo = self.repository
        with self._lock:
            self._bots.add(bot_id)
            spec = bp.EVENT_TABLE[kind]
            prior = 0
            if spec.habituate and principal_id:
                prior = repo.count_recent(
                    bot_id, kind=kind, principal_id=principal_id, since=stamp - bp.HABITUATION_WINDOW_SECONDS,
                )
            state = self._state(bot_id, stamp)
            closeness = self._closeness(bot_id, principal_id) if principal_id else 50.0
            new_hormones, applied = bp.apply_event(
                state["hormones"], spec, closeness=closeness, prior_count=prior, gain=gain,
            )
            event_id = repo.add_event(
                bot_id, kind=kind, deltas=applied, principal_id=principal_id, scene=scene,
                source_ref=source_ref, created_at=stamp,
            )
            if event_id is None:
                self._count("duplicate")
                return None
            state["hormones"] = new_hormones
            self._persist(bot_id, state, stamp, last_active_at=stamp)
            self._count(f"event:{kind}")
            return applied

    def note_human_message(
        self,
        bot_id: str,
        principal_id: str,
        *,
        directed: bool = False,
        scene: str = "",
        now: float | None = None,
    ) -> None:
        """真人在任何场合说了话：刷新活跃时间；被叫到则记一次；隔很久再见记一次重逢。"""
        if not self.active_for(bot_id) or not principal_id:
            return
        stamp = self._now(now)
        repo = self.repository
        with self._lock:
            self._bots.add(bot_id)
            previous = repo.touch_seen(bot_id, principal_id, at=stamp)
            state = self._state(bot_id, stamp)
            self._persist(bot_id, state, stamp, last_active_at=stamp, last_human_at=stamp)
        if previous and stamp - previous >= REUNION_GAP_SECONDS:
            self.note_event(bot_id, "reunion", principal_id=principal_id, scene=scene, now=stamp)
        if directed:
            self.note_event(bot_id, "addressed", principal_id=principal_id, scene=scene, now=stamp)

    def note_relationship_event(
        self,
        bot_id: str,
        event_type: str,
        *,
        principal_id: str,
        relationship_event_id: int | str | None = None,
        is_leap: bool = False,
        scene: str = "",
        now: float | None = None,
    ) -> dict[str, float] | None:
        """关系事件作用到身体：经映射表；跃迁时分量翻倍；按关系事件 id 幂等。"""
        kind = bp.RELATIONSHIP_EVENT_MAP.get(str(event_type or ""))
        if kind is None:
            return None
        ref = f"relationship_event:{relationship_event_id}" if relationship_event_id not in (None, "") else ""
        return self.note_event(
            bot_id, kind, principal_id=principal_id, scene=scene, source_ref=ref,
            gain=2.0 if is_leap else 1.0, now=now,
        )

    def note_snub(self, bot_id: str, *, principal_id: str = "", scene: str = "", source_ref: str = "", now: float | None = None):
        """主动开口后无人接话（每次主动只记一次，由读空气的无人接话计数触发）。"""
        return self.note_event(bot_id, "snubbed", principal_id=principal_id, scene=scene, source_ref=source_ref, now=now)

    # ─── 输出 ───

    def reading(self, bot_id: str, *, principal_id: str = "", now: float | None = None) -> bp.BodyReading | None:
        if not self.active_for(bot_id):
            return None
        stamp = self._now(now)
        with self._lock:
            self._bots.add(bot_id)
            state = self._state(bot_id, stamp)
            closeness = self._closeness(bot_id, principal_id) if principal_id else 45.0
            start, end = self._sleep_window(bot_id)
            result = bp.read(
                state["hormones"], hour=self.hour_of(stamp), closeness=closeness, sleep_start=start, sleep_end=end,
            )
            if state.get("fresh") or stamp - float(state.get("last_tick_at") or 0.0) >= MIN_TICK_SECONDS:
                self._persist(bot_id, state, stamp)
            return result

    def render_lines(self, bot_id: str, *, principal_id: str = "", now: float | None = None) -> list[str]:
        """注入用的边沿事实：状态刚越过阈值才说一句，持续时闭嘴；总长度 ≤ 80 字。"""
        if not self.active_for(bot_id):
            return []
        stamp = self._now(now)
        with self._lock:
            reading = self.reading(bot_id, principal_id=principal_id, now=stamp)
            if reading is None:
                return []
            state = self._state(bot_id, stamp)
            lines, memo = bp.edge_notes(reading.flags, state.get("edge_memo") or {}, now=stamp)
            if lines or memo != (state.get("edge_memo") or {}):
                self._persist(bot_id, state, stamp, edge_memo=memo)
            if lines:
                self._count("rendered")
            return lines

    def mood_bias(self, bot_id: str, *, now: float | None = None) -> tuple[float, float]:
        """给快层心情的小偏置 (valence, arousal)，各夹在 ±0.2。"""
        reading = self.reading(bot_id, now=now)
        if reading is None:
            return 0.0, 0.0
        return reading.mood_bias, reading.arousal_bias

    # ─── 睡眠态 ───

    def is_asleep(self, bot_id: str, *, now: float | None = None) -> bool:
        stamp = self._now(now)
        if stamp < self._awake_until.get(bot_id, 0.0):
            return False
        reading = self.reading(bot_id, now=stamp)
        return bool(reading and reading.asleep)

    def should_wake(
        self,
        bot_id: str,
        principal_id: str,
        *,
        roll: float,
        now: float | None = None,
    ) -> bool:
        """睡着时被叫一次：10 分钟窗口内第 n 次起掷骰（``roll`` 0..1 由调用方给，便于测试）。

        醒了之后 10 分钟内保持清醒，不会因为同一波对话又睡回去。
        """
        stamp = self._now(now)
        key = (bot_id, principal_id)
        with self._lock:
            calls = [t for t in self._wake_calls.get(key, []) if stamp - t < bp.WAKE_WINDOW_SECONDS]
            calls.append(stamp)
            self._wake_calls[key] = calls[-12:]
        reading = self.reading(bot_id, now=stamp)
        sleepiness = reading.sleepiness if reading else 0.0
        woke = float(roll) < bp.wake_probability(len(calls), sleepiness=sleepiness)
        if woke:
            self._awake_until[bot_id] = stamp + bp.WAKE_WINDOW_SECONDS
            self._count("woken")
        return woke

    def snapshot(self, bot_id: str, *, now: float | None = None) -> dict[str, Any]:
        """管理台用：当前激素、读数与最近 24 小时的事件采样（不含任何人的私聊原文）。"""
        stamp = self._now(now)
        reading = self.reading(bot_id, now=stamp)
        if reading is None:
            return {"enabled": False}
        repo = self.repository
        events = repo.recent_events(bot_id, since=stamp - 24 * 3600.0, limit=200)
        return {
            "enabled": True,
            "policy_version": bp.POLICY_VERSION,
            "hormones": reading.hormones,
            "mood_bias": reading.mood_bias,
            "arousal_bias": reading.arousal_bias,
            "sleepiness": reading.sleepiness,
            "asleep": reading.asleep,
            "proactive_drive": reading.proactive_drive,
            "reply_length_factor": reading.reply_length_factor,
            "flags": reading.flags,
            "events": [
                {"kind": e["kind"], "deltas": e["deltas"], "at": e["created_at"]}
                for e in events
            ],
        }


__all__ = ["BotBodyService"]
