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
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
LATE_NIGHT_CALL_COOLDOWN_SECONDS = 20 * 60.0


def _resolve_tz(tz_name: str):
    cleaned = str(tz_name or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, Exception):
        if cleaned in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Hong_Kong", "Asia/Taipei", "PRC"}:
            return dt_timezone(timedelta(hours=8))
        if cleaned.upper() == "UTC":
            return dt_timezone.utc
        return dt_timezone(timedelta(hours=8))


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
        timezone: str = "Asia/Shanghai",
    ):
        self.db = db
        self.profile_for = profile_for or (lambda _bot_id: None)
        # (bot_id, principal_id) -> 0..100 的关系温度；未提供则一律按一般熟
        self.closeness_for = closeness_for or (lambda _bot, _principal: 50.0)
        self.clock = clock or time.time
        self.timezone = str(timezone or "Asia/Shanghai")
        self._tz = _resolve_tz(self.timezone)
        self.hour_of = hour_of or self._wall_hour
        self.enabled = bool(enabled) and self.repository is not None
        self._lock = threading.RLock()
        self._stats: dict[str, int] = {}
        # 睡眠态下被叫醒的窗口计数：(bot_id, principal) -> [时间戳]
        self._wake_calls: dict[tuple[str, str], list[float]] = {}
        self._awake_until: dict[str, float] = {}
        self._sleep_replied: dict[tuple[str, str], float] = {}
        self._quiet_replied: dict[tuple[str, str], float] = {}
        self._late_night_noted: dict[str, float] = {}
        self._bots: set[str] = set()

    # ─── 基础 ───

    @property
    def repository(self) -> Any:
        return getattr(self.db, "bot_body", None)

    def _wall_hour(self, ts: float) -> float:
        dt = datetime.fromtimestamp(float(ts), tz=self._tz)
        return dt.hour + dt.minute / 60.0 + dt.second / 3600.0

    def _day_key(self, ts: float) -> str:
        return datetime.fromtimestamp(float(ts), tz=self._tz).strftime("%Y-%m-%d")

    @staticmethod
    def _local_hour(ts: float) -> float:
        dt = datetime.fromtimestamp(float(ts), tz=_resolve_tz("Asia/Shanghai"))
        return dt.hour + dt.minute / 60.0 + dt.second / 3600.0

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

    def sleep_window_for(self, bot_id: str) -> tuple[int | None, int | None]:
        """返回该 Bot 配置的作息窗口 (start, end)，供读空气与心情深夜感知统一对齐。"""
        return self._sleep_window(bot_id)

    def sleep_behavior(self, bot_id: str) -> str:
        """睡眠响应模式：正式 Profile 默认 wakable（可被吵醒）；裸测试桩未设字段时保持 strict。"""
        body = self._settings(bot_id)
        if body is None:
            return "wakable"
        raw = getattr(body, "sleep_behavior", None)
        if raw in {"wakable", "drowsy_only", "strict"}:
            return str(raw)
        return "strict"

    def _effective_sleep_window(self, bot_id: str, stamp: float) -> tuple[float | None, float | None]:
        start, end = self._sleep_window(bot_id)
        if start is None or end is None or start == end:
            return None, None
        body = self._settings(bot_id)
        jitter_min = int(getattr(body, "sleep_jitter_minutes", 0) or 0)
        if jitter_min <= 0:
            return float(start), float(end)
        j_start, j_end = bp.daily_window_jitter(bot_id, self._day_key(stamp), jitter_min)
        return (float(start) + j_start) % 24.0, (float(end) + j_end) % 24.0

    def _active_grace_seconds(self, bot_id: str) -> float:
        body = self._settings(bot_id)
        try:
            return max(0.0, float(getattr(body, "active_chat_grace_seconds", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

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
                "edge_memo": {"_energy": bp.ENERGY_DEFAULT, "_resting": 0.0},
                "fresh": True,
            }
        state = dict(stored)
        memo = dict(state.get("edge_memo") or {})
        energy = float(memo.get("_energy", bp.ENERGY_DEFAULT))
        was_resting = bool(float(memo.get("_resting", 0.0)) >= 0.5)
        woken_until = float(memo.get("_woken_until", 0.0) or 0.0)
        if woken_until > self._awake_until.get(bot_id, 0.0):
            self._awake_until[bot_id] = woken_until

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
            eff_start, eff_end = self._effective_sleep_window(bot_id, now)
            energy = bp.advance_energy(
                energy,
                elapsed_seconds=elapsed,
                hour_at=self.hour_of,
                start_ts=last_tick,
                sleep_start=eff_start,
                sleep_end=eff_end,
            )
            was_resting = bp.next_resting_state(energy, was_resting)
        memo["_energy"] = round(energy, 3)
        memo["_resting"] = 1.0 if was_resting else 0.0
        state["edge_memo"] = memo
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

    def note_bot_reply(
        self,
        bot_id: str,
        *,
        chars: int = 0,
        scene: str = "",
        now: float | None = None,
    ) -> dict[str, float] | None:
        """Bot 实际开口发出一句回复：消耗一点身体精力、刷新对话余温时钟，深夜熬夜说话还会推高困倦。"""
        if not self.active_for(bot_id):
            return None
        stamp = self._now(now)
        hour = self.hour_of(stamp)
        late_night = bp.in_quiet_window(hour, 23.0, 5.5)
        with self._lock:
            self._bots.add(bot_id)
            state = self._state(bot_id, stamp)
            memo = dict(state.get("edge_memo") or {})
            before = float(memo.get("_energy", bp.ENERGY_DEFAULT))
            was_resting = bool(float(memo.get("_resting", 0.0)) >= 0.5)
            after, spent = bp.consume_reply_energy(before, chars=chars, late_night=late_night)
            resting = bp.next_resting_state(after, was_resting)
            memo["_energy"] = after
            memo["_resting"] = 1.0 if resting else 0.0
            memo["_last_reply_at"] = stamp
            state["edge_memo"] = memo
            self._persist(bot_id, state, stamp, last_active_at=stamp, edge_memo=memo)
            self._count("bot_reply")
            last_ln = self._late_night_noted.get(bot_id, 0.0)
            trigger_ln = late_night and (stamp - last_ln >= LATE_NIGHT_CALL_COOLDOWN_SECONDS)
            if trigger_ln:
                self._late_night_noted[bot_id] = stamp
        if trigger_ln:
            self.note_event(bot_id, "late_night_call", scene=scene, now=stamp)
        return {"energy": after, "spent": spent}

    def _vitals_context(self, bot_id: str, state: Mapping[str, Any], stamp: float) -> tuple[float, bool, bool, bool]:
        memo = state.get("edge_memo") or {}
        energy = float(memo.get("_energy", bp.ENERGY_DEFAULT))
        was_resting = bool(float(memo.get("_resting", 0.0)) >= 0.5)
        awake_until = max(self._awake_until.get(bot_id, 0.0), float(memo.get("_woken_until", 0.0) or 0.0))
        woken_drowsy = stamp < awake_until
        last_reply = float(memo.get("_last_reply_at", 0.0) or 0.0)
        grace_sec = self._active_grace_seconds(bot_id)
        bedtime_grace = bool(grace_sec > 0 and last_reply > 0 and 0.0 <= stamp - last_reply <= grace_sec and not woken_drowsy)
        return energy, was_resting, woken_drowsy, bedtime_grace

    # ─── 输出 ───

    def reading(self, bot_id: str, *, principal_id: str = "", now: float | None = None) -> bp.BodyReading | None:
        if not self.active_for(bot_id):
            return None
        stamp = self._now(now)
        with self._lock:
            self._bots.add(bot_id)
            state = self._state(bot_id, stamp)
            closeness = self._closeness(bot_id, principal_id) if principal_id else 45.0
            start, end = self._effective_sleep_window(bot_id, stamp)
            energy, was_resting, woken_drowsy, bedtime_grace = self._vitals_context(bot_id, state, stamp)
            result = bp.read(
                state["hormones"],
                hour=self.hour_of(stamp),
                closeness=closeness,
                sleep_start=start,
                sleep_end=end,
                energy=energy,
                was_resting=was_resting,
                woken_drowsy=woken_drowsy,
                bedtime_grace=bedtime_grace,
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
            raw_memo = dict(state.get("edge_memo") or {})
            vitals = {k: v for k, v in raw_memo.items() if str(k).startswith("_")}
            announced = {k: v for k, v in raw_memo.items() if not str(k).startswith("_")}
            lines, next_announced = bp.edge_notes(reading.flags, announced, now=stamp)
            merged_memo = {**vitals, **next_announced}
            if lines or next_announced != announced:
                self._persist(bot_id, state, stamp, edge_memo=merged_memo)
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

    def is_resting(self, bot_id: str, *, now: float | None = None) -> bool:
        """是否处于日间高强度聊天后的歇口气回血态（不拒绝被 @，但暂停主动抢话）。"""
        reading = self.reading(bot_id, now=self._now(now))
        return bool(reading and reading.resting)

    def should_wake(
        self,
        bot_id: str,
        principal_id: str,
        *,
        roll: float,
        now: float | None = None,
    ) -> bool:
        """睡着时被叫一次：决定这次是否醒来应答。

        - ``drowsy_only`` 模式：作息期间不拦截呼叫，始终放行并靠困意边沿提示调整语气；
        - ``wakable`` 模式（默认）：不设死第 1 次，浅睡/高亲密熟人第一声就有概率醒来，连叫必醒；
        - ``strict`` 模式：10 分钟窗口内第 2 次起按阶梯唤醒，第 6 次必醒。
        醒来后 10 分钟内保持清醒，并带上刚被吵醒的迷糊实感与轻微起床气。
        """
        stamp = self._now(now)
        behavior = self.sleep_behavior(bot_id)
        if behavior == "drowsy_only":
            return True
        key = (bot_id, principal_id)
        with self._lock:
            calls = [t for t in self._wake_calls.get(key, []) if stamp - t < bp.WAKE_WINDOW_SECONDS]
            calls.append(stamp)
            self._wake_calls[key] = calls[-12:]
            state = self._state(bot_id, stamp)
            closeness = self._closeness(bot_id, principal_id) if principal_id else 45.0
            start, end = self._effective_sleep_window(bot_id, stamp)
            energy, was_resting, _wd, bedtime_grace = self._vitals_context(bot_id, state, stamp)
            if bedtime_grace:
                return True
            raw_reading = bp.read(
                state["hormones"],
                hour=self.hour_of(stamp),
                closeness=closeness,
                sleep_start=start,
                sleep_end=end,
                energy=energy,
                was_resting=was_resting,
            )
            prob = bp.wake_probability(
                len(calls),
                sleepiness=raw_reading.sleepiness,
                first_call_can_wake=(behavior == "wakable"),
                closeness=closeness,
                stage=raw_reading.sleep_stage,
            )
            woke = float(roll) < prob
            if woke:
                awake_until = stamp + bp.WAKE_WINDOW_SECONDS
                self._awake_until[bot_id] = awake_until
                memo = dict(state.get("edge_memo") or {})
                memo["_woken_until"] = awake_until
                state["edge_memo"] = memo
                self._persist(bot_id, state, stamp, edge_memo=memo)
                self._count("woken")
        if woke:
            self.note_event(bot_id, "woken_up", principal_id=principal_id, now=stamp)
        return woke

    def snapshot(self, bot_id: str, *, now: float | None = None) -> dict[str, Any]:
        """管理台用：当前激素、精力、睡眠阶段读数与最近 24 小时的事件采样（不含任何人的私聊原文）。"""
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
            "energy": reading.energy,
            "resting": reading.resting,
            "sleep_stage": reading.sleep_stage,
            "woken_drowsy": reading.woken_drowsy,
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
