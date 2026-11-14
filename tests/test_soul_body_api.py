"""管理台只读身体接口：只按 bot_id 取，不带人的身份或聊天原文。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart

from engine.db.bot_body_repo import BotBodyRepository
from engine.db.connection import ConnectionManager
from services.bot_body import BotBodyService
from webui.blueprints import soul as soul_module
from webui.container import ServiceContainer

BOT = "yushu"
NOW = 1_800_000_000.0


@pytest.fixture
def app_and_service(tmp_path):
    ServiceContainer.reset()
    container = ServiceContainer()
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
    profile = SimpleNamespace(body=SimpleNamespace(enabled=True, sleep_start=None, sleep_end=None, hormone_gain=1.0,
                                                   sleep_replies=["zzz"], sleep_reply_cooldown_seconds=1800.0))
    service = BotBodyService(db, profile_for=lambda b: profile if b == BOT else None, clock=lambda: NOW, hour_of=lambda _t: 14.0)
    container.password = ""
    container.body_service = service
    app = Quart(__name__)
    app.register_blueprint(soul_module.soul_bp)
    yield app, service
    ServiceContainer.reset()
    cm.close()


@pytest.mark.asyncio
async def test_body_snapshot_returns_hormones_and_events(app_and_service):
    app, service = app_and_service
    service.note_event(BOT, "praised", principal_id="qq:user:u1", now=NOW)
    payload = await (await app.test_client().get(f"/api/soul/body?bot_id={BOT}")).get_json()
    assert payload["enabled"] is True and payload["asleep"] is False
    assert set(payload["hormones"]) == {"dopamine", "serotonin", "oxytocin", "cortisol", "melatonin", "boredom"}
    assert payload["events"][0]["kind"] == "praised"
    assert "principal_id" not in payload["events"][0] and "qq:user:u1" not in str(payload)


@pytest.mark.asyncio
async def test_body_requires_bot_id(app_and_service):
    app, _ = app_and_service
    response = await app.test_client().get("/api/soul/body")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_disabled_bot_reports_disabled_instead_of_inventing_numbers(app_and_service):
    app, service = app_and_service
    service.profile_for = lambda _b: SimpleNamespace(body=SimpleNamespace(enabled=False))
    payload = await (await app.test_client().get(f"/api/soul/body?bot_id={BOT}")).get_json()
    assert payload["enabled"] is False and "hormones" not in payload
