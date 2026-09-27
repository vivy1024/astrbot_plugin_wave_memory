"""Bot 主页 API：以「这个 Bot 是一个人」为中心的只读聚合。

``GET /api/bot-home?bot_id=<db_id>&days=1`` 一次返回：

- ``memories``：窗口内新增记忆数、按群分布、最活跃发言人、最近的高光记忆；
- ``soul``：当前心情与主要关切（复用 ScopedSoulRepository.get_state）；
- ``relationships``：窗口内关系变化最大的人（scoped_soul_relationship_events）；
- ``learned``：窗口内新增的事实 / 信念 / 黑话候选 / 经历片段；
- ``replies``：最近几次注入 trace 与命中的通道。

约束：只读；每条 SQL 都走已有索引并带 LIMIT；各区块独立失败，某一块出错不拖垮整页。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

try:
    from quart import Blueprint, current_app, jsonify, request
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时的轻量兜底
    class Blueprint:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    def jsonify(value=None, **kwargs):  # type: ignore[no-redef]
        return value if value is not None else kwargs

    current_app = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]

try:
    from domain.display_name import sanitize_display_name
    from domain.relationship_policy import (
        DIMENSION_RANGES,
        DIMENSION_WEIGHTS,
        HOSTILITY_WEIGHT,
        NOISY_EVENT_REASONS,
        NOISY_EVENT_TYPES,
        compute_affinity,
    )
    from domain.scope import RuntimeScope, SessionRef
except ImportError:  # pragma: no cover - AstrBot 包导入路径
    from ...domain.display_name import sanitize_display_name
    from ...domain.relationship_policy import (
        DIMENSION_RANGES,
        DIMENSION_WEIGHTS,
        HOSTILITY_WEIGHT,
        NOISY_EVENT_REASONS,
        NOISY_EVENT_TYPES,
        compute_affinity,
    )
    from ...domain.scope import RuntimeScope, SessionRef

from ..api_contract import error_payload
from ..container import get_container

try:
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时直接放行
    def require_auth(func):
        return func

logger = logging.getLogger("astrbot")

bot_home_bp = Blueprint("bot_home", __name__, url_prefix="/api")

MAX_DAYS = 7
TOP_GROUPS = 5
TOP_SPEAKERS = 5
HIGHLIGHT_LIMIT = 8
TOP_RELATIONSHIPS = 5
LEARNED_SAMPLES = 3
REPLY_LIMIT = 5
CONCERN_LIMIT = 5
_CONTENT_PREVIEW = 200
# 历史上 Bot 自己的回复以 sender_id='bot' 落库；再加上 BotProfile 的平台账号。
_SELF_SENDER_SENTINELS = ("bot", "bot_self")
# 关系聚合的安全上限：窗口内 (群, 人, 维度) 组合超过它就截断，不会无界读。
_RELATIONSHIP_GROUP_CAP = 2000
# 已了结的关切不算「主要关切」。
_CLOSED_CONCERN_STATUSES = frozenset({"resolved", "expired", "archived"})


class BotHomeInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ─────────────────────────── 小工具 ───────────────────────────

def _text(value: Any) -> str:
    return str(value or "").strip()


def _preview(value: Any, limit: int = _CONTENT_PREVIEW) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _session_parts(session_id: Any) -> tuple[str, str, str] | None:
    """``platform:kind:conversation`` → 三段；不合法返回 None。"""
    parts = _text(session_id).split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def _user_id_from_principal(principal: Any) -> str:
    text = _text(principal)
    marker = ":user:"
    return text.split(marker, 1)[1] if marker in text else text


def _ready(**fields: Any) -> dict[str, Any]:
    return {"status": "ready", "reason_code": None, **fields}


def _failed(reason_code: str) -> dict[str, Any]:
    return {"status": "error", "reason_code": reason_code}


def _unavailable(reason_code: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason_code": reason_code}


class _Context:
    """一次请求内共享的只读上下文（群名缓存、会话映射、发言人昵称）。"""

    def __init__(
        self,
        conn: Any,
        *,
        bot_id: str,
        since: float,
        until: float,
        self_ids: Iterable[str],
        platform_ids: Iterable[str],
        group_name_resolver: Callable[[str, str], str | None] | None,
    ) -> None:
        self.conn = conn
        self.bot_id = bot_id
        self.since = since
        self.until = until
        self.self_ids = tuple(dict.fromkeys([*_SELF_SENDER_SENTINELS, *(_text(i) for i in self_ids if _text(i))]))
        self.platform_ids = [p for p in (_text(i) for i in platform_ids) if p]
        self._resolver = group_name_resolver
        self._group_names: dict[str, str | None] = {}
        # conversation_id → 已观测到的 canonical session_id（经历片段只存群号）。
        self.sessions_by_conversation: dict[str, str] = {}
        self.names_by_sender: dict[str, str] = {}
        self._tables: dict[str, bool] = {}

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[Any]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Any:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def has_table(self, name: str) -> bool:
        if name not in self._tables:
            row = self.fetchone("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
            self._tables[name] = row is not None
        return self._tables[name]

    def group_name(self, group_id: str) -> str | None:
        key = _text(group_id)
        if not key:
            return None
        if key not in self._group_names:
            name = None
            if callable(self._resolver):
                try:
                    name = _text(self._resolver(self.bot_id, key)) or None
                except Exception:
                    name = None
            self._group_names[key] = name
        return self._group_names[key]

    def observe_session(self, session_id: Any) -> None:
        parts = _session_parts(session_id)
        if parts and parts[1] == "group":
            self.sessions_by_conversation.setdefault(parts[2], _text(session_id))

    def session_for_group(self, group_id: Any) -> str | None:
        gid = _text(group_id)
        if not gid:
            return None
        if gid in self.sessions_by_conversation:
            return self.sessions_by_conversation[gid]
        if self.platform_ids:
            return f"{self.platform_ids[0]}:group:{gid}"
        return None

    def place(self, session_id: Any, *, visibility: Any = None, group_id: Any = None) -> dict[str, Any]:
        """把会话描述成前端可直接展示/深链接的地点。"""
        sid = _text(session_id)
        parts = _session_parts(sid)
        kind = parts[1] if parts else _text(visibility) or None
        conversation = parts[2] if parts else _text(group_id)
        name = self.group_name(conversation) if kind == "group" else None
        label = name or conversation or sid or "未知会话"
        if kind == "private":
            label = f"私聊 {conversation}" if conversation else "私聊"
        return {
            "session_id": sid or None,
            "visibility": _text(visibility) or kind,
            "group_id": conversation or None,
            "group_name": name,
            "label": label,
        }

    def display_name(self, user_id: str) -> str:
        """发言人昵称：窗口内昵称 → 该账号最近一条记忆的昵称 → 画像昵称 → 账号。"""
        uid = _text(user_id)
        if not uid:
            return ""
        cached = self.names_by_sender.get(uid)
        if cached:
            return cached
        name = ""
        try:
            # idx_memories_sender = (sender_id, rowid)：按 id 倒序取第一条是 O(1)。
            row = self.fetchone(
                "SELECT sender_name FROM memories WHERE sender_id=? ORDER BY id DESC LIMIT 1",
                (uid,),
            )
            name = sanitize_display_name(row[0]) if row else ""
            if not name:
                row = self.fetchone(
                    "SELECT nickname FROM user_profiles WHERE user_id=? AND bot_id=? "
                    "AND COALESCE(nickname,'')!='' ORDER BY last_seen DESC LIMIT 1",
                    (uid, self.bot_id),
                )
                name = sanitize_display_name(row[0]) if row else ""
        except Exception:
            name = ""
        self.names_by_sender[uid] = name or uid
        return self.names_by_sender[uid]


# ─────────────────────────── 各区块 ───────────────────────────

def _memories_section(ctx: _Context) -> dict[str, Any]:
    # `+bot_id` 关掉 bot_id 前缀索引，强制走 idx_memories_timestamp 的时间范围扫描：
    # 线上 bot_id 前缀索引要扫 Bot 的全部历史（~23ms），时间范围只扫窗口内（~3ms/天）。
    window = "timestamp>=? AND timestamp<? AND +bot_id=?"
    window_params = (ctx.since, ctx.until, ctx.bot_id)
    rows = ctx.fetchall(
        f"""SELECT session_id, visibility, source, COALESCE(quarantine,0), COUNT(*)
              FROM memories WHERE {window}
             GROUP BY session_id, visibility, source, COALESCE(quarantine,0)
             LIMIT 5000""",
        window_params,
    )
    total = 0
    quarantined = 0
    by_source: dict[str, int] = {}
    by_session: dict[tuple[str, str], int] = {}
    for session_id, visibility, source, quarantine, count in rows:
        count = int(count or 0)
        if int(quarantine or 0):
            quarantined += count
            continue
        total += count
        key = _text(source) or "unknown"
        by_source[key] = by_source.get(key, 0) + count
        session_key = (_text(session_id), _text(visibility))
        by_session[session_key] = by_session.get(session_key, 0) + count
        ctx.observe_session(session_id)
    top_groups = [
        {**ctx.place(session_id, visibility=visibility), "count": count}
        for (session_id, visibility), count in sorted(by_session.items(), key=lambda item: (-item[1], item[0]))[:TOP_GROUPS]
    ]

    placeholders = ",".join("?" for _ in ctx.self_ids)
    speaker_rows = ctx.fetchall(
        f"""SELECT sender_id, sender_name, session_id, visibility, MAX(timestamp), COUNT(*)
              FROM memories
             WHERE {window} AND COALESCE(quarantine,0)=0
               AND COALESCE(sender_id,'')!='' AND sender_id NOT IN ({placeholders})
             GROUP BY sender_id
             ORDER BY COUNT(*) DESC, MAX(timestamp) DESC
             LIMIT ?""",
        (*window_params, *ctx.self_ids, TOP_SPEAKERS),
    )
    top_speakers = []
    for sender_id, sender_name, session_id, visibility, last_at, count in speaker_rows:
        uid = _text(sender_id)
        name = sanitize_display_name(sender_name)
        if name:
            ctx.names_by_sender.setdefault(uid, name)
        top_speakers.append({
            "sender_id": uid,
            "display_name": name or ctx.display_name(uid),
            "count": int(count or 0),
            "last_at": last_at,
            **ctx.place(session_id, visibility=visibility),
        })

    highlight_rows = ctx.fetchall(
        f"""SELECT id, session_id, visibility, group_id, sender_id, sender_name, content,
                   timestamp, importance, source, memory_type
              FROM memories
             WHERE {window} AND COALESCE(quarantine,0)=0 AND COALESCE(source,'')!='noise'
               AND (sender_id IN ({placeholders}) OR source='core' OR importance>1.0)
             ORDER BY timestamp DESC
             LIMIT ?""",
        (*window_params, *ctx.self_ids, HIGHLIGHT_LIMIT),
    )
    highlights = []
    for row in highlight_rows:
        memory_id, session_id, visibility, group_id, sender_id, sender_name, content, ts, importance, source, memory_type = row
        uid = _text(sender_id)
        if uid in ctx.self_ids:
            reason = "self"
        elif float(importance or 0) > 1.0:
            reason = "important"
        else:
            reason = "core"
        highlights.append({
            "id": memory_id,
            "sender_id": uid,
            "sender_name": sanitize_display_name(sender_name) or (uid if reason != "self" else ""),
            "is_self": reason == "self",
            "content": _preview(content),
            "timestamp": ts,
            "importance": importance,
            "source": source,
            "memory_type": memory_type,
            "reason": reason,
            **ctx.place(session_id, visibility=visibility, group_id=group_id),
        })

    return _ready(
        total=total,
        quarantined=quarantined,
        by_source=by_source,
        top_groups=top_groups,
        top_speakers=top_speakers,
        highlights=highlights,
    )


def _soul_scope(ctx: _Context) -> RuntimeScope | None:
    """心情属于 Bot 本人：用最近一次群内心情所在会话构造只读 Scope。"""
    row = ctx.fetchone(
        """SELECT session_id FROM scoped_soul_mood
            WHERE bot_id=? AND visibility='group'
            ORDER BY observed_at DESC LIMIT 1""",
        (ctx.bot_id,),
    )
    candidates = [row[0]] if row else []
    candidates.extend(ctx.sessions_by_conversation.values())
    for session_id in candidates:
        parts = _session_parts(session_id)
        if not parts or parts[1] != "group":
            continue
        try:
            return RuntimeScope(ctx.bot_id, "group", SessionRef(_text(session_id), parts[0], "group", parts[2]))
        except Exception:
            continue
    return None


def _soul_section(ctx: _Context, soul_repository: Any) -> dict[str, Any]:
    if soul_repository is None or not callable(getattr(soul_repository, "get_state", None)):
        return _unavailable("soul_scoped_repository_unavailable")
    scope = _soul_scope(ctx)
    if scope is None:
        return _ready(mood=None, concerns=[], concerns_total=0, scope=None)
    state = soul_repository.get_state(scope, limit=25)
    mood_raw = state.get("mood") if isinstance(state, Mapping) else None
    mood = None
    if isinstance(mood_raw, Mapping) and mood_raw.get("state") == "known":
        components = mood_raw.get("components") if isinstance(mood_raw.get("components"), Mapping) else {}
        mood = {
            "valence": components.get("valence", mood_raw.get("value")),
            "arousal": components.get("arousal"),
            "cause": _text(mood_raw.get("cause")),
            "observed_at": mood_raw.get("observed_at"),
            "policy_version": mood_raw.get("policy_version"),
            **ctx.place(mood_raw.get("session_id")),
        }
    concerns_raw = state.get("concerns") if isinstance(state, Mapping) else None
    concern_items = concerns_raw.get("items", []) if isinstance(concerns_raw, Mapping) else []
    active = [
        item for item in concern_items
        if isinstance(item, Mapping) and _text(item.get("status") or "active") not in _CLOSED_CONCERN_STATUSES
    ]
    concerns = [
        {
            "id": item.get("id"),
            "topic": _text(item.get("topic")),
            "intensity": item.get("intensity"),
            "urgency": item.get("urgency"),
            "status": _text(item.get("status")) or "active",
            "concern_type": _text(item.get("concern_type")),
            "last_triggered": item.get("last_triggered"),
            **ctx.place(item.get("session_id")),
        }
        for item in active[:CONCERN_LIMIT]
    ]
    return _ready(
        mood=mood,
        concerns=concerns,
        concerns_total=int((concerns_raw or {}).get("total") or 0) if isinstance(concerns_raw, Mapping) else 0,
        scope={"session_id": scope.session.id if scope.session else None, "visibility": scope.visibility},
    )


def _relationships_section(ctx: _Context) -> dict[str, Any]:
    noise_types = tuple(sorted(NOISY_EVENT_TYPES))
    noise_reasons = tuple(sorted(NOISY_EVENT_REASONS))
    type_ph = ",".join("?" for _ in noise_types) or "NULL"
    reason_ph = ",".join("?" for _ in noise_reasons) or "NULL"
    # 走 idx_scoped_soul_relationship_events_scope_subject 的 bot_id 前缀；created_at 在索引内过滤。
    rows = ctx.fetchall(
        f"""SELECT session_id, visibility, subject_principal_id, dimension,
                   SUM(delta), SUM(ABS(delta)), COUNT(*), MAX(created_at)
              FROM scoped_soul_relationship_events
             WHERE bot_id=? AND created_at>=? AND created_at<?
               AND event_type NOT IN ({type_ph}) AND COALESCE(reason,'') NOT IN ({reason_ph})
             GROUP BY session_id, visibility, subject_principal_id, dimension
             LIMIT ?""",
        (ctx.bot_id, ctx.since, ctx.until, *noise_types, *noise_reasons, _RELATIONSHIP_GROUP_CAP),
    )
    people: dict[tuple[str, str, str], dict[str, Any]] = {}
    for session_id, visibility, subject, dimension, net, absolute, count, last_at in rows:
        key = (_text(session_id), _text(visibility), _text(subject))
        entry = people.setdefault(key, {"dimensions": {}, "abs_delta": 0.0, "event_count": 0, "last_at": 0.0})
        entry["dimensions"][_text(dimension)] = round(float(net or 0.0), 2)
        entry["abs_delta"] += float(absolute or 0.0)
        entry["event_count"] += int(count or 0)
        entry["last_at"] = max(entry["last_at"], float(last_at or 0.0))

    def affinity_shift(dims: Mapping[str, float]) -> float:
        # 与 compute_affinity 同一组权重的加权增量（不截断、不取整），只用于排序。
        score = sum(float(dims.get(key, 0.0)) * weight for key, weight in DIMENSION_WEIGHTS.items())
        return score - float(dims.get("hostility", 0.0)) * HOSTILITY_WEIGHT

    ranked = sorted(
        people.items(),
        key=lambda item: (-abs(affinity_shift(item[1]["dimensions"])), -item[1]["abs_delta"], -item[1]["last_at"]),
    )[:TOP_RELATIONSHIPS]

    items = []
    for (session_id, visibility, subject), entry in ranked:
        current = ctx.fetchone(
            """SELECT affinity, state, dimensions FROM scoped_soul_relationships
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            (ctx.bot_id, session_id, visibility, subject),
        )
        latest = ctx.fetchone(
            f"""SELECT reason, event_type, dimension, delta FROM scoped_soul_relationship_events
                 WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                   AND created_at>=? AND created_at<?
                   AND event_type NOT IN ({type_ph}) AND COALESCE(reason,'') NOT IN ({reason_ph})
                 ORDER BY created_at DESC LIMIT 1""",
            (ctx.bot_id, session_id, visibility, subject, ctx.since, ctx.until, *noise_types, *noise_reasons),
        )
        affinity_after = affinity_before = None
        state = None
        if current:
            affinity_after = int(current[0]) if current[0] is not None else None
            state = current[1]
            try:
                dims_now = {k: float(v) for k, v in (json.loads(current[2] or "{}") or {}).items()}
            except (TypeError, ValueError):
                dims_now = {}
            dims_before = dict(dims_now)
            for dimension, delta in entry["dimensions"].items():
                if dimension in DIMENSION_RANGES:
                    lo, hi = DIMENSION_RANGES[dimension]
                    dims_before[dimension] = max(lo, min(hi, dims_before.get(dimension, 0.0) - delta))
            affinity_before = compute_affinity(dims_before)
        user_id = _user_id_from_principal(subject)
        items.append({
            "subject_principal_id": subject,
            "user_id": user_id,
            "display_name": ctx.display_name(user_id),
            "event_count": entry["event_count"],
            "abs_delta": round(entry["abs_delta"], 2),
            "dimensions": entry["dimensions"],
            "affinity_before": affinity_before,
            "affinity_after": affinity_after,
            "state": state,
            "latest_reason": _text(latest[0]) if latest else "",
            "latest_event_type": _text(latest[1]) if latest else "",
            "latest_at": entry["last_at"] or None,
            **ctx.place(session_id, visibility=visibility),
        })
    return _ready(items=items, people_changed=len(people), truncated=len(rows) >= _RELATIONSHIP_GROUP_CAP)


