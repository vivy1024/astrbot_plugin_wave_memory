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


def _create_scoped_memories(cm: ConnectionManager) -> None:
    cm.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL,
            memory_type TEXT DEFAULT 'message',
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            quarantine INTEGER DEFAULT 0,
            resolution_state TEXT DEFAULT 'resolved'
        );
        """
    )


def _add_memory(cm: ConnectionManager, *, bot_id: str, session_id: str, group_id: str, sender_id: str, content: str, ts: float) -> int:
    cursor = cm.execute(
        """INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp, bot_id, session_id, visibility)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'group')""",
        (group_id, sender_id, sender_id, content, ts, bot_id, session_id),
    )
    cm.commit()
    return int(cursor.lastrowid)


@pytest.fixture
def evidence_app(tmp_path):
    ServiceContainer.reset()
    container = ServiceContainer()
    cm = ConnectionManager(str(tmp_path / "wave_memory.sqlite3"))
    _create_scoped_memories(cm)
    repo = PersonTimelineRepo(cm)
    container.password = ""
    container.db = SimpleNamespace(conn=cm, person_timeline=repo)
    app = Quart(__name__)
    app.register_blueprint(people_module.people_bp)
    app.extensions["wave_api_contract"] = {
        "request_scope_provider": _RequestScopeProvider(_scope()),
    }
    yield app, cm, repo
    ServiceContainer.reset()
    cm.close()


async def _timeline_items(app) -> dict[str, dict]:
    response = await app.test_client().get("/api/people/timeline?limit=50")
    assert response.status_code == 200
    payload = await response.get_json()
    return {item["summary"]: item for item in payload["items"]}


@pytest.mark.asyncio
async def test_person_timeline_extracts_quote_recorded_in_detail(evidence_app):
    app, _cm, repo = evidence_app
    repo.add_event(
        bot_id="bot-a", user_id="u1", group_id="g1", kind="impression",
        summary="爱接梗", detail="接得很快\n原话证据：“我来接这个梗”", occurred_at=10.0,
    )
    item = (await _timeline_items(app))["爱接梗"]
    assert item["source_quote"] == "我来接这个梗"
    assert item["source_quote_inferred"] is False


@pytest.mark.asyncio
async def test_person_timeline_resolves_memory_id_only_within_scope(evidence_app):
    app, cm, repo = evidence_app
    own = _add_memory(cm, bot_id="bot-a", session_id="qq:group:g1", group_id="g1", sender_id="u1", content="本群原话", ts=5.0)
    foreign = _add_memory(cm, bot_id="bot-b", session_id="qq:group:g1", group_id="g1", sender_id="u1", content="别的 Bot 原话", ts=5.0)
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="本群事件",
                   occurred_at=1000.0, provenance={"source_memory_id": own})
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="越界事件",
                   occurred_at=2000.0, provenance={"source_memory_id": foreign})
    items = await _timeline_items(app)
    assert items["本群事件"]["source_quote"] == "本群原话"
    assert items["本群事件"]["source_quote_inferred"] is False
    assert items["越界事件"]["source_quote"] is None


@pytest.mark.asyncio
async def test_person_timeline_window_quote_is_scoped_and_marked_inferred(evidence_app):
    app, cm, repo = evidence_app
    _add_memory(cm, bot_id="bot-b", session_id="qq:group:g1", group_id="g1", sender_id="u1", content="别的 Bot 更近的发言", ts=99.0)
    _add_memory(cm, bot_id="bot-a", session_id="qq:group:g2", group_id="g2", sender_id="u1", content="别群更近的发言", ts=99.5)
    own = _add_memory(cm, bot_id="bot-a", session_id="qq:group:g1", group_id="g1", sender_id="u1", content="当时的发言", ts=90.0)
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="窗口事件", occurred_at=100.0)
    repo.add_event(bot_id="bot-a", user_id="u1", group_id="g1", kind="impression", summary="窗口外事件", occurred_at=5000.0)
    items = await _timeline_items(app)
    assert items["窗口事件"]["source_quote"] == "当时的发言"
    assert items["窗口事件"]["source_memory_id"] == own
    assert items["窗口事件"]["source_quote_inferred"] is True
    assert items["窗口外事件"]["source_quote"] is None
    assert items["窗口外事件"]["source_quote_inferred"] is False
