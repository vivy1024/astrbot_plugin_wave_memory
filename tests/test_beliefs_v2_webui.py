"""信念 v2 管理台 API：按状态分组、依据日记与历史、一键否决 / 恢复（版本冲突 409）、旧数据只读。"""

from __future__ import annotations

from datetime import datetime

import pytest
from quart import Quart

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.bot_belief_repo import BeliefWrite
from services.belief_lifecycle import BeliefPolicy
from webui.blueprints.beliefs import beliefs_bp, build_beliefs_payload
from webui.blueprints.bot_home import build_bot_home_payload
from webui.container import get_container

NOW = datetime(2026, 9, 29, 20, 0).timestamp()
BOT = "bot-demo"


def _evidence(diary_id: int, date: str) -> dict:
    return {"diary_id": diary_id, "date": date, "session_id": "qq:group:g1", "group_name": "摸鱼测试群", "title": f"日记{diary_id}"}


@pytest.fixture
def seeded(tmp_path):
    path = str(tmp_path / "webui.sqlite")
    db = WaveMemoryDB(path, dimension=3)
    scope = RuntimeScope(BOT, "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
    db.scoped_knowledge.upsert_scoped_belief(scope, belief_key="legacy-1", content="测试群友甲偏爱射击游戏",
                                             belief_type="person_judgment", status="active")
    db.close()
    db = WaveMemoryDB(path, dimension=3)  # 迁移把旧信念归档
    formed_id, sprout_id = db.bot_beliefs.apply(BOT, [
        BeliefWrite(None, {"content": "我讲冷笑话群里大多不接，他们更吃玩梗", "category": "group", "status": "sprout",
                           "triggers": ["冷笑话"], "support_dates": ["2026-09-21"], "confirm_runs": 1,
                           "first_seen_at": NOW - 3 * 86400, "last_confirmed_at": NOW - 3 * 86400},
                    events=[{"event_type": "sprouted", "from_status": "", "to_status": "sprout", "reason": "第一次冷场",
                             "evidence": [_evidence(11, "2026-09-21")], "occurred_at": NOW - 3 * 86400}]),
        BeliefWrite(None, {"content": "直播时我更喜欢在游戏里盖房子", "category": "self", "status": "sprout",
                           "support_dates": ["2026-09-28"], "confirm_runs": 1, "last_confirmed_at": NOW - 86400}),
    ], now=NOW - 3 * 86400)
    db.bot_beliefs.apply(BOT, [BeliefWrite(formed_id, {"status": "formed", "support_dates": ["2026-09-21", "2026-09-24"], "confirm_runs": 2},
                                           events=[{"event_type": "confirmed", "from_status": "sprout", "to_status": "formed",
                                                    "reason": "又冷场了", "evidence": [_evidence(12, "2026-09-24")],
                                                    "occurred_at": NOW - 86400}])], now=NOW - 86400)
    yield db, formed_id, sprout_id
    db.close()


def test_payload_groups_by_status_with_evidence_history_and_legacy(seeded):
    db, formed_id, sprout_id = seeded
    container = type("C", (), {"db": db, "belief_policy": BeliefPolicy(), "belief_reflection": None})()
    payload = build_beliefs_payload(container, BOT, now=NOW)
    assert [g["status"] for g in payload["groups"]] == ["sprout", "formed", "stable", "shaken", "faded", "vetoed", "legacy_archived"]
    assert [g["label"] for g in payload["groups"]] == ["萌芽", "已形成", "稳固", "动摇中", "已淡出", "已否决", "已归档（旧版人物判断）"]
    assert payload["counts"] == {"sprout": 1, "formed": 1, "stable": 0, "shaken": 0, "faded": 0, "vetoed": 0, "legacy_archived": 1}
    formed = payload["groups"][1]["items"][0]
    assert formed["id"] == formed_id and formed["status_label"] == "已形成" and formed["category_label"] == "关于群"
    assert formed["experience_phrase"] == "来自 9 月下旬的两次经历"
    assert [e["diary_id"] for e in formed["evidence"]] == [12, 11], "依据日记最新在前"
    assert [h["label"] for h in formed["history"]] == ["萌芽", "被印证"]
    assert formed["history"][1]["from_label"] == "萌芽" and formed["history"][1]["to_label"] == "已形成"
    assert formed["actions"]["veto"]["available"] and not formed["actions"]["restore"]["available"]
    legacy = payload["groups"][-1]["items"][0]
    assert legacy["legacy"] and legacy["content"] == "测试群友甲偏爱射击游戏"
    assert legacy["legacy_type_label"] == "人物判断" and legacy["legacy_status"] == "active"
    assert not legacy["actions"]["veto"]["available"] and legacy["actions"]["veto"]["reason_code"] == "legacy_readonly"
    assert payload["capabilities"]["create"] == {"available": False, "reason_code": "belief_formed_by_reflection_only"}
    assert payload["reflection"]["enabled"] is False and payload["policy"]["max_active"] == 20


@pytest.fixture
def api(seeded):
    db, formed_id, sprout_id = seeded
    container = get_container()
    saved = (container.db, container.password, getattr(container, "belief_policy", None))
    container.db, container.password, container.belief_policy = db, "", BeliefPolicy()
    app = Quart(__name__)
    app.register_blueprint(beliefs_bp)
    yield app.test_client(), db, formed_id
    container.db, container.password, container.belief_policy = saved


@pytest.mark.asyncio
async def test_list_requires_bot_id_and_returns_groups(api):
    client, _db, formed_id = api
    res = await client.get("/api/beliefs/")
    assert res.status_code == 400 and (await res.get_json())["error"]["code"] == "bot_id_required"
    res = await client.get(f"/api/beliefs/?bot_id={BOT}")
    data = await res.get_json()
    assert res.status_code == 200 and data["bot_id"] == BOT
    assert data["groups"][1]["items"][0]["id"] == formed_id
    other = await (await client.get("/api/beliefs/?bot_id=bot-other")).get_json()
    assert all(group["count"] == 0 for group in other["groups"]), "按 Bot 独立"


@pytest.mark.asyncio
async def test_veto_and_restore_with_revision_conflict(api):
    client, db, formed_id = api
    revision = db.bot_beliefs.get_belief(BOT, formed_id)["revision"]
    stale = await client.post(f"/api/beliefs/{formed_id}/veto", json={"bot_id": BOT, "revision": revision - 1})
    assert stale.status_code == 409 and (await stale.get_json())["error"]["code"] == "belief_revision_conflict"
    missing = await client.post(f"/api/beliefs/{formed_id}/veto", json={"bot_id": BOT})
    assert missing.status_code == 400
    vetoed = await client.post(f"/api/beliefs/{formed_id}/veto", json={"bot_id": BOT, "revision": revision, "reason": "不像她"})
    body = await vetoed.get_json()
    assert vetoed.status_code == 200 and body["ok"] and body["operation"]["kind"] == "belief.veto"
    assert body["item"]["status"] == "vetoed" and body["revision"] == revision + 1
    assert body["item"]["history"][-1]["reason"] == "不像她"
    again = await client.post(f"/api/beliefs/{formed_id}/veto", json={"bot_id": BOT, "revision": body["revision"]})
    assert again.status_code == 409 and (await again.get_json())["error"]["code"] == "belief_already_vetoed"
    restored = await client.post(f"/api/beliefs/{formed_id}/restore", json={"bot_id": BOT, "revision": body["revision"]})
    restored_body = await restored.get_json()
    assert restored.status_code == 200 and restored_body["item"]["status"] == "formed"
    wrong_bot = await client.post(f"/api/beliefs/{formed_id}/veto", json={"bot_id": "bot-other", "revision": restored_body["revision"]})
    assert wrong_bot.status_code == 404


@pytest.mark.asyncio
async def test_old_review_routes_are_retired_and_legacy_stays_read_only(api):
    client, db, _formed_id = api
    for path in ("/api/beliefs/1/approve", "/api/beliefs/1/archive", "/api/beliefs/commands/batch-lifecycle"):
        res = await client.post(path, json={})
        assert res.status_code == 410 and (await res.get_json()) == {"error": {"code": "legacy_mutation_disabled"}}
    assert (await client.post("/api/beliefs/", json={})).status_code == 503
    assert (await client.delete("/api/beliefs/1")).status_code == 410
    legacy = db.scoped_knowledge.list_bot_scoped_beliefs(BOT, status=None)
    assert [row["status"] for row in legacy] == ["legacy_archived"]


def test_bot_home_card_lists_recent_changes(seeded):
    db, formed_id, _sprout_id = seeded
    payload = build_bot_home_payload(db.conn, bot_id=BOT, now=NOW, soul_repository=db.soul_repository)
    section = payload["beliefs"]
    assert section["status"] == "ready"
    assert section["items"][0]["belief_id"] == formed_id and section["items"][0]["change_label"] == "形成了"
    assert section["counts"] == {"formed": 1, "sprout": 1} and section["held"] == 1
    learned = payload["learned"]["beliefs"]
    assert learned["status"] == "ready" and learned["total"] == 0, "窗口内（1 天）没有新增看法"
