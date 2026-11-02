"""运行时错误缓冲：24 小时过期、最新在前、warning 不算故障。"""

from __future__ import annotations

import time

import pytest

from utils import health_registry as hr


@pytest.fixture(autouse=True)
def _clean():
    hr.clear_errors()
    yield
    hr.clear_errors()


def _push(source: str, ts: float, level: str = "error") -> None:
    hr.record_error(source, f"{source} message", level=level)
    hr._errors[-1]["ts"] = ts


def test_recent_errors_newest_first():
    now = time.time()
    _push("a", now - 30)
    _push("b", now - 20)
    _push("c", now - 10)
    assert [item["source"] for item in hr.get_recent_errors(2, now=now)] == ["c", "b"]
    assert [item["source"] for item in hr.get_recent_errors(10, now=now)] == ["c", "b", "a"]


def test_errors_older_than_24h_are_hidden():
    now = time.time()
    _push("stale", now - hr.ERROR_MAX_AGE_SECONDS - 1)
    _push("fresh", now - 60)
    assert [item["source"] for item in hr.get_recent_errors(10, now=now)] == ["fresh"]
    assert hr.error_count(now=now) == 1
    # 关掉过期过滤时仍能拿到全部（排查用）
    assert hr.error_count(max_age_seconds=None) == 2


def test_record_error_keeps_level():
    hr.record_error("DbAccessGuard", "astrbot_execute_python:wave_memory.db", level="warning")
    item = hr.get_recent_errors(1)[0]
    assert item["level"] == "warning" and item["source"] == "DbAccessGuard"
    assert not any(e["level"] == "error" for e in hr.get_recent_errors(10)), "warning 不应让 has_errors 为真"


def test_zero_limit_returns_empty():
    _push("x", time.time())
    assert hr.get_recent_errors(0) == []