def _learned_bucket(
    ctx: _Context,
    *,
    table: str,
    text_sql: str,
    status_sql: str,
    place_sql: str,
) -> dict[str, Any]:
    counts = ctx.fetchall(
        f"""SELECT {status_sql}, COUNT(*) FROM {table}
             WHERE bot_id=? AND created_at>=? AND created_at<?
             GROUP BY 1 LIMIT 50""",
        (ctx.bot_id, ctx.since, ctx.until),
    )
    by_status = {(_text(status) or "unknown"): int(count or 0) for status, count in counts}
    samples_rows = ctx.fetchall(
        f"""SELECT id, {text_sql}, {status_sql}, {place_sql}, created_at FROM {table}
             WHERE bot_id=? AND created_at>=? AND created_at<?
             ORDER BY created_at DESC, id DESC LIMIT ?""",
        (ctx.bot_id, ctx.since, ctx.until, LEARNED_SAMPLES),
    )
    samples = []
    for item_id, text, status, place_a, place_b, created_at in samples_rows:
        if table == "experience_episodes":
            place = ctx.place(ctx.session_for_group(place_a), visibility="group", group_id=place_a)
        else:
            place = ctx.place(place_a, visibility=place_b)
        samples.append({
            "id": item_id,
            "text": _preview(text, 160),
            "status": _text(status) or "unknown",
            "created_at": created_at,
            **place,
        })
    return {"total": sum(by_status.values()), "by_status": by_status, "samples": samples}


