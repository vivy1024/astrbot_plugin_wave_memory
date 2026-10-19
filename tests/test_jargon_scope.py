import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


if "astrbot.api" not in sys.modules:
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod


class _Repo:
    def __init__(self):
        self.rows, self.calls = {}, []

    @staticmethod
    def _key(scope):
        return (scope.bot_id, scope.session.id, scope.visibility)

    def upsert_scoped_jargon(self, scope, **record):
        self.calls.append(("upsert", self._key(scope), record["word"]))
        key = (self._key(scope), record["word"])
        previous = self.rows.get(key, {})
        self.rows[key] = {"id": previous.get("id", len(self.rows) + 1), **record}

    def list_scoped_jargon(self, scope, *, status=None, limit, include_archived=False):
        self.calls.append(("list", self._key(scope), status, limit))
        rows = [row for (key, _), row in self.rows.items() if key == self._key(scope)]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        elif not include_archived:
            rows = [row for row in rows if row.get("status") != "archived"]
        return rows[:limit]


class _Db:
    def __init__(self):
        self.scoped_knowledge = _Repo()
        self.blocked = {}

    @staticmethod
    def normalize(word):
        import unicodedata
        return unicodedata.normalize("NFKC", str(word or "")).casefold().strip()

    def is_jargon_blocked(self, word):
        return self.normalize(word) in self.blocked

    def add_jargon_blocklist(self, word, *, reason, source):
        normalized = self.normalize(word)
        self.blocked[normalized] = {"word": normalized, "reason": reason, "source": source}
        return self.blocked[normalized]

    def list_jargon_blocklist(self):
        return list(self.blocked.values())

    def remove_jargon_blocklist(self, word=None, *, blocklist_id=None, source="user_global_reject"):
        normalized = self.normalize(word)
        return 1 if self.blocked.pop(normalized, None) else 0


