"""消息入库健康：统计最近一段时间被接收/被拒的入站消息，拒绝过多时报警。

2026-09 羽书的 QQ 换号后，所有消息因 ``unknown_bot_self_id`` 被拒约 20 小时，只在日志里
每分钟一条 WARN，没人发现，记忆静默丢失。这里按分钟桶统计，并记下被拒消息的 Bot 账号，
由 ``/api/health`` 与 9876 首页「系统健康」直接显示原因和处理办法。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

WINDOW_SECONDS = 600
# 窗口内至少这么多条被拒、且被拒占比过半才报警（偶发的单条非 Bot 事件不算）
MIN_REJECTED = 5
REJECT_RATIO = 0.5

_REASON_HINTS = {
    "unknown_bot_self_id": "收到消息的 Bot 账号没有绑定到任何 Bot：到 9876「Bot 管理」给对应 Bot 添加该账号的绑定",
    "platform_unresolved": "消息没有平台标识，检查 AstrBot 平台适配器",
    "sender_id_required": "消息缺少发送者 id",
    "scope_resolver_unavailable": "作用域解析器未初始化，插件启动可能不完整",
}


class IngressHealth:
    def __init__(self, *, window_seconds: int = WINDOW_SECONDS, clock=time.time) -> None:
        self._window = int(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        # (分钟桶, accepted, {reason: count})
        self._buckets: deque[list[Any]] = deque()
        self._unknown_self_ids: dict[str, float] = {}
        self._last_accepted_at: float | None = None
        self._last_rejected_at: float | None = None
        self._total_accepted = 0
        self._total_rejected = 0

    def _bucket(self, now: float) -> list[Any]:
        minute = int(now // 60)
        if not self._buckets or self._buckets[-1][0] != minute:
            self._buckets.append([minute, 0, {}])
        horizon = int((now - self._window) // 60)
        while self._buckets and self._buckets[0][0] < horizon:
            self._buckets.popleft()
        return self._buckets[-1]

    def record_accepted(self) -> None:
        now = self._clock()
        with self._lock:
            self._bucket(now)[1] += 1
            self._last_accepted_at = now
            self._total_accepted += 1

    def record_rejected(self, reason: str, *, self_id: str = "") -> None:
        now = self._clock()
        reason = str(reason or "unknown")
        with self._lock:
            reasons = self._bucket(now)[2]
            reasons[reason] = reasons.get(reason, 0) + 1
            self._last_rejected_at = now
            self._total_rejected += 1
            if reason == "unknown_bot_self_id" and self_id:
                self._unknown_self_ids[str(self_id)] = now
                if len(self._unknown_self_ids) > 20:
                    oldest = min(self._unknown_self_ids, key=self._unknown_self_ids.get)
                    self._unknown_self_ids.pop(oldest, None)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._bucket(now)
            accepted = sum(bucket[1] for bucket in self._buckets)
            reasons: dict[str, int] = {}
            for bucket in self._buckets:
                for reason, count in bucket[2].items():
                    reasons[reason] = reasons.get(reason, 0) + count
            unknown = sorted(
                (sid for sid, seen in self._unknown_self_ids.items() if now - seen <= self._window),
                key=lambda sid: -self._unknown_self_ids[sid],
            )
            last_accepted, last_rejected = self._last_accepted_at, self._last_rejected_at
            totals = (self._total_accepted, self._total_rejected)
        rejected = sum(reasons.values())
        alarming = rejected >= MIN_REJECTED and rejected >= REJECT_RATIO * (accepted + rejected)
        top_reason = max(reasons, key=reasons.get) if reasons else ""
        return {
            "status": "error" if alarming else "ok",
            "window_seconds": self._window,
            "accepted": accepted,
            "rejected": rejected,
            "reasons": reasons,
            "unknown_self_ids": unknown,
            "last_accepted_at": last_accepted,
            "last_rejected_at": last_rejected,
            "total_accepted": totals[0],
            "total_rejected": totals[1],
            "hint": _REASON_HINTS.get(top_reason, "") if alarming else "",
        }

    def summary_text(self) -> str:
        """给健康列表的一行说明。"""
        snap = self.snapshot()
        minutes = snap["window_seconds"] // 60
        if snap["status"] == "ok":
            return f"最近 {minutes} 分钟入库 {snap['accepted']} 条" + (f"，被拒 {snap['rejected']} 条" if snap["rejected"] else "")
        reasons = "、".join(f"{reason} {count}" for reason, count in sorted(snap["reasons"].items(), key=lambda kv: -kv[1]))
        accounts = f"；未绑定的账号：{', '.join(snap['unknown_self_ids'])}" if snap["unknown_self_ids"] else ""
        return f"最近 {minutes} 分钟 {snap['rejected']} 条消息没有入库（{reasons}）{accounts}。{snap['hint']}"


__all__ = ["IngressHealth", "MIN_REJECTED", "REJECT_RATIO", "WINDOW_SECONDS"]
