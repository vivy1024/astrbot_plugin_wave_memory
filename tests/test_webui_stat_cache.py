import asyncio
import threading

from webui.stat_cache import cached_stat, clear_stat_cache


def test_cached_stat_runs_off_loop_thread_and_caches():
    clear_stat_cache()
    calls = []

    def compute():
        calls.append(threading.get_ident())
        return 42

    async def main():
        loop_thread = threading.get_ident()
        first = await cached_stat("k", 60, compute)
        second = await cached_stat("k", 60, compute)
        return loop_thread, first, second

    loop_thread, first, second = asyncio.run(main())
    assert first == second == 42
    assert len(calls) == 1, "TTL 内第二次应命中缓存"
    assert calls[0] != loop_thread, "统计查询必须在工作线程里执行，不能占 WebUI 事件循环"


def test_cached_stat_concurrent_requests_compute_once_and_refresh_bypasses():
    clear_stat_cache()
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    async def main():
        results = await asyncio.gather(*(cached_stat("c", 60, compute) for _ in range(5)))
        refreshed = await cached_stat("c", 60, compute, refresh=True)
        return results, refreshed

    results, refreshed = asyncio.run(main())
    assert results == [1] * 5
    assert refreshed == 2