class JargonScopeTest(unittest.TestCase):
    @staticmethod
    def _scope(bot="yushu", conversation="g1"):
        from domain.scope import RuntimeScope, SessionRef
        return RuntimeScope(bot, "group", SessionRef(f"qq:group:{conversation}", "qq", "group", conversation))

    def test_injector_reads_only_current_runtime_scope_and_cache_key(self):
        from services.jargon.inference import JargonInjector
        db, first, second = _Db(), self._scope("yushu", "g1"), self._scope("bzz", "g1")
        db.scoped_knowledge.upsert_scoped_jargon(first, word="本群梗", meaning="只属于 yushu", status="confirmed", is_jargon=True, frequency=1)
        db.scoped_knowledge.upsert_scoped_jargon(second, word="本群梗", meaning="只属于 bzz", status="confirmed", is_jargon=True, frequency=99)
        injector = JargonInjector(db)
        self.assertIn("yushu", injector.get_injection("本群梗", first))
        self.assertIn("bzz", injector.get_injection("本群梗", second))
        self.assertEqual(injector.get_injection("本群梗", None), "")

    def test_service_fails_closed_for_missing_or_non_group_scope(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.jargon.service import JargonService
        db, service = _Db(), JargonService(_Db(), enabled=True, config={"min_messages": 1})
        service.feed_message("猫猫税", None, "u1")
        self.assertFalse(service.should_mine(None))
        private = RuntimeScope("yushu", "private", SessionRef("qq:private:u1", "qq", "private", "u1"))
        self.assertEqual(asyncio.run(service.mine(private)), [])
        self.assertEqual(db.scoped_knowledge.calls, [])

    def test_service_persists_by_scope_repository_not_legacy_table(self):
        from services.jargon.service import JargonService
        db, scope = _Db(), self._scope()
        service = JargonService(db, enabled=True, config={"min_messages": 1, "min_frequency": 1})
        service._filter.get_candidates = lambda *_args, **_kwargs: [{"word": "猫猫税", "frequency": 3, "contexts": []}]
        asyncio.run(service.mine(scope))
        self.assertEqual(db.scoped_knowledge.calls[0][:2], ("list", ("yushu", "qq:group:g1", "group")))
        self.assertEqual(db.scoped_knowledge.calls[1][0], "upsert")

    def test_service_resolves_and_persists_same_scope_memory_anchor(self):
        from services.jargon.service import JargonService

        class _Cursor:
            def __init__(self, rows): self.rows = rows
            def fetchall(self): return list(self.rows)
            def fetchone(self): return self.rows[0] if self.rows else None

        class _Conn:
            def execute(self, sql, params=()):
                if "PRAGMA table_info(memories)" in sql:
                    columns = ["id", "content", "timestamp", "sender_id", "bot_id", "session_id", "visibility", "resolution_state", "quarantine", "memory_type"]
                    return _Cursor([(index, name) for index, name in enumerate(columns)])
                self.last_sql, self.last_params = sql, params
                if "SELECT id FROM memories" in sql:
                    return _Cursor([(42,)])
                return _Cursor([])

        db, scope = _Db(), self._scope()
        db.conn = _Conn()
        service = JargonService(db, enabled=True, config={"min_messages": 1, "min_frequency": 1})
        service._filter.get_candidates = lambda *_args, **_kwargs: [{
            "word": "猫猫税",
            "frequency": 3,
            "contexts": ["交猫猫税"],
            "source_contexts": [{"content": "交猫猫税", "timestamp": 1000.0, "sender_id": "u1"}],
        }]

        asyncio.run(service.mine(scope))

        row = db.scoped_knowledge.list_scoped_jargon(scope, limit=10)[0]
        self.assertEqual(row["source_memory_id"], 42)
        self.assertIn("交猫猫税", row["source_context"])
        self.assertIn("bot_id=?", db.conn.last_sql)
        self.assertIn("qq:group:g1", db.conn.last_params)

    def test_reject_is_global_and_blocks_other_scope_before_llm_or_persist(self):
        from services.jargon.service import JargonService

        db = _Db()
        first, second = self._scope("yushu", "g1"), self._scope("bzz", "g2")
        service = JargonService(db, enabled=True, config={"min_messages": 1, "min_frequency": 1})
        db.scoped_knowledge.upsert_scoped_jargon(first, word="猫猫税", meaning="", status="pending", is_jargon=None, frequency=1, confidence=0.0, contexts=[])
        item = db.scoped_knowledge.list_scoped_jargon(first, limit=10)[0]
        result = service.review(first, item["id"], "reject", reason="用户明确拒绝")
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(db.is_jargon_blocked("  CAT猫猫税 ".replace("CAT", "")))

        llm_calls = []
        service_b = JargonService(db, llm_client=types.SimpleNamespace(text_chat=lambda **_: llm_calls.append(1)), enabled=True, config={"min_messages": 1, "min_frequency": 1})
        upserts_before = len(db.scoped_knowledge.calls)
        service_b._filter.get_candidates = lambda *_args, **_kwargs: [{"word": "猫猫税", "frequency": 3, "contexts": ["上下文"]}]
        asyncio.run(service_b.mine(second))
        self.assertEqual(llm_calls, [])
        self.assertEqual(len(db.scoped_knowledge.calls), upserts_before)

    def test_local_and_holyman_matches_are_filtered_before_final_render(self):
        from services.jargon.inference import JargonInjector

        db = _Db()
        scope = self._scope()
        db.scoped_knowledge.upsert_scoped_jargon(scope, word="猫猫税", meaning="本地释义", status="confirmed", is_jargon=True, frequency=1)
        holyman = types.SimpleNamespace(match_text=lambda *_args, **_kwargs: [{"term": "猫猫税", "explanation": "Holyman释义", "confidence": 0.9}])
        injector = JargonInjector(db, holyman_reference=holyman)
        self.assertIn("本地释义", injector.get_injection("猫猫税", scope))
        db.blocked["猫猫税"] = {"word": "猫猫税", "reason": "拒绝", "source": "user_global_reject"}
        self.assertEqual(injector.get_injection("猫猫税", scope), "")

    def test_inference_checker_fails_closed_before_llm(self):
        from services.jargon.inference import JargonInferenceEngine

        calls = []

        class _LLM:
            async def text_chat(self, **_kwargs):
                calls.append(True)
                return types.SimpleNamespace(completion_text='{"meaning":"不应调用"}')

        engine = JargonInferenceEngine(_LLM(), blocklist_checker=lambda _word: (_ for _ in ()).throw(RuntimeError("db unavailable")))
        result = asyncio.run(engine.infer("猫猫税", ["上下文"]))
        self.assertEqual(result["reject_reason"], "global_blocklist")
        self.assertFalse(result["enter_llm"])
        self.assertEqual(calls, [])

    def test_holyman_sync_uses_source_scoped_blocklist_replace(self):
        from services.jargon import sync as sync_module

        calls = []

        class _FakeDB:
            def __init__(self, _path):
                pass
            def upsert_jargon_knowledge_snapshot(self, *_args, **_kwargs):
                pass
            def replace_jargon_knowledge_table(self, table, *_args, **_kwargs):
                calls.append(("generic", table))
            def replace_jargon_blocklist_source(self, rows, *, source):
                calls.append(("blocklist", source, [row["word"] for row in rows]))
            def close(self):
                pass

        original_db = sync_module.WaveMemoryDB
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "holyman"
            assets_dir.mkdir()
            (assets_dir.parent / "wave_memory.db").write_bytes(b"not-empty")
            service = sync_module.HolymanSyncService(assets_dir=assets_dir)
            sync_module.WaveMemoryDB = _FakeDB
            try:
                service._sync_runtime_snapshot({
                    "manifest": {},
                    "phrases": {},
                    "quality_report": {},
                    "examples": [],
                    "concepts": [],
                    "candidates": [],
                    "blocked": {"猫猫税": "默认屏蔽"},
                })
            finally:
                sync_module.WaveMemoryDB = original_db
        self.assertIn(("blocklist", "holyman_skills", ["猫猫税"]), calls)
        self.assertNotIn(("generic", "jargon_blocklist"), calls)

    def test_blocklist_normalizes_and_holyman_sync_preserves_manual_priority(self):
        from engine.database import WaveMemoryDB

        class _Facade(WaveMemoryDB):
            @property
            def conn(self):
                return self._test_conn

        db = object.__new__(_Facade)
        db._test_conn = sqlite3.connect(":memory:")
        try:
            db._setup_jargon_knowledge_tables()
            manual = db.add_jargon_blocklist("  ＡＢＣ  ", reason="人工拒绝", source="user_global_reject")
            db.replace_jargon_blocklist_source(
                [{"word": "abc", "reason": "Holyman 默认屏蔽"}],
                source="holyman_skills",
            )
            self.assertEqual(db.list_jargon_blocklist()[0]["source"], "user_global_reject")
            sources = {row[0] for row in db.conn.execute("SELECT source FROM jargon_blocklist WHERE word='abc'").fetchall()}
            self.assertEqual(sources, {"user_global_reject", "holyman_skills"})

            db.replace_jargon_blocklist_source(
                [{"word": "ＡＢＣ", "reason": "同步更新"}],
                source="holyman_skills",
            )
            self.assertEqual(db.list_jargon_blocklist()[0]["reason"], "人工拒绝")
            self.assertEqual(db.remove_jargon_blocklist(blocklist_id=manual["id"]), 1)
            restored = db.list_jargon_blocklist()
            self.assertEqual(restored, [{
                "id": restored[0]["id"],
                "word": "abc",
                "reason": "同步更新",
                "source": "holyman_skills",
                "created_at": restored[0]["created_at"],
            }])
        finally:
            db._test_conn.close()

    def test_edit_requeues_and_archive_removes_from_default_list(self):
        from services.jargon.service import JargonService

        db, scope = _Db(), self._scope()
        db.scoped_knowledge.upsert_scoped_jargon(
            scope,
            word="猫猫税",
            meaning="旧释义",
            status="confirmed",
            is_jargon=True,
            frequency=3,
            confidence=0.9,
            contexts=["上下文"],
            source_memory_id=42,
            source_context='["上下文"]',
            provenance={"source": "wave_memory"},
        )
        service = JargonService(db)
        item = db.scoped_knowledge.list_scoped_jargon(scope, limit=10)[0]

        updated = service.update_meaning(scope, item["id"], "新释义")
        self.assertEqual(updated["meaning"], "新释义")
        self.assertEqual(updated["status"], "pending")
        service.archive(scope, item["id"])
        self.assertEqual(db.scoped_knowledge.list_scoped_jargon(scope, limit=10), [])
        archived = db.scoped_knowledge.list_scoped_jargon(scope, limit=10, include_archived=True)[0]
        self.assertEqual(archived["status"], "archived")


class JargonHumanReviewGateTest(unittest.TestCase):
    """人工审批的证据门禁（2026-10-18 重写）。

    线上 `scoped_jargon` 152 条 pending 全部卡在旧门禁上（WebUI 一次 approve 都没成功）：
    前端从不发送 `query_trace_id`（拦 152/152），标签抽取异步落后导致 `source_tags` 为空
    （拦 111/152），而 `source_memory_id` 其实一条都不缺（拦 0/152）。
    人工审核是最终权威，溯源字段降为可选信息。
    """

    @staticmethod
    def _scope(bot="yushu", conversation="g1"):
        from domain.scope import RuntimeScope, SessionRef
        return RuntimeScope(bot, "group", SessionRef(f"qq:group:{conversation}", "qq", "group", conversation))

    @staticmethod
    def _pending(db, scope, **overrides):
        record = {
            "word": "猫猫税", "meaning": "本群梗", "status": "pending", "is_jargon": None,
            "frequency": 3, "confidence": 0.7, "contexts": ["上下文"],
            "source_memory_id": 42,
            "provenance": {"source": "wave_memory", "evidence": {"memory_ids": [42]}},
        }
        record.update(overrides)
        db.scoped_knowledge.upsert_scoped_jargon(scope, **record)
        return db.scoped_knowledge.list_scoped_jargon(scope, limit=10)[0]

    def test_approve_without_query_trace_or_source_tags_succeeds(self):
        """这正是线上 152 条 pending 的形态：有锚点、无 trace、无 source_tags。"""
        from services.jargon.service import JargonService

        db, scope = _Db(), self._scope()
        item = self._pending(db, scope, source_memory_id=42, provenance={"evidence": {"memory_ids": [42]}})
        result = JargonService(db).review(scope, item["id"], "approve")
        self.assertEqual(result["status"], "confirmed")
        stored = db.scoped_knowledge.list_scoped_jargon(scope, limit=10)[0]
        self.assertEqual(stored["status"], "confirmed")
        self.assertEqual(stored["provenance"]["reviewed_by"], "webui")
        self.assertEqual(stored["provenance"]["review_action"], "approve")

    def test_approve_accepts_source_tags_alone_as_anchor(self):
        """标签链完整但没锚点也必须能批：标签是派生证据，不能反过来当批准前提。"""
        from services.jargon.service import JargonService

        db, scope = _Db(), self._scope()
        item = self._pending(db, scope, source_memory_id=None, provenance={"source_tags": [{"name": "梗"}]})
        self.assertEqual(JargonService(db).review(scope, item["id"], "approve")["status"], "confirmed")

    def test_approve_without_any_evidence_is_rejected(self):
        """完全凭空（无锚点、无标签、无 evidence）才拒绝，避免放进无从考证的候选。"""
        from services.jargon.service import JargonService

        db, scope = _Db(), self._scope()
        item = self._pending(db, scope, source_memory_id=None, provenance={"source": "wave_memory"})
        with self.assertRaises(ValueError) as ctx:
            JargonService(db).review(scope, item["id"], "approve")
        self.assertEqual(str(ctx.exception), "jargon_evidence_required")

    def test_provided_but_invalid_query_trace_is_still_rejected(self):
        """提供了 trace 就必须有效 —— 防止伪造检索记录。"""
        from services.jargon.service import JargonService

        class _Store:
            @staticmethod
            def get_for_scope(_trace_id, _scope):
                return None

        db, scope = _Db(), self._scope()
        item = self._pending(db, scope)
        with self.assertRaises(ValueError) as ctx:
            JargonService(db, trace_store=_Store()).review(scope, item["id"], "approve", query_trace_id="forged")
        self.assertEqual(str(ctx.exception), "jargon_query_trace_invalid")

    def test_valid_query_trace_accepted_when_provided(self):
        from services.jargon.service import JargonService

        class _Store:
            @staticmethod
            def get_for_scope(_trace_id, _scope):
                return {"trace_id": "ok"}

        db, scope = _Db(), self._scope()
        item = self._pending(db, scope)
        result = JargonService(db, trace_store=_Store()).review(scope, item["id"], "approve", query_trace_id="ok")
        self.assertEqual(result["status"], "confirmed")

    def test_reject_still_blocks_globally_without_any_evidence(self):
        """拒绝不需要证据，且必须写全局拉黑（这条路径不能被我改动波及）。"""
        from services.jargon.service import JargonService

        db, scope = _Db(), self._scope()
        item = self._pending(db, scope, source_memory_id=None, provenance={"source": "wave_memory"})
        self.assertEqual(JargonService(db).review(scope, item["id"], "reject")["status"], "rejected")
        self.assertIn("猫猫税", db.blocked)


if __name__ == "__main__":
    unittest.main()
