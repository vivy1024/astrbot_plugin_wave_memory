"""游戏知识库管理台 API：游戏库、知识卡 / 词条编辑（版本 409）、批量启用归档、导入预览与导入、导出、迁移、试匹配，
以及各 Bot 自己的经验卡（列表、删除 / 恢复、「现在想一次」）。"""

from __future__ import annotations

import pytest
from quart import Quart

from engine.db.connection import ConnectionManager
from engine.db.game_knowledge_repo import GameKnowledgeRepo
from services.game_knowledge import GameKnowledgeService
from webui.blueprints.game_knowledge import game_knowledge_bp
from webui.container import get_container

BASE = "/api/game-knowledge"


@pytest.fixture
def client(tmp_path):
    cm = ConnectionManager(str(tmp_path / "gk.sqlite"))
    service = GameKnowledgeService(GameKnowledgeRepo(cm))
    container = get_container()
    saved = (container.password, getattr(container, "game_knowledge", None), getattr(container, "game_lessons", None))
    container.password, container.game_knowledge, container.game_lessons = "", service, None
    app = Quart(__name__)
    app.register_blueprint(game_knowledge_bp)
    try:
        yield app.test_client(), service
    finally:
        container.password, container.game_knowledge, container.game_lessons = saved
        cm.close()


async def _json(response):
    return response.status_code, await response.get_json()


@pytest.mark.asyncio
async def test_games_overview_create_and_update_with_version(client):
    http, _ = client
    status, created = await _json(await http.post(f"{BASE}/games", json={"id": "Slay_The_Spire", "name": "杀戮尖塔", "aliases": ["尖塔", "STS"]}))
    assert status == 200 and created["ok"] and created["item"]["id"] == "slay_the_spire" and created["revision"] == 1
    status, duplicated = await _json(await http.post(f"{BASE}/games", json={"id": "slay_the_spire", "name": "重复"}))
    assert status == 409 and duplicated["error"]["code"] == "game_exists"
    status, bad = await _json(await http.post(f"{BASE}/games", json={"id": "杀戮尖塔"}))
    assert status == 400 and bad["error"]["code"] == "invalid_game_id"
    status, overview = await _json(await http.get(f"{BASE}/overview"))
    assert status == 200 and [g["id"] for g in overview["games"]] == ["slay_the_spire"]
    assert overview["games"][0]["counts"]["cards"] == {"draft": 0, "active": 0, "archived": 0}, "杀戮尖塔可以建空库，心得以后积累"
    assert "packs" not in overview, "仓库不带预设攻略"
    assert overview["legacy_sources"][0]["source"] == "oni_lore" and overview["lessons_enabled"] is False
    assert overview["statuses"] == {"draft": "草稿", "active": "启用", "archived": "归档"} and "关键词" in overview["trigger_rule"]
    status, missing_version = await _json(await http.put(f"{BASE}/games/slay_the_spire", json={"enabled": False}))
    assert status == 400 and missing_version["error"]["code"] == "version_required"
    status, updated = await _json(await http.put(f"{BASE}/games/slay_the_spire", json={"enabled": False, "version": 1}))
    assert status == 200 and updated["item"]["enabled"] is False and updated["revision"] == 2
    status, conflict = await _json(await http.put(f"{BASE}/games/slay_the_spire", json={"name": "旧页面", "version": 1}))
    assert status == 409 and conflict["error"]["code"] == "game_version_conflict" and conflict["error"]["current_version"] == 2
    status, detail = await _json(await http.get(f"{BASE}/games/slay_the_spire"))
    assert status == 200 and detail["item"]["name"] == "杀戮尖塔" and detail["stages"] == []
    assert (await http.get(f"{BASE}/games/nope")).status_code == 404