_LEARNED_SPECS: dict[str, dict[str, str]] = {
    # 表均带 (bot_id, …) 前缀索引；experience_episodes 带 (bot_id, created_at)。
    "facts": {
        "table": "scoped_facts",
        "text_sql": "subject || ' ' || predicate || ' ' || object",
        "status_sql": "status",
        "place_sql": "session_id, visibility",
    },
    "beliefs": {
        "table": "scoped_beliefs",
        "text_sql": "content",
        "status_sql": "status",
        "place_sql": "session_id, visibility",
    },
    "jargon": {
        "table": "scoped_jargon",
        "text_sql": "word || CASE WHEN COALESCE(meaning,'')!='' THEN '：' || meaning ELSE '' END",
        "status_sql": "status",
        "place_sql": "session_id, visibility",
    },
    "experiences": {
        "table": "experience_episodes",
        "text_sql": "COALESCE(NULLIF(trigger_text,''), NULLIF(outcome,''), NULLIF(bot_reply,''), episode_type)",
        # 经历片段没有审核状态：沉淀即生效。
        "status_sql": "'recorded'",
        "place_sql": "group_id, NULL",
    },
}


def _learned_section(ctx: _Context) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, spec in _LEARNED_SPECS.items():
        if not ctx.has_table(spec["table"]):
            out[key] = {**_unavailable("table_missing"), "total": None, "by_status": {}, "samples": []}
            continue
        try:
            out[key] = {"status": "ready", "reason_code": None, **_learned_bucket(ctx, **spec)}
        except Exception as exc:
            logger.warning("[WaveMemory BotHome] learned.%s 读取失败: %s", key, exc)
            out[key] = {**_failed(f"{key}_read_failed"), "total": None, "by_status": {}, "samples": []}
    return _ready(**out)


