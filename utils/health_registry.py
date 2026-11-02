"""健康状态注册表 + 运行时错误收集器

main.py 各服务初始化/运行时调用 register/record_error，
WebUI system.py 读取展示给用户。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Optional


# ─── 服务状态注册表 ───
_services: dict[str, dict] = {}


def register(name: str, status: str = "ok", reason: str = "", dependency: str = ""):
    """注册/更新服务状态。status: ok / degraded / off / error"""
    _services[name] = {"name": name, "status": status, "reason": reason, "dependency": dependency, "ts": time.time()}


def get_all_services() -> list[dict]:
    """返回所有已注册服务的状态。"""
    return list(_services.values())


# ─── 运行时错误收集（ring buffer 最近 50 条）───
_errors: deque = deque(maxlen=50)

# 超过这个时长的条目不再展示、不再计入告警。
# 2026-10-28 一条已经过去的误报因为环形缓冲只在攒满 50 条时才挤掉旧记录，
# 让仪表盘「检测到系统异常故障」挂了一天多，直到容器重启才消失。
ERROR_MAX_AGE_SECONDS = 24 * 3600.0


def record_error(source: str, message: str, level: str = "error"):
    """记录一条运行时错误/警告。level: error / warning"""
    _errors.append({
        "source": source,
        "message": str(message)[:200],
        "level": level,
        "ts": time.time(),
    })


def _fresh(max_age_seconds: Optional[float], now: Optional[float]) -> list[dict]:
    if max_age_seconds is None:
        return list(_errors)
    cutoff = (time.time() if now is None else now) - max_age_seconds
    return [item for item in _errors if float(item.get("ts") or 0) >= cutoff]


def get_recent_errors(
    n: int = 20,
    *,
    max_age_seconds: Optional[float] = ERROR_MAX_AGE_SECONDS,
    now: Optional[float] = None,
) -> list[dict]:
    """获取最近 N 条未过期的错误，最新在前。"""
    items = _fresh(max_age_seconds, now)
    return list(reversed(items[-n:])) if n > 0 else []


def error_count(*, max_age_seconds: Optional[float] = ERROR_MAX_AGE_SECONDS, now: Optional[float] = None) -> int:
    return len(_fresh(max_age_seconds, now))


def clear_errors() -> None:
    """测试用：清空错误缓冲。"""
    _errors.clear()