@pytest.mark.asyncio
async def test_cards_and_terms_crud_filters_and_batch_status(client):
    http, _ = client
    await http.post(f"{BASE}/games", json={"id": "minecraft", "name": "我的世界"})
    status, created = await _json(await http.post(f"{BASE}/games/minecraft/cards", json={"item": {
        "title": "苦力怕", "stage": "前期", "points": "听到嘶嘶声马上后退\n用弓箭远程打", "keywords": "苦力怕、爬行者",
        "conditions": "time_of_day == night", "source": "测试来源",
    }}))
    assert status == 200 and created["item"]["status"] == "draft" and created["item"]["source_type"] == "manual"
    card = created["item"]
    assert card["points"] == ["听到嘶嘶声马上后退", "用弓箭远程打"] and card["conditions_text"] == ["time_of_day == night"]
    assert card["trigger_mode"] == "keywords"
    status, bad = await _json(await http.post(f"{BASE}/games/minecraft/cards", json={"title": "条件错", "points": ["x"], "conditions": "health < 低"}))
    assert status == 400 and bad["error"]["code"] == "invalid_condition" and "比较值必须是数字" in bad["error"]["message"]
    status, edited = await _json(await http.put(f"{BASE}/games/minecraft/cards/{card['id']}", json={"item": {"pitfalls": ["爆炸会炸坏方块"]}, "version": 1}))
    assert status == 200 and edited["item"]["version"] == 2 and edited["item"]["pitfalls"] == ["爆炸会炸坏方块"]
    status, stale = await _json(await http.put(f"{BASE}/games/minecraft/cards/{card['id']}", json={"title": "旧页面的编辑", "version": 1}))
    assert status == 409 and stale["error"]["code"] == "card_version_conflict" and stale["error"]["current_version"] == 2
    status, no_version = await _json(await http.put(f"{BASE}/games/minecraft/cards/{card['id']}", json={"title": "x"}))
    assert status == 400 and no_version["error"]["code"] == "version_required"
    assert (await http.get(f"{BASE}/games/minecraft/cards/99999")).status_code == 404

    await http.post(f"{BASE}/games/minecraft/cards", json={"title": "下界", "stage": "中期", "points": ["穿金护甲"], "keywords": ["下界"]})
    status, term = await _json(await http.post(f"{BASE}/games/minecraft/terms", json={"term": "苦力怕", "aliases": "Creeper", "explanation": "会爆炸的怪"}))
    assert status == 200 and term["item"]["aliases"] == ["Creeper"]

    status, page = await _json(await http.get(f"{BASE}/games/minecraft/cards", query_string={"stage": "前期"}))
    assert page["total"] == 1 and page["items"][0]["title"] == "苦力怕"
    status, page = await _json(await http.get(f"{BASE}/games/minecraft/cards", query_string={"q": "嘶嘶"}))
    assert [item["title"] for item in page["items"]] == ["苦力怕"]
    status, page = await _json(await http.get(f"{BASE}/games/minecraft/terms", query_string={"q": "爆炸"}))
    assert page["total"] == 1

    ids = [item["id"] for item in (await (await http.get(f"{BASE}/games/minecraft/cards")).get_json())["items"]]
    status, result = await _json(await http.post(f"{BASE}/games/minecraft/cards/status", json={"ids": ids, "status": "active", "versions": {str(card["id"]): 1}}))
    assert status == 200 and len(result["changed"]) == 1 and result["conflicts"][0]["id"] == card["id"], "版本对不上的那条跳过"
    status, page = await _json(await http.get(f"{BASE}/games/minecraft/cards?status=draft"))
    assert [item["title"] for item in page["items"]] == ["苦力怕"]
    status, empty = await _json(await http.post(f"{BASE}/games/minecraft/cards/status", json={"ids": [], "status": "active"}))
    assert status == 400 and empty["error"]["code"] == "ids_required"
    status, invalid = await _json(await http.post(f"{BASE}/games/minecraft/terms/status", json={"ids": [term["item"]["id"]], "status": "published"}))
    assert status == 400 and invalid["error"]["code"] == "invalid_status"


