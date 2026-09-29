"""角色经历工作台 API：概览、经历增改（版本冲突 409）、批量启用（整体回滚）、设置与当前时间点、
时间线、需要确认的提取任务、导入导出、续跑、服务不可用 503。虚构 Bot 与虚构内容。"""

from __future__ import annotations

import json

import pytest
from quart import Quart

from character_experience_support import FakeEmbedding, FakeJobs, FakeLLM, bot, make_registry, make_service
from domain import bot_identity
from engine.db.connection import ConnectionManager
from webui.blueprints.character_experience import character_experience_bp
from webui.container import get_container

NOVEL = "\n".join([
    "第1章 入门", "墨言第一次走进书院，被门槛绊了一跤，全院的人都笑了。" * 5,
    "第2章 夜读", "墨言在藏书阁里读到半夜，灯油烧干了才发现。" * 5,
])


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    registry = make_registry(cm, bot("oc_m", "墨言", "10000007"), bot("oc_n", "旁人", "10000008"))
    roots = tmp_path / "novel_docs"
    roots.mkdir()
    (roots / "虚构书院.txt").write_text(NOVEL, encoding="utf-8")
    jobs = FakeJobs()
    service = make_service(cm, registry, llm=FakeLLM(), embedding=FakeEmbedding(), jobs=jobs, source_roots=[str(roots)])
    container = get_container()
    saved = (container.character_experience, container.password)
    container.character_experience, container.password = service, ""
    app = Quart(__name__)
    app.register_blueprint(character_experience_bp)
    yield {"client": app.test_client(), "service": service, "registry": registry, "jobs": jobs, "novel": str(roots / "虚构书院.txt")}
    container.character_experience, container.password = saved
    bot_identity.publish()
    cm.close()


BASE = "/api/character-experience/bots/oc_m"


@pytest.mark.asyncio
async def test_overview_and_unknown_bot(env):
    res = await env["client"].get(f"{BASE}/overview")
    assert res.status_code == 200
    data = await res.get_json()
    assert data["bot"]["name"] == "墨言" and data["settings"]["enabled"] is False and data["stages"] == []
    missing = await env["client"].get("/api/character-experience/bots/nobody/overview")
    assert missing.status_code == 404 and (await missing.get_json())["error"]["code"] == "bot_not_found"


@pytest.mark.asyncio
async def test_create_update_conflict_and_bulk_activate(env):
    client = env["client"]
    res = await client.post(f"{BASE}/experiences", json={"item": {"content": "我第一次进书院就摔了一跤。", "story_time": "十四岁"}})
    assert res.status_code == 201
    created = (await res.get_json())["item"]
    assert created["status"] == "draft" and created["version"] == 1
    dup = await client.post(f"{BASE}/experiences", json={"item": {"content": "我第一次进书院就摔了一跤。"}})
    assert dup.status_code == 409 and (await dup.get_json())["error"]["code"] == "duplicate_content"

    res = await client.put(f"{BASE}/experiences/{created['id']}", json={"version": 1, "fields": {"emotion": "搞笑"}})
    assert res.status_code == 200 and (await res.get_json())["item"]["version"] == 2
    stale = await client.put(f"{BASE}/experiences/{created['id']}", json={"version": 1, "fields": {"emotion": "伤感"}})
    assert stale.status_code == 409
    body = await stale.get_json()
    assert body["error"]["code"] == "version_conflict" and body["error"]["ids"] == [str(created["id"])]
    missing = await client.put(f"{BASE}/experiences/999", json={"version": 1, "fields": {"emotion": "伤感"}})
    assert missing.status_code == 404
    no_version = await client.put(f"{BASE}/experiences/{created['id']}", json={"fields": {"emotion": "伤感"}})
    assert no_version.status_code == 400

    second = (await (await client.post(f"{BASE}/experiences", json={"item": {"content": "我在藏书阁读到灯油烧干。"}})).get_json())["item"]
    items = [{"id": created["id"], "version": 2}, {"id": second["id"], "version": 7}]
    res = await client.post(f"{BASE}/experiences/status", json={"items": items, "status": "active"})
    assert res.status_code == 409
    listing = await (await client.get(f"{BASE}/experiences?status=active")).get_json()
    assert listing["total"] == 0, "有一条版本不对就整体不写"
    items[1]["version"] = 1
    res = await client.post(f"{BASE}/experiences/status", json={"items": items, "status": "active"})
    data = await res.get_json()
    assert res.status_code == 200 and sorted(data["changed"]) == sorted([created["id"], second["id"]])
    assert data["embed_job"]["job_id"], "启用后没有向量的自动排补算向量"
    searched = await client.get(f"{BASE}/experiences", query_string={"status": "active", "q": "藏书阁"})
    assert (await searched.get_json())["total"] == 1
    detail = await (await client.get(f"{BASE}/experiences/{second['id']}")).get_json()
    assert detail["item"]["content"] == "我在藏书阁读到灯油烧干。" and detail["stage"] is None


