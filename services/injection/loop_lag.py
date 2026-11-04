"""事件循环延迟探针：每 100ms 醒一次，量实际晚醒了多少。

2026-11 线上实测：safety / meme_hint / persona 这些不读库、只跑几个正则的通道
p50 5ms，p95 却到 206ms，且和线程化读库通道一起变慢（r≈0.56~0.87）。这说明
有时是事件循环本身被同步代码占住了。探针记下每次晚醒的毫秒数，注入时取这一段
窗口里的最大值写进 trace，和线程池排队耗时一起判断瓶颈在哪。

开销：一个协程每 100ms 唤醒一次，只做一次 perf_counter 与 deque.append。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

INTERVAL_S = 0.1
MAX_SAMPLES = 600  # 约 1 分钟


class LoopLagProbe:
    def __init__(self, *, interval_s: float = INTERVAL_S, max_samples: int = MAX_SAMPLES):
        self.interval_s = interval_s
        # (醒来时刻 perf_counter, 晚醒毫秒)
        self._samples: deque[tuple[float, float]] = deque(maxlen=max_samples)
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.enabled = True

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """在当前事件循环上启动；重复调用幂等，换了事件循环会重建。"""
        loop = asyncio.get_running_loop()
        if self.running and self._loop is loop:
            return
        self.stop()
        self._loop = loop
        self._samples.clear()
        self._task = loop.create_task(self._run(), name="wave_memory_loop_lag_probe")

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    async def _run(self) -> None:
        interval = self.interval_s
        try:
            while True:
                before = time.perf_counter()
                await asyncio.sleep(interval)
                now = time.perf_counter()
                lag_ms = max(0.0, (now - before - interval) * 1000)
                self._samples.append((now, lag_ms))
        except asyncio.CancelledError:
            return

    def window(self, since: float, until: float | None = None) -> dict[str, Any]:
        """``[since, until]`` 这段 perf_counter 时间内的晚醒统计；关闭或没有样本时返回空。

        取样点在「醒来时刻」，一次长时间阻塞会在阻塞结束那一刻记下一个大值，
        所以窗口右端多放半个间隔，避免恰好错过注入期间那次阻塞的样本。
        """
        if not self.enabled or not self._samples:
            return {}
        end = (time.perf_counter() if until is None else until) + self.interval_s / 2
        values = [lag for at, lag in list(self._samples) if since <= at <= end]
        if not values:
            return {}
        values.sort()
        return {
            "loop_lag_max_ms": round(values[-1], 1),
            "loop_lag_p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 1),
            "loop_lag_samples": len(values),
        }


_probe = LoopLagProbe()


def get_probe() -> LoopLagProbe:
    return _probe


def start_probe() -> None:
    try:
        _probe.start()
    except RuntimeError as exc:  # 没有运行中的事件循环
        logger.debug("[WaveMemory] loop lag probe not started: %s", exc)


def stop_probe() -> None:
    _probe.stop()


__all__ = ["LoopLagProbe", "get_probe", "start_probe", "stop_probe"]
