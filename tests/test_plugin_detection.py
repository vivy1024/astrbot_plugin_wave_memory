from plugin_source import plugin_source_text
import unittest
from pathlib import Path


class FakeStar:
    def __init__(self, name, display_name=None, activated=True, root_dir_name=None):
        self.name = name
        self.display_name = display_name
        self.activated = activated
        self.root_dir_name = root_dir_name or name


class BrokenPlugin:
    @property
    def name(self):
        raise RuntimeError("broken metadata")


class PluginDetectionTest(unittest.TestCase):
    def test_detects_known_memory_plugins_from_star_metadata_like_sources(self):
        from services.compat.plugin_detection import detect_memory_plugins

        detected = detect_memory_plugins([
            FakeStar("astrbot_plugin_livingmemory", "LivingMemory", True),
            FakeStar("astrbot_plugin_self_learning", "SelfLearning", False),
            FakeStar("ordinary_plugin", "Ordinary", True),
        ])

        self.assertEqual([item["id"] for item in detected], ["astrbot_plugin_livingmemory", "astrbot_plugin_self_learning"])
        self.assertTrue(detected[0]["active"])
        self.assertFalse(detected[1]["active"])
        self.assertEqual(detected[0]["name"], "LivingMemory")

    def test_detects_known_memory_plugins_from_dict_sources(self):
        from services.compat.plugin_detection import detect_memory_plugins

        detected = detect_memory_plugins({
            "astrbot_plugin_chatplus": {"display_name": "ChatPlus", "enabled": True},
            "not_memory": {"display_name": "Other", "enabled": True},
        })

        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0]["id"], "astrbot_plugin_chatplus")
        self.assertEqual(detected[0]["name"], "ChatPlus")
        self.assertTrue(detected[0]["active"])

    def test_detection_failure_is_non_fatal(self):
        from services.compat.plugin_detection import detect_memory_plugins

        self.assertEqual(detect_memory_plugins([BrokenPlugin()]), [])

    def test_duplicate_warnings_only_include_active_memory_plugins(self):
        from services.compat.plugin_detection import build_duplicate_memory_warnings, detect_memory_plugins

        detected = detect_memory_plugins([
            FakeStar("astrbot_plugin_livingmemory", "LivingMemory", True),
            FakeStar("astrbot_plugin_self_learning", "SelfLearning", False),
        ])
        warnings = build_duplicate_memory_warnings(detected)

        self.assertEqual(len(warnings), 1)
        self.assertIn("重复记忆插件", warnings[0]["message"])
        self.assertEqual(warnings[0]["plugin_id"], "astrbot_plugin_livingmemory")

    def test_webui_payload_uses_detection_result_for_duplicate_warnings(self):
        from services.compat.plugin_detection import detect_memory_plugins
        from webui.blueprints.compatibility import build_compatibility_payload

        detected = detect_memory_plugins([FakeStar("astrbot_plugin_chatplus", "ChatPlus", True)])
        payload = build_compatibility_payload({}, detected)

        self.assertEqual(payload["detected_plugins"][0]["id"], "astrbot_plugin_chatplus")
        self.assertEqual(len(payload["duplicate_warnings"]), 1)

    def test_main_source_logs_and_passes_detected_plugins_to_webui(self):
        source = plugin_source_text()

        self.assertIn("detect_memory_plugins(context=self.context)", source)
        self.assertIn("self.detected_memory_plugins", source)
        self.assertIn("detected_memory_plugins=self.detected_memory_plugins", source)


if __name__ == "__main__":
    unittest.main()
