"""Bot 心情 v2：一个 Bot 一份心情，事件驱动、带惯性，向画像基线回落。

v5.1 之前的心情是「当前群最近 30 分钟的关键词统计」，按群各存一份：结果是群氛围而不是她本人的
心情，在 A 群被骂、B 群照样开心，也和发生在她身上的事无关。这里改成：

- **一份心情**：按 bot_id 存事件流水（``bot_mood_events``），所有群与私聊共享；
- **事件驱动**：冲她来的话（@/回复/私聊，只按这句话本身的情绪词，不读好感分数）、正式关系事件（被夸/被攻击/深谈……）、
  事后记账那次 LLM 调用顺带给出的「这段对话对你心情的影响」、群氛围（小权重），每条带来源与时间；
- **惯性与回归**：当前心情 = 画像基线 + Σ 事件冲击 × 0.5^(经过时间/半衰期)，强事件半衰期更长；
  可选疲劳（一段时间回复太多、深夜）；
- **注入自然语言**：情绪词 + 1–2 条最近原因（谁/什么事/多久前）+ 可选说话倾向，不输出裸数字；
  私聊里发生的事会影响心情，但在别处只说「私下有件事」，不带人名与内容。

画像（基线、敏感度、半衰期、氛围权重、疲劳、说话倾向）来自 Bot Profile 的 ``mood`` 段，
未配置时用中性默认值；这里不写死任何角色。
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.display_name import sanitize_display_name
    from ..domain.mood_policy import (
        MAX_FATIGUE_REPLY_THRESHOLD,
        MAX_FATIGUE_WINDOW_MINUTES,
        POLICY_VERSION,
        MoodContribution,
        MoodSettings,
        compose_mood,
        describe_age,
        effect_phrase,
        event_half_life,
        mood_event_horizon,
        mood_word,
        fatigue_level,
        night_active,
        parse_bookkeeping_mood,
        relationship_event_impulse,
        score_directed_message,
        speaking_hint,
    )
    from ..domain.scope import RuntimeScope
    from ..engine.db.bot_mood_repo import MoodReadBudgetExceeded
except ImportError:  # pragma: no cover - focused tests import top-level packages
    from domain.display_name import sanitize_display_name
    from domain.mood_policy import (
        MAX_FATIGUE_REPLY_THRESHOLD,
        MAX_FATIGUE_WINDOW_MINUTES,
        POLICY_VERSION,
        MoodContribution,
        MoodSettings,
        compose_mood,
        describe_age,
        effect_phrase,
        event_half_life,
        mood_event_horizon,
        mood_word,
        fatigue_level,
        night_active,
        parse_bookkeeping_mood,
        relationship_event_impulse,
        score_directed_message,
        speaking_hint,
    )
    from domain.scope import RuntimeScope
    from engine.db.bot_mood_repo import MoodReadBudgetExceeded

# 冲她来的话：同一场合同一个人 45 秒内的同向情绪只记最强的一条。
MESSAGE_MERGE_SECONDS = 45.0
# 关系事件导入：首次启用时回看这么久（之后按游标增量），更早的事件对心情早已衰减殆尽。
FIRST_IMPORT_LOOKBACK = 12 * 3600.0
# 同一个人 10 分钟内已经因为原话记过同向情绪，关系事件再来只按一半记，避免同一件事记两遍。
RELATIONSHIP_DEDUP_SECONDS = 600.0
ATMOSPHERE_INTERVAL = 900.0
SYNC_INTERVAL = 15.0
EVENT_RETENTION = 14 * 86400.0
# 一次合成最多 32768 条，原因只保留最多 20 条；超限明确不可用，绝不合成截断后的假心情。
EVENT_PAGE_SIZE = 256
EVENT_MAX_PAGES = 128
MAX_CAUSES = 20
MAX_REPLY_RECORDS = 2 * MAX_FATIGUE_REPLY_THRESHOLD
CAUSE_MIN_WEIGHT = 0.03
# 贡献权重 |v| + 0.5|a| 在仓储冲击上限 (|v|,|a|≤1) 下的最大值。
MAX_CONTRIBUTION_WEIGHT = 1.5
# 注入时心情快照的可信时长：tick 每 600 秒写一次，事件写入也会即时刷新。
SNAPSHOT_FRESH_SECONDS = 600.0
MAX_EVENT_VALENCE = 0.6
MAX_EVENT_AROUSAL = 0.5
_MARKUP = re.compile(r"\[[^\]]{0,200}\]|\[CQ:[^\]]*\]|@\S+")


def _principal_user_id(principal: str) -> str:
    text = str(principal or "")
    return text.rsplit(":user:", 1)[1] if ":user:" in text else ""


def _quote(content: str, limit: int = 18) -> str:
    text = _MARKUP.sub(" ", str(content or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


class BotMoodService:
    def __init__(
        self,
        db: Any,
        *,
        profile_for: Callable[[str], Any] | None = None,
        default_half_life_minutes: float = 120.0,
        enabled: bool = True,
        clock: Callable[[], float] | None = None,
        hour_of: Callable[[float], int] | None = None,
    ):
        self.db = db
        self.profile_for = profile_for or (lambda _bot_id: None)
        try:
            default_minutes = float(default_half_life_minutes)
        except (TypeError, ValueError, OverflowError):
            default_minutes = 120.0
        if not math.isfinite(default_minutes) or default_minutes <= 0:
            default_minutes = 120.0
        self.default_half_life_minutes = max(1.0, min(7 * 24 * 60.0, default_minutes))
        self.enabled = bool(enabled) and self.repository is not None
        self.clock = clock or time.time
        self.hour_of = hour_of or (lambda ts: time.localtime(ts).tm_hour)
        self.stats: dict[str, int] = {}
        self._lock = threading.RLock()
        self._replies: dict[str, deque[float]] = {}
        self._replies_warmed: set[str] = set()
        self._last_sync: dict[str, float] = {}
        self._last_atmosphere: dict[tuple[str, str], float] = {}
        self._bots: set[str] = set()

    # ─── 基础 ───

    @property
    def repository(self) -> Any:
        return getattr(self.db, "bot_mood", None)

    def _count(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def _now(self, now: float | None) -> float:
        return float(now if now is not None else self.clock())

    def settings_for(self, bot_id: str) -> MoodSettings:
        try:
            profile = self.profile_for(bot_id)
        except Exception:
            profile = None
        settings = getattr(profile, "mood", None)
        if isinstance(settings, MoodSettings):
            return settings
        if isinstance(settings, Mapping):
            return MoodSettings.from_dict(settings)
        return MoodSettings()

    def _active(self, bot_id: str) -> MoodSettings | None:
        if not self.enabled or not bot_id:
            return None
        settings = self.settings_for(bot_id)
        return settings if settings.enabled else None

    @staticmethod
    def _valid_scope(scope: Any) -> bool:
        return (
            isinstance(scope, RuntimeScope)
            and scope.session is not None
            and scope.visibility in {"group", "private"}
        )

    def _name_for(self, bot_id: str, principal: str, fallback: str = "") -> str:
        name = sanitize_display_name(fallback) if fallback else ""
        if name:
            return name[:20]
        user_id = _principal_user_id(principal)
        try:
            name = sanitize_display_name(self.repository.display_name(bot_id, user_id)) if user_id else ""
        except Exception:
            name = ""
        return (name or "有人")[:20]

    def _add(self, bot_id: str, **kwargs) -> int | None:
        # 护栏：单条事件的冲击有上限，一句话不能把心情直接打到极端（叠加后还有软饱和）。
        if not all(math.isfinite(float(kwargs.get(key) or 0.0)) for key in ("valence", "arousal", "intensity")):
            self._count("invalid_event")
            return None
        kwargs["valence"] = max(-MAX_EVENT_VALENCE, min(MAX_EVENT_VALENCE, float(kwargs.get("valence") or 0.0)))
        kwargs["arousal"] = max(-MAX_EVENT_AROUSAL, min(MAX_EVENT_AROUSAL, float(kwargs.get("arousal") or 0.0)))
        try:
            event_id = self.repository.add_event(bot_id, **kwargs)
        except Exception as exc:
            self._count("write_error")
            logger.debug(f"[WaveMemory] 心情事件写入失败 {bot_id}: {exc!r}")
            return None
        if event_id is None:
            self._count("duplicate")
            return None
        self._bots.add(bot_id)
        self._count(f"events:{kwargs.get('source', 'unknown')}")
        return event_id

    # ─── 输入：冲她来的话 ───

    def observe_message(
        self,
        scope: RuntimeScope,
        *,
        content: str,
        sender_name: str = "",
        directed: bool = False,
        now: float | None = None,
    ) -> int | None:
        """别人说的一句话。只有冲她来的（@/回复/私聊）才按情绪词评估，其余只顺带增量导入关系事件。"""
        if not self._valid_scope(scope):
            return None
        settings = self._active(scope.bot_id)
        if settings is None:
            return None
        timestamp = self._now(now)
        self._bots.add(scope.bot_id)
        self.sync_relationship_events(scope.bot_id, now=timestamp)
        if not directed:
            return None
        # 只按这句话本身评估，不读好感分数：玩笑还是冒犯不由分数替她判断（群聊里由事后记账回看整段对话给出心情影响）。
        impact = score_directed_message(content, settings)
        if impact.is_empty:
            return None
        subject = str(scope.subject_principal_id or "")
        name = self._name_for(scope.bot_id, subject, sender_name)
        quote = _quote(content)
        quoted = f"「{quote}」" if quote else ""
        templates = {
            "praise": f"{name}夸你{quoted}",
            "insult": f"{name}说你{quoted}",
            "banter": f"{name}跟你斗嘴{quoted}",
            "tease": f"{name}逗你{quoted}",
            "extreme": f"{name}对你恶语相向",
        }
        event_id = self._add(
            scope.bot_id,
            source="directed_message",
            valence=impact.valence,
            arousal=impact.arousal,
            intensity=impact.intensity,
            cause=templates.get(impact.kind, f"{name}对你说{quoted}"),
            subject_principal_id=subject,
            subject_name=name,
            session_id=scope.session.id,
            visibility=scope.visibility,
            evidence={"kind": impact.kind, "matched": impact.matched},
            created_at=timestamp,
            merge_seconds=MESSAGE_MERGE_SECONDS,
        )
        if event_id is not None:
            self.snapshot(scope.bot_id, now=timestamp)
        else:
            self._count("merged")
        return event_id

    # ─── 输入：群氛围（小权重） ───

    def observe_atmosphere(
        self,
        scope: RuntimeScope,
        *,
        valence: float,
        arousal: float,
        now: float | None = None,
    ) -> int | None:
        """旧关键词统计得到的群氛围：只按画像的氛围权重作小幅输入，不再直接覆盖心情。"""
        if not self._valid_scope(scope) or scope.visibility != "group":
            return None
        settings = self._active(scope.bot_id)
        if settings is None or settings.atmosphere_weight <= 0:
            return None
        try:
            group_valence = float(valence)
            group_arousal = float(arousal)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(group_valence) or not math.isfinite(group_arousal):
            return None
        if abs(group_valence) < 0.2:
            return None
        timestamp = self._now(now)
        key = (scope.bot_id, scope.session.id)
        if timestamp - self._last_atmosphere.get(key, 0.0) < ATMOSPHERE_INTERVAL:
            return None
        self._last_atmosphere[key] = timestamp
        weight = settings.atmosphere_weight * settings.sensitivity * 0.5
        event_id = self._add(
            scope.bot_id,
            source="atmosphere",
            valence=round(group_valence * weight, 4),
            arousal=round((group_arousal - 0.4) * weight, 4),
            intensity=0.1,
            cause="群里气氛不错" if group_valence > 0 else "群里气氛有点糟",
            session_id=scope.session.id,
            visibility="group",
            source_ref=f"atmosphere:{scope.session.id}:{int(timestamp // ATMOSPHERE_INTERVAL)}",
            evidence={"group_valence": round(group_valence, 3), "group_arousal": round(group_arousal, 3)},
            created_at=timestamp,
        )
        if event_id is not None:
            self.snapshot(scope.bot_id, now=timestamp)
        return event_id

    # ─── 输入：事后记账那次 LLM 顺带给出的心情影响 ───

    def record_bookkeeping(self, scope: RuntimeScope, raw: Any, *, ref: str = "", now: float | None = None) -> int | None:
        """``raw`` 形如 ``{"direction": "up|down|none", "strength": 0~1, "reason": "一句话"}``；解析失败安全忽略。"""
        if not self._valid_scope(scope):
            return None
        settings = self._active(scope.bot_id)
        if settings is None:
            return None
        if raw is None:
            self._count("bookkeeping:absent")
            return None
        parsed = parse_bookkeeping_mood(raw, settings)
        if parsed is None:
            self._count("bookkeeping:ignored")
            return None
        valence, arousal, strength, reason = parsed
        timestamp = self._now(now)
        event_id = self._add(
            scope.bot_id,
            source="bookkeeping",
            valence=valence,
            arousal=arousal,
            intensity=strength,
            cause=reason,
            session_id=scope.session.id,
            visibility=scope.visibility,
            source_ref=f"bookkeeping:{scope.session.id}:{ref}" if ref else "",
            evidence={"direction": str(raw.get("direction")), "strength": strength},
            created_at=timestamp,
        )
        if event_id is not None:
            self.snapshot(scope.bot_id, now=timestamp)
        return event_id

    # ─── 输入：正式关系事件（增量导入） ───

    def sync_relationship_events(self, bot_id: str, *, now: float | None = None, force: bool = False) -> int:
        settings = self._active(bot_id)
        repo = self.repository
        if settings is None or repo is None:
            return 0
        timestamp = self._now(now)
        if not force and timestamp - self._last_sync.get(bot_id, 0.0) < SYNC_INTERVAL:
            return 0
        self._last_sync[bot_id] = timestamp
        try:
            state = repo.get_state(bot_id)
        except Exception:
            return 0
        first_run = state is None
        cursor = 0 if first_run else int(state.get("relationship_cursor") or 0)
        since = timestamp - (FIRST_IMPORT_LOOKBACK if first_run else 3 * 86400.0)
        imported = 0
        new_cursor = cursor
        for _ in range(5):
            rows = repo.relationship_events_after(bot_id, cursor=new_cursor, since=since, limit=200)
            if not rows:
                break
            for row in rows:
                new_cursor = max(new_cursor, int(row["id"]))
                if self._import_relationship_event(bot_id, row, settings):
                    imported += 1
            if len(rows) < 200:
                break
        if first_run and new_cursor == 0:
            new_cursor = repo.max_relationship_event_id(bot_id)
        if imported or new_cursor != cursor or first_run:
            self._bots.add(bot_id)
            self.snapshot(bot_id, now=timestamp, relationship_cursor=new_cursor)
        if imported:
            self._count("relationship_imported", imported)
        return imported

    def _import_relationship_event(self, bot_id: str, row: Mapping[str, Any], settings: MoodSettings) -> bool:
        impulse = relationship_event_impulse(row.get("event_type"), row.get("delta"), settings)
        if impulse is None:
            return False
        valence, arousal, intensity, verb = impulse
        subject = str(row.get("subject_principal_id") or "")
        created_at = float(row.get("created_at") or 0.0)
        recent = self.repository.last_subject_event(
            bot_id, subject_principal_id=subject, source="directed_message",
            since=created_at - RELATIONSHIP_DEDUP_SECONDS,
        ) if subject else None
        if recent is not None and recent.created_at <= created_at + 60 and (recent.valence >= 0) == (valence >= 0):
            valence, arousal = round(valence * 0.5, 4), round(arousal * 0.5, 4)
        name = self._name_for(bot_id, subject)
        reason = re.sub(r"\s+", " ", str(row.get("reason") or "")).strip()
        cause = f"{name}{verb}" + (f"（{reason[:30]}{'…' if len(reason) > 30 else ''}）" if reason else "")
        # 多维更新是同一原始互动：维度与关系行 ID 都不进入源事件幂等键。
        # episode 可能包含多次互动，不能单凭 episode_id 把它们合并。
        origin_kind = "memory" if row.get("source_memory_id") is not None else "operation"
        origin_id = row.get("source_memory_id") if origin_kind == "memory" else row.get("operation_id")
        source_ref = f"relationship_event:{int(row['id'])}"
        if origin_id is not None:
            source_ref = "relationship_origin:" + json.dumps(
                [row.get("session_id") or "", row.get("visibility") or "group", subject,
                 row.get("event_type"), origin_kind, str(origin_id)], separators=(",", ":"),
            )
        return self._add(
            bot_id,
            source="relationship_event",
            valence=valence,
            arousal=arousal,
            intensity=intensity,
            cause=cause,
            subject_principal_id=subject,
            subject_name=name,
            session_id=str(row.get("session_id") or ""),
            visibility=str(row.get("visibility") or "group"),
            source_ref=source_ref,
            relationship_origin=row if origin_id is not None else None,
            evidence={"event_type": row.get("event_type"), "delta": row.get("delta"),
                      "relationship_event_id": row["id"], "source_memory_id": row.get("source_memory_id"),
                      "operation_id": row.get("operation_id")},
            created_at=created_at,
        ) is not None

    # ─── 输入：Bot 自己的回复（疲劳） ───

    def note_bot_reply(self, bot_id: str, *, now: float | None = None) -> None:
        settings = self._active(bot_id)
        if settings is None or not settings.fatigue_enabled:
            return
        timestamp = self._now(now)
        bucket = self._reply_bucket(bot_id, settings, timestamp)
        with self._lock:
            if bucket and timestamp < bucket[-1]:
                # 延迟/乱序回调不能把过期记录留在队列尾部，也不能挤掉更新的回复。
                stamps = sorted([*bucket, timestamp])
                bucket.clear()
                bucket.extend(stamps[-MAX_REPLY_RECORDS:])
            else:
                bucket.append(timestamp)

    def _reply_bucket(self, bot_id: str, settings: MoodSettings, now: float) -> deque[float]:
        # 保留最大合法窗口与两倍最大阈值，热调参后无需重建缓存。
        window = MAX_FATIGUE_WINDOW_MINUTES * 60.0
        with self._lock:
            bucket = self._replies.setdefault(bot_id, deque(maxlen=MAX_REPLY_RECORDS))
            warm = bot_id not in self._replies_warmed
            if warm:
                # 预热与实时追加共用锁，避免读取半预热队列或集合去重吞掉同时间戳的多条回复。
                stamps = self.repository.bot_reply_timestamps(
                    bot_id, since=now - window, until=now, limit=MAX_REPLY_RECORDS,
                ) if self.repository is not None else []
                bucket.extend(stamps)
                self._replies_warmed.add(bot_id)
            while bucket and bucket[0] < now - window:
                bucket.popleft()
        return bucket

    def recent_replies(self, bot_id: str, settings: MoodSettings, now: float) -> int:
        if not settings.fatigue_enabled:
            return 0
        bucket = self._reply_bucket(bot_id, settings, now)
        cutoff = now - settings.fatigue_window_minutes * 60.0
        with self._lock:
            return sum(cutoff <= stamp <= now for stamp in bucket)

    # ─── 读取 ───

    def current(
        self,
        bot_id: str,
        *,
        viewer_scope: RuntimeScope | None = None,
        now: float | None = None,
        max_causes: int = 3,
    ) -> dict[str, Any] | None:
        """现算当前心情（只读，不写库）。``viewer_scope`` 决定私聊来源的原因是否可见。"""
        if not self.enabled or not bot_id or self.repository is None:
            return None
        settings = self.settings_for(bot_id)
        timestamp = self._now(now)
        if not settings.enabled:
            return {"bot_id": bot_id, "enabled": False, "policy_version": POLICY_VERSION}
        horizon = mood_event_horizon(settings, self.default_half_life_minutes)
        max_causes = max(0, min(MAX_CAUSES, int(max_causes)))
        replies = self.recent_replies(bot_id, settings, timestamp)
        hour = int(self.hour_of(timestamp))
        fatigue = fatigue_level(settings, recent_replies=replies, hour=hour)
        try:
            events = self.repository.iter_events(
                bot_id, since=timestamp - horizon, until=timestamp,
                page_size=EVENT_PAGE_SIZE, max_pages=EVENT_MAX_PAGES,
            )
            reading = compose_mood(
                events, settings, now=timestamp, fatigue=fatigue,
                default_half_life_minutes=self.default_half_life_minutes,
                max_contributions=max_causes,
            )
        except MoodReadBudgetExceeded:
            self._count("read_budget_exceeded")
            logger.warning(f"[WaveMemory] 心情事件超出单次读取预算，未使用截断结果: {bot_id}")
            return None
        except Exception as exc:
            logger.debug(f"[WaveMemory] 心情读取失败 {bot_id}: {exc!r}")
            return None
        causes = [
            self._cause(item, viewer_scope, timestamp)
            for item in reading.contributions
            if item.weight >= CAUSE_MIN_WEIGHT
        ][:max(0, int(max_causes))]
        fatigue_reasons: list[str] = []
        if fatigue > 0:
            if replies > settings.fatigue_reply_threshold:
                fatigue_reasons.append(f"这 {settings.fatigue_window_minutes // 60 or 1} 小时回了很多消息" if settings.fatigue_window_minutes >= 60 else "刚才回了很多消息")
            if night_active(hour, settings.night_start_hour, settings.night_end_hour):
                fatigue_reasons.append("夜深了")
        return {
            "bot_id": bot_id,
            "enabled": True,
            "valence": reading.valence,
            "arousal": reading.arousal,
            "fatigue": reading.fatigue,
            "word": reading.word,
            "category": reading.category,
            "baseline": {"valence": settings.baseline_valence, "arousal": settings.baseline_arousal},
            "causes": causes,
            "fatigue_reasons": fatigue_reasons,
            "hint": speaking_hint(settings, reading.word, reading.category),
            "recent_replies": replies,
            "active_events": reading.active_events,
            "observed_at": timestamp,
            "policy_version": POLICY_VERSION,
        }

    @staticmethod
    def _cause(item: MoodContribution, viewer: RuntimeScope | None, now: float) -> dict[str, Any]:
        event = item.event
        viewer_session = viewer.session.id if isinstance(viewer, RuntimeScope) and viewer.session else ""
        same_place = bool(viewer_session) and event.session_id == viewer_session
        private = event.visibility == "private"
        visible = not private or same_place
        age = describe_age(now - event.created_at)
        if not visible:
            text = f"{age}私下有件事{effect_phrase(item.valence, item.arousal)}"
        else:
            if same_place:
                place = ""
            elif private:
                place = "在私聊里"
            elif viewer_session:
                place = "在别的群里"
            else:
                place = "在群里"
            text = f"{age}{place}，{event.cause}"
        return {
            "text": text,
            "source": event.source,
            "private": private,
            "redacted": not visible,
            "subject_name": event.subject_name if visible else "",
            "age_seconds": round(max(0.0, now - event.created_at), 1),
            "created_at": event.created_at,
            "session_id": event.session_id if visible else "",
            "visibility": event.visibility,
            "valence": round(item.valence, 4),
            "arousal": round(item.arousal, 4),
        }

    def _current_for_injection(
        self,
        bot_id: str,
        *,
        viewer_scope: RuntimeScope | None,
        now: float | None,
        max_causes: int,
    ) -> dict[str, Any] | None:
        """注入热路径：心情值取新鲜快照，原因只读还能达到展示阈值的那段事件。

        ``current`` 每次都要把约 80 小时（最强事件衰减到 1e-6）的事件全部读出合成，
        心情多的 Bot 会让 soul_state 通道超时。两处等价近似：
        - 心情值：每次写入事件与每 600 秒 tick 都会写快照；快照新鲜（≤ SNAPSHOT_FRESH_SECONDS）
          且策略版本一致时直接用，否则回退全量 ``current``。
        - 原因：贡献权重 = |v| + 0.5|a|，仓储冲击上限为 1，权重上限 1.5；窗口外的事件
          即使按最慢半衰期衰减，权重也已低于 CAUSE_MIN_WEIGHT，不可能被列为原因，
          所以只读窗口内事件得到的原因与全量完全一致（跨群、私聊的可见性规则不变）。
        """
        repo = self.repository
        if not self.enabled or not bot_id or repo is None:
            return None
        timestamp = self._now(now)
        settings = self.settings_for(bot_id)
        if not settings.enabled:
            return {"bot_id": bot_id, "enabled": False, "policy_version": POLICY_VERSION}
        try:
            state = repo.get_state(bot_id)
        except Exception:
            state = None
        fresh = (
            isinstance(state, Mapping)
            and state.get("policy_version") == POLICY_VERSION
            and 0.0 <= timestamp - float(state.get("updated_at") or 0.0) <= SNAPSHOT_FRESH_SECONDS
        )
        if not fresh:
            return self.current(bot_id, viewer_scope=viewer_scope, now=timestamp, max_causes=max_causes)
        max_causes = max(0, min(MAX_CAUSES, int(max_causes)))
        replies = self.recent_replies(bot_id, settings, timestamp)
        hour = int(self.hour_of(timestamp))
        fatigue = fatigue_level(settings, recent_replies=replies, hour=hour)
        slowest = event_half_life(settings, 1.0, self.default_half_life_minutes)
        cause_window = slowest * math.log2(MAX_CONTRIBUTION_WEIGHT / CAUSE_MIN_WEIGHT)
        try:
            events = repo.iter_events(
                bot_id, since=timestamp - cause_window, until=timestamp,
                page_size=EVENT_PAGE_SIZE, max_pages=EVENT_MAX_PAGES,
            )
            reading = compose_mood(
                events, settings, now=timestamp, fatigue=fatigue,
                default_half_life_minutes=self.default_half_life_minutes,
                max_contributions=max_causes,
            )
        except MoodReadBudgetExceeded:
            self._count("read_budget_exceeded")
            return None
        except Exception as exc:
            logger.debug(f"[WaveMemory] 心情原因读取失败 {bot_id}: {exc!r}")
            return None
        causes = [
            self._cause(item, viewer_scope, timestamp)
            for item in reading.contributions
            if item.weight >= CAUSE_MIN_WEIGHT
        ][:max_causes]
        # 快照里的 valence/arousal 已按写入时刻的疲劳压低过（compose_mood：arousal-0.35f、
        # valence-0.10f）；疲劳随回复数与时段即时变化，这里只补上两次疲劳之差再重算心情词。
        delta = float(state.get("fatigue") or 0.0) - fatigue
        valence = max(-1.0, min(1.0, float(state.get("valence") or 0.0) + 0.10 * delta))
        arousal = max(0.0, min(1.0, float(state.get("arousal") or 0.0) + 0.35 * delta))
        word, category = mood_word(valence, arousal, fatigue)
        fatigue_reasons: list[str] = []
        if fatigue > 0:
            if replies > settings.fatigue_reply_threshold:
                fatigue_reasons.append(f"这 {settings.fatigue_window_minutes // 60 or 1} 小时回了很多消息" if settings.fatigue_window_minutes >= 60 else "刚才回了很多消息")
            if night_active(hour, settings.night_start_hour, settings.night_end_hour):
                fatigue_reasons.append("夜深了")
        self._count("rendered_from_snapshot")
        return {
            "bot_id": bot_id,
            "enabled": True,
            "word": word,
            "category": category,
            "causes": causes,
            "fatigue_reasons": fatigue_reasons,
            "hint": speaking_hint(settings, word, category),
        }

    def render_lines(self, bot_id: str, viewer_scope: RuntimeScope, *, now: float | None = None, max_causes: int = 2) -> list[str]:
        """注入用的自然语言行；心情就在基线附近、也没有可说的原因时返回空列表。"""
        mood = self._current_for_injection(bot_id, viewer_scope=viewer_scope, now=now, max_causes=max_causes)
        if not mood or not mood.get("enabled"):
            return []
        causes = [cause["text"] for cause in mood.get("causes") or []]
        reasons = [*causes, *(mood.get("fatigue_reasons") or [])]
        if not reasons:
            return []
        lines = [f"心情：{mood['word']}", "缘由：" + "；".join(reasons[: max_causes + 1])]
        if mood.get("hint"):
            lines.append(f"说话倾向：{mood['hint']}")
        self._count("rendered")
        return lines

    # ─── 维护 ───

    def snapshot(self, bot_id: str, *, now: float | None = None, relationship_cursor: int | None = None) -> None:
        repo = self.repository
        if repo is None:
            return
        mood = self.current(bot_id, now=now, max_causes=0)
        if not mood or not mood.get("enabled"):
            # 不完整读取不能用全零状态换取游标推进；保留上次快照，重试由源事件键去重。
            return
        try:
            repo.save_state(
                bot_id,
                valence=float(mood.get("valence") or 0.0),
                arousal=float(mood.get("arousal") or 0.0),
                fatigue=float(mood.get("fatigue") or 0.0),
                word=str(mood.get("word") or ""),
                relationship_cursor=relationship_cursor,
                updated_at=self._now(now),
            )
        except Exception as exc:
            logger.debug(f"[WaveMemory] 心情快照写入失败 {bot_id}: {exc!r}")

    def tick(self, bot_ids: list[str] | None = None, *, now: float | None = None) -> None:
        """周期维护：增量导入关系事件、清理过期流水、刷新快照。"""
        if not self.enabled:
            return
        timestamp = self._now(now)
        for bot_id in sorted(set(bot_ids or ()) | self._bots):
            settings = self._active(bot_id)
            if settings is None:
                continue
            try:
                self.sync_relationship_events(bot_id, now=timestamp, force=True)
                # 至少保留原有审计期；长半衰期事件直到残余确实归零才允许分批删除。
                retention = max(EVENT_RETENTION, mood_event_horizon(settings, self.default_half_life_minutes))
                pruned = self.repository.prune(bot_id, before=timestamp - retention)
                if pruned:
                    self._count("pruned", pruned)
                self.snapshot(bot_id, now=timestamp)
            except Exception as exc:
                logger.debug(f"[WaveMemory] 心情维护失败 {bot_id}: {exc!r}")


__all__ = ["BotMoodService"]
