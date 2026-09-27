import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class InjectionTraceStoreTest(unittest.TestCase):
    def _store(self, max_preview_chars=40, **kwargs):
        from services.injection.trace_store import InjectionTraceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "trace.db"
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        store = InjectionTraceStore(conn, max_preview_chars=max_preview_chars, **kwargs)
        store.ensure_schema()
        return store, conn

    def test_record_and_read_trace_with_channel_details(self):
        from services.injection.channel_base import InjectionResult

        store, _ = self._store(max_preview_chars=20)
        trace_id = store.record(
            {
                "trace_id": "trace-1",
                "timestamp": 1000.0,
                "mode": "full",
                "group_id": "g1",
                "sender_id": "u1",
                "sender_name": "用户",
                "bot_id": "botqq",
                "bot_profile_id": "yushu",
                "message": "这是一条很长的请求消息，需要被截断保存",
                "final_text": "最终注入文本也需要截断保存",
                "total_latency_ms": 123.4,
                "status": "ok",
                "metadata": {"provider_api_key": "sk-secret", "safe": "ok"},
            },
            [
                InjectionResult.hit("memory", "命中的记忆文本", items=[{"id": 7}], score=0.8, latency_ms=12),
                InjectionResult.empty("facts", reason="no facts"),
            ],
        )

        detail = store.get(trace_id)

        self.assertEqual(detail["trace_id"], "trace-1")
        self.assertEqual(detail["mode"], "full")
        self.assertEqual(detail["message_hash"].__len__(), 64)
        self.assertLessEqual(len(detail["message_preview"]), 20)
        self.assertLessEqual(len(detail["final_preview"]), 20)
        self.assertNotIn("sk-secret", detail["metadata_json"])
        self.assertEqual([c["channel"] for c in detail["channels"]], ["memory", "facts"])
        self.assertEqual(detail["channels"][0]["item_count"], 1)
        self.assertIn("命中的记忆", detail["channels"][0]["preview"])

    def test_query_filters_by_time_status_channel_and_error(self):
        from services.injection.channel_base import InjectionResult

        store, _ = self._store()
        store.record({"trace_id": "ok-1", "timestamp": 10, "mode": "full", "message": "a", "status": "ok"}, [InjectionResult.hit("memory", "m")])
        store.record({"trace_id": "err-1", "timestamp": 20, "mode": "full", "message": "b", "status": "error", "error": "boom"}, [InjectionResult.error_result("jargon", "boom")])
        store.record({"trace_id": "ok-2", "timestamp": 30, "mode": "memory_only", "message": "c", "status": "ok"}, [InjectionResult.empty("facts")])

        rows = store.query(from_ts=0, to_ts=25, status="error", channel="jargon", has_error=True)

        self.assertEqual([row["trace_id"] for row in rows], ["err-1"])
        self.assertEqual(rows[0]["status"], "error")

    def test_query_timeout_and_channel_filters_use_error_and_payload_fallback(self):
        from services.injection.channel_base import InjectionResult

        store, conn = self._store()
        store.record(
            {
                "trace_id": "missing-row",
                "timestamp": 40,
                "mode": "full",
                "message": "x",
                "status": "degraded",
                "error": "book_lore:timeout",
            },
            [
                InjectionResult.hit("memory", "记忆"),
                InjectionResult.timeout("book_lore", timeout_ms=800),
            ],
        )
        conn.execute("DELETE FROM injection_trace_channels WHERE trace_id=? AND channel='book_lore'", ("missing-row",))
        conn.commit()

        by_channel = store.query(from_ts=0, to_ts=99, channel="book_lore")
        by_timeout = store.query(from_ts=0, to_ts=99, status="timeout")

        self.assertEqual([row["trace_id"] for row in by_channel], ["missing-row"])
        self.assertEqual([row["trace_id"] for row in by_timeout], ["missing-row"])

    def test_query_has_error_includes_channel_errors_and_timeouts(self):
        from services.injection.channel_base import InjectionResult

        store, _ = self._store()
        store.record({"trace_id": "ok", "timestamp": 10, "mode": "full", "message": "ok", "status": "ok"}, [InjectionResult.hit("memory", "m")])
        store.record({"trace_id": "channel-error", "timestamp": 20, "mode": "full", "message": "err", "status": "ok"}, [InjectionResult.error_result("facts", "boom")])
        store.record({"trace_id": "channel-timeout", "timestamp": 30, "mode": "full", "message": "timeout", "status": "ok"}, [InjectionResult.timeout("jargon", timeout_ms=10)])

        error_rows = store.query(from_ts=0, to_ts=99, has_error=True)
        clean_rows = store.query(from_ts=0, to_ts=99, has_error=False)

        self.assertEqual([row["trace_id"] for row in error_rows], ["channel-timeout", "channel-error"])
        self.assertEqual([row["trace_id"] for row in clean_rows], ["ok"])

    def test_record_truncates_nested_channel_details_to_preview_budget(self):
        from services.injection.channel_base import InjectionResult

        store, _ = self._store(max_preview_chars=30)
        long_text = "长文本" * 200
        store.record(
            {"trace_id": "bounded-details", "timestamp": 10, "mode": "full", "message": long_text, "final_text": long_text, "status": "ok"},
            [
                InjectionResult.hit(
                    "memory",
                    long_text,
                    items=[{"id": 1, "content": long_text, "preview": long_text}],
                    filtered=[{"id": 2, "reason": "too_long", "content": long_text}],
                    warnings=[long_text],
                )
            ],
        )

        detail = store.get("bounded-details")
        raw_details = detail["channels"][0]["details"]
        details = json.loads(raw_details)

        self.assertNotIn(long_text, raw_details)
        self.assertLessEqual(len(details["items"][0]["content"]), 30)
        self.assertLessEqual(len(details["items"][0]["preview"]), 30)
        self.assertLessEqual(len(details["filtered"][0]["content"]), 30)
        self.assertLessEqual(len(details["warnings"][0]), 30)

    def test_full_payload_and_revision_session_filters_preserve_tail(self):
        from services.injection.channel_base import InjectionResult

        store, _ = self._store(max_preview_chars=30)
        final_text = f"body-{'x' * 60_000}-TAIL-MARKER"
        store.record(
            {
                "trace_id": "full-payload",
                "timestamp": 10,
                "mode": "full",
                "message": "request",
                "final_text": final_text,
                "status": "ok",
                "metadata": {
                    "config_revision": "cfg-abc",
                    "runtime_scope": {"session": {"id": "qq:group:g1", "kind": "group", "label": "g1"}},
                },
            },
            [InjectionResult.hit("memory", "result")],
        )

        detail = store.get("full-payload")
        self.assertIn("TAIL-MARKER", detail["payload_json"])
        self.assertLess(len(detail["final_preview"]), len(final_text))
        rows = store.query(
            from_ts=0,
            to_ts=20,
            session_id="qq:group:g1",
            config_revision="cfg-abc",
        )
        self.assertEqual([row["trace_id"] for row in rows], ["full-payload"])
        self.assertEqual(rows[0]["session_id"], "qq:group:g1")
        self.assertEqual(rows[0]["config_revision"], "cfg-abc")

    def _record_legacy_and_scoped_traces(self, store):
        base = {"mode": "full", "message": "m", "final_text": "f", "status": "ok"}
        # AstrBot 路径旧 trace：bot_id 列是 QQ 号，bot_profile_id 才是 db_id，metadata 无 runtime_scope
        store.record({**base, "trace_id": "legacy-g1", "timestamp": 10, "group_id": "398291136",
                      "bot_id": "20000001", "bot_profile_id": "yushu"}, [])
        store.record({**base, "trace_id": "legacy-g1-bot2", "timestamp": 11, "group_id": "398291136",
                      "bot_id": "2500447291", "bot_profile_id": "yushu"}, [])
        store.record({**base, "trace_id": "legacy-g2", "timestamp": 12, "group_id": "900000001",
                      "bot_id": "20000001", "bot_profile_id": "yushu"}, [])
        store.record({**base, "trace_id": "legacy-private", "timestamp": 13, "group_id": "",
                      "bot_id": "20000001", "bot_profile_id": "yushu"}, [])
        store.record({**base, "trace_id": "other-bot", "timestamp": 14, "group_id": "398291136",
                      "bot_id": "123456", "bot_profile_id": "other"}, [])
        # Cortico 路径：bot_id 列就是 db_id，metadata 带 canonical session
        store.record({**base, "trace_id": "cortico-g1", "timestamp": 15, "group_id": "398291136",
                      "bot_id": "yushu", "bot_profile_id": "yushu",
                      "metadata": {"runtime_scope": {"bot_id": "yushu", "session": {"id": "羽书:group:398291136"}}}}, [])
        # 同群号但 canonical session 属于别的平台前缀：以 metadata 为准，不回退群号
        store.record({**base, "trace_id": "scoped-other-platform", "timestamp": 16, "group_id": "398291136",
                      "bot_id": "yushu", "bot_profile_id": "yushu",
                      "metadata": {"runtime_scope": {"bot_id": "yushu", "session": {"id": "cortico:group:398291136"}}}}, [])

    def test_bot_filter_matches_profile_id_or_bot_id_column(self):
        store, _ = self._store()
        self._record_legacy_and_scoped_traces(store)

        ids = {row["trace_id"] for row in store.query(from_ts=0, to_ts=100, bot_id="yushu")}
        self.assertEqual(ids, {"legacy-g1", "legacy-g1-bot2", "legacy-g2", "legacy-private",
                               "cortico-g1", "scoped-other-platform"})
        self.assertEqual(store.count(from_ts=0, to_ts=100, bot_id="yushu"), 6)
        # 直接按 QQ 号筛也仍然有效
        qq_ids = {row["trace_id"] for row in store.query(from_ts=0, to_ts=100, bot_id="20000001")}
        self.assertEqual(qq_ids, {"legacy-g1", "legacy-g2", "legacy-private"})

    def test_session_filter_falls_back_to_group_id_for_legacy_traces(self):
        store, _ = self._store()
        self._record_legacy_and_scoped_traces(store)

        filters = {"from_ts": 0, "to_ts": 100, "bot_id": "yushu", "session_id": "羽书:group:398291136"}
        ids = {row["trace_id"] for row in store.query(**filters)}
        self.assertEqual(ids, {"legacy-g1", "legacy-g1-bot2", "cortico-g1"})
        self.assertEqual(store.count(**filters), 3)

        other_group = {row["trace_id"] for row in store.query(
            from_ts=0, to_ts=100, bot_id="yushu", session_id="羽书:group:900000001")}
        self.assertEqual(other_group, {"legacy-g2"})

        # 私聊 session 不按群号回退，旧的无 scope 私聊 trace 不会被错配
        private = store.query(from_ts=0, to_ts=100, bot_id="yushu", session_id="羽书:private:20000001")
        self.assertEqual(private, [])

    def test_record_applies_configured_retention_days_and_max_rows(self):
        from services.injection.channel_base import InjectionResult

        now = 200000.0
        store, _ = self._store(retention_days=1, max_rows=3, cleanup_on_record=True, now_provider=lambda: now)
        store.record({"trace_id": "old", "timestamp": now - 2 * 86400, "mode": "full", "message": "old", "status": "ok"}, [InjectionResult.empty("memory")])
        for i in range(5):
            store.record({"trace_id": f"new-{i}", "timestamp": now - (5 - i), "mode": "full", "message": str(i), "status": "ok"}, [InjectionResult.empty("memory")])

        rows = store.query(from_ts=0, to_ts=now + 1, limit=10)

        self.assertEqual([row["trace_id"] for row in rows], ["new-4", "new-3", "new-2"])
        self.assertIsNone(store.get("old"))
        self.assertIsNone(store.get("new-0"))

    def test_old_trace_payload_is_stripped_but_trace_kept(self):
        from services.injection.channel_base import InjectionResult

        now = 900000.0
        store, conn = self._store(
            retention_days=14, payload_retention_days=3, cleanup_on_record=True, now_provider=lambda: now,
        )
        store.record({"trace_id": "old", "timestamp": now - 5 * 86400, "mode": "full", "message": "old", "status": "ok"}, [InjectionResult.hit("memory", "m")])
        store.record({"trace_id": "new", "timestamp": now - 60, "mode": "full", "message": "new", "status": "ok"}, [InjectionResult.hit("memory", "m")])

        payloads = dict(conn.execute("SELECT trace_id, payload_json FROM injection_traces").fetchall())
        self.assertIsNone(payloads["old"])
        self.assertTrue(payloads["new"])
        old = store.get("old")
        self.assertIsNotNone(old)
        self.assertEqual([item["channel"] for item in old["channels"]], ["memory"])

    def test_payload_cleanup_never_leaves_write_transaction_open(self):
        from services.injection.channel_base import InjectionResult

        now = 900000.0
        store, conn = self._store(
            retention_days=14, payload_retention_days=3, cleanup_on_record=True, now_provider=lambda: now,
        )
        # 没有可去载荷、也没有可删的 trace：清理不能留下未提交的写事务（会一直占着写锁）
        store.record({"trace_id": "new", "timestamp": now - 60, "mode": "full", "message": "m", "status": "ok"}, [InjectionResult.hit("memory", "m")])
        self.assertFalse(conn.in_transaction)
        store.cleanup(now=now, retention_seconds=14 * 86400, payload_retention_seconds=3 * 86400)
        self.assertFalse(conn.in_transaction)

    def test_cleanup_removes_old_rows_and_enforces_max_rows(self):
        from services.injection.channel_base import InjectionResult

        store, _ = self._store()
        for i in range(5):
            store.record({"trace_id": f"t{i}", "timestamp": 100 + i, "mode": "full", "message": str(i), "status": "ok"}, [InjectionResult.empty("memory")])

        old_deleted = store.cleanup(now=200, retention_seconds=98, max_rows=2)
        rows = store.query(from_ts=0, to_ts=999)

        self.assertGreaterEqual(old_deleted, 2)
        self.assertEqual([row["trace_id"] for row in rows], ["t4", "t3"])
        self.assertIsNone(store.get("t0"))

    def test_schema_exposes_trace_retention_settings(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))

        trace_settings = schema["Trace_Settings"]["items"]

        self.assertEqual(trace_settings["retention_days"]["default"], 14)
        self.assertEqual(trace_settings["max_rows"]["default"], 5000)
        self.assertEqual(trace_settings["max_preview_chars"]["default"], 1200)

    def test_record_keeps_later_channels_when_one_channel_detail_is_not_json(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.injection.channel_base import InjectionResult

        store, conn = self._store()
        scope = RuntimeScope(
            bot_id="yushu",
            visibility="group",
            session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        )
        store.record(
            {"trace_id": "partial-channels", "timestamp": 50, "mode": "full", "message": "x", "status": "degraded", "error": "book_lore:timeout"},
            [
                InjectionResult.hit("affinity", "关系", items=[{"preview": "关系"}]),
                InjectionResult.hit("belief", "信念", items=[{"scope": scope}]),
                InjectionResult.timeout("book_lore", timeout_ms=800),
            ],
        )

        rows = conn.execute(
            "SELECT channel, status FROM injection_trace_channels WHERE trace_id=? ORDER BY id",
            ("partial-channels",),
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("affinity", "hit"), ("belief", "hit"), ("book_lore", "timeout")])
        belief_details = json.loads(
            conn.execute(
                "SELECT details FROM injection_trace_channels WHERE trace_id=? AND channel='belief'",
                ("partial-channels",),
            ).fetchone()[0]
        )
        self.assertEqual(belief_details["error"], "channel_details_not_json_serializable")
        self.assertEqual(belief_details["items"], [])

    def test_safe_record_returns_false_when_storage_fails(self):
        from services.injection.channel_base import InjectionResult

        store, conn = self._store()
        conn.close()

        ok = store.safe_record({"trace_id": "broken", "timestamp": 1, "mode": "full", "message": "x"}, [InjectionResult.empty("memory")])

        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
