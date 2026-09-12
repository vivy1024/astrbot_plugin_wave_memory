import json
import unittest
from pathlib import Path


class RuntimeModeTest(unittest.TestCase):
    def test_missing_runtime_settings_defaults_to_full_mode(self):
        from services.runtime_mode import resolve_runtime_mode

        mode = resolve_runtime_mode({})

        self.assertEqual(mode.mode, "full")
        self.assertTrue(mode.advanced_query_default)
        self.assertTrue(mode.native_injection_default)
        self.assertEqual(mode.disabled_capabilities, [])

    def test_memory_only_defaults_advanced_query_flags_to_disabled(self):
        from services.runtime_mode import effective_query_feature, resolve_runtime_mode

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "memory_only"}})

        self.assertEqual(mode.mode, "memory_only")
        self.assertFalse(mode.advanced_query_default)
        self.assertTrue(mode.native_injection_default)
        self.assertFalse(effective_query_feature({}, "enable_spike_routing", mode))
        self.assertFalse(effective_query_feature({}, "enable_residual_pyramid", mode))
        self.assertFalse(effective_query_feature({}, "enable_epa", mode))
        self.assertFalse(effective_query_feature({}, "enable_geodesic_rerank", mode))
        self.assertIn("advanced_query", mode.disabled_capabilities)

    def test_memory_only_disables_advanced_runtime_capabilities_by_default(self):
        from services.runtime_mode import resolve_runtime_mode, runtime_capability_enabled

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "memory_only"}})

        disabled = [
            "affinity", "persona", "mood", "dream", "consolidation",
            "metathinking", "belief", "belief_emergence", "concern",
            "mood_trajectory", "subjective_time", "desire", "jargon",
            "fewshot", "book_lore", "study", "self_reflect",
            "book_lore_tools", "affinity_tools", "persona_tools",
        ]
        for capability in disabled:
            self.assertFalse(runtime_capability_enabled(mode, capability, configured=True), capability)

        enabled = ["message_capture", "writer_queue", "vector_query", "basic_injection", "injection_trace", "memory_tools", "compat_facade"]
        for capability in enabled:
            self.assertTrue(runtime_capability_enabled(mode, capability, configured=True), capability)

    def test_main_uses_runtime_capability_gates_for_memory_only_services(self):
        source = Path("main.py").read_text(encoding="utf-8")

        for capability in ["affinity", "mood", "dream", "consolidation", "book_lore", "jargon", "fewshot", "metathinking", "belief", "self_reflect"]:
            self.assertIn(f'runtime_capability_enabled(self.runtime_mode, "{capability}"', source)

    def test_full_mode_preserves_existing_query_flags_and_repairs_advanced_defaults(self):
        from services.runtime_mode import effective_query_feature, resolve_runtime_mode, should_self_heal_advanced_query

        mode = resolve_runtime_mode({"Query_Settings": {"enable_spike_routing": False}})

        self.assertEqual(mode.mode, "full")
        self.assertFalse(effective_query_feature({"enable_spike_routing": False}, "enable_spike_routing", mode))
        self.assertTrue(effective_query_feature({}, "enable_geodesic_rerank", mode))
        self.assertTrue(should_self_heal_advanced_query(mode))

    def test_compat_only_exposes_web_payload_and_disables_native_injection(self):
        from services.runtime_mode import resolve_runtime_mode, should_self_heal_advanced_query

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "compat_only"}})
        payload = mode.to_web_payload()

        self.assertEqual(payload["mode"], "compat_only")
        self.assertFalse(payload["native_injection_default"])
        self.assertIn("native_injection", payload["disabled_capabilities"])
        self.assertFalse(should_self_heal_advanced_query(mode))

    def test_compat_only_ignores_legacy_auto_inject_true_unless_explicitly_enabled(self):
        from services.runtime_mode import effective_native_injection_enabled, resolve_runtime_mode

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "compat_only"}})

        self.assertFalse(effective_native_injection_enabled(
            {"enable_auto_inject": True},
            mode,
            compat_cfg={},
        ))
        self.assertTrue(effective_native_injection_enabled(
            {"enable_auto_inject": True},
            mode,
            compat_cfg={"compat_only_auto_inject_enabled": True},
        ))

    def test_compat_only_keeps_facade_and_aliases_but_disables_native_memory_tools(self):
        from services.runtime_mode import resolve_runtime_mode, runtime_capability_enabled

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "compat_only"}})

        self.assertTrue(runtime_capability_enabled(mode, "writer_queue", True))
        self.assertTrue(runtime_capability_enabled(mode, "vector_query", True))
        self.assertTrue(runtime_capability_enabled(mode, "compat_facade", True))
        self.assertTrue(runtime_capability_enabled(mode, "compat_tool_aliases", True))
        self.assertFalse(runtime_capability_enabled(mode, "memory_tools", True))
        self.assertFalse(runtime_capability_enabled(mode, "agent_feedback_tools", True))

    def test_main_uses_compat_only_native_injection_and_tool_gates(self):
        source = Path("main.py").read_text(encoding="utf-8")

        self.assertIn("effective_native_injection_enabled", source)
        self.assertIn('runtime_capability_enabled(self.runtime_mode, "memory_tools"', source)
        self.assertIn('runtime_capability_enabled(self.runtime_mode, "agent_feedback_tools"', source)

    def test_unknown_mode_falls_back_to_full_instead_of_crashing(self):
        from services.runtime_mode import resolve_runtime_mode

        mode = resolve_runtime_mode({"Runtime_Settings": {"runtime_mode": "unexpected"}})

        self.assertEqual(mode.mode, "full")
        self.assertEqual(mode.source_value, "unexpected")

    def test_schema_exposes_runtime_mode_setting(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("Runtime_Settings", schema)
        self.assertEqual(schema["Runtime_Settings"]["items"]["runtime_mode"]["default"], "full")
        self.assertIn("full", schema["Runtime_Settings"]["items"]["runtime_mode"]["hint"])
        self.assertIn("memory_only", schema["Runtime_Settings"]["items"]["runtime_mode"]["hint"])
        self.assertIn("compat_only", schema["Runtime_Settings"]["items"]["runtime_mode"]["hint"])


if __name__ == "__main__":
    unittest.main()
