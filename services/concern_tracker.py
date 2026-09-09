"""ConcernTracker — 关切系统的只读投影。

本体定位：灵魂关切是尚未闭合的牵挂（L2 主观心智），不是人情锚点，也不会自动
成为事实或信念。

写入铁律：关切的创建与生命周期推进只能经
``ProductionWriteGateway.transition_concern`` → WriteCoordinator → domain 表 + outbox。
本类**不再持有任何写路径**（历史上的 ``add``/``_persist`` 全量替换会互相覆盖，
``tick`` 会物理剔除未结案关切，均已移除），只负责按 RuntimeScope 读取与摘要。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - repository tests run without AstrBot
    import logging

    logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    try:
        from ..engine.database import WaveMemoryDB
    except ImportError:  # pragma: no cover
        from engine.database import WaveMemoryDB


# 只有未决状态才允许作为"当前心事"参与匹配与注入；resolved/expired/archived
# 必须显式 reopen 后才会重新影响行为。
INJECTABLE_CONCERN_STATUSES = frozenset({"active", "progressing", "dormant"})

# 投影缓存有效期：写入方通过 invalidate() 主动失效，TTL 只作为兜底。
_CACHE_TTL_SECONDS = 30.0


class Concern:
    """一条关切（只读快照）。"""

    __slots__ = (
        "topic",
        "intensity",
        "origin_memory_id",
        "bot_id",
        "created_at",
        "last_triggered",
        "decay_rate",
        "status",
        "concern_type",
        "last_progress_at",
        "expected_resolution_at",
    )

    def __init__(
        self,
        topic: str,
        intensity: float = 0.7,
        origin_memory_id: int = 0,
        bot_id: str = "",
        created_at: float = 0,
        last_triggered: float = 0,
        decay_rate: float = 0.9,
        status: str = "active",
        concern_type: str = "",
        last_progress_at: float | None = None,
        expected_resolution_at: float | None = None,
    ):
        self.topic = topic
        self.intensity = intensity
        self.origin_memory_id = origin_memory_id
        self.bot_id = bot_id
        self.created_at = created_at or time.time()
        self.last_triggered = last_triggered or time.time()
        self.decay_rate = decay_rate
        self.status = str(status or "active")
        self.concern_type = concern_type or ""
        self.last_progress_at = last_progress_at
        self.expected_resolution_at = expected_resolution_at

    @property
    def injectable(self) -> bool:
        return self.status in INJECTABLE_CONCERN_STATUSES


class ConcernTracker:
    """关切只读投影 — 回答"bot 当前在意什么"，不负责写入。

    ``coordinator`` 参数仅为兼容既有构造注入而保留；本类不使用它写库。
    """

    def __init__(
        self,
        db: WaveMemoryDB,
        bot_id: str = "",
        max_concerns: int = 10,
        *,
        scope=None,
        repository=None,
        coordinator=None,  # noqa: ARG002 - 兼容旧注入，写路径已迁出本类
    ):
        self.db = db
        self.bot_id = bot_id
        self.max_concerns = max_concerns
        self.scope = scope
        self.repository = repository
        self.concerns: list[Concern] = []
        self._scoped_concerns: dict[tuple[str, str, str], list[Concern]] = {}
        self._cached_at: dict[tuple[str, str, str], float] = {}
        # Legacy concerns 只读加载用于兼容展示，不再创建或写入。
        self._load()

    def _load(self):
        """启动时从 legacy 表恢复（只读兼容）。"""
        try:
            rows = self.db.conn.execute(
                "SELECT topic, intensity, origin_memory_id, bot_id, created_at, last_triggered "
                "FROM concerns WHERE bot_id = ? ORDER BY intensity DESC LIMIT ?",
                (self.bot_id, self.max_concerns),
            ).fetchall()
            for r in rows:
                self.concerns.append(Concern(
                    topic=r[0], intensity=r[1], origin_memory_id=r[2],
                    bot_id=r[3], created_at=r[4], last_triggered=r[5],
                ))
        except Exception:
            pass

    @staticmethod
    def _scope_key(scope) -> tuple[str, str, str]:
        if scope is None or getattr(scope, "session", None) is None:
            raise ValueError("scope_required")
        return scope.bot_id, scope.session.id, scope.visibility

    def invalidate(self, scope=None) -> None:
        """写入方在命令成功后调用，使下一次读取重新拉取正式投影。"""
        effective_scope = scope or self.scope
        if effective_scope is None:
            self._scoped_concerns.clear()
            self._cached_at.clear()
            return
        try:
            key = self._scope_key(effective_scope)
        except ValueError:
            return
        self._scoped_concerns.pop(key, None)
        self._cached_at.pop(key, None)

    def _concerns_for(self, scope=None) -> list[Concern]:
        effective_scope = scope or self.scope
        if effective_scope is None:
            return self.concerns
        key = self._scope_key(effective_scope)
        cached_at = self._cached_at.get(key, 0.0)
        bucket = self._scoped_concerns.get(key)
        if bucket is not None and time.time() - cached_at < _CACHE_TTL_SECONDS:
            return bucket
        bucket = []
        if self.repository is not None and hasattr(self.repository, "get_state"):
            try:
                items = self.repository.get_state(effective_scope, limit=25, offset=0)["concerns"]["items"]
                bucket.extend(Concern(
                    topic=item["topic"],
                    intensity=float(item.get("intensity", 0.7)),
                    origin_memory_id=int(item.get("origin_memory_id") or 0),
                    bot_id=effective_scope.bot_id,
                    created_at=float(item.get("created_at") or 0),
                    last_triggered=float(item.get("last_triggered") or 0),
                    status=str(item.get("status") or "active"),
                    concern_type=str(item.get("concern_type") or ""),
                    last_progress_at=item.get("last_progress_at"),
                    expected_resolution_at=item.get("expected_resolution_at"),
                ) for item in items)
            except Exception as exc:
                logger.debug(f"[ConcernTracker] Scoped load failed: {exc}")
        self._scoped_concerns[key] = bucket
        self._cached_at[key] = time.time()
        return bucket

    def pending_for(self, scope=None) -> list[Concern]:
        """指定 Scope 中仍属未决状态的关切。"""
        return [c for c in self._concerns_for(scope or self.scope) if c.injectable]

    def match(self, message: str, *, scope=None) -> float:
        """返回消息与指定 Scope 未决关切的最高匹配度（0-1）。"""
        concerns = self.pending_for(scope or self.scope)
        if not concerns:
            return 0.0

        msg_lower = message.lower()
        max_score = 0.0
        for c in concerns:
            words = [w for w in c.topic.split() if len(w) > 1] or [c.topic]
            hit_count = sum(1 for w in words if w.lower() in msg_lower)
            if hit_count > 0:
                max_score = max(max_score, c.intensity * (hit_count / len(words)))

        return max_score

    def active_topics_for(self, scope=None) -> list[str]:
        """返回指定 Scope 仍未决的主题。"""
        return [c.topic for c in sorted(self.pending_for(scope), key=lambda c: -c.intensity)]

    @property
    def active_topics(self) -> list[str]:
        return self.active_topics_for()

    def summary_for(self, scope=None) -> str:
        """生成指定 Scope 的关切摘要；已结案/已归档的牵挂不会作为心事注入。"""
        active = [c for c in self.pending_for(scope) if c.intensity > 0.3]
        if not active:
            return ""
        topics = [c.topic for c in sorted(active, key=lambda c: -c.intensity)[:3]]
        return f"[当前在想] {'、'.join(topics)}"

    @property
    def summary(self) -> str:
        return self.summary_for()

    @staticmethod
    def _is_similar(a: str, b: str) -> bool:
        """简单判断两个主题是否相似。"""
        a_set = set(a.lower())
        b_set = set(b.lower())
        if not a_set or not b_set:
            return False
        overlap = len(a_set & b_set) / max(len(a_set | b_set), 1)
        return overlap > 0.5


__all__ = ["Concern", "ConcernTracker", "INJECTABLE_CONCERN_STATUSES"]
