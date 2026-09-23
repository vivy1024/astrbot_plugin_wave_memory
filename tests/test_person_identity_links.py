"""跨平台身份：管理员确认同一个人在不同平台的账号后，态度与印象时间线合并。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.person_identity_repo import PersonIdentityError
from services.injection.channels.relationship import RelationshipChannel
from webui.blueprints import people as people_module
from webui.container import ServiceContainer

QQ = "aiocqhttp:user:10001"
BILI = "bilibili:user:778899"


def _qq_group(group: str = "g1", bot_id: str = "yushu") -> RuntimeScope:
    return RuntimeScope(bot_id, "group", SessionRef(f"aiocqhttp:group:{group}", "aiocqhttp", "group", group), subject_principal_id=QQ)


def _bili_room(bot_id: str = "yushu") -> RuntimeScope:
    return RuntimeScope(
        bot_id, "group", SessionRef("bilibili:group:24292304", "bilibili", "group", "24292304"),
        subject_principal_id=BILI,
    )


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "identity.sqlite"), dimension=3)
    try:
        yield database
    finally:
        database.close()


def test_link_merge_unlink_and_per_bot_isolation(db):
    repo = db.person_identity
    assert repo.linked_principals("yushu", QQ) == [QQ]
    key = repo.link("yushu", [QQ, BILI], note="同一个观众")
    assert sorted(repo.linked_principals("yushu", QQ)) == sorted([QQ, BILI])
    assert repo.linked_principals("baizz", QQ) == [QQ], "谁是谁是每个 Bot 自己的认识"

    third = "discord:user:42"
    other_key = repo.link("yushu", [third, "discord:user:43"])
    assert other_key != key
    merged = repo.link("yushu", [BILI, third])
    assert sorted(repo.linked_principals("yushu", QQ)) == sorted([QQ, BILI, third, "discord:user:43"])
    assert merged in {key, other_key}

    assert repo.unlink("yushu", third)
    assert third not in repo.linked_principals("yushu", QQ)
    assert repo.unlink("yushu", "discord:user:43")
    assert repo.unlink("yushu", BILI)
    assert repo.linked_principals("yushu", QQ) == [QQ], "只剩一个账号时关联自动解除"

    with pytest.raises(PersonIdentityError):
        repo.link("yushu", [QQ, "不是账号"])
    with pytest.raises(PersonIdentityError):
        repo.link("yushu", [QQ])


def test_linked_accounts_share_attitude_and_impression_timeline(db):
    soul = db.soul_repository
    soul.upsert_relationship(_qq_group(), subject_principal_id=QQ, affinity=30, state="friendly",
                             dimensions={"familiarity": 80.0, "trust": 15.0, "depth": 20.0})
    db.person_timeline.add_event(bot_id="yushu", user_id="10001", group_id="g1", kind="impression",
                                 summary="在群里帮新人解答问题", occurred_at=1000.0)

    def run():
        ctx = SimpleNamespace(
            mode="full", config={"channels": {"affinity": {"enabled": True}}},
            scope=_bili_room(), sender_id="778899", group_id="24292304",
        )
        return asyncio.run(RelationshipChannel(repository=soul, db=db).build(ctx))

    before = run()
    assert "在群里帮新人解答问题" not in (before.text or "")

    db.person_identity.link("yushu", [QQ, BILI])
    after = run()
    assert after.status == "hit"
    assert "在群里帮新人解答问题" in after.text, "直播间里认出这是 QQ 群里那位"
    assert "综合值=29" in after.text, "态度同样合并"
    assert "综合值=29" not in (before.text or "")


@pytest.fixture
def identity_app(db):
    ServiceContainer.reset()
    container = ServiceContainer()
    container.password = ""
    container.db = db

    class _Provider:
        def get_request_scope(self):
            return _qq_group()

    app = Quart(__name__)
    app.register_blueprint(people_module.people_bp)
    app.extensions["wave_api_contract"] = {"request_scope_provider": _Provider()}
    yield app
    ServiceContainer.reset()


@pytest.mark.asyncio
async def test_identity_link_api_round_trip(identity_app):
    client = identity_app.test_client()
    created = await client.post("/api/people/identity-links", json={"principals": [QQ, BILI], "note": "同一人"})
    assert created.status_code == 200
    payload = await created.get_json()
    assert sorted(payload["item"]["principals"]) == sorted([QQ, BILI])

    listed = await (await client.get("/api/people/identity-links")).get_json()
    assert listed["bot_id"] == "yushu"
    assert len(listed["items"]) == 1

    invalid = await client.post("/api/people/identity-links", json={"principals": [QQ, "bad"]})
    assert invalid.status_code == 422
    assert (await invalid.get_json())["error"]["code"] == "invalid_principal"

    removed = await client.post("/api/people/identity-links/unlink", json={"principal": BILI})
    assert removed.status_code == 200
    missing = await client.post("/api/people/identity-links/unlink", json={"principal": BILI})
    assert missing.status_code == 404
