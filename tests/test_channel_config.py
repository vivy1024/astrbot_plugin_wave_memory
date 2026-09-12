import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace


class ChannelConfigTest(unittest.TestCase):
    def test_old_query_and_inject_settings_map_to_channel_defaults(self):
        from services.config.channel_config import build_default_channel_config

        config = build_default_channel_config(
            runtime_mode="full",
            query_cfg={"inject_top_k": 7, "min_similarity": "0.42"},
            inject_cfg={"skip_recent_minutes": 45, "facts_max": 4, "timeline_max": 3, "enable_timeline": True},
        )

        self.assertEqual(config.mode, "full")
        self.assertEqual(config.recent_dedup_minutes, 45)
        self.assertEqual(config.channels["memory"].top_k, 7)
        self.assertAlmostEqual(config.channels["memory"].min_score, 0.42)
        self.assertEqual(config.channels["facts"].max_items, 4)
        # 独立 timeline 通道已退役：旧 inject 开关被接受但不生成通道。
        self.assertNotIn("timeline", config.channels)
        self.assertTrue(config.channels["safety"].enabled)

    def test_retired_timeline_override_is_ignored_for_legacy_configs(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config

        base = build_default_channel_config(runtime_mode="full")
        updated = apply_channel_overrides(
            base,
            {"channels": {"timeline": {"enabled": False, "priority": 1}}},
        )

        self.assertNotIn("timeline", updated.channels)
        self.assertTrue(updated.channels["memory"].enabled)

    def test_timeline_days_defaults_to_zero_and_preserves_explicit_zero(self):
        from services.config.channel_config import build_default_channel_config

        default_config = build_default_channel_config(runtime_mode="full")
        explicit_zero = build_default_channel_config(
            runtime_mode="full",
            inject_cfg={"timeline_days": 0},
        )

        self.assertEqual(default_config.timeline_days, 0)
        self.assertEqual(explicit_zero.timeline_days, 0)
        self.assertEqual(default_config.to_dict()["timeline_days"], 0)
        self.assertEqual(explicit_zero.to_dict()["timeline_days"], 0)
        self.assertEqual(default_config.timeline_decay_half_life_days, 21.0)

    def test_memory_only_disables_advanced_channels_by_default(self):
        from services.config.channel_config import build_default_channel_config

        config = build_default_channel_config(runtime_mode="memory_only")

        self.assertTrue(config.channels["memory"].enabled)
        self.assertTrue(config.channels["safety"].enabled)
        for name in ["persona", "belief", "jargon", "fewshot", "book_lore", "affinity"]:
            self.assertFalse(config.channels[name].enabled, name)
            self.assertNotIn("memory_only", config.channels[name].modes, name)

    def test_private_scope_forces_safe_channel_and_query_envelope(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.config.channel_config import build_channel_config_from_plugin_config

        scope = RuntimeScope(
            "bot-alpha", "private", SessionRef("qq:private:u", "qq", "private", "u")
        )
        config = build_channel_config_from_plugin_config(
            {
                "Query_Settings": {"enable_epa": True, "enable_residual_pyramid": True},
                "Channel_Settings": {
                    "trace_enabled": True,
                    "channels": {
                        "timeline": {"enabled": True},
                        "facts": {"enabled": True},
                        "persona": {"enabled": True},
                        "fts5": {"enabled": True},
                    },
                    "layers": {
                        "session": [{
                            "selector": {"bot_id": "bot-alpha", "session_id": "qq:private:u", "visibility": "private"},
                            "patch": {
                                "trace_enabled": True,
                                "query_options": {"stages": {"epa": True, "spike": True}},
                                "channels": {"timeline": {"enabled": True}, "belief": {"enabled": True}},
                            },
                        }],
                    },
                },            },
            scope=scope,
        )

        self.assertTrue(config.channels["safety"].enabled)
        self.assertTrue(config.channels["memory"].enabled)
        self.assertTrue(config.channels["fts5"].enabled)
        for name in ("facts", "persona", "belief", "jargon", "fewshot", "book_lore", "affinity", "soul_state"):
            self.assertFalse(config.channels[name].enabled, name)
        self.assertNotIn("timeline", config.channels)
        self.assertFalse(config.trace_enabled)
        self.assertEqual(config.query_stages, {"epa": False, "pyramid": False, "spike": False, "geodesic": False})
        self.assertFalse(config.memory_recall["enable_shotgun"])

    def test_valid_hot_overrides_apply_without_losing_old_defaults(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config

        base = build_default_channel_config(runtime_mode="full", query_cfg={"inject_top_k": 5})
        updated = apply_channel_overrides(
            base,
            {
                "channels": {
                    "memory": {"priority": 120, "top_k": 9, "timeout_ms": 250, "min_score": 0.55},
                    "facts": {"enabled": False},
                }
            },
        )

        self.assertEqual(updated.channels["memory"].priority, 120)
        self.assertEqual(updated.channels["memory"].top_k, 9)
        self.assertEqual(updated.channels["memory"].timeout_ms, 250)
        self.assertAlmostEqual(updated.channels["memory"].min_score, 0.55)
        self.assertFalse(updated.channels["facts"].enabled)
        self.assertEqual(updated.channels["fts5"].max_items, base.channels["fts5"].max_items)

    def test_invalid_hot_config_is_rejected(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config

        base = build_default_channel_config(runtime_mode="full")
        bad_overrides = [
            {"channels": {"unknown": {"enabled": True}}},
            {"channels": {"memory": {"token_budget": -1}}},
            {"channels": {"memory": {"timeout_ms": 999999}}},
            {"channels": {"safety": {"enabled": False}}},
            {"channels": {"safety": {"modes": []}}},
            {"channels": {"memory": {"modes": ["full"]}}},
        ]

        for override in bad_overrides:
            with self.assertRaises(ValueError, msg=str(override)):
                apply_channel_overrides(base, override)

    def test_config_exports_webui_payload(self):
        from services.config.channel_config import build_default_channel_config

        payload = build_default_channel_config(runtime_mode="compat_only").to_dict()

        self.assertEqual(payload["mode"], "compat_only")
        self.assertIn("channels", payload)
        self.assertFalse(payload["channels"]["memory"]["enabled"])
        self.assertTrue(payload["channels"]["safety"]["enabled"])
        self.assertEqual(payload["timeline_decay_half_life_days"], 21.0)

    def test_build_channel_config_from_plugin_config_applies_stored_overrides(self):
        from services.config.channel_config import build_channel_config_from_plugin_config

        config = build_channel_config_from_plugin_config({
            "Runtime_Settings": {"runtime_mode": "full"},
            "Query_Settings": {"inject_top_k": 5},
            "Inject_Settings": {"skip_recent_minutes": 30},
            "Channel_Settings": {"channels": {"memory": {"top_k": 9, "timeout_ms": 250}}},
        })

        self.assertEqual(config.channels["memory"].top_k, 9)
        self.assertEqual(config.channels["memory"].timeout_ms, 250)

    def test_channel_config_diff_reports_changed_fields(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config, channel_config_diff

        base = build_default_channel_config(runtime_mode="full")
        updated = apply_channel_overrides(base, {"channels": {"memory": {"top_k": 8}}, "recent_dedup_minutes": 10})
        diff = channel_config_diff(base, updated)
        paths = {item["path"] for item in diff}

        self.assertIn("channels.memory.top_k", paths)
        self.assertIn("recent_dedup_minutes", paths)

    def test_channel_config_api_helpers_validate_and_preview_without_applying(self):
        from services.config.channel_config import build_default_channel_config
        from webui.blueprints.channel_config import build_channel_config_payload, validate_channel_config_patch

        current = build_default_channel_config(runtime_mode="full")
        payload = build_channel_config_payload({}, current)
        preview = validate_channel_config_patch({}, {"channels": {"memory": {"top_k": 11}}}, current)
        invalid = validate_channel_config_patch({}, {"channels": {"safety": {"enabled": False}}}, current)

        self.assertEqual(payload["current"]["channels"]["memory"]["top_k"], 5)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["candidate"]["channels"]["memory"]["top_k"], 11)
        self.assertFalse(invalid["ok"])
        self.assertIn("safety channel cannot be disabled", invalid["errors"][0])

    def test_channel_config_payload_exposes_jargon_runtime_status_fields(self):
        from services.config.channel_config import build_default_channel_config
        from webui.blueprints.channel_config import build_channel_config_payload

        current = build_default_channel_config(runtime_mode="full")
        payload = build_channel_config_payload({}, current)
        jargon = payload["current"]["channels"]["jargon"]

        self.assertEqual(jargon["status"], "unknown")
        self.assertEqual(jargon["last_latency_ms"], 0)
        self.assertEqual(jargon["last_hit_count"], 0)

    def test_channel_config_payload_reads_latest_jargon_runtime_status_from_trace_store(self):
        from services.config.channel_config import build_default_channel_config
        from services.injection.channel_base import InjectionResult
        from services.injection.trace_store import InjectionTraceStore
        from webui.blueprints.channel_config import build_channel_config_payload

        store = InjectionTraceStore(sqlite3.connect(":memory:"), retention_days=None, max_rows=None)
        store.ensure_schema()
        store.record(
            {"trace_id": "old", "timestamp": 10, "mode": "full", "message": "old", "status": "ok"},
            [InjectionResult.empty("jargon", latency_ms=12, reason="no match")],
        )
        store.record(
            {"trace_id": "new", "timestamp": 20, "mode": "full", "message": "new", "status": "ok"},
            [
                InjectionResult.hit(
                    "jargon",
                    "[黑话理解参考]\n- v我50 → 疯狂星期四转账梗",
                    items=[{"word": "v我50"}, {"word": "KFC"}],
                    latency_ms=34.6,
                )
            ],
        )

        current = build_default_channel_config(runtime_mode="full")
        try:
            payload = build_channel_config_payload({}, current, trace_store=store)
        except TypeError as exc:
            self.fail(f"build_channel_config_payload should accept trace_store: {exc}")
        jargon = payload["current"]["channels"]["jargon"]

        self.assertEqual(jargon["status"], "hit")
        self.assertEqual(jargon["last_latency_ms"], 35)
        self.assertEqual(jargon["last_hit_count"], 2)

    def test_apply_requires_latest_preflight_and_returns_runtime_revision(self):
        from services.config.channel_config import build_default_channel_config
        from webui.blueprints.channel_config import apply_channel_config_patch, validate_channel_config_patch

        current = build_default_channel_config(runtime_mode="full")
        container = SimpleNamespace(
            plugin_config={},
            injection_channel_config=current,
            injection_channel_config_setter=lambda value: None,
        )
        patch = {"channels": {"memory": {"top_k": 9}}}
        denied = apply_channel_config_patch(container, patch)
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error_code"], "channel_config_confirmation_required")

        preview = validate_channel_config_patch({}, patch, current)
        applied = apply_channel_config_patch(container, {
            **patch,
            "preflight_token": preview["preflight_token"],
            "confirmation": "apply",
        })
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["operation"]["status"], "succeeded")
        self.assertEqual(applied["revision"], preview["preflight_token"])
        self.assertIn("config_revision=", applied["verification_url"])

    def test_timeline_half_life_falls_back_and_strict_overrides_round_trip(self):
        from services.config.channel_config import (
            apply_channel_overrides,
            build_channel_config_from_plugin_config,
            build_default_channel_config,
            _config_set_from_payload,
        )

        for invalid in (None, "", 0, -1, True, False, "abc", float("nan"), float("inf")):
            config = build_default_channel_config(
                runtime_mode="full",
                inject_cfg={"timeline_decay_half_life_days": invalid},
            )
            self.assertEqual(config.timeline_decay_half_life_days, 21.0, invalid)

        valid = apply_channel_overrides(
            build_default_channel_config(runtime_mode="full"),
            {"timeline_decay_half_life_days": "14"},
        )
        self.assertEqual(valid.timeline_decay_half_life_days, 14.0)
        restored = _config_set_from_payload(valid.to_dict())
        self.assertEqual(restored.timeline_decay_half_life_days, 14.0)
        for invalid in (0, -3, True, "nope", float("nan")):
            with self.assertRaises(ValueError):
                apply_channel_overrides(
                    valid,
                    {"timeline_decay_half_life_days": invalid},
                )
        plugin = build_channel_config_from_plugin_config({
            "Runtime_Settings": {"runtime_mode": "full"},
            "Inject_Settings": {"timeline_decay_half_life_days": "not-a-number"},
            "Channel_Settings": {"timeline_decay_half_life_days": 7},
        })
        self.assertEqual(plugin.timeline_decay_half_life_days, 7.0)

    def test_layer_patch_is_fully_validated_and_explicit_false_zero_are_preserved(self):
        from services.config.channel_config import apply_channel_overrides, build_default_channel_config

        base = build_default_channel_config(runtime_mode="full")
        candidate = apply_channel_overrides(
            base,
            {
                "trace_enabled": False,
                "recent_dedup_minutes": 0,
                "timeline_days": 0,
                "channels": {"memory": {"top_k": 0, "enabled": False}},
                "query_options": {"stages": {"epa": False}, "params": {"pyramid_top_k": 1}},
                "memory_recall": {"enable_shotgun": False, "skip_recent_minutes": 0},
            },
        )
        self.assertIs(candidate.trace_enabled, False)
        self.assertEqual(candidate.recent_dedup_minutes, 0)
        self.assertEqual(candidate.timeline_days, 0)
        self.assertIs(candidate.channels["memory"].enabled, False)
        self.assertEqual(candidate.channels["memory"].top_k, 0)
        self.assertIs(candidate.query_stages["epa"], False)
        self.assertEqual(candidate.query_params["pyramid_top_k"], 1)
        self.assertIs(candidate.memory_recall["enable_shotgun"], False)

        for invalid in (
            {"unknown_root": True},
            {"channels": {"memory": {"top_k": ""}}},
            {"channels": {"memory": {"enabled": "not-bool"}}},
            {"query_options": {"stages": {"unknown": True}}},
            {"memory_recall": {"unknown": 1}},
        ):
            with self.assertRaises(ValueError, msg=str(invalid)):
                apply_channel_overrides(base, invalid)

    def test_scoped_patch_apply_persists_layer_without_replacing_global_runtime_config(self):
        from services.config.channel_config import build_default_channel_config
        from domain.scope import RuntimeScope, SessionRef
        from webui.blueprints.channel_config import apply_channel_config_patch, validate_channel_config_patch

        scope = RuntimeScope(
            bot_id="bot-alpha",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:u1",
        )
        current = build_default_channel_config(runtime_mode="full")
        container = SimpleNamespace(
            plugin_config={},
            injection_channel_config=current,
            injection_channel_config_setter=lambda value: self.fail("scoped patch must not replace global runtime config"),
        )
        body = {
            "layer": "session",
            "scope": scope.to_dict(),
            "channels": {"memory": {"top_k": 2}},
        }
        preview = validate_channel_config_patch({}, body, current, expected_scope=scope)
        self.assertTrue(preview["ok"])
        applied = apply_channel_config_patch(container, {
            **body,
            "preflight_token": preview["preflight_token"],
            "confirmation": "apply",
        })

        self.assertTrue(applied["ok"])
        self.assertEqual(applied["layer"], "session")
        self.assertIs(container.injection_channel_config, current)
        entries = container.plugin_config["Channel_Settings"]["layers"]["session"]
        self.assertEqual(entries[0]["selector"]["session_id"], "test:group:g1")
        self.assertEqual(entries[0]["patch"]["channels"]["memory"]["top_k"], 2)

    def test_frontend_exposes_channel_config_page(self):
        page = Path("webui/frontend/src/pages/channels/ChannelConfigPage.tsx").read_text(encoding="utf-8")
        table = Path("webui/frontend/src/pages/channels/ChannelConfigTable.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/channels.ts").read_text(encoding="utf-8")
        routes = Path("webui/frontend/src/app/routes.tsx").read_text(encoding="utf-8")

        self.assertIn("通道配置", page)
        self.assertIn("validateChannelConfig", page)
        self.assertIn("applyChannelConfig", page)
        self.assertIn("resetChannelConfigDefaults", page)
        self.assertIn("安全通道", page)
        self.assertIn("disabled={safety}", table)
        self.assertIn("运行状态", table)
        self.assertIn("last_hit_count", table)
        self.assertIn("last_latency_ms", table)
        self.assertIn("默认 ", table)
        self.assertIn("保存 ", table)
        self.assertIn("生效 ", table)
        self.assertIn("status?: string", api)
        self.assertIn("last_latency_ms?: number", api)
        self.assertIn("last_hit_count?: number", api)
        self.assertIn("/api/config/channels", api)
        self.assertIn("/channels", routes)

    def test_stage5_channel_config_uses_server_descriptors_and_revision_verification(self):
        page = Path("webui/frontend/src/pages/channels/ChannelConfigPage.tsx").read_text(encoding="utf-8")
        table = Path("webui/frontend/src/pages/channels/ChannelConfigTable.tsx").read_text(encoding="utf-8")
        backend = Path("webui/blueprints/channel_config.py").read_text(encoding="utf-8")

        for marker in ("descriptors", "purpose", "dependencies", "risk", "management_route", "verification_filters"):
            self.assertIn(marker, backend)
        for marker in ("revision", "effectiveSince", "verificationUrl", "operation?.status === 'succeeded'"):
            self.assertIn(marker, page)
        for marker in ("descriptorMap", "默认 ", "保存 ", "生效 ", "apply_mode"):
            self.assertIn(marker, table)
        self.assertNotIn("channelMetadata", table)


if __name__ == "__main__":
    unittest.main()

