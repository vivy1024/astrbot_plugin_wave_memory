"""软超时排查的测量项：线程池排队耗时、事件循环晚醒、写进 trace metadata。只测量，不改注入结果。"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from services.config.channel_config import build_default_channel_config
from services.injection.channel_base import InjectionResult
from services.injection.context import InjectionContext
from services.injection.loop_lag import LoopLagProbe
from services.injection import loop_lag
from services.injection.orchestrator import InjectionOrchestrator, _latency_probe_metadata
from services.injection.trace_store import InjectionTraceStore


class _TextPart:
    def __init__(self, text):
        self.text = text


class _Req:
    def __init__(self):
        self.extra_user_content_parts = []


class _Threaded:
    name = "facts"
    offload_to_thread = True

    async def build(self, ctx):
        return InjectionResult.hit("facts", "事实")


class _Inline:
    name = "persona"

    async def build(self, ctx):
        return InjectionResult.hit("persona", "人格")


def _ctx(trace_id: str) -> InjectionContext:
    return InjectionContext(
        event="event", req=_Req(), message="hello", group_id="g1", sender_id="u1", sender_name="用户",
        bot_id="bot", bot_profile_id="yushu", mode="full", trace_id=trace_id,
    )


def test_threaded_channel_records_queue_ms_inline_channel_does_not():
    orchestrator = InjectionOrchestrator(
        channels=[_Threaded(), _Inline()],
        config=build_default_channel_config(runtime_mode="full"),
        text_part_factory=_TextPart,
    )
    result = asyncio.run(orchestrator.run(_ctx("trace-queue")))
    by_name = {item.channel: item for item in result.channel_results}
    assert by_name["facts"].queue_ms >= 0.0
    assert by_name["persona"].queue_ms == 0.0
    assert "事实" in result.final_text and "人格" in result.final_text


def test_probe_measures_blocked_loop():
    async def scenario():
        probe = LoopLagProbe(interval_s=0.02)
        probe.start()
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        time.sleep(0.15)  # 故意同步阻塞事件循环
        await asyncio.sleep(0.05)
        stats = probe.window(started)
        probe.stop()
        return stats

    stats = asyncio.run(scenario())
    assert stats["loop_lag_max_ms"] >= 100
    assert stats["loop_lag_samples"] >= 1


def test_probe_disabled_or_empty_returns_nothing():
    probe = LoopLagProbe()
    assert probe.window(0.0) == {}
    probe.enabled = False
    probe._samples.append((time.perf_counter(), 50.0))
    assert probe.window(0.0) == {}


def test_probe_start_is_idempotent_and_stop_cancels():
    async def scenario():
        probe = LoopLagProbe(interval_s=0.01)
        probe.start()
        first = probe._task
        probe.start()
        assert probe._task is first and probe.running
        probe.stop()
        await asyncio.sleep(0.02)
        return probe.running

    assert asyncio.run(scenario()) is False


def test_latency_probe_metadata_shape(monkeypatch):
    fake = LoopLagProbe()
    fake._samples.append((time.perf_counter(), 42.0))
    monkeypatch.setattr(loop_lag, "_probe", fake)
    results = [InjectionResult.hit("facts", "x"), InjectionResult.hit("persona", "y")]
    results[0].queue_ms = 12.34
    meta = _latency_probe_metadata(results, time.perf_counter() - 1)["latency_probe"]
    assert meta["queue_ms_max"] == 12.3
    assert meta["queue_ms_by_channel"] == {"facts": 12.3}
    assert meta["loop_lag_max_ms"] == 42.0


def test_trace_metadata_contains_latency_probe(tmp_path, monkeypatch):
    import json

    # 固定一个探针样本，保证这条 trace 一定有 latency_probe（不依赖本机线程池是否真的排队）
    fake = LoopLagProbe()
    monkeypatch.setattr(loop_lag, "_probe", fake)

    class _Slow(_Threaded):
        async def build(self, ctx):
            fake._samples.append((time.perf_counter(), 7.0))
            return InjectionResult.hit("facts", "事实")

    conn = sqlite3.connect(tmp_path / "trace.db")
    store = InjectionTraceStore(conn)
    store.ensure_schema()
    orchestrator = InjectionOrchestrator(
        channels=[_Slow()],
        config=build_default_channel_config(runtime_mode="full"),
        trace_store=store,
        text_part_factory=_TextPart,
    )
    asyncio.run(orchestrator.run(_ctx("trace-probe")))
    row = conn.execute("SELECT metadata_json FROM injection_traces WHERE trace_id='trace-probe'").fetchone()
    conn.close()

    probe = json.loads(row[0])["latency_probe"]
    assert probe["loop_lag_max_ms"] == 7.0
    assert probe["loop_lag_samples"] == 1
