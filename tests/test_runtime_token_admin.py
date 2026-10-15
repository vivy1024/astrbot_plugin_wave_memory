"""Runtime API 令牌：来源优先级（环境变量 > 管理台 > 公开默认值）、管理台修改立即生效、调用情况统计。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart

from engine.db.config_override_repo import ConfigOverrideRepo
from engine.db.connection import ConnectionManager
from webui.blueprints import runtime as runtime_api
from webui.blueprints.config import config_bp
from webui.blueprints.runtime import runtime_bp
from webui.container import ServiceContainer


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv(runtime_api.TOKEN_ENV, raising=False)
    ServiceContainer.reset()
    container = ServiceContainer()
    cm = ConnectionManager(str(tmp_path / "wm.sqlite3"))
    container.password = ""
    container.db = SimpleNamespace(conn=cm, config_overrides=ConfigOverrideRepo(cm))
    runtime_api.invalidate_token_cache()
    for key in runtime_api._usage:
        runtime_api._usage[key] = 0 if key in ("accepted", "rejected") else (None if key.endswith("_at") else "")
    app = Quart(__name__)
    app.register_blueprint(runtime_bp)
    app.register_blueprint(config_bp)
    yield app
    runtime_api.invalidate_token_cache()
    ServiceContainer.reset()
    cm.close()


async def _caps(client, token: str) -> int:
    res = await client.get("/api/runtime/v1/capabilities", headers={"Authorization": f"Bearer {token}"})
    return res.status_code


@pytest.mark.asyncio
async def test_default_token_still_works_and_is_flagged(app):
    client = app.test_client()
    info = await (await client.get("/api/config/runtime-token")).get_json()
    assert info["source"] == "default" and info["is_default"] is True and info["can_edit"] is True
    assert await _caps(client, runtime_api.DEFAULT_DEV_TOKEN) == 200


@pytest.mark.asyncio
async def test_generated_token_takes_effect_immediately_and_retires_default(app):
    client = app.test_client()
    res = await client.post("/api/config/runtime-token", json={"action": "generate"})
    body = await res.get_json()
    assert body["ok"] is True and body["source"] == "database"
    new_token = body["token"]
    assert len(new_token) >= runtime_api.TOKEN_MIN_LENGTH
    assert await _caps(client, new_token) == 200
    assert await _caps(client, runtime_api.DEFAULT_DEV_TOKEN) == 401

    usage = (await (await client.get("/api/config/runtime-token")).get_json())["usage"]
    assert usage["accepted"] == 1 and usage["rejected"] == 1
    assert usage["last_rejected_path"] == "/api/runtime/v1/capabilities"


@pytest.mark.asyncio
async def test_custom_token_validation_and_reset(app):
    client = app.test_client()
    for bad in ("short", "has space in the middle!!", runtime_api.DEFAULT_DEV_TOKEN):
        res = await client.post("/api/config/runtime-token", json={"token": bad})
        assert res.status_code == 400
    res = await client.post("/api/config/runtime-token", json={"token": "my-own-token-0123456789"})
    assert (await res.get_json())["token"] == "my-own-token-0123456789"
    assert await _caps(client, "my-own-token-0123456789") == 200

    res = await client.post("/api/config/runtime-token", json={"action": "reset"})
    assert (await res.get_json())["source"] == "default"
    assert await _caps(client, runtime_api.DEFAULT_DEV_TOKEN) == 200


@pytest.mark.asyncio
async def test_env_token_wins_and_locks_webui(app, monkeypatch):
    client = app.test_client()
    await client.post("/api/config/runtime-token", json={"token": "db-token-0123456789"})
    monkeypatch.setenv(runtime_api.TOKEN_ENV, "env-token-0123456789")
    info = await (await client.get("/api/config/runtime-token")).get_json()
    assert info["source"] == "env" and info["can_edit"] is False
    assert await _caps(client, "env-token-0123456789") == 200
    assert await _caps(client, "db-token-0123456789") == 401
    res = await client.post("/api/config/runtime-token", json={"action": "generate"})
    assert res.status_code == 409
