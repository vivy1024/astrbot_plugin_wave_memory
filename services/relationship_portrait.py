"""关系小传：由模型为每个人写一段「关系小传」，关系注入以它为主（摘要 + 可主动查全部）。

好感分数只是给群友玩的数字；真正承载关系的是印象时间线。时间线里大半是迁移来的人物事实，
最常聊的人时间线最长（几十到两三百条），规则挑出的 8 条只是一小片，旧的重要事件会被衰减掉。
这里让 Bot 自己把一个人的时间线读完，写成不超过 ``max_chars`` 字的小传；注入时小传 + 最近几条
原始记录 + 话题命中的旧事线索 + 查询提示，完整时间线仍可用 ``wave_memory_person_search`` 查。

- **隐私边界（硬性）**：私聊里记下的事件 ``group_id`` 为 ``private:<会话>``，只在同一私聊可见。
  小传只用非私聊事件（以及非私聊场合的互动次数）生成，因此在任何场合注入都不外泄；私聊场合注入时
  由关系通道另外附上该私聊可见的最近原始记录。
- **候选**：自上次小传后新增 ≥ ``min_new_events`` 条印象/好感/里程碑/社交锚点类事件；或距上次 ≥
  ``refresh_days`` 天且期间有新事件。非私聊事件少于 ``min_events`` 条的人不写，注入沿用规则摘要。
- **节流**：后台循环启动后延迟、之后每 30 分钟检查一次；每轮最多 ``max_per_round`` 人、每天最多
  ``daily_limit`` 次，调用串行。
- **不覆盖人工**：管理员手改过（status=edited）的小传不自动覆盖，有新记录时只标记待更新。
- 输入不给好感分数、维度数值或态度；输出过身份污染检查，命中不保存并记错误。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import PRIVATE_SCENE_PREFIX
    from ..engine.db.person_identity_repo import linked_principals_via, local_user_id
    from ..engine.db.relationship_portrait_repo import PortraitConflict, PortraitError
    from .identity_safety import is_identity_contamination, prepend_identity_safety_system_prompt
    from .impression_timeline import _injection_summary, load_timeline_events
except ImportError:  # pragma: no cover - top-level imports in isolated tests
    from domain.scope import PRIVATE_SCENE_PREFIX
    from engine.db.person_identity_repo import linked_principals_via, local_user_id
    from engine.db.relationship_portrait_repo import PortraitConflict, PortraitError
    from services.identity_safety import is_identity_contamination, prepend_identity_safety_system_prompt
    from services.impression_timeline import _injection_summary, load_timeline_events


# 计入「新增事件」的类别：印象、好感结算、里程碑、社交锚点（人物事实不算关系变化）
RELATIONAL_KINDS = ("impression", "affinity", "milestone", "social_anchor")
KIND_LABELS = {
    "impression": "印象",
    "affinity": "关系变化",
    "person_fact": "人物事实",
    "milestone": "里程碑",
    "social_anchor": "人情往来",
}
# 生成输入：非私聊时间线最近这么多条（按时间）
PORTRAIT_INPUT_EVENTS = 200
PORTRAIT_INPUT_SUMMARY_CHARS = 150
# 上次失败后多久内不再自动重试（避免每 30 分钟对同一个人重复失败）
RETRY_BACKOFF_SECONDS = 6 * 3600.0
STARTUP_DELAY_SECONDS = 600.0
CHECK_INTERVAL_SECONDS = 1800.0
_PRIVATE_LIKE = f"{PRIVATE_SCENE_PREFIX}%"
# 非私聊事件的场合键：viewer_scene="" 时时间线仓储只返回群聊等公开场合的事件（私聊键一律带前缀）
NON_PRIVATE_VIEWER = ""

PROMPT = """你是{bot_name}。下面是你和「{person_name}」交往中记下的记录，只含群里等公开场合的记录。
请以你自己的视角给 ta 写一段「关系小传」。之后你和 ta 说话前会先读到它；正文里用「你」指你自己（{bot_name}），用 ta 的名字或「ta」指对方。

【基本情况】
{basics}

【上一版小传】
{previous}

【记录（按时间从早到晚，共 {shown} 条{omitted}）】
{records}