def _replies_section(ctx: _Context) -> dict[str, Any]:
    if not ctx.has_table("injection_traces"):
        return _unavailable("trace_store_missing")
    # AstrBot 写入的 bot_id 列是 QQ 号，bot_profile_id 才是 db_id；两列都认。
    rows = ctx.fetchall(
        """SELECT trace_id, timestamp, group_id, sender_id, sender_name, total_latency_ms,
                  status, message_preview, metadata_json
             FROM injection_traces
            WHERE (bot_profile_id=? OR bot_id=?)
            ORDER BY timestamp DESC LIMIT ?""",
        (ctx.bot_id, ctx.bot_id, REPLY_LIMIT),
    )
    trace_ids = [row[0] for row in rows]
    channels: dict[str, list[dict[str, Any]]] = {trace_id: [] for trace_id in trace_ids}
    if trace_ids and ctx.has_table("injection_trace_channels"):
        placeholders = ",".join("?" for _ in trace_ids)
        for trace_id, channel, status, item_count, tokens in ctx.fetchall(
            f"""SELECT trace_id, channel, status, item_count, tokens
                  FROM injection_trace_channels
                 WHERE trace_id IN ({placeholders}) AND status='hit'
                 ORDER BY id LIMIT 500""",
            trace_ids,
        ):
            channels.setdefault(trace_id, []).append({
                "channel": channel,
                "item_count": int(item_count or 0),
                "tokens": int(tokens or 0),
            })
    items = []
    for trace_id, ts, group_id, sender_id, sender_name, latency, status, preview, metadata_json in rows:
        session_id = None
        visibility = None
        source = None
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if isinstance(metadata, Mapping):
            source = metadata.get("source")
            runtime_scope = metadata.get("runtime_scope")
            payload = runtime_scope.get("payload") if isinstance(runtime_scope, Mapping) else None
            if isinstance(payload, Mapping):
                visibility = payload.get("visibility")
                session = payload.get("session")
                if isinstance(session, Mapping):
                    session_id = session.get("id")
        if not session_id and group_id:
            session_id = ctx.session_for_group(group_id)
            visibility = visibility or "group"
        uid = _text(sender_id)
        items.append({
            "trace_id": trace_id,
            "timestamp": ts,
            "latency_ms": round(float(latency or 0.0), 1),
            "status": status,
            "sender_id": uid or None,
            "sender_name": sanitize_display_name(sender_name) or (ctx.display_name(uid) if uid else ""),
            "message_preview": _preview(preview, 120),
            "source": source,
            "hit_channels": channels.get(trace_id, []),
            **ctx.place(session_id, visibility=visibility, group_id=group_id),
        })
    return _ready(items=items)


