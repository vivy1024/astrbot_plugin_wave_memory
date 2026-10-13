"""WebUI 统计类查询的线程执行 + 短时缓存。

WebUI 跑在自己的单个事件循环上：一条秒级的同步全表计数会把同时打开的
所有页面一起卡住（线上 /api/system 33 秒，记忆页、总览页随之超时）。
这类数字只是面板展示用，几分钟的陈旧完全可以接受。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_cache: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def cached_stat(key: str, ttl: float, compute: Callable[[], T], *, refresh: bool = False) -> T:
    """在工作线程里执行 ``compute``，结果缓存 ``ttl`` 秒；同 key 并发请求只算一次。

    ``compute`` 经 ConnectionManager 读库时会自动用当前工作线程自己的读连接。
    """
    hit = _cache.get(key)
    if not refresh and hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if not refresh and hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = await asyncio.to_thread(compute)
        _cache[key] = (time.monotonic(), value)
        return value


def hot_seconds(key: str, default: float) -> float:
    """管理台缓存时长从 9876 热参数读（webui.*）；没有运行时就用默认值。"""
    try:
        from ..services.hot_config import hot_value
    except ImportError:  # pragma: no cover - 独立测试
        from services.hot_config import hot_value
    return float(hot_value(key, default))


def clear_stat_cache() -> None:
    _cache.clear()
