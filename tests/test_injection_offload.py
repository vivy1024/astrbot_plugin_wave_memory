"""声明 offload_to_thread 的同步通道在工作线程里跑：不占事件循环，多个同步通道真正并行。"""

from __future__ import annotations

import asyncio
import threading
import time

from services.injection.orchestrator import _build


class _SyncChannel:
    offload_to_thread = True

    def __init__(self):
        self.thread = None

    async def build(self, ctx):
        self.thread = threading.get_ident()
        time.sleep(0.2)  # 模拟同步读库
        return "done"


class _AsyncChannel:
    async def build(self, ctx):
        return threading.get_ident()


def test_offloaded_channels_run_off_loop_and_in_parallel():
    channels = [_SyncChannel(), _SyncChannel(), _SyncChannel()]

    async def scenario():
        loop_thread = threading.get_ident()
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        started = time.perf_counter()
        results = await asyncio.gather(*(_build(ch, None) for ch in channels), _build(_AsyncChannel(), None))
        elapsed = time.perf_counter() - started
        beat.cancel()
        return loop_thread, results, elapsed, ticks

    loop_thread, results, elapsed, ticks = asyncio.run(scenario())
    assert results[:3] == ["done"] * 3
    assert results[3] == loop_thread  # 普通通道仍在事件循环上
    assert all(ch.thread != loop_thread for ch in channels)
    assert elapsed < 0.45  # 串行会是 0.6 秒
    assert ticks >= 5  # 事件循环在此期间没被卡住