@pytest.mark.asyncio
async def test_import_preview_import_export_and_trial(client):
    http, _ = client
    await http.post(f"{BASE}/games", json={"id": "minecraft", "name": "我的世界"})
    document = {
        "format": "wavememory.game_knowledge",
        "cards": [{"key": "mc_night", "title": "夜里在外面", "points": ["别乱跑"], "trigger": {"keywords": ["夜晚"], "conditions": ["time_of_day == night"]}}],
        "terms": [{"term": "溺尸", "explanation": "水里的僵尸"}],
    }
    status, missing = await _json(await http.post(f"{BASE}/games/minecraft/import/preview", json={}))
    assert status == 400 and missing["error"]["code"] == "data_required"
    status, preview = await _json(await http.post(f"{BASE}/games/minecraft/import/preview", json={"data": document}))
    assert status == 200 and preview["format"] == "generic" and preview["cards"]["new"] == 1
    assert preview["samples"]["cards"][0]["conditions"] == ["time_of_day == night"]
    status, result = await _json(await http.post(f"{BASE}/games/minecraft/import", json={"data": document, "status": "active"}))
    assert status == 200 and result["ok"] and result["cards"]["created"] == 1 and result["terms"]["created"] == 1
    status, unknown = await _json(await http.post(f"{BASE}/games/minecraft/import", json={"data": {"x": 1}}))
    assert status == 400 and unknown["error"]["code"] == "unknown_import_format"
    status, exported = await _json(await http.get(f"{BASE}/games/minecraft/export?status=active"))
    assert status == 200 and [c["key"] for c in exported["cards"]] == ["mc_night"] and exported["terms"][0]["term"] == "溺尸"
    status, trial = await _json(await http.post(f"{BASE}/games/minecraft/trial", json={
        "message": "河里有溺尸", "keywords": ["夜晚"], "state": {"time_of_day": "night"},
    }))
    assert status == 200 and [c["title"] for c in trial["cards"]] == ["夜里在外面"]
    assert trial["terms"][0]["term"] == "溺尸" and "攻略参考" in trial["text"] and "time_of_day=night" in trial["cards"][0]["reason"]
    status, blocked = await _json(await http.post(f"{BASE}/games/minecraft/trial", json={"keywords": ["夜晚"], "state": {"time_of_day": "day"}}))
    assert blocked["cards"] == [] and blocked["blocked"][0]["title"] == "夜里在外面" and "条件不满足" in blocked["blocked"][0]["reason"]


@pytest.mark.asyncio
async def test_legacy_migration_endpoints(client):
    http, service = client
    assert (await http.post(f"{BASE}/packs/minecraft_starter/import", json={})).status_code == 404, "起始包接口已下线"
    status, scan = await _json(await http.post(f"{BASE}/legacy/oni_lore/scan"))
    assert status == 200 and scan["rows"] == 0, "测试库没有 memories 表时安全返回 0"
    status, migrated = await _json(await http.post(f"{BASE}/legacy/oni_lore/migrate"))
    assert status == 200 and migrated["skipped"] and migrated["reason"] == "no_rows"
    assert service.get_game("oni") is None
    assert (await http.post(f"{BASE}/legacy/other/migrate")).status_code == 404


@pytest.mark.asyncio
async def test_unavailable_service_returns_503(client):
    http, _ = client
    container = get_container()
    container.game_knowledge = None
    status, payload = await _json(await http.get(f"{BASE}/overview"))
    assert status == 503 and payload["error"]["code"] == "game_knowledge_unavailable"


