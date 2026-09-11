import asyncio
import unittest


class InjectionContractTest(unittest.TestCase):
    def test_injection_context_carries_request_identity_trace_and_optional_scope(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.injection.context import InjectionContext

        scope = RuntimeScope(
            bot_id="yushu",
            visibility="group",
            session=SessionRef(
                id="qq:group:10001",
                platform_id="qq",
                kind="group",
                conversation_id="10001",
            ),
            subject_principal_id="qq:user:20002",
        )
        ctx = InjectionContext(
            event="event",
            req="req",
            message="你好，记得我吗",
            group_id="10001",
            sender_id="20002",
            sender_name="测试用户",
            bot_id="30003",
            bot_profile_id="yushu",
            scope=scope,
            recent_context=["上一句"],
            mode="memory_only",
            config={"channels": {}},
            now=123.45,
            trace_id="trace-1",
        )

        self.assertEqual(ctx.message, "你好，记得我吗")
        self.assertEqual(ctx.group_id, "10001")
        self.assertEqual(ctx.sender_id, "20002")
        self.assertEqual(ctx.bot_profile_id, "yushu")
        self.assertEqual(ctx.scope, scope)
        self.assertEqual(ctx.mode, "memory_only")
        self.assertEqual(ctx.trace_id, "trace-1")

    def test_result_factories_cover_success_empty_disabled_skipped_timeout_error(self):
        from services.injection.channel_base import InjectionResult

        hit = InjectionResult.hit("memory", "一段记忆", items=[{"id": 1}], score=0.9, latency_ms=12.5)
        empty = InjectionResult.empty("facts", latency_ms=1.0)
        disabled = InjectionResult.disabled("belief", reason="mode memory_only")
        skipped = InjectionResult.skipped("affinity", reason="no sender")
        timeout = InjectionResult.timeout("jargon", timeout_ms=300)
        error = InjectionResult.error_result("fewshot", ValueError("bad sample"))

        self.assertEqual(hit.status, "hit")
        self.assertEqual(hit.chars, len("一段记忆"))
        self.assertGreaterEqual(hit.tokens, 1)
        self.assertEqual(hit.items[0]["id"], 1)
        self.assertEqual(empty.status, "empty")
        self.assertEqual(disabled.status, "disabled")
        self.assertIn("mode memory_only", disabled.warnings[0])
        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(timeout.status, "timeout")
        self.assertIn("300ms", timeout.error)
        self.assertEqual(error.status, "error")
        self.assertIn("bad sample", error.error)

    def test_invalid_result_status_is_rejected(self):
        from services.injection.channel_base import InjectionResult

        with self.assertRaises(ValueError):
            InjectionResult(channel="memory", status="unknown")

    def test_channel_protocol_requires_name_and_async_build(self):
        from services.injection.channel_base import InjectionChannel, InjectionResult

        class DummyChannel:
            name = "dummy"

            async def build(self, ctx):
                return InjectionResult.empty(self.name)

        channel = DummyChannel()

        self.assertIsInstance(channel, InjectionChannel)
        result = asyncio.run(channel.build(object()))
        self.assertEqual(result.channel, "dummy")
        self.assertEqual(result.status, "empty")


if __name__ == "__main__":
    unittest.main()
