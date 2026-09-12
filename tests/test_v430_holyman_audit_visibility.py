import asyncio
import json
import sqlite3
import sys
import tempfile
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


class V430HolymanAuditVisibilityTest(unittest.TestCase):
    def _assets_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        assets_dir = Path(tmp.name)
        files = {
            "phrases.json": {
                "_version": "local-test",
                "v我50": {
                    "meaning": "疯狂星期四转账梗，只作为理解参考。",
                    "category": "kfc",
                    "kind": "curated_phrase",
                    "layer": "catchphrase",
                    "reference_only": True,
                    "runtime_match": True,
                },
            },
            "concepts.json": [{"title": "抽象话术", "summary": "只读文化概念", "source": "internet-culture.md"}],
            "examples.json": [{"text": "v我50", "linked_terms": ["v我50"], "source": "iconic.md"}],
            "corpus.json": [{"text": "原始语料", "source": "神言.txt", "line": 1}],
            "candidates.json": [{"id": "holyman-candidate-0001", "word": "候选词", "status": "pending_review"}],
            "blocked.json": {"屏蔽词": "manual_block"},
            "manifest.json": {
                "repo": "xunxiing/holyman-skills",
                "files": [
                    {"path": "神人.skill/SKILL.md", "parse_status": "ok"},
                    {"path": "神言.txt", "parse_status": "ok"},
                ],
            },
            "quality_report.json": {
                "status": "ready",
                "declared_corpus_count": 365,
                "parsed_corpus_count": 1,
                "errors": {},
            },
        }
        for name, payload in files.items():
            (assets_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return assets_dir

    def test_holyman_api_exposes_manifest_and_quality_report_for_audit(self):
        import importlib

        jargon_bp_mod = importlib.import_module("webui.blueprints.jargon")
        assets_dir = self._assets_dir()
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)

        async def _fake_update_check(force=False):
            return {
                "remote_version": "remote-test",
                "asset_status": "ready",
                "content_hash": "abc123",
                "content_count": 1,
                "has_update": False,
                "checked_at": "2026-07-06T00:00:00Z",
                "cached": False,
            }

        old_container = jargon_bp_mod.get_container
        old_assets_dir = jargon_bp_mod._resolve_holyman_assets_dir
        old_update_check = jargon_bp_mod._check_holyman_update
        old_jsonify = jargon_bp_mod.jsonify
        jargon_bp_mod.get_container = lambda: types.SimpleNamespace(db=types.SimpleNamespace(conn=conn))
        jargon_bp_mod._resolve_holyman_assets_dir = lambda: assets_dir
        jargon_bp_mod._check_holyman_update = _fake_update_check
        jargon_bp_mod.jsonify = lambda payload: payload
        try:
            payload = asyncio.run(jargon_bp_mod.get_holyman.__wrapped__())
        finally:
            jargon_bp_mod.get_container = old_container
            jargon_bp_mod._resolve_holyman_assets_dir = old_assets_dir
            jargon_bp_mod._check_holyman_update = old_update_check
            jargon_bp_mod.jsonify = old_jsonify

        self.assertEqual(payload["manifest"]["repo"], "xunxiing/holyman-skills")
        self.assertEqual(len(payload["manifest"]["files"]), 2)
        self.assertEqual(payload["manifest_summary"]["source_count"], 2)
        self.assertEqual(payload["manifest_summary"]["parse_statuses"]["ok"], 2)
        self.assertEqual(payload["quality_report"]["status"], "ready")
        self.assertEqual(payload["quality_summary"]["declared_corpus_count"], 365)
        self.assertEqual(payload["quality_summary"]["parsed_corpus_count"], 1)
        self.assertEqual(payload["quality_summary"]["error_count"], 0)

    def test_frontend_exposes_holyman_asset_audit_panel(self):
        # 广域资产审计已与「广域黑话」合并进同一个资产面板，因此断言随之指向该文件。
        page = Path("webui/frontend/src/pages/jargon/GlobalJargonPanel.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/jargon.ts").read_text(encoding="utf-8")

        self.assertIn("manifest?:", api)
        self.assertIn("manifest_summary?:", api)
        self.assertIn("quality_summary?:", api)
        self.assertIn("资产版本审计", page)
        self.assertIn("catalog", page)
        self.assertIn("local_version", page)
        self.assertIn("remote_version", page)
        self.assertIn("quality_summary", page)
        self.assertIn("reference-only", page)
        self.assertIn("runtime-match", page)
        self.assertIn("catalog_sync_command_unavailable", page)


if __name__ == "__main__":
    unittest.main()