写法要求：
- 不超过 {max_chars} 字，写成一段通顺的话。
- 写清楚：ta 是谁；有哪些稳定的事实与偏好；你们的关系是怎么一步步走到现在的；有没有还没了结的事（答应过的事、欠着的人情、说好之后再聊的话题）。
- 只依据上面给出的记录和上一版小传，不编造；前后矛盾时以较新的记录为准；拿不准的就不写。
- 上一版里仍然成立的内容可以保留，被新记录推翻的要改掉。
- 不写分数、等级或「好感度」之类的数值。
只输出小传正文（纯文本，不要标题、不要 Markdown、不要 JSON）。"""


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        return default
    return bool(value)


def _number(cfg: Mapping[str, Any], key: str, default: float, *, minimum: float, integer: bool) -> float | int:
    raw = cfg.get(key)
    try:
        value = float(default if raw is None or isinstance(raw, bool) else raw)
    except (TypeError, ValueError):
        value = float(default)
    if value != value:  # NaN
        value = float(default)
    value = max(float(minimum), value)
    return int(value) if integer else value


@dataclass(frozen=True)
class PortraitSettings:
    """Learning_Settings 里的关系小传参数；旧配置缺字段时按默认值（总开关默认关闭）。"""

    enabled: bool = False
    min_new_events: int = 3
    refresh_days: float = 7.0
    min_events: int = 3
    max_chars: int = 300
    max_per_round: int = 3
    daily_limit: int = 30

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "PortraitSettings":
        cfg = cfg if isinstance(cfg, Mapping) else {}
        return cls(
            # 新增模型消耗的能力默认关闭：旧配置没有该字段（None）时按 False 处理
            enabled=_as_bool(cfg.get("relationship_portrait_enabled"), False),
            min_new_events=int(_number(cfg, "portrait_min_new_events", 3, minimum=1, integer=True)),
            refresh_days=float(_number(cfg, "portrait_refresh_days", 7.0, minimum=0.5, integer=False)),
            min_events=int(_number(cfg, "portrait_min_events", 3, minimum=1, integer=True)),
            max_chars=int(_number(cfg, "portrait_max_chars", 300, minimum=60, integer=True)),
            max_per_round=int(_number(cfg, "portrait_max_per_round", 3, minimum=1, integer=True)),
            daily_limit=int(_number(cfg, "portrait_daily_limit", 30, minimum=1, integer=True)),
        )


# 注入与工具是否使用小传（由启动流程按 relationship_portrait_enabled 设置；关闭时完全沿用规则摘要）
_INJECTION: dict[str, bool] = {"enabled": False}


def configure_portrait_injection(*, enabled: bool | None = None) -> None:
    """None 表示不改。"""
    if enabled is not None:
        _INJECTION["enabled"] = bool(enabled)


def portrait_injection_enabled() -> bool:
    return bool(_INJECTION["enabled"])


def is_private_scene(group_id: Any) -> bool:
    return str(group_id or "").startswith(PRIVATE_SCENE_PREFIX)


# ─── 人物口径：与 load_timeline_events / 关系注入的 linked_user_ids 相同 ───


def _identity_groups(connection: Any, bot_id: str) -> dict[str, set[str]]:
    """本地账号 → 同一个人（管理员确认的关联）的全部本地账号；表不存在时为空。"""
    try:
        rows = connection.execute(
            "SELECT principal_id, person_key FROM person_identity_links WHERE bot_id=?",
            (str(bot_id or ""),),
        ).fetchall()
    except Exception:
        return {}
    by_person: dict[str, set[str]] = {}
    for principal, person_key in rows:
        local = local_user_id(str(principal or ""))
        if local and person_key:
            by_person.setdefault(str(person_key), set()).add(local)
    groups: dict[str, set[str]] = {}
    for members in by_person.values():
        for local in members:
            groups.setdefault(local, set()).update(members)
    return groups


def linked_user_ids(connection: Any, bot_id: str, user_id: str, *, principal: str | None = None) -> list[str]:
    """同一 Bot 认定的同一个人的全部本地账号（含自身，自身在前）。

    有完整 principal（``平台:user:账号``）时按关联表精确取；后台只有本地账号时按本地账号匹配。"""
    user = str(user_id or "").strip()
    if not user:
        return []
    if connection is None:
        return [user]
    if principal:
        linked = [local_user_id(item) for item in linked_principals_via(connection, bot_id, principal)]
    else:
        linked = sorted(_identity_groups(connection, bot_id).get(user, set()))
    return list(dict.fromkeys([user, *(item for item in linked if item)]))


def _connection(db: Any, connection: Any = None) -> Any:
    return connection if connection is not None else getattr(db, "conn", None)


def load_portrait_for(db: Any, *, bot_id: str, user_ids: Sequence[str], connection: Any = None) -> dict[str, Any] | None:
    """该人（含关联账号）当前的小传；没有正文时返回 None。"""
    repo = getattr(db, "relationship_portraits", None)
    if repo is None or not user_ids:
        return None
    try:
        item = repo.find(bot_id=bot_id, user_ids=list(user_ids), connection=connection)
    except Exception:
        return None
    return item if item and str(item.get("content") or "").strip() else None


def portrait_generation_events(
    db: Any,
    *,
    bot_id: str,
    user_ids: Sequence[str],
    limit: int = PORTRAIT_INPUT_EVENTS,
    connection: Any = None,
) -> list[dict[str, Any]]:
    """小传的生成输入：该人（含关联账号）非私聊时间线最近 ``limit`` 条，按时间从早到晚。

    私聊事件（``group_id`` 以 ``private:`` 开头）一律不进入——仓储按 viewer_scene="" 过滤，
    这里再过滤一遍，任何一层出错都不会把私聊内容写进小传。"""
    ids = [str(item) for item in user_ids if str(item or "").strip()]
    if not ids:
        return []
    events = load_timeline_events(
        db,
        bot_id=bot_id,
        user_id=ids[0],
        user_ids=ids,
        limit=max(1, int(limit)),
        viewer_scene=NON_PRIVATE_VIEWER,
        connection=connection,
    )
    kept = [dict(item) for item in events if isinstance(item, Mapping) and not is_private_scene(item.get("group_id"))]
    kept.sort(key=lambda item: (float(item.get("occurred_at") or 0.0), int(item.get("id") or 0)))
    return kept


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def non_private_stats(db: Any, *, bot_id: str, user_ids: Sequence[str], after_event_id: int = 0, connection: Any = None) -> dict[str, Any]:
    """非私聊时间线的条数、最大编号、最早/最近时间，以及 ``after_event_id`` 之后新增的条数（含关系类）。"""
    ids = [str(item) for item in user_ids if str(item or "").strip()]
    conn = _connection(db, connection)
    empty = {"total": 0, "max_event_id": 0, "first_at": None, "last_at": None, "new_events": 0, "new_relational": 0}
    if conn is None or not ids:
        return empty
    kinds = RELATIONAL_KINDS
    try:
        row = conn.execute(
            f"""SELECT COUNT(*), COALESCE(MAX(id), 0), MIN(occurred_at), MAX(occurred_at),
                       SUM(CASE WHEN id > ? THEN 1 ELSE 0 END),
                       SUM(CASE WHEN id > ? AND kind IN ({_marks(kinds)}) THEN 1 ELSE 0 END)
                  FROM person_timeline_events
                 WHERE bot_id=? AND user_id IN ({_marks(ids)}) AND COALESCE(group_id, '') NOT LIKE ?""",
            (int(after_event_id or 0), int(after_event_id or 0), *kinds, str(bot_id or ""), *ids, _PRIVATE_LIKE),
        ).fetchone()
    except Exception:
        return empty
    if not row:
        return empty
    return {
        "total": int(row[0] or 0),
        "max_event_id": int(row[1] or 0),
        "first_at": float(row[2]) if row[2] is not None else None,
        "last_at": float(row[3]) if row[3] is not None else None,
        "new_events": int(row[4] or 0),
        "new_relational": int(row[5] or 0),
    }


def _interaction_count(conn: Any, bot_id: str, user_ids: Sequence[str]) -> int | None:
    """非私聊场合的互动次数合计（私聊的档案不计入：小传只用公开场合的信息）。"""
    try:
        row = conn.execute(
            f"""SELECT SUM(interaction_count) FROM user_profiles
                 WHERE bot_id=? AND user_id IN ({_marks(user_ids)}) AND COALESCE(group_id, '') NOT LIKE ?""",
            (str(bot_id or ""), *user_ids, _PRIVATE_LIKE),
        ).fetchone()
    except Exception:
        return None
    return int(row[0]) if row and row[0] is not None else None


def _person_name(conn: Any, bot_id: str, user_ids: Sequence[str]) -> str:
    for user in user_ids:
        try:
            row = conn.execute(
                "SELECT display_name FROM person_registry WHERE qq_id=? LIMIT 1", (user,),
            ).fetchone()
        except Exception:
            row = None
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()
    try:
        row = conn.execute(
            f"""SELECT nickname FROM user_profiles
                 WHERE bot_id=? AND user_id IN ({_marks(user_ids)}) AND COALESCE(group_id, '') NOT LIKE ?
                   AND COALESCE(nickname, '') <> ''
                 ORDER BY COALESCE(last_seen, 0) DESC LIMIT 1""",
            (str(bot_id or ""), *user_ids, _PRIVATE_LIKE),
        ).fetchone()
    except Exception:
        row = None
    if row and str(row[0] or "").strip():
        return str(row[0]).strip()
    return str(user_ids[0]) if user_ids else ""


# ─── 提示词与输出清洗 ───


def _date(ts: Any) -> str:
    try:
        value = float(ts)
        if value <= 0:
            return "日期未知"
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return "日期未知"


def _record_line(item: Mapping[str, Any]) -> str:
    text = _injection_summary(item) or str(item.get("detail") or "").strip()
    text = text.replace("\r\n", " / ").replace("\n", " / ").replace("\r", " / ")
    if len(text) > PORTRAIT_INPUT_SUMMARY_CHARS:
        text = text[:PORTRAIT_INPUT_SUMMARY_CHARS] + "…"
    kind = str(item.get("kind") or "")
    return f"- {_date(item.get('occurred_at'))} [{KIND_LABELS.get(kind, '记录')}] {text}"


def build_portrait_prompt(
    *,
    bot_name: str,
    person_name: str,
    events: Sequence[Mapping[str, Any]],
    total_events: int,
    first_at: float | None,
    interactions: int | None,
    previous: str,
    max_chars: int,
    now: float,
) -> str:
    """只放 Bot 名、上一版小传、非私聊时间线（带日期与类别）、认识多久与互动次数；不放分数、维度或态度。"""
    basics: list[str] = []
    if first_at:
        days = max(0, int((now - float(first_at)) // 86400))
        basics.append(f"- 你第一次记下 ta：{_date(first_at)}（约 {days} 天前）")
    if interactions is not None:
        basics.append(f"- 你们打过的交道：互动约 {interactions} 次")
    basics.append(f"- 公开场合记下的记录一共 {int(total_events)} 条")
    shown = len(events)
    omitted = ""
    if total_events > shown:
        rest = int(total_events) - shown
        omitted = f"，更早的 {rest} 条没有列出" + ("，已概括在上一版小传里" if previous.strip() else "")
    records = [_record_line(item) for item in events]
    return PROMPT.format(
        bot_name=bot_name,
        person_name=person_name,
        basics="\n".join(basics),
        previous=previous.strip() or "（还没有，这是第一版）",
        shown=shown,
        omitted=omitted,
        records="\n".join(line for line in records if line.strip()) or "（没有）",
        max_chars=int(max_chars),
    )


_FENCE = re.compile(r"^```[A-Za-z]*\s*|\s*```$")
# 模型自带的标题：单独一行的「关系小传：」，或正文前的「【关系小传】」「关系小传：」
_TITLE = re.compile(r"^(?:#+\s*)?【?(?:关系小传|小传)】?\s*[:：]?[ \t]*\n")
_TITLE_INLINE = re.compile(r"^(?:【(?:关系小传|小传)】|(?:关系小传|小传)\s*[:：])\s*")
_SENTENCE_END = "。！？!?；;"


def truncate_portrait(text: str, max_chars: int) -> str:
    """超长截断到上限：尽量停在句末（不短于上限的六成），否则硬截断。"""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(mark) for mark in _SENTENCE_END)
    if end + 1 >= int(limit * 0.6):
        return cut[: end + 1]
    return cut


def clean_portrait_text(raw: Any, *, max_chars: int) -> str:
    text = str(raw or "").strip()
    text = _FENCE.sub("", text).strip()
    text = _TITLE.sub("", text).strip()
    text = _TITLE_INLINE.sub("", text).strip()
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return truncate_portrait(text, max_chars)


def _provider_label(response: Any, llm: Any) -> str:
    for source in (response, llm):
        for attr in ("provider_id", "last_provider_id"):
            value = getattr(source, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ─── 候选判定 ───


def portrait_decision(
    *,
    total_events: int,
    new_relational: int,
    new_events: int,
    portrait: Mapping[str, Any] | None,
    now: float,
    settings: PortraitSettings,
) -> tuple[str, str]:
    """返回 (动作, 原因)：动作为 generate / pending（管理员改过，只标记待更新）/ skip。"""
    if int(total_events) < settings.min_events:
        return "skip", "too_few_events"
    has_content = bool(portrait and str(portrait.get("content") or "").strip())
    reason = ""
    if int(new_relational) >= settings.min_new_events:
        reason = "new_events"
    elif has_content and int(new_events) > 0:
        since = float((portrait or {}).get("reviewed_at") or (portrait or {}).get("generated_at") or 0.0)
        if now - since >= settings.refresh_days * 86400.0:
            reason = "refresh"
    if not reason:
        return "skip", "no_new_events" if int(new_events) <= 0 else "not_due"
    if has_content and (portrait or {}).get("status") == "edited":
        return "pending", reason
    last_attempt = (portrait or {}).get("last_attempt_at")
    if (portrait or {}).get("last_error") and last_attempt and now - float(last_attempt) < RETRY_BACKOFF_SECONDS:
        return "skip", "retry_backoff"
    return "generate", reason


@dataclass
class PortraitCandidate:
    bot_id: str
    owner_user_id: str
    user_ids: tuple[str, ...]
    reason: str
    new_relational: int
    total_events: int
    last_at: float
    version: int = 0


@dataclass
class _Person:
    user_ids: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    max_id: int = 0
    last_at: float = 0.0


class RelationshipPortraitService:
    """后台为候选人物写关系小传；管理台的「重新生成」也走这里（串行、同一套校验）。"""

    def __init__(
        self,
        db: Any,
        llm: Any,
        *,
        settings: PortraitSettings | None = None,
        llm_for: Callable[[str], Any] | None = None,
        bot_ids_for: Callable[[], Iterable[str]] | None = None,
        bot_name_for: Callable[[str], str] | None = None,
    ):
        self.db = db
        # llm：与 text_chat 接口相同的客户端；llm_for(bot_id) 可按 Bot 取客户端（取不到时用 llm）
        self.llm = llm
        self.llm_for = llm_for
        self.settings = settings or PortraitSettings()
        # enabled 只管后台自动生成；管理员在管理台手动生成/编辑不受它限制
        self.enabled = bool(self.settings.enabled) and llm is not None
        self.bot_ids_for = bot_ids_for
        self.bot_name_for = bot_name_for or (lambda bot_id: bot_id)
        self.stats: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._day = ""
        self._runs_today = 0

    @property
    def repo(self) -> Any:
        return getattr(self.db, "relationship_portraits", None)

    def _client(self, bot_id: str) -> Any:
        if callable(self.llm_for):
            try:
                client = self.llm_for(bot_id)
            except Exception:
                client = None
            if client is not None:
                return client
        return self.llm

    def _count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    async def _run(self, func, *args, **kwargs):
        """正式连接可跨线程读写；旧的直连测试库保持线程绑定。"""
        conn = getattr(self.db, "conn", None)
        if hasattr(conn, "write_transaction"):
            return await asyncio.to_thread(func, *args, **kwargs)
        return func(*args, **kwargs)

    # ─── 后台循环 ───

    async def loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                if self.enabled:
                    await self.run_round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[WaveMemory] 关系小传检查失败: {exc!r}")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._runs_today = day, 0

    def _bot_ids(self) -> list[str]:
        if callable(self.bot_ids_for):
            return [str(item) for item in dict.fromkeys(self.bot_ids_for() or ()) if str(item or "").strip()]
        try:
            rows = self.db.conn.execute("SELECT DISTINCT bot_id FROM person_timeline_events").fetchall()
        except Exception:
            return []
        return [str(row[0]) for row in rows if row and row[0]]

    def plan(self, bot_id: str, *, now: float) -> list[PortraitCandidate]:
        """列出这个 Bot 这一轮该写小传的人（按新增关系事件多少、最近活跃排序）；
        管理员改过的小传只标记待更新。同步执行（调用方放进线程）。"""
        conn = getattr(self.db, "conn", None)
        repo = self.repo
        if conn is None or repo is None:
            return []
        try:
            rows = conn.execute(
                """SELECT user_id, COUNT(*), COALESCE(MAX(id), 0), MAX(occurred_at)
                     FROM person_timeline_events
                    WHERE bot_id=? AND COALESCE(group_id, '') NOT LIKE ?
                    GROUP BY user_id""",
                (bot_id, _PRIVATE_LIKE),
            ).fetchall()
        except Exception:
            return []
        groups = _identity_groups(conn, bot_id)
        persons: dict[frozenset[str], _Person] = {}
        for user_id, count, max_id, last_at in rows:
            user = str(user_id or "").strip()
            if not user:
                continue
            key = frozenset(groups.get(user) or {user})
            person = persons.setdefault(key, _Person())
            person.user_ids.update(key)
            person.counts[user] = int(count or 0)
            person.total += int(count or 0)
            person.max_id = max(person.max_id, int(max_id or 0))
            person.last_at = max(person.last_at, float(last_at or 0.0))
        portraits: dict[str, dict[str, Any]] = {item["user_id"]: item for item in repo.list_for_bot(bot_id)}
        planned: list[PortraitCandidate] = []
        for person in persons.values():
            ordered = sorted(person.user_ids, key=lambda user: (-person.counts.get(user, 0), user))
            owned = [portraits[user] for user in ordered if user in portraits]
            owned.sort(key=lambda item: (bool(item.get("content")), item.get("version", 0), item.get("updated_at", 0.0)), reverse=True)
            portrait = owned[0] if owned else None
            cursor = int((portrait or {}).get("reviewed_through_event_id") or 0)
            if person.total < self.settings.min_events:
                self._count("skip:too_few_events")
                continue
            if person.max_id <= cursor:
                new = {"new_events": 0, "new_relational": 0}
            else:
                new = non_private_stats(self.db, bot_id=bot_id, user_ids=ordered, after_event_id=cursor)
            action, reason = portrait_decision(
                total_events=person.total,
                new_relational=int(new["new_relational"]),
                new_events=int(new["new_events"]),
                portrait=portrait,
                now=now,
                settings=self.settings,
            )
            if action == "skip":
                self._count(f"skip:{reason}")
                continue
            owner = str((portrait or {}).get("user_id") or ordered[0])
            if action == "pending":
                if not (portrait or {}).get("pending_update"):
                    repo.mark_pending(bot_id=bot_id, user_id=owner, now=now)
                self._count("pending:edited")
                continue
            planned.append(PortraitCandidate(
                bot_id=bot_id,
                owner_user_id=owner,
                user_ids=tuple(ordered),
                reason=reason,
                new_relational=int(new["new_relational"]),
                total_events=person.total,
                last_at=person.last_at,
                version=int((portrait or {}).get("version") or 0),
            ))
        planned.sort(key=lambda item: (-item.new_relational, -item.last_at, item.owner_user_id))
        return planned

    async def run_round(self, *, now: float | None = None) -> int:
        """跑一轮：每轮最多 max_per_round 人、每天最多 daily_limit 次调用，串行。返回写成的人数。"""
        if not self.enabled or self.repo is None:
            return 0
        stamp = float(now if now is not None else time.time())
        self._roll_day(stamp)
        attempted = written = 0
        for bot_id in await self._run(self._bot_ids):
            for candidate in await self._run(self.plan, bot_id, now=stamp):
                if attempted >= self.settings.max_per_round:
                    self._count("skip:round_limit")
                    continue
                if self._runs_today >= self.settings.daily_limit:
                    self._count("skip:daily_limit")
                    continue
                attempted += 1
                self._runs_today += 1
                result = await self.generate(
                    candidate.bot_id,
                    candidate.user_ids,
                    owner_user_id=candidate.owner_user_id,
                    expected_version=candidate.version,
                    now=stamp,
                )
                if result.get("ok"):
                    written += 1
        return written

    # ─── 生成 ───

    def _material(self, bot_id: str, user_ids: Sequence[str], owner_user_id: str) -> dict[str, Any]:
        conn = getattr(self.db, "conn", None)
        events = portrait_generation_events(self.db, bot_id=bot_id, user_ids=user_ids)
        stats = non_private_stats(self.db, bot_id=bot_id, user_ids=user_ids)
        repo = self.repo
        return {
            "events": events,
            "stats": stats,
            "interactions": _interaction_count(conn, bot_id, list(user_ids)) if conn is not None else None,
            "person_name": _person_name(conn, bot_id, list(user_ids)) if conn is not None else owner_user_id,
            "portrait": repo.get(bot_id=bot_id, user_id=owner_user_id) if repo is not None else None,
        }

    async def _record_error(self, bot_id: str, owner_user_id: str, code: str, now: float) -> None:
        try:
            await self._run(self.repo.record_error, bot_id=bot_id, user_id=owner_user_id, error=code, now=now)
        except Exception as exc:
            logger.debug(f"[WaveMemory] 关系小传错误记录失败 {owner_user_id}: {exc!r}")

    async def generate(
        self,
        bot_id: str,
        user_ids: Sequence[str],
        *,
        owner_user_id: str,
        expected_version: int | None,
        allow_overwrite_edited: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """为一个人写一版小传。返回 {"ok": True, "item": …} 或 {"ok": False, "code": …}。"""
        llm = self._client(bot_id)
        if llm is None or self.repo is None:
            return {"ok": False, "code": "portrait_service_unavailable"}
        ids = tuple(dict.fromkeys([str(owner_user_id), *(str(item) for item in user_ids)]))
        async with self._lock:
            stamp = float(now if now is not None else time.time())
            material = await self._run(self._material, bot_id, ids, str(owner_user_id))
            current = material["portrait"] or {}
            if expected_version is not None and int(current.get("version") or 0) != int(expected_version):
                self._count("skip:portrait_version_conflict")
                return {"ok": False, "code": "portrait_version_conflict"}
            if current.get("status") == "edited" and current.get("content") and not allow_overwrite_edited:
                await self._run(self.repo.mark_pending, bot_id=bot_id, user_id=str(owner_user_id), now=stamp)
                self._count("pending:edited")
                return {"ok": False, "code": "portrait_edited_by_admin"}
            events = material["events"]
            if len(events) < self.settings.min_events:
                self._count("skip:too_few_events")
                return {"ok": False, "code": "portrait_too_few_events"}
            prompt = build_portrait_prompt(
                bot_name=self.bot_name_for(bot_id) or bot_id,
                person_name=material["person_name"] or str(owner_user_id),
                events=events,
                total_events=int(material["stats"]["total"]),
                first_at=material["stats"]["first_at"],
                interactions=material["interactions"],
                previous=str(current.get("content") or ""),
                max_chars=self.settings.max_chars,
                now=stamp,
            )
            self._count("calls")
            try:
                response = await llm.text_chat(
                    prompt=prompt,
                    system_prompt=prepend_identity_safety_system_prompt(None, always=True),
                    contexts=[],
                )
            except Exception as exc:
                self._count("failed")
                self._count("failed:llm_error")
                await self._record_error(bot_id, str(owner_user_id), f"llm_error: {type(exc).__name__}: {exc}", stamp)
                logger.warning(f"[WaveMemory] 关系小传生成失败 {bot_id}:{owner_user_id}: {exc!r}")
                return {"ok": False, "code": "portrait_llm_failed"}
            text = clean_portrait_text(getattr(response, "completion_text", ""), max_chars=self.settings.max_chars)
            if not text:
                self._count("failed")
                self._count("failed:empty_output")
                await self._record_error(bot_id, str(owner_user_id), "empty_output", stamp)
                return {"ok": False, "code": "portrait_empty_output"}
            if is_identity_contamination(text):
                self._count("failed")
                self._count("failed:identity_contamination")
                await self._record_error(bot_id, str(owner_user_id), "identity_contamination", stamp)
                logger.warning(f"[WaveMemory] 关系小传疑似身份污染，未保存 {bot_id}:{owner_user_id}")
                return {"ok": False, "code": "identity_contamination"}
            try:
                saved = await self._run(
                    self.repo.save_content,
                    bot_id=bot_id,
                    user_id=str(owner_user_id),
                    content=text,
                    status="auto",
                    expected_version=int(current.get("version") or 0),
                    provider=_provider_label(response, llm),
                    source_max_event_id=max(int(item.get("id") or 0) for item in events),
                    source_event_count=len(events),
                    allow_overwrite_edited=allow_overwrite_edited,
                    now=stamp,
                )
            except PortraitConflict as exc:
                self._count(f"skip:{exc.code}")
                return {"ok": False, "code": exc.code}
            except PortraitError as exc:
                self._count("failed")
                await self._record_error(bot_id, str(owner_user_id), exc.code, stamp)
                return {"ok": False, "code": exc.code}
            self._count("success")
            logger.info(f"[WaveMemory] 已写关系小传 {bot_id}:{owner_user_id} v{saved['version']}（{len(text)} 字）")
            return {"ok": True, "item": saved}

    async def regenerate(
        self, bot_id: str, user_id: str, *, expected_version: int | None, principal: str | None = None,
    ) -> dict[str, Any]:
        """管理台「重新生成」：管理员改过的也覆盖（旧版本留档）；不占每日自动生成的名额。"""
        repo = self.repo
        if repo is None:
            return {"ok": False, "code": "portrait_service_unavailable"}

        def resolve() -> tuple[list[str], dict[str, Any] | None]:
            ids = linked_user_ids(getattr(self.db, "conn", None), bot_id, user_id, principal=principal)
            return ids, repo.find(bot_id=bot_id, user_ids=ids)

        ids, portrait = await self._run(resolve)
        owner = str((portrait or {}).get("user_id") or user_id)
        return await self.generate(
            bot_id, ids, owner_user_id=owner, expected_version=expected_version, allow_overwrite_edited=True,
        )


def keep_portrait(db: Any, *, bot_id: str, user_ids: Sequence[str], expected_version: int | None, now: float | None = None) -> dict[str, Any]:
    """管理员选择保留当前小传：清掉待更新标记，把「上次小传之后」的起点推进到当前最新的非私聊事件。"""
    repo = getattr(db, "relationship_portraits", None)
    if repo is None:
        raise PortraitError("portrait_store_unavailable", "关系小传存储未初始化")
    portrait = repo.find(bot_id=bot_id, user_ids=list(user_ids))
    if portrait is None or not portrait.get("content"):
        raise PortraitError("portrait_not_found", "还没有关系小传")
    stats = non_private_stats(db, bot_id=bot_id, user_ids=list(user_ids))
    return repo.keep_current(
        bot_id=bot_id,
        user_id=portrait["user_id"],
        expected_version=expected_version,
        reviewed_through_event_id=int(stats["max_event_id"]),
        now=now,
    )


__all__ = [
    "KIND_LABELS",
    "NON_PRIVATE_VIEWER",
    "PROMPT",
    "PortraitCandidate",
    "PortraitSettings",
    "RELATIONAL_KINDS",
    "RelationshipPortraitService",
    "build_portrait_prompt",
    "clean_portrait_text",
    "configure_portrait_injection",
    "is_private_scene",
    "keep_portrait",
    "linked_user_ids",
    "load_portrait_for",
    "non_private_stats",
    "portrait_decision",
    "portrait_generation_events",
    "portrait_injection_enabled",
    "truncate_portrait",
]