# ─────────────────────────── 组装 ───────────────────────────

def build_bot_home_payload(
    conn: Any,
    *,
    bot_id: str,
    days: int = 1,
    now: float | None = None,
    bot_name: str = "",
    self_ids: Iterable[str] = (),
    platform_ids: Iterable[str] = (),
    soul_repository: Any = None,
    group_name_resolver: Callable[[str, str], str | None] | None = None,
) -> dict[str, Any]:
    """构造 Bot 主页聚合；各区块独立失败。``conn`` 只需要 ``execute``。"""
    started = time.perf_counter()
    until = float(now if now is not None else time.time())
    since = until - int(days) * 86400.0
    ctx = _Context(
        conn,
        bot_id=bot_id,
        since=since,
        until=until,
        self_ids=self_ids,
        platform_ids=platform_ids,
        group_name_resolver=group_name_resolver,
    )
    section_ms: dict[str, float] = {}

    def run(name: str, builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            return builder()
        except Exception as exc:
            logger.warning("[WaveMemory BotHome] %s 区块读取失败 bot=%s: %s: %s", name, bot_id, type(exc).__name__, exc)
            return _failed(f"{name}_read_failed")
        finally:
            section_ms[name] = round((time.perf_counter() - t0) * 1000, 1)

    # memories 先跑：它顺带收集会话映射，后面的区块用来补群名/会话。
    memories = run("memories", lambda: _memories_section(ctx))
    soul = run("soul", lambda: _soul_section(ctx, soul_repository))
    relationships = run("relationships", lambda: _relationships_section(ctx))
    learned = run("learned", lambda: _learned_section(ctx))
    replies = run("replies", lambda: _replies_section(ctx))
    return {
        "bot": {"db_id": bot_id, "name": bot_name or bot_id},
        "window": {"days": int(days), "from_ts": since, "to_ts": until},
        "memories": memories,
        "soul": soul,
        "relationships": relationships,
        "learned": learned,
        "replies": replies,
        "meta": {
            "generated_at": time.time(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "section_ms": section_ms,
        },
    }


def parse_bot_home_args(args: Mapping[str, Any]) -> tuple[str, int]:
    bot_id = _text(args.get("bot_id"))
    if not bot_id:
        raise BotHomeInputError("bot_id_required", "bot_id 必填（BotProfile.db_id）")
    if bot_id.isdecimal() or bot_id.casefold() in {"bot", "default"}:
        raise BotHomeInputError("invalid_bot_id", "bot_id 必须是 BotProfile.db_id，不是 QQ 号或占位值")
    raw_days = args.get("days")
    if raw_days in (None, ""):
        return bot_id, 1
    try:
        days = int(str(raw_days).strip())
    except (TypeError, ValueError):
        raise BotHomeInputError("invalid_days", "days 必须是整数") from None
    if days < 1 or days > MAX_DAYS:
        raise BotHomeInputError("invalid_days", f"days 取值 1..{MAX_DAYS}")
    return bot_id, days


def _group_name_resolver() -> Callable[[str, str], str | None] | None:
    try:
        composition = current_app.extensions.get("wave_api_contract", {})
    except (RuntimeError, AttributeError):
        return None
    source = composition.get("scope_options_source") if isinstance(composition, Mapping) else None
    resolver = getattr(source, "resolve_group_name", None)
    return resolver if callable(resolver) else None


@bot_home_bp.route("/bot-home", methods=["GET"])
@require_auth
async def bot_home():
    try:
        bot_id, days = parse_bot_home_args(request.args)
    except BotHomeInputError as exc:
        return jsonify(error_payload(exc.code, str(exc))), 400

    container = get_container()
    db = getattr(container, "db", None)
    if db is not None and getattr(db, "closed", False):
        try:
            db.reopen()
        except Exception:
            db = None
    conn = getattr(db, "conn", None) if db is not None else None
    if conn is None:
        return jsonify(error_payload("service_unavailable", "数据库未就绪", retryable=True)), 503

    bot_name = ""
    self_ids: list[str] = []
    platform_ids: list[str] = []
    registry = getattr(container, "bot_registry", None)
    getter = getattr(registry, "get_any", None)
    if callable(getter):
        profile = getter(bot_id)
        if profile is None:
            return jsonify(error_payload("bot_not_found", f"没有 Bot {bot_id!r}")), 404
        bot_name = _text(getattr(profile, "name", ""))
        self_ids = list(getattr(profile, "self_ids", []) or [])
        platform_ids = list(getattr(profile, "platform_ids", []) or [])

    soul_repository = getattr(container, "soul_repository", None) or getattr(db, "soul_repository", None)
    payload = build_bot_home_payload(
        conn,
        bot_id=bot_id,
        days=days,
        bot_name=bot_name,
        self_ids=self_ids,
        platform_ids=platform_ids,
        soul_repository=soul_repository,
        group_name_resolver=_group_name_resolver(),
    )
    return jsonify(payload)


__all__ = ["bot_home_bp", "build_bot_home_payload", "parse_bot_home_args"]
