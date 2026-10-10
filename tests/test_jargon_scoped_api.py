from __future__ import annotations

import asyncio
import types
import unittest


class _ScopedRepo:
    def __init__(self) -> None:
        self.list_calls = []
        self.upsert_calls = []
        self.rows = [
            {
                "id": 7,
                "word": "团建",
                "meaning": "群内活动",
                "status": "confirmed",
                "is_jargon": True,
                "frequency": 3,
                "confidence": 0.9,
                "contexts": [],
                "source_memory_id": None,
                "source_context": None,
                "provenance": {},
                "created_at": 1.0,
                "updated_at": 2.0,
            }
        ]

    def list_scoped_jargon(self, scope, *, status=None, limit=50):
        self.list_calls.append((scope, status, limit))
        return list(self.rows)

    def upsert_scoped_jargon(self, scope, **kwargs):
        self.upsert_calls.append((scope, kwargs))
        return 8


class JargonScopedApiTest(unittest.TestCase):
    @staticmethod
    def _scope_envelope():
        from domain.scope import RuntimeScope, ScopeCodec, SessionRef

        scope = RuntimeScope(
            "bot-alpha",
            "group",
            SessionRef("qq:group:g1", "qq", "group", "g1"),
        )
        return scope, ScopeCodec.to_dict(scope)

    def setUp(self):
        from webui.blueprints import jargon as module

        self.module = module
        self.repo = _ScopedRepo()
        self.old_container = module.get_container
        self.old_request = module.request
        self.old_jsonify = module.jsonify
        module.get_container = lambda: types.SimpleNamespace(
            db=types.SimpleNamespace(scoped_knowledge=self.repo)
        )
        module.jsonify = lambda payload: payload

    def tearDown(self):
        self.module.get_container = self.old_container
        self.module.request = self.old_request
        self.module.jsonify = self.old_jsonify

    def test_list_requires_complete_scope_and_explicit_page(self):
        self.module.request = types.SimpleNamespace(args={})

        payload, status = asyncio.run(self.module.list_jargon.__wrapped__())

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "scope_required"}})
        self.assertEqual(self.repo.list_calls, [])

    def test_list_uses_scoped_repository_and_nested_exact_page(self):
        self.module.request = types.SimpleNamespace(
            args={
                "bot_id": "bot-alpha",
                "session_id": "qq:group:g1",
                "visibility": "group",
                "page": "1",
                "page_size": "10",
            }
        )

        payload = asyncio.run(self.module.list_jargon.__wrapped__())

        self.assertEqual(payload["items"][0]["word"], "团建")
        self.assertEqual(payload["page"], {
            "total": 1,
            "total_status": "exact",
            "reason_code": None,
            "limit": 25,
            "offset": 0,
            "page": 1,
            "page_count": 1,
            "has_more": False,
        })
        self.assertEqual(self.repo.list_calls[0][0].bot_id, "bot-alpha")
        self.assertEqual(self.repo.list_calls[0][0].session.id, "qq:group:g1")

    def test_scoped_evidence_restores_only_same_scope_messages(self):
        scope, _ = self._scope_envelope()

        class _Cursor:
            def __init__(self, rows=None):
                self.rows = rows or []
            def fetchall(self):
                return list(self.rows)
            def fetchone(self):
                return self.rows[0] if self.rows else None

        class _Conn:
            def __init__(self):
                self.calls = []
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "PRAGMA table_info(memories)" in sql:
                    columns = ["id", "group_id", "sender_id", "sender_name", "content", "timestamp", "bot_id", "session_id", "visibility", "resolution_state", "quarantine", "memory_type"]
                    return _Cursor([(index, name) for index, name in enumerate(columns)])
                self.assert_scope(sql, params)
                if "WHERE id=?" in sql:
                    return _Cursor([(11, "g1", "u1", "用户甲", "今天疯狂星期四 v我50", 1000.0)])
                if "timestamp <" in sql:
                    return _Cursor([(10, "g1", "u2", "用户乙", "前文", 990.0)])
                if "timestamp >" in sql:
                    return _Cursor([(12, "g1", "u3", "用户丙", "后文", 1010.0)])
                return _Cursor([])
            @staticmethod
            def assert_scope(sql, params):
                assert "bot_id=?" in sql and "session_id=?" in sql and "visibility=?" in sql
                assert "bot-alpha" in params and "qq:group:g1" in params and "group" in params

        conn = _Conn()
        container = types.SimpleNamespace(db=types.SimpleNamespace(conn=conn))
        item = {"id": 7, "word": "v我50", "meaning": "疯狂星期四", "revision": 2, "source_memory_id": 11, "contexts": [], "source_context": None}

        payload = self.module._scoped_jargon_evidence(container, scope, item, before=1, after=1)

        self.assertFalse(payload["used_fallback"])
        self.assertEqual([message["role"] for message in payload["messages"]], ["before", "anchor", "after"])
        self.assertEqual(payload["anchor"]["content"], "今天疯狂星期四 v我50")

    def test_scoped_evidence_missing_object_ref_is_rejected(self):
        self.module.request = types.SimpleNamespace(args={
            "bot_id": "bot-alpha",
            "session_id": "qq:group:g1",
            "visibility": "group",
            "before": "5",
            "after": "5",
        })

        payload, status = asyncio.run(self.module.get_scoped_jargon_evidence.__wrapped__(7))

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "object_ref_required")

    def test_batch_review_validates_each_object_ref_before_domain_calls(self):
        scope, envelope = self._scope_envelope()
        review_calls = []
        validated = []

        class _Service:
            def review(self, runtime_scope, jargon_id, action):
                review_calls.append((runtime_scope.bot_id, jargon_id, action))
                return {"id": jargon_id, "status": "rejected", "scope": runtime_scope}

        original_require = self.module._require_object_ref
        self.module._require_object_ref = lambda body, **kwargs: validated.append((body["object_ref"]["ref"], kwargs["locator"]))
        self.module.get_container = lambda: types.SimpleNamespace(
            db=types.SimpleNamespace(scoped_knowledge=self.repo),
            jargon_service=_Service(),
        )
        self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({
            "scope": envelope,
            "action": "reject",
            "items": [{"id": 7, "object_ref": {"ref": "opaque-7"}, "revision": 2000}],
        }))
        try:
            payload = asyncio.run(self.module.batch_review_scoped_jargon.__wrapped__())
        finally:
            self.module._require_object_ref = original_require

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reviewed_count"], 1)
        self.assertEqual(validated, [("opaque-7", 7)])
        self.assertEqual(review_calls, [(scope.bot_id, 7, "reject")])

    def test_single_and_batch_review_use_same_domain_service(self):
        scope, envelope = self._scope_envelope()
        calls = []
        validated = []

        class _Service:
            def review(self, runtime_scope, jargon_id, action, query_trace_id=None, reason=None):
                calls.append((runtime_scope.bot_id, jargon_id, action, reason))
                return {"id": jargon_id, "status": "rejected", "scope": runtime_scope}

        original_require = self.module._require_object_ref
        self.module._require_object_ref = lambda body, **kwargs: validated.append((body["object_ref"]["ref"], kwargs["locator"]))
        self.module.get_container = lambda: types.SimpleNamespace(
            db=types.SimpleNamespace(scoped_knowledge=self.repo),
            jargon_service=_Service(),
        )
        try:
            self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({
                "scope": envelope,
                "object_ref": {"ref": "opaque-7"},
                "revision": 2000,
                "reason": "用户拒绝",
            }))
            single = asyncio.run(self.module.review_scoped_jargon.__wrapped__(7, "reject"))
            self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({
                "scope": envelope,
                "action": "reject",
                "reason": "用户拒绝",
                "items": [{"id": 7, "object_ref": {"ref": "opaque-7"}, "revision": 2000}],
            }))
            batch = asyncio.run(self.module.batch_review_scoped_jargon.__wrapped__())
        finally:
            self.module._require_object_ref = original_require

        self.assertTrue(single["ok"])
        self.assertTrue(batch["ok"])
        self.assertEqual(calls, [
            (scope.bot_id, 7, "reject", "用户拒绝"),
            (scope.bot_id, 7, "reject", "用户拒绝"),
        ])
        self.assertEqual(validated, [("opaque-7", 7), ("opaque-7", 7)])

    def test_approve_without_query_trace_passes_gate(self):
        _, envelope = self._scope_envelope()
        calls = []

        class _Service:
            def review(self, runtime_scope, jargon_id, action, query_trace_id=None, reason=None):
                calls.append((jargon_id, action, query_trace_id))
                return {"id": jargon_id, "status": "confirmed", "scope": runtime_scope}

        original_require = self.module._require_object_ref
        original_evidence = self.module._memory_evidence_available
        self.module._require_object_ref = lambda *_args, **_kwargs: None
        self.module._memory_evidence_available = lambda *_args, **_kwargs: True
        self.module.get_container = lambda: types.SimpleNamespace(
            db=types.SimpleNamespace(scoped_knowledge=self.repo),
            jargon_service=_Service(),
            injection_trace_store=None,
        )
        self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({
            "scope": envelope,
            "object_ref": {"ref": "opaque-7"},
        }))
        try:
            payload = asyncio.run(self.module.review_scoped_jargon.__wrapped__(7, "approve"))
        finally:
            self.module._require_object_ref = original_require
            self.module._memory_evidence_available = original_evidence

        self.assertTrue(payload["ok"])
        self.assertEqual(calls, [(7, "approve", None)])

    def test_approve_with_invalid_query_trace_fails(self):
        _, envelope = self._scope_envelope()
        calls = []

        class _Service:
            def review(self, *_args, **_kwargs):
                calls.append(_args)
                return {"id": 7, "status": "confirmed"}

        original_require = self.module._require_object_ref
        original_evidence = self.module._memory_evidence_available
        self.module._require_object_ref = lambda *_args, **_kwargs: None
        self.module._memory_evidence_available = lambda *_args, **_kwargs: True
        self.module.get_container = lambda: types.SimpleNamespace(
            db=types.SimpleNamespace(scoped_knowledge=self.repo),
            jargon_service=_Service(),
            injection_trace_store=None,
        )
        self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({
            "scope": envelope,
            "object_ref": {"ref": "opaque-7"},
            "query_trace_id": "not-a-real-trace",
        }))
        try:
            payload, status = asyncio.run(self.module.review_scoped_jargon.__wrapped__(7, "approve"))
        finally:
            self.module._require_object_ref = original_require
            self.module._memory_evidence_available = original_evidence

        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "query_trace_invalid")
        self.assertEqual(calls, [])

    def test_single_review_fails_closed_on_scope_or_object_ref(self):
        _, envelope = self._scope_envelope()
        calls = []

        class _Service:
            def review(self, *_args, **_kwargs):
                calls.append(True)
                return {"id": 7, "status": "rejected"}

        self.module.get_container = lambda: types.SimpleNamespace(
            db=types.SimpleNamespace(scoped_knowledge=self.repo),
            jargon_service=_Service(),
        )
        self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({}))
        missing_scope = asyncio.run(self.module.review_scoped_jargon.__wrapped__(7, "reject"))
        self.assertEqual(missing_scope[1], 400)

        original_require = self.module._require_object_ref
        self.module._require_object_ref = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            self.module.ScopedKnowledgeScopeError("object_ref_stale")
        )
        self.module.request = types.SimpleNamespace(get_json=lambda **_kwargs: _async_value({
            "scope": envelope,
            "object_ref": {"ref": "wrong-scope"},
            "revision": 2000,
        }))
        try:
            stale = asyncio.run(self.module.review_scoped_jargon.__wrapped__(7, "reject"))
        finally:
            self.module._require_object_ref = original_require
        self.assertEqual(stale[1], 422)
        self.assertEqual(calls, [])

    def test_global_blocklist_audit_and_remove_use_db_facade(self):
        calls = []

        class _DB:
            def list_jargon_blocklist(self):
                calls.append(("list",))
                return [{"id": 3, "word": "abc", "reason": "拒绝", "source": "user_global_reject", "created_at": 1.0}]
            def remove_jargon_blocklist(self, *, blocklist_id, source):
                calls.append(("remove", blocklist_id, source))
                return 1

        self.module.get_container = lambda: types.SimpleNamespace(db=_DB())
        listed = asyncio.run(self.module.holyman_blocklist.__wrapped__())
        removed = asyncio.run(self.module.remove_global_jargon_blocklist.__wrapped__(3))

        self.assertEqual(listed["items"][0]["word"], "abc")
        self.assertTrue(removed["ok"])
        self.assertEqual(calls, [("list",), ("remove", 3, "user_global_reject")])

    def test_create_requires_codec_envelope_and_passes_runtime_scope(self):
        _, envelope = self._scope_envelope()
        self.module.request = types.SimpleNamespace(
            get_json=lambda **_kwargs: _async_value(
                {
                    "scope": envelope,
                    "word": "团建",
                    "meaning": "群内活动",
                    "provenance": {"source": "manual"},
                }
            )
        )

        payload, status = asyncio.run(self.module.create_jargon.__wrapped__())

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "anchored_jargon_command_unavailable")
        self.assertEqual(self.repo.upsert_calls, [])

    def test_legacy_jargon_mutations_are_terminally_rejected(self):
        payload, status = asyncio.run(self.module.edit_jargon.__wrapped__(7))

        self.assertEqual((payload, status), ({"error": {"code": "legacy_mutation_disabled"}}, 410))
        self.assertEqual(self.repo.upsert_calls, [])


def _async_value(value):
    async def _result():
        return value

    return _result()


if __name__ == "__main__":
    unittest.main()
