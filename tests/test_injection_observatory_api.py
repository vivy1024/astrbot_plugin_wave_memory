import sqlite3
import tempfile
import unittest
from pathlib import Path


class InjectionObservatoryApiTest(unittest.TestCase):
    def _stores(self):
        from services.injection.feedback_store import MemoryFeedbackStore
        from services.injection.trace_store import InjectionTraceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(Path(tmp.name) / "observatory.db")
        self.addCleanup(conn.close)
        trace_store = InjectionTraceStore(conn)
        trace_store.ensure_schema()
        feedback_store = MemoryFeedbackStore(conn)
        feedback_store.ensure_schema()
        return trace_store, feedback_store

    def test_trace_list_empty_returns_empty_payload(self):
        from webui.blueprints.injection_observatory import build_trace_list_payload

        trace_store, _ = self._stores()
        payload = build_trace_list_payload(trace_store, {"from_ts": 0, "to_ts": 999, "limit": 20})

        self.assertEqual(payload["traces"], [])
        self.assertEqual(payload["count"], 0)

    def test_trace_list_supports_channel_and_status_filters(self):
        from services.injection.channel_base import InjectionResult
        from webui.blueprints.injection_observatory import build_trace_list_payload

        trace_store, _ = self._stores()
        trace_store.record(
            {"trace_id": "trace-api-1", "timestamp": 100, "mode": "full", "group_id": "g1", "sender_id": "u1", "bot_id": "bot", "message": "苹果派", "final_text": "记忆", "status": "ok"},
            [InjectionResult.hit("memory", "记忆", items=[{"id": 7, "preview": "记忆"}])],
        )
        trace_store.record(
            {"trace_id": "trace-api-2", "timestamp": 110, "mode": "full", "group_id": "g2", "sender_id": "u2", "bot_id": "bot", "message": "黑话", "final_text": "黑话", "status": "ok"},
            [InjectionResult.hit("jargon", "黑话")],
        )

        payload = build_trace_list_payload(trace_store, {"from_ts": 0, "to_ts": 999, "channel": "memory", "status": "ok", "limit": 20})

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["traces"][0]["trace_id"], "trace-api-1")
        self.assertEqual(payload["traces"][0]["group_id"], "g1")

    def test_trace_list_supports_group_private_scope_filter(self):
        from services.injection.channel_base import InjectionResult
        from webui.blueprints.injection_observatory import build_trace_list_payload

        trace_store, _ = self._stores()
        trace_store.record(
            {"trace_id": "trace-group", "timestamp": 100, "mode": "full", "group_id": "g1", "sender_id": "u1", "bot_id": "bot", "message": "群聊", "status": "ok"},
            [InjectionResult.hit("memory", "群聊")],
        )
        trace_store.record(
            {"trace_id": "trace-private", "timestamp": 101, "mode": "full", "group_id": "", "sender_id": "u2", "bot_id": "bot", "message": "私聊", "status": "ok"},
            [InjectionResult.hit("memory", "私聊")],
        )

        group_payload = build_trace_list_payload(trace_store, {"from_ts": 0, "to_ts": 999, "scope": "group"})
        private_payload = build_trace_list_payload(trace_store, {"from_ts": 0, "to_ts": 999, "scope": "private"})

        self.assertEqual([t["trace_id"] for t in group_payload["traces"]], ["trace-group"])
        self.assertEqual([t["trace_id"] for t in private_payload["traces"]], ["trace-private"])

    def test_trace_list_limits_large_result_sets(self):
        from services.injection.channel_base import InjectionResult
        from services.injection.trace_store import InjectionTraceStore
        from webui.blueprints.injection_observatory import build_trace_list_payload

        mem_conn = sqlite3.connect(":memory:")
        self.addCleanup(mem_conn.close)
        trace_store = InjectionTraceStore(mem_conn)
        trace_store.ensure_schema()

        for i in range(550):
            trace_store.record(
                {"trace_id": f"trace-many-{i}", "timestamp": i, "mode": "full", "message": str(i), "status": "ok"},
                [InjectionResult.empty("memory")],
            )

        default_payload = build_trace_list_payload(trace_store, {"from_ts": 0, "to_ts": 999})
        capped_payload = build_trace_list_payload(trace_store, {"from_ts": 0, "to_ts": 999, "limit": 9999})

        self.assertEqual(default_payload["limit"], 100)
        self.assertEqual(default_payload["count"], 100)
        self.assertEqual(capped_payload["limit"], 500)
        self.assertEqual(capped_payload["count"], 500)

    def test_trace_detail_contains_channels_hits_filtered_budget_and_feedback(self):
        from services.injection.channel_base import InjectionResult
        from webui.blueprints.injection_observatory import build_trace_detail_payload

        trace_store, feedback_store = self._stores()
        trace_store.record(
            {
                "trace_id": "trace-api-detail",
                "timestamp": 123,
                "mode": "full",
                "group_id": "g1",
                "sender_id": "u1",
                "sender_name": "用户",
                "bot_id": "bot",
                "bot_profile_id": "yushu",
                "message": "苹果派是什么",
                "final_text": "苹果派记忆",
                "total_tokens": 8,
                "total_chars": 5,
                "total_latency_ms": 10.5,
                "status": "ok",
            },
            [
                InjectionResult.hit(
                    "memory",
                    "苹果派记忆",
                    items=[{"id": 7, "source": "core", "score": 0.9, "preview": "苹果派记忆"}],
                    filtered=[{"id": 8, "filter_reason": "recent_context_duplicate", "preview": "重复"}],
                    latency_ms=5.0,
                )
            ],
        )
        feedback_store.record(trace_id="trace-api-detail", memory_id=7, feedback="useful", reason="helpful")

        payload = build_trace_detail_payload(trace_store, feedback_store, "trace-api-detail")

        self.assertEqual(payload["trace_id"], "trace-api-detail")
        self.assertEqual(payload["request"]["message_preview"], "苹果派是什么")
        self.assertEqual(payload["budget"]["total_tokens"], 8)
        self.assertEqual(payload["channels"][0]["hit_items"][0]["id"], 7)
        self.assertEqual(payload["channels"][0]["filtered_items"][0]["reason"], "recent_context_duplicate")
        self.assertEqual(payload["feedback"][0]["feedback"], "useful")

    def test_trace_detail_preserves_jargon_source_metadata(self):
        from services.injection.channel_base import InjectionResult
        from webui.blueprints.injection_observatory import build_trace_detail_payload

        trace_store, feedback_store = self._stores()
        trace_store.record(
            {"trace_id": "trace-jargon-meta", "timestamp": 123, "mode": "full", "message": "v我50", "status": "ok"},
            [
                InjectionResult.hit(
                    "jargon",
                    "[黑话理解参考]\n- \"v我50\" → 疯狂星期四转账梗",
                    items=[{
                        "word": "v我50",
                        "meaning": "疯狂星期四转账梗",
                        "source": "holyman_skills",
                        "source_layer": "phrases",
                        "reference_only": True,
                        "runtime_match": True,
                        "matched_by": "explicit_user_message",
                        "preview": "v我50 → 疯狂星期四转账梗",
                    }],
                )
            ],
        )

        payload = build_trace_detail_payload(trace_store, feedback_store, "trace-jargon-meta")

        item = payload["channels"][0]["hit_items"][0]
        self.assertEqual(item["word"], "v我50")
        self.assertEqual(item["meaning"], "疯狂星期四转账梗")
        self.assertEqual(item["source"], "holyman_skills")
        self.assertEqual(item["source_layer"], "phrases")
        self.assertIs(item["reference_only"], True)
        self.assertIs(item["runtime_match"], True)
        self.assertEqual(item["matched_by"], "explicit_user_message")

    def test_trace_detail_missing_returns_none(self):
        from webui.blueprints.injection_observatory import build_trace_detail_payload

        trace_store, feedback_store = self._stores()
        self.assertIsNone(build_trace_detail_payload(trace_store, feedback_store, "missing"))

    def test_observatory_routes_require_auth(self):
        source = Path("webui/blueprints/injection_observatory.py").read_text(encoding="utf-8")

        self.assertIn("require_auth", source)
        self.assertIn('@injection_observatory_bp.route("/traces", methods=["GET"])\n@require_auth', source)
        self.assertIn('@injection_observatory_bp.route("/traces/<trace_id>", methods=["GET"])\n@require_auth', source)

    def test_frontend_exposes_injection_observatory_page(self):
        page = Path("webui/frontend/src/pages/injection/InjectionPage.tsx").read_text(encoding="utf-8")
        sheet = Path("webui/frontend/src/pages/injection/TraceDetailSheet.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/injection.ts").read_text(encoding="utf-8")
        routes = Path("webui/frontend/src/app/routes.tsx").read_text(encoding="utf-8")

        self.assertIn("注入观测台", page)
        self.assertIn("listInjectionTraces", page)
        self.assertIn("usePaginationSearchParams", page)
        self.assertIn("ScopeSelect", page)
        self.assertIn("PaginationControls", page)
        self.assertIn("config_revision", page)
        self.assertIn("TracePayloadViewer", sheet)
        self.assertIn("ObjectDeepLink", sheet)
        self.assertNotIn("50000", sheet)
        self.assertNotIn("?id=", sheet)
        self.assertIn("/api/observatory/traces", api)
        self.assertNotIn("/api/injection/traces", api)
        self.assertIn("/injection", routes)  # 普通路径重命名由 Task 29 的 replace 路由迁移。


if __name__ == "__main__":
    unittest.main()
