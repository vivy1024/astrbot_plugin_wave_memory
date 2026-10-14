import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class FakeTextPart:
    def __init__(self, text):
        self.text = text


class FakeReq:
    def __init__(self, parts=None):
        self.system_prompt = "原系统提示"
        self.extra_user_content_parts = list(parts or [])


class RecordingChannel:
    name = "memory"

    def __init__(self, text, seen_req_part_counts):
        self.text = text
        self.seen_req_part_counts = seen_req_part_counts

    async def build(self, ctx):
        from services.injection.channel_base import InjectionResult

        self.seen_req_part_counts.append(len(ctx.req.extra_user_content_parts))
        return InjectionResult.hit("memory", self.text, items=[{"id": 1, "preview": self.text}])


class InjectionShadowTest(unittest.TestCase):
    def _trace_store(self):
        from services.injection.trace_store import InjectionTraceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(Path(tmp.name) / "trace.db")
        self.addCleanup(conn.close)
        store = InjectionTraceStore(conn)
        store.ensure_schema()
        return store

    def test_shadow_runs_orchestrator_without_mutating_real_request_and_records_diff(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.context import InjectionContext
        from services.injection.shadow import run_injection_shadow

        trace_store = self._trace_store()
        seen_counts = []
        req = FakeReq(parts=[FakeTextPart("旧注入文本")])
        ctx = InjectionContext(
            event="event",
            req=req,
            message="帮我回忆苹果派",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="1000000005",
            bot_profile_id="yushu",
            mode="full",
            trace_id="trace-shadow-1",
            now=123.0,
        )

        result = asyncio.run(run_injection_shadow(
            ctx=ctx,
            channels=[RecordingChannel("新通道文本", seen_counts)],
            config=build_default_channel_config(runtime_mode="full"),
            trace_store=trace_store,
            old_text="旧注入文本",
            text_part_factory=FakeTextPart,
        ))
        detail = trace_store.get("trace-shadow-1")
        metadata = json.loads(detail["metadata_json"])

        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(req.extra_user_content_parts[0].text, "旧注入文本")
        self.assertEqual(seen_counts, [1])
        self.assertEqual(result.final_text, "新通道文本")
        self.assertEqual(detail["status"], "shadow_diff")
        self.assertEqual(detail["final_preview"], "新通道文本")
        self.assertFalse(metadata["shadow_comparison"]["exact_match"])
        self.assertEqual(metadata["shadow_comparison"]["old_chars"], len("旧注入文本"))
        self.assertEqual(metadata["shadow_comparison"]["new_chars"], len("新通道文本"))
        self.assertEqual(detail["channels"][0]["channel"], "memory")
        self.assertEqual(detail["channels"][0]["status"], "hit")

    def test_shadow_records_match_when_texts_are_equal(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.context import InjectionContext
        from services.injection.shadow import run_injection_shadow

        trace_store = self._trace_store()
        ctx = InjectionContext(
            event="event",
            req=FakeReq(),
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="bot",
            mode="full",
            trace_id="trace-shadow-2",
        )

        asyncio.run(run_injection_shadow(
            ctx=ctx,
            channels=[RecordingChannel("同一文本", [])],
            config=build_default_channel_config(runtime_mode="full"),
            trace_store=trace_store,
            old_text="同一文本",
            text_part_factory=FakeTextPart,
        ))
        detail = trace_store.get("trace-shadow-2")
        metadata = json.loads(detail["metadata_json"])

        self.assertEqual(detail["status"], "shadow_match")
        self.assertTrue(metadata["shadow_comparison"]["exact_match"])

    def test_shadow_runner_persists_exact_runtime_scope_for_trace_reads(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.config.channel_config import build_default_channel_config
        from services.injection.context import InjectionContext
        from services.injection.shadow import run_injection_shadow

        scope = RuntimeScope(
            bot_id="bot-alpha",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:u1",
        )
        trace_store = self._trace_store()
        ctx = InjectionContext(
            event="event",
            req=FakeReq(),
            message="scope metadata",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot-alpha",
            bot_profile_id="bot-alpha",
            scope=scope,
            mode="full",
            trace_id="trace-shadow-scope",
        )

        asyncio.run(run_injection_shadow(
            ctx=ctx,
            channels=[RecordingChannel("scope text", [])],
            config=build_default_channel_config(runtime_mode="full"),
            trace_store=trace_store,
            old_text="",
            text_part_factory=FakeTextPart,
        ))

        self.assertIsNotNone(trace_store.get_for_scope("trace-shadow-scope", scope))


if __name__ == "__main__":
    unittest.main()
