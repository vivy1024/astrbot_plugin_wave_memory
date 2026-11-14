"""注入性能门禁：线程化通道跑在常驻工作循环里，同步阻塞不拖慢主事件循环，也不每次新建循环。"""

from __future__ import annotations

import asyncio
import threading
import time

from services.injection import orchestrator as orch
from services.injection.channel_base import InjectionResult


class _Channel:
    """build 里是阻塞的同步代码（模拟 HNSW 检索 / 同步读库），并记录自己跑在哪个线程与循环。"""

    offload_to_thread = True

    def __init__(self, name: str, block_seconds: float = 0.0) -> None:
        self.name = name
        self.block_seconds = block_seconds
        self.thread_ids: list[int] = []
        self.loop_ids: list[int] = []

    async def build(self, ctx):
        self.thread_ids.append(threading.get_ident())
        self.loop_ids.append(id(asyncio.get_running_loop()))
        if self.block_seconds:
            time.sleep(self.block_seconds)  # 故意同步阻塞
        return InjectionResult.empty(self.name, reason="test")


def test_threaded_channels_do_not_run_on_the_main_loop():
    channel = _Channel("blocking")

    async def scenario():
        main_thread = threading.get_ident()
        main_loop = id(asyncio.get_running_loop())
        await orch._build(channel, object())
        return main_thread, main_loop

    main_thread, main_loop = asyncio.run(scenario())
    assert channel.thread_ids[0] != main_thread
    assert channel.loop_ids[0] != main_loop


def test_worker_loops_are_persistent_not_recreated_per_call():
    channel = _Channel("steady")

    async def scenario():
        for _ in range(12):
            await orch._build(channel, object())

    asyncio.run(scenario())
    # 12 次调用只用到常驻池里有限的几个循环，而不是 12 个各自新建的循环
    assert len(set(channel.loop_ids)) <= orch._WORKERS._size
    assert len(set(channel.thread_ids)) <= orch._WORKERS._size


def test_blocking_channel_does_not_stall_the_main_event_loop():
    slow = _Channel("slow", block_seconds=0.4)
    ticks: list[float] = []

    async def heartbeat():
        # 主循环每 20ms 醒一次；若被同步阻塞占住，相邻两次的间隔会远大于 20ms
        last = time.perf_counter()
        for _ in range(15):
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            ticks.append(now - last)
            last = now

    async def scenario():
        await asyncio.gather(orch._build(slow, object()), heartbeat())

    asyncio.run(scenario())
    assert max(ticks) < 0.2, f"主事件循环被同步通道拖住了：最大间隔 {max(ticks) * 1000:.0f}ms"


def test_one_blocking_channel_does_not_delay_the_others():
    slow = _Channel("slow", block_seconds=0.5)
    fast = [_Channel(f"fast{i}") for i in range(3)]
    finished: dict[str, float] = {}

    async def timed(channel):
        started = time.perf_counter()
        await orch._build(channel, object())
        finished[channel.name] = time.perf_counter() - started

    async def scenario():
        await asyncio.gather(timed(slow), *(timed(c) for c in fast))

    asyncio.run(scenario())
    assert finished["slow"] >= 0.45
    assert all(finished[c.name] < 0.25 for c in fast), finished


def test_shutdown_then_reuse_recreates_the_pool():
    channel = _Channel("again")

    async def once():
        await orch._build(channel, object())

    asyncio.run(once())
    orch.shutdown_worker_loops()
    asyncio.run(once())
    assert len(channel.loop_ids) == 2 and channel.loop_ids[0] != channel.loop_ids[1]


def test_queue_time_is_still_measured():
    channel = _Channel("measured")

    async def scenario():
        return await orch._build(channel, object())

    result = asyncio.run(scenario())
    assert isinstance(result, InjectionResult) and result.queue_ms >= 0.0
