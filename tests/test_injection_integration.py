import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _group_scope():
    from domain.scope import RuntimeScope, SessionRef

    return RuntimeScope(
        bot_id="yushu",
        visibility="group",
        session=SessionRef(
            id="qq:group:group-1",
            platform_id="qq",
            kind="group",
            conversation_id="group-1",
        ),
        subject_principal_id="qq:user:user-1",
    )


class FakeTextPart:
    def __init__(self, text):
        self.text = text


class SyntheticProviderRequest:
    def __init__(self):
        self.extra_user_content_parts = []


class HitChannel:
    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.calls = 0

    async def build(self, ctx):
        self.calls += 1
        from services.injection.channel_base import InjectionResult

        return InjectionResult.hit(self.name, self.text, items=[{"id": self.name, "preview": self.text}])


class FakeQueryEngine:
    def __init__(self):
        self.calls = []

    async def query(self, *, text, group_id=None, top_k=5, scope=None):
        self.calls.append({"text": text, "group_id": group_id, "top_k": top_k, "scope": scope})
        return [{"id": 7, "content": "兼容记忆", "score": 0.8, "importance": 1.2, "source": "core"}]


class FakeWriter:
    def __init__(self):
        self.items = []

    async def enqueue(self, item):
        self.items.append(dict(item))


class FakeMetricsDb:
    def __init__(self, store):
        self.store = store

    def get_injection_metrics(self, from_ts, to_ts, bucket_seconds):
        return self.store.query(from_ts, to_ts, bucket_seconds)


class InjectionIntegrationTest(unittest.TestCase):
    def _trace_store(self):
        from services.injection.trace_store import InjectionTraceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(Path(tmp.name) / "integration.db")
        self.addCleanup(conn.close)
        store = InjectionTraceStore(conn)
        store.ensure_schema()
        return store

    def _ctx(self, *, req, mode, trace_id):
        from services.injection.context import InjectionContext

        return InjectionContext(
            event="synthetic-event",
            req=req,
            message="合成 AstrBot LLM 请求",
            group_id="group-1",
            sender_id="user-1",
            sender_name="测试用户",
            bot_id="bot-qq",
            bot_profile_id="yushu",
            mode=mode,
            trace_id=trace_id,
        )

    def test_synthetic_llm_request_in_full_mode_writes_final_textpart_and_trace(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.active import run_injection_active

        req = SyntheticProviderRequest()
        trace_store = self._trace_store()
        memory = HitChannel("memory", "基础记忆")
        belief = HitChannel("belief", "高级信念")

        result = asyncio.run(run_injection_active(
            ctx=self._ctx(req=req, mode="full", trace_id="integration-full"),
            channels=[belief, memory],
            config=build_default_channel_config(runtime_mode="full"),
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        ))
        detail = trace_store.get("integration-full")

        self.assertTrue(result.injected)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(req.extra_user_content_parts[0].text, "基础记忆\n\n高级信念")
        self.assertEqual(detail["status"], "ok")
        self.assertEqual([row["channel"] for row in detail["channels"]], ["memory", "belief"])
        self.assertEqual(memory.calls, 1)
        self.assertEqual(belief.calls, 1)

    def test_memory_only_mode_does_not_run_advanced_channels(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.active import run_injection_active

        req = SyntheticProviderRequest()
        trace_store = self._trace_store()
        memory = HitChannel("memory", "基础记忆")
        persona = HitChannel("persona", "不该运行的人格")
        jargon = HitChannel("jargon", "不该运行的黑话")

        result = asyncio.run(run_injection_active(
            ctx=self._ctx(req=req, mode="memory_only", trace_id="integration-memory-only"),
            channels=[persona, jargon, memory],
            config=build_default_channel_config(runtime_mode="memory_only"),
            trace_store=trace_store,
            text_part_factory=FakeTextPart,
        ))
        detail = trace_store.get("integration-memory-only")

        self.assertTrue(result.injected)
        self.assertEqual(req.extra_user_content_parts[0].text, "基础记忆")
        self.assertEqual([row["channel"] for row in detail["channels"]], ["memory"])
        self.assertEqual(memory.calls, 1)
        self.assertEqual(persona.calls, 0)
        self.assertEqual(jargon.calls, 0)

    def test_compat_only_disables_native_injection_but_facade_search_and_write_work(self):
        from services.compat.livingmemory_facade import WaveMemoryLivingMemoryFacade
        from services.runtime_mode import effective_native_injection_enabled, resolve_runtime_mode

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "compat_only"}})
        query_engine = FakeQueryEngine()
        writer = FakeWriter()
        facade = WaveMemoryLivingMemoryFacade(query_engine=query_engine, writer=writer, now=lambda: 123.0)

        self.assertFalse(effective_native_injection_enabled({"enable_auto_inject": True}, mode, compat_cfg={}))
        scope = _group_scope()
        results = asyncio.run(facade.search_memories(
            "兼容", k=2, session_id="group-1", persona_id="yushu", scope=scope
        ))
        queued_id = asyncio.run(facade.add_memory(
            "兼容写入", session_id="group-1", persona_id="yushu", scope=scope
        ))

        self.assertEqual(len(query_engine.calls), 1)
        self.assertEqual(query_engine.calls[0]["text"], "兼容")
        self.assertEqual(query_engine.calls[0]["group_id"], "group-1")
        self.assertEqual(query_engine.calls[0]["top_k"], 2)
        self.assertIs(query_engine.calls[0]["scope"], scope)
        self.assertEqual(results[0]["id"], "7")
        self.assertEqual(results[0]["metadata"]["session_id"], "qq:group:group-1")
        self.assertTrue(queued_id.startswith("queued:"))
        self.assertEqual(writer.items[0]["source"], "compat_livingmemory")
        self.assertEqual(writer.items[0]["content"], "兼容写入")

    def test_legacy_injection_metrics_api_payload_still_exposes_series_summary_and_ranking(self):
        from engine.metrics_store import InjectionMetricStore
        from webui.blueprints.system import build_injection_metrics_payload

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(Path(tmp.name) / "metrics.db")
        self.addCleanup(conn.close)
        store = InjectionMetricStore(conn)
        store.ensure_schema()
        base = 1_700_000_000
        store.record({"total_tokens": 100, "memory_tokens": 60, "total_ms": 120}, ts=base)
        store.record({"total_tokens": 50, "facts_tokens": 20, "total_ms": 80}, ts=base + 60)

        payload = build_injection_metrics_payload(
            FakeMetricsDb(store),
            {"count": 2, "legacy": "kept"},
            range_key="custom",
            from_ts=base,
            to_ts=base + 3600,
            bucket_seconds=3600,
        )

        self.assertEqual(payload["range"], "custom")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["summary"]["total_tokens"]["sum"], 150)
        self.assertEqual(payload["window"]["sample_count"], 2)
        self.assertEqual(payload["window"]["total_tokens_sum"], 150)
        self.assertEqual(payload["window"]["avg_tokens_per_sample"], 75)
        self.assertEqual(payload["window"]["avg_tokens_per_day"], 3600)
        self.assertTrue(payload["series"])
        self.assertTrue(payload["ranking"])
        self.assertEqual(payload["legacy"], "kept")


if __name__ == "__main__":
    unittest.main()
