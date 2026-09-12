import asyncio
import sqlite3
import sys
import types
import unittest
from pathlib import Path


if "quart" not in sys.modules:
    quart_mod = types.ModuleType("quart")

    class _Blueprint:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    quart_mod.Blueprint = _Blueprint
    quart_mod.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    quart_mod.request = types.SimpleNamespace(args={}, headers={}, method="GET", get_json=lambda *args, **kwargs: {})
    sys.modules["quart"] = quart_mod


class V430JargonWebuiAuditTest(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE jargon (
                id INTEGER PRIMARY KEY,
                word TEXT,
                meaning TEXT,
                is_jargon INTEGER DEFAULT 1,
                frequency INTEGER DEFAULT 1,
                confidence REAL DEFAULT 1.0,
                is_global INTEGER DEFAULT 0,
                group_id TEXT,
                contexts TEXT DEFAULT '[]',
                created_at INTEGER DEFAULT 0,
                source_memory_id INTEGER,
                source_message_ts INTEGER,
                source_sender_id TEXT,
                source_context TEXT DEFAULT '[]',
                candidate_type TEXT DEFAULT 'jargon',
                reject_reason TEXT,
                status TEXT DEFAULT 'pending',
                source TEXT DEFAULT 'wave_memory'
            )"""
        )
        rows = [
            (1, "真黑话", "本地确认黑话", "local_jargon_candidate", "", "confirmed", "wave_memory"),
            (2, "小明", "群友昵称", "jargon", "person_alias_diverted", "rejected", "audit_router"),
            (3, "object", "技术噪声", "jargon", "technical_noise_filtered", "rejected", "audit_router"),
        ]
        conn.executemany(
            """INSERT INTO jargon
               (id, word, meaning, candidate_type, reject_reason, status, source, contexts, source_context, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '[]', 100)""",
            rows,
        )
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    def _list_words(self, conn, args):
        import importlib

        jargon_bp_mod = importlib.import_module("webui.blueprints.jargon")

        class _DB:
            def __init__(self, conn):
                self.conn = conn

        old_container = jargon_bp_mod.get_container
        old_request = jargon_bp_mod.request
        old_jsonify = jargon_bp_mod.jsonify
        jargon_bp_mod.get_container = lambda: types.SimpleNamespace(db=_DB(conn))
        jargon_bp_mod.request = types.SimpleNamespace(args=args)
        jargon_bp_mod.jsonify = lambda payload: payload
        try:
            payload = asyncio.run(jargon_bp_mod.legacy_list_jargon.__wrapped__())
        finally:
            jargon_bp_mod.get_container = old_container
            jargon_bp_mod.request = old_request
            jargon_bp_mod.jsonify = old_jsonify
        return [item["word"] for item in payload["items"]], payload

    def test_default_list_hides_reject_reason_audit_noise_but_include_rejected_shows_it(self):
        conn = self._conn()

        default_words, default_payload = self._list_words(conn, {"limit": "50", "offset": "0"})
        audit_words, audit_payload = self._list_words(conn, {"limit": "50", "offset": "0", "include_rejected": "true"})

        self.assertEqual(default_words, ["真黑话"])
        self.assertEqual(default_payload["total"], 1)
        self.assertEqual(audit_words, ["真黑话", "小明", "object"])
        self.assertEqual(audit_payload["items"][1]["source"], "audit_router")
        self.assertEqual(audit_payload["items"][1]["reject_reason"], "person_alias_diverted")
        self.assertEqual(audit_payload["items"][2]["reject_reason"], "technical_noise_filtered")

    def test_frontend_exposes_audit_toggle_and_candidate_metadata_columns(self):
        page = Path("webui/frontend/src/pages/jargon/JargonPage.tsx").read_text(encoding="utf-8")
        # 广域资产审计与同步预览已合并进广域黑话资产面板。
        panel = Path("webui/frontend/src/pages/jargon/GlobalJargonPanel.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/jargon.ts").read_text(encoding="utf-8")

        self.assertIn("source: string", api)
        self.assertIn("anchors: EvidenceRef[]", api)
        self.assertIn("ScopeSelect", page)
        self.assertIn("资产版本审计", panel)
        self.assertIn("EvidenceList", page)
        self.assertIn("ObjectDeepLink", page)
        self.assertIn("catalog_sync_command_unavailable", panel)


if __name__ == "__main__":
    unittest.main()
