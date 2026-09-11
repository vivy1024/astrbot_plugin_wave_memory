from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.person_timeline_repo import PersonTimelineRepo
from webui.container import ServiceContainer
from webui.blueprints import people as people_module


class _RequestScopeProvider:
    def __init__(self, scope: RuntimeScope) -> None:
        self.scope = scope

    def get_request_scope(self) -> RuntimeScope:
        return self.scope


def _scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-a",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
    )


@pytest.fixture
def timeline_app(tmp_path):
    ServiceContainer.reset()
    container = ServiceContainer()
    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    repo = PersonTimelineRepo(cm)
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="person_fact", summary="别名 时雨", occurred_at=1.0)
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="愿意核对事实", occurred_at=2.0)
    repo.add_event(bot_id="bot-a", user_id="u2", group_id="g1", kind="impression", summary="爱接梗", occurred_at=3.0)
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g2", kind="impression", summary="别群印象", occurred_at=4.0)
    container.password = ""
    container.db = SimpleNamespace(conn=cm, person_timeline=repo)
    app = Quart(__name__)
    app.register_blueprint(people_module.people_bp)
    app.extensions["wave_api_contract"] = {
        "request_scope_provider": _RequestScopeProvider(_scope()),
    }
    yield app
    ServiceContainer.reset()
    cm.close()


@pytest.mark.asyncio
async def test_person_timeline_pages_current_group_only(timeline_app):
    response = await timeline_app.test_client().get("/api/people/timeline?limit=1&offset=0")
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["page"]["total"] == 3
    assert payload["timeline"] == "impression"
    assert payload["readonly"] is True
    assert payload["items"][0]["summary"] == "爱接梗"
    assert payload["items"][0]["user_id"] == "u2"
    assert all(item["group_id"] == "g1" for item in payload["items"])


@pytest.mark.asyncio
async def test_person_timeline_filters_user_kind_and_search(timeline_app):
    client = timeline_app.test_client()
    person = await (await client.get("/api/people/timeline?user_id=u1")).get_json()
    assert person["page"]["total"] == 2
    facts = await (await client.get("/api/people/timeline?kind=person_fact")).get_json()
    assert facts["page"]["total"] == 1
    assert facts["items"][0]["summary"] == "别名 时雨"
    searched = await (await client.get("/api/people/timeline?search=%E6%A0%B8%E5%AF%B9")).get_json()
    assert searched["page"]["total"] == 1
    assert searched["items"][0]["summary"] == "愿意核对事实"
    invalid = await client.get("/api/people/timeline?kind=daily_diary")
    assert invalid.status_code == 400
    payload = await invalid.get_json()
    assert payload["error"]["code"] == "invalid_timeline_kind"
