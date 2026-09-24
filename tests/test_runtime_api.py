import pytest
from unittest.mock import AsyncMock, MagicMock
from quart import Quart
from webui.blueprints.runtime import runtime_bp
from webui.container import get_container
from domain.bot_profile import BotProfile
from services.bot_registry import BotRegistry


@pytest.fixture
def container_mock():
    c = get_container()
    writer_mock = MagicMock()
    writer_mock.enqueue = AsyncMock(return_value=None)
    c.writer = writer_mock

    db_mock = MagicMock()
    repo_mock = MagicMock()
    repo_mock.upsert_scoped_fact = MagicMock(return_value=101)
    db_mock.scoped_knowledge = repo_mock
    db_mock.conn = MagicMock()
    c.db = db_mock

    gateway_mock = MagicMock()
    gateway_mock.record_episode = AsyncMock(return_value=202)
    c.write_gateway = gateway_mock

    profile = BotProfile.from_dict({
        "db_id": "yushu",
        "name": "羽书",
        "qq_id": "10001",
        "persona": {
            "lore_title": "羽书出自《没钱修什么仙》",
            "lore_lines": ["出处与本体：你来自修真小说《没钱修什么仙》。"],
        },
    })
    c.bot_registry = BotRegistry.from_profiles([profile])

    yield c
    c.bot_registry = None


@pytest.fixture
def app(container_mock):
    app = Quart(__name__)
    app.register_blueprint(runtime_bp)
    return app


@pytest.mark.asyncio
async def test_runtime_capabilities_auth(app):
    client = app.test_client()
    res = await client.get("/api/runtime/v1/capabilities")
    assert res.status_code == 401
    
    res_auth = await client.get("/api/runtime/v1/capabilities", headers={"Authorization": "Bearer yushu-dev-token"})
    assert res_auth.status_code == 200
    data = await res_auth.get_json()
    assert data["ok"] is True
    assert data["capabilities"]["book_lore_search"] is True
    assert data["capabilities"]["observations_batch"] is True
    assert data["capabilities"]["commands"] is True


@pytest.mark.asyncio
async def test_runtime_observations_batch(app, container_mock):
    client = app.test_client()
    payload = {
        "batch_id": "b1",
        "events": [
            {
                "event_id": "ev-1",
                "content": "羽书你好！今天盖了房子没？",
                "sender_id": "user_123",
                "sender_name": "老观众",
                "scope": {
                    "bot_id": "yushu",
                    "visibility": "group",
                    "session": {
                        "id": "bilibili:group:24292304",
                        "platform_id": "bilibili",
                        "kind": "group",
                        "conversation_id": "24292304"
                    }
                }
            }
        ]
    }
    res = await client.post(
        "/api/runtime/v1/observations/batch",
        headers={"Authorization": "Bearer yushu-dev-token"},
        json=payload
    )
    assert res.status_code == 200
    data = await res.get_json()
    assert data["ok"] is True
    assert len(data["receipts"]) == 1
    assert data["receipts"][0]["status"] == "accepted"
    assert data["receipts"][0]["delivery_state"] == "inbox_enqueued"
    container_mock.writer.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_commands_propose_fact(app, container_mock):
    client = app.test_client()
    payload = {
        "command": "propose_fact",
        "operation_key": "op-fact-test",
        "scope": {
            "bot_id": "yushu",
            "visibility": "group",
            "session": {
                "id": "bilibili:group:24292304",
                "platform_id": "bilibili",
                "kind": "group",
                "conversation_id": "24292304"
            },
            "subject_principal_id": "bilibili:user:123"
        },
        "arguments": {
            "subject": "bilibili:user:123",
            "predicate": "偏好的直播内容",
            "object": "建筑与挖矿",
            "source_quote": "我更喜欢看你建筑"
        }
    }
    res = await client.post(
        "/api/runtime/v1/commands",
        headers={"Authorization": "Bearer yushu-dev-token"},
        json=payload
    )
    assert res.status_code == 200
    data = await res.get_json()
    assert data["ok"] is True
    assert data["data"]["command"] == "propose_fact"
    assert data["data"]["domain_state"] == "pending"
    assert data["data"]["transport_state"] == "committed"
    assert data["data"]["entity"]["id"] == "101"
    container_mock.db.scoped_knowledge.upsert_scoped_fact.assert_called_once()


class _FakeChannel:
    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.seen = []

    async def build(self, ctx):
        from services.injection.channel_base import InjectionResult

        self.seen.append(ctx)
        return InjectionResult.hit(self.name, f"{self.text}：{ctx.sender_name}", items=[{"id": 1}])


def _install_preparer(container, channels):
    from services.config.channel_config import build_channel_config_from_plugin_config
    from services.injection.runtime_prepare import RuntimeContextPreparer

    container.runtime_context_preparer = RuntimeContextPreparer(
        channels_provider=lambda: channels,
        config_resolver=lambda scope: build_channel_config_from_plugin_config({}, scope=scope),
        context_config_builder=lambda **kwargs: {},
        query_options_factory=lambda cfg: None,
    )


@pytest.mark.asyncio
async def test_runtime_context_prepare_uses_orchestrator(app, container_mock):
    memory = _FakeChannel("memory", "相关记忆")
    lore = _FakeChannel("book_lore", "书设检索")
    _install_preparer(container_mock, [memory, lore])
    client = app.test_client()
    payload = {
        "text": "张羽师兄最近有去过万法大学吗？",
        "speaker": {"id": "user_456", "name": "老张"},
        "tier": "light",
        "scope": {
            "bot_id": "yushu",
            "visibility": "group",
            "session": {
                "id": "bilibili:group:24292304",
                "platform_id": "bilibili",
                "kind": "group",
                "conversation_id": "24292304"
            }
        }
    }
    try:
        res = await client.post(
            "/api/runtime/v1/context/prepare",
            headers={"Authorization": "Bearer yushu-dev-token"},
            json=payload
        )
        assert res.status_code == 200
        data = await res.get_json()
        assert data["ok"] is True
        # Profile 的常驻书设在最前，其后是编排器输出。
        assert data["block"].startswith("[世界观书设：羽书出自《没钱修什么仙》]")
        assert "相关记忆：老张" in data["block"]
        # light 档不跑书设检索通道。
        assert "book_lore" not in data["channels"] and not lore.seen
        assert data["channels"]["memory"]["status"] == "hit"
        ctx = memory.seen[0]
        assert ctx.source == "cortico"
        assert ctx.scope.subject_principal_id == "bilibili:user:user_456"
        assert data["trace_id"].startswith("cortico-")
    finally:
        container_mock.runtime_context_preparer = None


@pytest.mark.asyncio
async def test_runtime_context_prepare_rejects_unknown_bot_and_missing_preparer(app, container_mock):
    client = app.test_client()
    headers = {"Authorization": "Bearer yushu-dev-token"}
    scope = {"bot_id": "nobody", "session": {"id": "qq:group:1"}}
    res = await client.post("/api/runtime/v1/context/prepare", headers=headers, json={"text": "hi", "scope": scope})
    assert res.status_code == 400
    assert "unknown_bot" in (await res.get_json())["error"]["message"]

    container_mock.runtime_context_preparer = None
    scope["bot_id"] = "yushu"
    res = await client.post("/api/runtime/v1/context/prepare", headers=headers, json={"text": "hi", "scope": scope})
    assert res.status_code == 503
