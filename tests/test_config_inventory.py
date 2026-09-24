"""配置总览与热参数数据库覆盖。"""

from __future__ import annotations

from types import SimpleNamespace

from domain.bot_profile import BotProfile
from engine.db.config_override_repo import ConfigOverrideRepo
from engine.db.connection import ConnectionManager
from services.config.channel_config import build_channel_config_from_plugin_config, build_default_channel_config
from services.config.inventory import bool_flip_suspects, build_inventory


def test_override_repo_round_trip(tmp_path):
    cm = ConnectionManager(str(tmp_path / "cfg.sqlite3"))
    repo = ConfigOverrideRepo(cm)
    repo.set_many({"ingress.debounce_seconds": 2.5, "injection.slow_warning_ms": 3000}, updated_by="t")
    repo.set_many({"ingress.debounce_seconds": 3.0})
    assert repo.values() == {"ingress.debounce_seconds": 3.0, "injection.slow_warning_ms": 3000}
    assert repo.delete(["injection.slow_warning_ms"]) == 1
    cm.close()


def test_inventory_marks_layers_and_suspects():
    schema = {
        "Query_Settings": {
            "type": "object",
            "items": {
                "enable_auto_inject": {"type": "bool", "default": True, "description": "自动注入"},
                "inject_top_k": {"type": "int", "default": 5, "description": "条数"},
            },
        }
    }
    saved = {"Query_Settings": {"enable_auto_inject": False}}
    settings_payload = {
        "groups": [{
            "key": "Query_Settings", "kind": "object",
            "items": [
                {"key": "enable_auto_inject", "default": True, "saved": False, "effective": False, "source": "plugin_config", "apply_mode": "restart"},
                {"key": "inject_top_k", "default": 5, "saved": None, "effective": 5, "source": "schema_default_missing", "apply_mode": "restart"},
            ],
        }]
    }
    hot = [
        {"key": "ingress.debounce_seconds", "default": 4.0, "saved": 2.5, "effective": 2.5, "source": "wavememory_db.config_overrides"},
        {"key": "spike.max_hops", "default": 4, "saved": 4, "effective": 4, "source": "builtin_default"},
    ]
    defaults = build_default_channel_config()
    effective = build_channel_config_from_plugin_config({"Channel_Settings": {"channels": {"jargon": {"enabled": False}}}})
    profile = BotProfile.from_dict({"db_id": "yushu", "name": "羽书", "channels": {"fewshot": {"enabled": False}}})
    inv = build_inventory(
        settings_payload=settings_payload,
        hot_params=hot,
        default_channel_config=defaults,
        effective_channel_config=effective,
        bot_profiles=[profile],
        schema=schema,
        saved_config=saved,
    )
    by_key = {row["key"]: row for row in inv["items"]}
    assert by_key["Query_Settings.enable_auto_inject"]["layer"] == "static"
    assert by_key["Query_Settings.inject_top_k"]["layer"] == "builtin"
    assert by_key["hot:ingress.debounce_seconds"]["layer"] == "override"
    assert by_key["hot:spike.max_hops"]["changed"] is False
    assert by_key["bot:yushu.channels.fewshot.enabled"]["scope"] == "bot:yushu"
    assert inv["suspects"][0]["key"] == "Query_Settings.enable_auto_inject"
    assert inv["revision"].startswith("inv-")
    assert inv["precedence"] == ["builtin", "static", "bot", "override"]


def test_bool_flip_suspects_ignores_default_false():
    schema = {"X": {"type": "object", "items": {"a": {"type": "bool", "default": False}}}}
    assert bool_flip_suspects(schema, {"X": {"a": False}}) == []


import pytest


@pytest.mark.asyncio
async def test_inventory_endpoint_serves_real_schema():
    from quart import Quart

    from webui.blueprints.config import config_bp
    from webui.container import get_container

    container = get_container()
    container.plugin_config = {"Query_Settings": {"inject_top_k": 7}}
    container.password = ""
    app = Quart(__name__)
    app.register_blueprint(config_bp)
    try:
        res = await app.test_client().get("/api/config/inventory")
        assert res.status_code == 200, await res.get_data()
        data = await res.get_json()
        keys = {row["key"] for row in data["items"]}
        assert "Query_Settings.inject_top_k" in keys
        assert "hot:ingress.debounce_seconds" in keys
        assert any(key.startswith("channel:memory.") for key in keys)
    finally:
        container.plugin_config = {}