@pytest.mark.asyncio
async def test_lesson_endpoints_list_veto_and_restore(client):
    """经验卡：只在 lessons 接口里按 Bot 列出（知识卡列表默认只列共用的攻略）；删掉 = 归档并记为否决，可以恢复。"""
    http, service = client
    service.create_game({"id": "minecraft", "name": "我的世界"})
    service.create_item("card", "minecraft", {"title": "苦力怕", "points": ["听到嘶嘶声后退"], "keywords": ["苦力怕"], "status": "active"})
    created = service.repo.apply_lessons("minecraft", "bot-demo", creates=[{
        "key": "exp:bot-demo:1", "title": "夜里别在河边待", "points": ["天黑前离开水边"], "pitfalls": ["9 月 18 日夜里在河边被溺尸戳死"],
        "keywords": ["河边", "溺尸"], "source": "自己的经历", "source_type": "experience", "status": "active",
        "meta": {"times": 2, "last_at": 1790000000.0, "evidence": [{"memory_id": 11, "at": 1790000000.0, "type": "death", "text": "被溺尸戳死"}]},
    }])
    lesson_id = created["created"][0]

    status, cards = await _json(await http.get(f"{BASE}/games/minecraft/cards"))
    assert status == 200 and [c["title"] for c in cards["items"]] == ["苦力怕"], "知识卡列表默认只列共用的攻略"
    status, everything = await _json(await http.get(f"{BASE}/games/minecraft/cards?owner=all"))
    assert status == 200 and everything["total"] == 2

    assert (await http.get(f"{BASE}/games/minecraft/lessons")).status_code == 400
    status, lessons = await _json(await http.get(f"{BASE}/games/minecraft/lessons?bot_id=bot-demo"))
    assert status == 200 and lessons["enabled"] is False and lessons["runs"] == []
    [lesson] = lessons["items"]
    assert lesson["experience"] is True and lesson["bot_id"] == "bot-demo" and lesson["meta"]["times"] == 2
    status, others = await _json(await http.get(f"{BASE}/games/minecraft/lessons?bot_id=bot-other"))
    assert others["items"] == [], "别的 Bot 看不到这条经验"

    status, overview = await _json(await http.get(f"{BASE}/overview"))
    assert overview["games"][0]["counts"]["cards"]["active"] == 1 and overview["games"][0]["counts"]["lessons"]["active"] == 1

    status, stale = await _json(await http.post(f"{BASE}/games/minecraft/lessons/{lesson_id}/veto", json={"bot_id": "bot-demo", "version": 99}))
    assert status == 409
    status, wrong = await _json(await http.post(f"{BASE}/games/minecraft/lessons/{lesson_id}/veto", json={"bot_id": "bot-other", "version": lesson["version"]}))
    assert status == 404 and wrong["error"]["code"] == "lesson_not_found"
    status, vetoed = await _json(await http.post(f"{BASE}/games/minecraft/lessons/{lesson_id}/veto", json={"bot_id": "bot-demo", "version": lesson["version"]}))
    assert status == 200 and vetoed["item"]["status"] == "archived" and vetoed["item"]["meta"]["vetoed"] is True
    status, restored = await _json(await http.post(
        f"{BASE}/games/minecraft/lessons/{lesson_id}/restore", json={"bot_id": "bot-demo", "version": vetoed["item"]["version"]},
    ))
    assert status == 200 and restored["item"]["status"] == "active" and "vetoed" not in restored["item"]["meta"]

    status, off = await _json(await http.post(f"{BASE}/games/minecraft/lessons/reflect", json={"bot_id": "bot-demo"}))
    assert status == 409 and off["error"]["code"] == "game_lessons_disabled"

    # 试一下：带上 bot_id 才算上她自己的经验
    status, trial = await _json(await http.post(f"{BASE}/games/minecraft/trial", json={"message": "河边好多溺尸", "bot_id": "bot-demo"}))
    assert status == 200 and trial["cards"][0]["title"] == "夜里别在河边待" and trial["cards"][0]["experience"] is True
    assert "你自己玩出来的经验" in trial["text"]
    status, anonymous = await _json(await http.post(f"{BASE}/games/minecraft/trial", json={"message": "河边好多溺尸"}))
    assert anonymous["cards"] == []
