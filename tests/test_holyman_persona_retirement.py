"""holyman_persona 风格通道下线后的旧配置兼容。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from domain.scope import COMMAND_SCOPE_MATRIX, RuntimeScope, SessionRef
from services.config.channel_config import (
    KNOWN_CHANNELS,
    RETIRED_CHANNELS,
    _config_set_from_payload,
    apply_channel_overrides,
    build_channel_config_from_plugin_config,
    build_default_channel_config,
    resolve_effective_channel_config,
)

ROOT = Path(__file__).resolve().parents[1]
GROUP = RuntimeScope("bot-test", "group", SessionRef("qq:group:20000001", "qq", "group", "20000001"))
LEGACY_PATCH = {"enabled": True, "priority": 48, "max_items": 6, "token_budget": 900, "timeout_ms": 200}


def test_channel_is_retired_everywhere():
    assert "holyman_persona" not in KNOWN_CHANNELS
    assert "holyman_persona" in RETIRED_CHANNELS
    assert "holyman_persona.inject" not in COMMAND_SCOPE_MATRIX
    assert not (ROOT / "services" / "injection" / "channels" / "holyman_persona.py").exists()
    assert not (ROOT / "services" / "jargon" / "holyman_persona.py").exists()
    from services.injection.channels.safety import is_channel_allowed_in_mode

    assert not is_channel_allowed_in_mode("holyman_persona", "full")


def test_schema_keeps_deprecated_switch():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    item = schema["Inject_Settings"]["items"]["enable_holyman_persona"]
    assert item["type"] == "bool" and item["default"] is False
    assert "已废弃" in item["description"]


def test_legacy_enable_switch_true_still_loads():
    config = build_channel_config_from_plugin_config({"Inject_Settings": {"enable_holyman_persona": True}})
    assert "holyman_persona" not in config.channels
    assert config.channels["meme_hint"].enabled is False


def test_saved_channel_settings_with_holyman_persona_still_load():
    plugin_config = {
        "Inject_Settings": {"enable_holyman_persona": True},
        "Channel_Settings": {
            "channels": {"holyman_persona": LEGACY_PATCH, "jargon": {"enabled": True}},
            "layers": {
                "bot": [{
                    "selector": {"bot_id": "bot-test"},
                    "patch": {"channels": {"holyman_persona": {"enabled": False}}},
                }],
            },
        },
    }
    flat = build_channel_config_from_plugin_config(plugin_config)
    assert "holyman_persona" not in flat.channels
    scoped, effective = resolve_effective_channel_config(
        plugin_config,
        scope=GROUP,
        bot_channels={"holyman_persona": {"enabled": True}, "meme_hint": {"enabled": True}},
    )
    assert "holyman_persona" not in scoped.channels
    assert scoped.channels["meme_hint"].enabled is True
    assert "holyman_persona" not in effective.values["channels"]


def test_request_override_with_retired_channel_is_ignored():
    updated = apply_channel_overrides(build_default_channel_config(), {"channels": {"holyman_persona": {"enabled": True}}})
    assert "holyman_persona" not in updated.channels


def test_historical_effective_payload_with_holyman_persona_and_without_meme_hint_loads():
    payload = build_default_channel_config().to_dict()
    payload["channels"].pop("meme_hint")
    payload["channels"]["holyman_persona"] = {"name": "holyman_persona", **LEGACY_PATCH, "top_k": None, "min_score": None, "modes": ["full"]}
    restored = _config_set_from_payload(payload)
    assert "holyman_persona" not in restored.channels
    assert asdict(restored.channels["meme_hint"]) == asdict(build_default_channel_config().channels["meme_hint"])