@pytest.mark.asyncio
async def test_stages_settings_and_current_time_point(env):
    client, registry = env["client"], env["registry"]
    res = await client.put(f"{BASE}/stages/s1", json={"version": 0, "fields": {"title": "初入书院", "story_time": "十四岁"}})
    assert res.status_code == 200
    again = await client.put(f"{BASE}/stages/s1", json={"version": 0, "fields": {"title": "改名"}})
    assert again.status_code == 409
    await client.put(f"{BASE}/stages/s2", json={"version": 0, "fields": {"title": "夜读"}})

    version = registry.get("oc_m").version
    res = await client.put(f"{BASE}/settings", json={"version": version, "patch": {"enabled": True, "current_stage": "s1", "top_k": 2}})
    assert res.status_code == 200
    data = await res.get_json()
    assert data["version"] == version + 1 and data["settings"]["current_stage"] == "s1" and data["settings"]["top_k"] == 2
    assert registry.get("oc_m").character_experience.enabled
    stale = await client.put(f"{BASE}/settings", json={"version": version, "patch": {"current_stage": "s2"}})
    assert stale.status_code == 409, "沿用 Bot 资料的版本冲突"
    unknown = await client.put(f"{BASE}/settings", json={"version": version + 1, "patch": {"current_stage": "s9"}})
    assert unknown.status_code == 404 and (await unknown.get_json())["error"]["code"] == "stage_not_found"
    bad_field = await client.put(f"{BASE}/settings", json={"version": version + 1, "patch": {"name": "改名"}})
    assert bad_field.status_code == 400

    stage = (await (await client.get(f"{BASE}/stages")).get_json())["items"][0]
    current = await client.delete(f"{BASE}/stages/s1?version={stage['version']}")
    assert current.status_code == 409 and (await current.get_json())["error"]["code"] == "stage_is_current"
    overview = await (await client.get(f"{BASE}/overview")).get_json()
    assert [s["is_current"] for s in overview["stages"]] == [True, False]
    assert [s["recallable"] for s in overview["stages"]] == [True, False]

    stages = (await (await client.get(f"{BASE}/stages")).get_json())["items"]
    res = await client.post(f"{BASE}/stages/reorder", json={"items": [{"stage_key": s["stage_key"], "version": s["version"]} for s in reversed(stages)]})
    assert res.status_code == 200 and [s["stage_key"] for s in (await res.get_json())["items"]] == ["s2", "s1"]


@pytest.mark.asyncio
async def test_extraction_needs_explicit_confirmation(env):
    client = env["client"]
    body = {"path": env["novel"], "name": "墨言", "description": "爱读书、有点迷糊"}
    res = await client.post(f"{BASE}/extract/preview", json=body)
    plan = await res.get_json()
    assert res.status_code == 200 and plan["calls"] == 2 and "scene_keys" not in plan and plan["sample"][0]["chapter"] == 1
    res = await client.post(f"{BASE}/extract", json=body)
    data = await res.get_json()
    assert res.status_code == 409 and data["error"]["code"] == "confirmation_required" and data["plan"]["calls"] == 2
    res = await client.post(f"{BASE}/extract", json={**body, "confirm": True, "expected_calls": 2})
    assert res.status_code == 202 and (await res.get_json())["calls"] == 2
    outside = await client.post(f"{BASE}/extract/preview", json={**body, "path": "C:/Windows/win.ini"})
    assert outside.status_code == 400 and (await outside.get_json())["error"]["code"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_import_export_recall_and_resume(env):
    client, jobs = env["client"], env["jobs"]
    lines = "\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"memory": "我在书院门槛上摔过一跤，至今记得那种丢脸。", "arc": "arc01_书院", "approx_time": "十四岁"},
        {"memory": "藏书阁的灯油烧干那晚，我摸黑回的宿舍。", "arc": "arc01_书院"},
    ])
    res = await client.post(f"{BASE}/import", json={"text": lines, "status": "active"})
    data = await res.get_json()
    assert res.status_code == 200 and data["inserted"] == 2 and data["stages_created"] == ["arc01_书院"]
    res = await client.get(f"{BASE}/export?status=active")
    assert res.status_code == 200 and "attachment" in res.headers["Content-Disposition"]
    exported = (await res.get_data(as_text=True)).strip().splitlines()
    assert len(exported) == 2 and json.loads(exported[0])["arc"] == "arc01_书院"

    await env["service"].embed_missing("oc_m")
    res = await client.post(f"{BASE}/recall", json={"message": "你在书院门槛摔过跤吗"})
    data = await res.get_json()
    assert res.status_code == 200 and data["items"] and data["items"][0]["rendered_text"].startswith("【你过去的经历·十四岁】")

    started = await (await client.post(f"{BASE}/embed")).get_json()
    assert started["job"] is None, "都有向量了就不排任务"
    envelope = await jobs.enqueue(idempotency_key="k", kind="character_experience.extract.v1", scope={}, payload={}, schedule_slot="main")
    run_id = envelope.to_dict()["job_id"]
    pending = await client.post(f"/api/character-experience/jobs/{run_id}/resume")
    assert pending.status_code == 409 and (await pending.get_json())["error"]["code"] == "job_not_resumable"
    jobs.runs[run_id].status = "failed"
    resumed = await client.post(f"/api/character-experience/jobs/{run_id}/resume")
    assert resumed.status_code == 202 and (await resumed.get_json())["generation"] == 1
    assert (await (await client.get(f"{BASE}/jobs")).get_json())["items"] == []  # 替身任务服务不落库


@pytest.mark.asyncio
async def test_legacy_migration_endpoint_and_unavailable_service(env):
    res = await env["client"].post("/api/character-experience/legacy-migration", json={"dry_run": True})
    data = await res.get_json()
    assert res.status_code == 200 and data["reports"][0]["status"] in {"target_bot_missing", "nothing_to_migrate"}
    get_container().character_experience = None
    res = await env["client"].get(f"{BASE}/overview")
    assert res.status_code == 503
