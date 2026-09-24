"""Bot 管理 API：新增第三个 Bot 立即生效、版本冲突、导入导出。"""

from __future__ import annotations

import pytest
from quart import Quart

from domain import bot_identity
from engine.db.bot_profile_repo import BotProfileRepo
from engine.db.connection import ConnectionManager
from services.bot_registry import BotRegistry
from webui.blueprints.bots import bots_bp
from webui.container import get_container


@pytest.fixture
def registry(tmp_path):
    cm = ConnectionManager(str(tmp_path / "bots.sqlite3"))
    reg = BotRegistry({"MetaThinking_Bot1": {"qq_id": "10001", "name": "甲", "db_id": "bot_a"}})
    reg.attach(BotProfileRepo(cm), connection=cm)
    container = get_container()
    container.bot_registry = reg
    container.password = ""
    yield reg
    container.bot_registry = None
    bot_identity.publish()
    cm.close()


@pytest.fixture
def client(registry):
    app = Quart(__name__)
    app.register_blueprint(bots_bp)
    return app.test_client()


@pytest.mark.asyncio
async def test_create_third_bot_hot_applies(client, registry):
    res = await client.get("/api/bots")
    data = await res.get_json()
    assert [item["db_id"] for item in data["items"]] == ["bot_a"]

    payload = {
        "version": 0,
        "item": {
            "name": "丙",
            "qq_id": "30003",
            "bindings": [{"host": "cortico", "deployment": "live-c"}],
            "persona": {"lore_lines": ["设定一"]},
        },
    }
    res = await client.put("/api/bots/bot_c", json=payload)
    assert res.status_code == 200, await res.get_data()
    created = (await res.get_json())["item"]
    assert created["version"] == 1 and created["origin"] == "webui"
    # 立即生效：注册表共享字典、Cortico 部署解析、身份快照都已更新。
    assert registry.profiles["30003"].db_id == "bot_c"
    assert registry.resolve(deployment="live-c").db_id == "bot_c"
    assert "丙" in bot_identity.identity_terms()

    stale = await client.put("/api/bots/bot_c", json=payload)
    assert stale.status_code == 409

    res = await client.post("/api/bots/bot_c/enabled", json={"enabled": False, "version": 1})
    assert res.status_code == 200
    assert "30003" not in registry.profiles


@pytest.mark.asyncio
async def test_validation_and_conflicts(client):
    res = await client.put("/api/bots/123", json={"version": 0, "item": {"name": "x"}})
    assert res.status_code == 400
    res = await client.put("/api/bots/bot_x", json={"item": {"name": "x"}})
    assert (await res.get_json())["error"]["code"] == "version_required"
    res = await client.put("/api/bots/bot_x", json={"version": 0, "item": {"name": "x", "qq_id": "10001"}})
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_export_import_round_trip(client, registry):
    exported = await (await client.get("/api/bots/export")).get_json()
    assert exported["items"][0]["db_id"] == "bot_a"
    item = dict(exported["items"][0], db_id="bot_b", qq_id="20002", name="乙")
    res = await client.post("/api/bots/import", json={"items": [item]})
    body = await res.get_json()
    assert body["imported"] == ["bot_b"] and body["ok"]
    assert registry.get("bot_b").name == "乙"
