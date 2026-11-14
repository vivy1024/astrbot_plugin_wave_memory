import unittest
from pathlib import Path


class CompatibilityPageTest(unittest.TestCase):
    def test_compatibility_payload_defaults_without_other_plugins(self):
        from webui.blueprints.compatibility import build_compatibility_payload

        payload = build_compatibility_payload({"Runtime_Settings": {"runtime_mode": "compat_only"}})

        self.assertEqual(payload["runtime"]["mode"], "compat_only")
        self.assertFalse(payload["facade"]["enabled"])
        self.assertIn("recall_long_term_memory", payload["tool_aliases"])
        self.assertEqual(payload["detected_plugins"], [])
        self.assertEqual(payload["duplicate_warnings"], [])

    def test_compatibility_payload_warns_duplicate_memory_plugins(self):
        from webui.blueprints.compatibility import build_compatibility_payload

        payload = build_compatibility_payload(
            {"Runtime_Settings": {"runtime_mode": "full"}},
            detection_result=[
                {"id": "astrbot_plugin_livingmemory", "name": "LivingMemory", "active": True},
                {"id": "astrbot_plugin_self_learning", "name": "SelfLearning", "active": True},
            ],
        )

        self.assertEqual(len(payload["duplicate_warnings"]), 2)
        self.assertIn("重复记忆插件", payload["duplicate_warnings"][0]["message"])

    def test_frontend_exposes_compatibility_mode_page(self):
        page = Path("webui/frontend/src/pages/review/CompatibilityPage.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/compatibility.ts").read_text(encoding="utf-8")
        routes = Path("webui/frontend/src/app/routes.tsx").read_text(encoding="utf-8")

        self.assertIn("Compatibility", page)
        self.assertIn("getCompatibilityStatus", page)
        self.assertIn("重复记忆插件风险", page)
        self.assertIn("recall_long_term_memory", api)
        self.assertIn("memorize_long_term_memory", api)
        self.assertIn("/api/compat/status", api)
        self.assertIn("/compatibility", routes)

    def test_container_exposes_livingmemory_facade_status(self):
        from webui.container import ServiceContainer

        ServiceContainer.reset()
        self.addCleanup(ServiceContainer.reset)
        facade = object()
        container = ServiceContainer()

        container.initialize(
            db=None,
            query_engine=None,
            embedding_service=None,
            memory_index=None,
            tag_index=None,
            cooccurrence=None,
            livingmemory_facade=facade,
            livingmemory_facade_enabled=True,
        )

        self.assertIs(container.livingmemory_facade, facade)
        self.assertTrue(container.livingmemory_facade_enabled)

    def test_schema_removed_legacy_compatibility_section(self):
        import json

        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("Compatibility_Settings", schema)


if __name__ == "__main__":
    unittest.main()
