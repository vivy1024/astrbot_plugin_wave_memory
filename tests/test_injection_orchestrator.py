import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeTextPart:
    def __init__(self, text):
        self.text = text


class FakeReq:
    def __init__(self):
        self.extra_user_content_parts = []


class DummyChannel:
    def __init__(self, name, result, seen_req_parts=None):
        self.name = name
        self._result = result
        self.seen_req_parts = seen_req_parts

    async def build(self, ctx):
        if self.seen_req_parts is not None:
            self.seen_req_parts.append(len(ctx.req.extra_user_content_parts))
        return self._result


class SlowChannel:
    name = "memory"

    async def build(self, ctx):
        await asyncio.sleep(0.05)
        from services.injection.channel_base import InjectionResult
        return InjectionResult.hit("slow", "too late")


class DelayedHitChannel:
    def __init__(self, name, text, delay):
        self.name = name
        self.text = text
        self.delay = delay

    async def build(self, ctx):
        await asyncio.sleep(self.delay)
        from services.injection.channel_base import InjectionResult
        return InjectionResult.hit(self.name, self.text)


class ErroringChannel:
    name = "facts"

    async def build(self, ctx):
        raise RuntimeError("facts failed")


class InjectionOrchestratorTest(unittest.TestCase):
    def _trace_store(self):
        from services.injection.trace_store import InjectionTraceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(Path(tmp.name) / "trace.db")
        self.addCleanup(conn.close)
        store = InjectionTraceStore(conn)
        store.ensure_schema()
        return store

    def test_orchestrator_composes_textpart_and_trace_without_channel_mutation(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.channel_base import InjectionResult
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        seen_req_parts = []
        trace_store = self._trace_store()
        config = build_default_channel_config(runtime_mode="full")
        orchestrator = InjectionOrchestrator(
            channels=[
                DummyChannel("facts", InjectionResult.hit("facts", "事实文本"), seen_req_parts),
                DummyChannel("memory", InjectionResult.hit("memory", "记忆文本"), seen_req_parts),
            ],
            config=config,
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        )
        ctx = InjectionContext(
            event="event",
            req=req,
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            mode="full",
            trace_id="trace-orch-1",
        )

        result = asyncio.run(orchestrator.run(ctx))
        detail = trace_store.get("trace-orch-1")

        self.assertTrue(result.injected)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(req.extra_user_content_parts[0].text, "记忆文本\n\n事实文本")
        self.assertEqual(seen_req_parts, [0, 0])
        self.assertEqual([c["channel"] for c in detail["channels"]], ["memory", "facts"])
        self.assertEqual(detail["final_preview"], "记忆文本\n\n事实文本")

    def test_disabled_empty_and_timeout_channels_do_not_enter_final_text(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config
        from services.injection.channel_base import InjectionResult
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        trace_store = self._trace_store()
        base = build_default_channel_config(runtime_mode="full")
        config = apply_channel_overrides(base, {"channels": {"facts": {"enabled": False}, "memory": {"timeout_ms": 10}}})
        orchestrator = InjectionOrchestrator(
            channels=[
                DummyChannel("facts", InjectionResult.hit("facts", "不该注入")),
                DummyChannel("fts5", InjectionResult.empty("fts5")),
                SlowChannel(),
            ],
            config=config,
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        )
        ctx = InjectionContext(
            event="event",
            req=req,
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            mode="full",
            trace_id="trace-orch-2",
        )

        result = asyncio.run(orchestrator.run(ctx))
        detail = trace_store.get("trace-orch-2")

        self.assertTrue(result.injected)
        self.assertEqual(req.extra_user_content_parts[0].text, "too late")
        statuses = {c["channel"]: c["status"] for c in detail["channels"]}
        self.assertNotIn("facts", statuses)
        self.assertEqual(statuses["fts5"], "empty")
        self.assertEqual(statuses["slow"], "hit")
        timed = next(item for item in result.channel_results if item.channel == "slow")
        self.assertIn("channel exceeded 10ms", timed.warnings)

    def test_memory_only_trace_omits_disabled_advanced_channels(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.channel_base import InjectionResult
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        trace_store = self._trace_store()
        config = build_default_channel_config(runtime_mode="memory_only")
        orchestrator = InjectionOrchestrator(
            channels=[
                DummyChannel("memory", InjectionResult.hit("memory", "记忆文本")),
                DummyChannel("persona", InjectionResult.hit("persona", "不该出现的人格文本")),
                DummyChannel("belief", InjectionResult.hit("belief", "不该出现的信念文本")),
                DummyChannel("jargon", InjectionResult.hit("jargon", "不该出现的黑话文本")),
            ],
            config=config,
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        )
        ctx = InjectionContext(
            event="event",
            req=req,
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            mode="memory_only",
            trace_id="trace-memory-only-advanced-disabled",
        )

        result = asyncio.run(orchestrator.run(ctx))
        detail = trace_store.get("trace-memory-only-advanced-disabled")

        self.assertTrue(result.injected)
        self.assertEqual(req.extra_user_content_parts[0].text, "记忆文本")
        self.assertEqual([c["channel"] for c in detail["channels"]], ["memory"])
        self.assertNotIn("人格", detail["final_preview"])
        self.assertNotIn("信念", detail["final_preview"])
        self.assertNotIn("黑话", detail["final_preview"])

    def test_global_budget_skips_oversized_channel_and_records_errors(self):
        from services.config.channel_config import KNOWN_CHANNELS, apply_channel_overrides, build_default_channel_config
        from services.injection.channel_base import InjectionResult
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        trace_store = self._trace_store()
        disabled = {name: {"enabled": False} for name in KNOWN_CHANNELS if name not in {"safety", "memory", "fts5", "facts"}}
        config = apply_channel_overrides(
            build_default_channel_config(runtime_mode="full"),
            {
                "channels": {
                    **disabled,
                    "facts": {"priority": 300, "token_budget": 0},
                    "fts5": {"priority": 200, "token_budget": 1},
                    "memory": {"priority": 100, "token_budget": 1},
                }
            },
        )
        orchestrator = InjectionOrchestrator(
            channels=[
                DummyChannel("memory", InjectionResult.hit("memory", "这是一段会超过全局剩余预算的长记忆文本")),
                DummyChannel("fts5", InjectionResult.hit("fts5", "短")),
                ErroringChannel(),
            ],
            config=config,
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        )
        ctx = InjectionContext(
            event="event",
            req=req,
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            mode="full",
            trace_id="trace-orch-budget-error",
        )

        result = asyncio.run(orchestrator.run(ctx))
        detail = trace_store.get("trace-orch-budget-error")
        statuses = {row["channel"]: row["status"] for row in detail["channels"]}

        self.assertTrue(result.injected)
        self.assertEqual(result.final_text, "短")
        self.assertEqual(req.extra_user_content_parts[0].text, "短")
        self.assertEqual(detail["status"], "degraded")
        self.assertIn("facts", detail["error"])
        self.assertEqual(statuses["facts"], "error")
        self.assertEqual(statuses["fts5"], "hit")
        self.assertEqual(statuses["memory"], "skipped")

    def test_channel_timeout_keeps_other_channels_and_records_channel_latency(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        trace_store = self._trace_store()
        config = apply_channel_overrides(
            build_default_channel_config(runtime_mode="full"),
            {"channels": {"memory": {"timeout_ms": 10}, "facts": {"priority": 200}}},
        )
        orchestrator = InjectionOrchestrator(
            channels=[
                DelayedHitChannel("memory", "超时记忆", 0.05),
                DelayedHitChannel("facts", "可用事实", 0.01),
            ],
            config=config,
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        )
        ctx = InjectionContext(
            event="event",
            req=req,
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            mode="full",
            trace_id="trace-orch-timeout-with-latency",
        )

        result = asyncio.run(orchestrator.run(ctx))
        detail = trace_store.get("trace-orch-timeout-with-latency")
        statuses = {row["channel"]: row["status"] for row in detail["channels"]}
        latency_by_channel = {row["channel"]: row["latency_ms"] for row in detail["channels"]}

        self.assertTrue(result.injected)
        self.assertIn("超时记忆", result.final_text)
        self.assertIn("可用事实", result.final_text)
        self.assertEqual(statuses["memory"], "hit")
        self.assertEqual(statuses["facts"], "hit")
        timed = next(item for item in result.channel_results if item.channel == "memory")
        self.assertIn("channel exceeded 10ms", timed.warnings)
        self.assertGreaterEqual(latency_by_channel["memory"], 1)
        self.assertGreaterEqual(latency_by_channel["facts"], 1)

    def test_slow_total_injection_warning_contains_channel_breakdown(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        config = apply_channel_overrides(
            build_default_channel_config(runtime_mode="full"),
            {"channels": {"memory": {"timeout_ms": 1000}}},
        )
        orchestrator = InjectionOrchestrator(
            channels=[DelayedHitChannel("memory", "慢记忆", 0.55)],
            config=config,
            trace_store=None,
            text_part_factory=FakeTextPart,
        )
        ctx = InjectionContext(
            event="event",
            req=req,
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            mode="full",
            trace_id="trace-orch-slow-warning",
        )

        import services.injection.orchestrator as orchestrator_module

        self.assertEqual(orchestrator_module.SLOW_INJECTION_WARNING_MS, 2000)
        with patch.object(orchestrator_module, "SLOW_INJECTION_WARNING_MS", 500):
            with self.assertLogs("services.injection.orchestrator", level="WARNING") as logs:
                result = asyncio.run(orchestrator.run(ctx))

        self.assertTrue(result.injected)
        message = "\n".join(logs.output)
        self.assertIn("inject_memory 耗时过长", message)
        self.assertIn("memory", message)
        self.assertIn("hit", message)
        self.assertIn("ms", message)

    def test_preserved_affinity_timeline_survives_exhausted_budget(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config
        from services.injection.channel_base import InjectionResult
        from services.injection.context import InjectionContext
        from services.injection.orchestrator import InjectionOrchestrator

        req = FakeReq()
        config = apply_channel_overrides(
            build_default_channel_config(runtime_mode="full"),
            {
                "channels": {
                    "affinity": {"token_budget": 1, "priority": 1},
                    "memory": {"token_budget": 1, "priority": 100},
                    "facts": {"token_budget": 1, "priority": 50},
                }
            },
        )
        long_timeline = "印象时间线\n" + "\n".join(f"- 事件#{i:02d} 旧摘要{i:02d}" for i in range(40))
        orchestrator = InjectionOrchestrator(
            channels=[
                DummyChannel("memory", InjectionResult.hit("memory", "记忆占满预算")),
                DummyChannel(
                    "affinity",
                    InjectionResult.hit("affinity", long_timeline, preserve_full_text=True),
                ),
                DummyChannel("facts", InjectionResult.hit("facts", "事实也应保留")),
            ],
            config=config,
            text_part_factory=FakeTextPart,
        )
        result = asyncio.run(orchestrator.run(InjectionContext(
            event="event", req=req, message="hello", group_id="g1", sender_id="u1",
            sender_name="用户", bot_id="bot", bot_profile_id="yushu", mode="full",
        )))
        self.assertTrue(result.injected)
        self.assertIn("事件#00 旧摘要00", result.final_text)
        self.assertIn("事件#39 旧摘要39", result.final_text)
        self.assertIn("记忆占满预算", result.final_text)
        self.assertIn("事实也应保留", result.final_text)
        self.assertIn("full_timeline_exceeds_injection_budget", result.channel_results[2].warnings)
        self.assertEqual(req.extra_user_content_parts[0].text, result.final_text)


if __name__ == "__main__":
    unittest.main()
