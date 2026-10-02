"""游戏知识库存储：迁移幂等、游戏库 / 知识卡 / 词条的增改与版本冲突、批量启用归档、检索索引、导入去重与合并。"""

from __future__ import annotations

import sqlite3

import pytest

from domain.game_knowledge import GameKnowledgeConflict, GameKnowledgeError, normalize_card_fields, normalize_term_fields
from engine.db.connection import ConnectionManager
from engine.db.game_knowledge_repo import GameKnowledgeRepo
from engine.db.migrations.game_knowledge import GAME_KNOWLEDGE_TABLES, ensure_game_knowledge_schema


@pytest.fixture
def repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "game.sqlite"))
    try:
        yield GameKnowledgeRepo(cm)
    finally:
        cm.close()


def _card(**overrides):
    payload = {"key": "mc_night", "title": "第一夜怎么过", "stage": "前期", "points": ["天黑前封好房子"], "keywords": ["夜晚"]}
    payload.update(overrides)
    return payload


def _tables(cm):
    return {row[0] for row in cm.execute_read("SELECT name FROM sqlite_master WHERE type IN ('table')").fetchall()}


def test_migration_is_idempotent_and_backfills_missing_columns(tmp_path):
    path = str(tmp_path / "old.sqlite")
    # 模拟一个更早版本留下的表：缺 note / meta 等后加的列
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE game_knowledge_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, card_key TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft', created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (game_id, card_key))""")
    con.execute("INSERT INTO game_knowledge_cards (game_id, card_key, title, status, created_at, updated_at) VALUES ('oni', 'k', '旧卡', 'active', 1, 1)")
    con.commit()
    con.close()
    cm = ConnectionManager(path)
    try:
        ensure_game_knowledge_schema(cm)
        ensure_game_knowledge_schema(cm)
        assert set(GAME_KNOWLEDGE_TABLES) <= _tables(cm)
        columns = {row[1] for row in cm.execute_read("PRAGMA table_info(game_knowledge_cards)").fetchall()}
        assert {"note", "meta", "keywords", "conditions", "version", "source_type"} <= columns
        row = cm.execute_read("SELECT title, status, version, keywords, meta FROM game_knowledge_cards").fetchone()
        assert tuple(row) == ("旧卡", "active", 1, "[]", "{}"), "补列不改已有数据"
        GameKnowledgeRepo(cm)  # 再建一次仓储也不报错
    finally:
        cm.close()


def test_game_crud_and_version_conflict(repo):
    game = repo.create_game({"id": "Minecraft", "name": "我的世界", "aliases": "MC、Minecraft", "description": "Java 版"})
    assert game["id"] == "minecraft" and game["aliases"] == ["MC", "Minecraft"] and game["enabled"] and game["version"] == 1
    assert game["counts"]["cards"] == {"draft": 0, "active": 0, "archived": 0}
    with pytest.raises(GameKnowledgeConflict) as info:
        repo.create_game({"id": "minecraft", "name": "重复"})
    assert info.value.code == "game_exists"
    updated = repo.update_game("minecraft", {"enabled": False, "description": "停用测试"}, expected_version=1)
    assert updated["enabled"] is False and updated["version"] == 2
    with pytest.raises(GameKnowledgeConflict) as info:
        repo.update_game("minecraft", {"name": "改名"}, expected_version=1)
    assert info.value.code == "game_version_conflict" and info.value.actual == 2
    same = repo.update_game("minecraft", {"enabled": False}, expected_version=2)
    assert same["version"] == 2, "没有变化不升版本"
    with pytest.raises(GameKnowledgeError) as info:
        repo.update_game("nope", {"name": "x"}, expected_version=1)
    assert info.value.code == "game_not_found"
    repo.create_game({"id": "slay_the_spire", "name": "杀戮尖塔"})
    assert [g["id"] for g in repo.list_games()] == ["slay_the_spire", "minecraft"], "启用的在前"


def test_card_crud_version_conflict_and_status(repo):
    repo.create_game({"id": "minecraft", "name": "我的世界"})
    card = repo.create_item("card", "minecraft", _card(conditions=["time_of_day == night"]))
    assert card["status"] == "draft" and card["version"] == 1 and card["key"] == "mc_night"
    assert card["conditions"] == [{"key": "time_of_day", "op": "eq", "value": "night"}]
    with pytest.raises(GameKnowledgeConflict) as info:
        repo.create_item("card", "minecraft", _card())
    assert info.value.code == "card_key_exists"
    with pytest.raises(GameKnowledgeError) as info:
        repo.create_item("card", "nope", _card())
    assert info.value.code == "game_not_found"

    edited = repo.update_item("card", card["id"], {"points": ["有床就睡"], "status": "active"}, expected_version=1, game_id="minecraft")
    assert edited["points"] == ["有床就睡"] and edited["status"] == "active" and edited["version"] == 2, "正文和状态一起改只升一次版本"
    with pytest.raises(GameKnowledgeConflict) as info:
        repo.update_item("card", card["id"], {"title": "旧版本的编辑"}, expected_version=1, game_id="minecraft")
    assert info.value.code == "card_version_conflict" and info.value.expected == 1 and info.value.actual == 2
    with pytest.raises(GameKnowledgeError) as info:
        repo.update_item("card", card["id"], {"points": [], "pitfalls": []}, expected_version=2, game_id="minecraft")
    assert info.value.code == "card_content_required"
    with pytest.raises(GameKnowledgeError) as info:
        repo.update_item("card", card["id"], {"title": "x"}, expected_version=2, game_id="other")
    assert info.value.code == "card_not_found"
    unchanged = repo.update_item("card", card["id"], {"title": "第一夜怎么过"}, expected_version=2)
    assert unchanged["version"] == 2


def test_batch_status_reports_changed_unchanged_missing_conflicts(repo):
    repo.create_game({"id": "oni", "name": "缺氧"})
    a = repo.create_item("card", "oni", _card(key="a", title="甲"))
    b = repo.create_item("card", "oni", _card(key="b", title="乙"))
    c = repo.create_item("card", "oni", _card(key="c", title="丙", status="active"))
    result = repo.set_status("card", "oni", [a["id"], b["id"], c["id"], 9999, "x"], "active", versions={str(b["id"]): 7})
    assert result["changed"] == [a["id"]]
    assert result["unchanged"] == [c["id"]]
    assert result["missing"] == [9999]
    assert result["conflicts"] == [{"id": b["id"], "expected": 7, "actual": 1}]
    assert repo.get_item("card", a["id"])["version"] == 2
    archived = repo.set_status("card", "oni", [a["id"]], "archived")
    assert archived["changed"] == [a["id"]] and repo.get_item("card", a["id"])["status"] == "archived"
    with pytest.raises(GameKnowledgeError):
        repo.set_status("card", "oni", [a["id"]], "published")


def test_list_filters_and_chinese_search(repo):
    repo.create_game({"id": "minecraft", "name": "我的世界"})
    repo.create_item("card", "minecraft", _card(key="creeper", title="苦力怕", stage="前期", topic="战斗", points=["听到嘶嘶声马上后退"], status="active"))
    repo.create_item("card", "minecraft", _card(key="nether", title="去下界", stage="中期", points=["穿一件金护甲猪灵不打你"]))
    repo.create_item("card", "minecraft", _card(key="bed", title="床与重生点", stage="前期", points=["床用羊毛做"], status="archived"))
    assert repo.list_items("card", "minecraft")["total"] == 3
    assert [c["key"] for c in repo.list_items("card", "minecraft", status="active")["items"]] == ["creeper"]
    assert [c["key"] for c in repo.list_items("card", "minecraft", stage="中期")["items"]] == ["nether"]
    assert [c["key"] for c in repo.list_items("card", "minecraft", topic="战斗")["items"]] == ["creeper"]
    assert repo.topic_facets("minecraft") == [{"name": "战斗", "count": 1}]
    assert [c["key"] for c in repo.list_items("card", "minecraft", query="嘶嘶")["items"]] == ["creeper"]  # 两字切词命中正文
    assert [c["key"] for c in repo.list_items("card", "minecraft", query="护甲")["items"]] == ["nether"]
    assert [c["key"] for c in repo.list_items("card", "minecraft", query="床")["items"]] == ["bed"]  # 单字走 LIKE
    assert [c["key"] for c in repo.list_items("card", "minecraft", query="nether")["items"]] == ["nether"]  # 键
    assert [f["name"] for f in repo.stage_facets("minecraft")] == ["前期", "中期"]
    # 检索索引随编辑更新
    creeper = repo.get_item_by_key("card", "minecraft", "creeper")
    repo.update_item("card", creeper["id"], {"points": ["被闪电劈过的更危险"]}, expected_version=1)
    assert repo.list_items("card", "minecraft", query="嘶嘶")["total"] == 0
    assert repo.list_items("card", "minecraft", query="闪电")["total"] == 1
    from engine.db import fts_cjk

    assert [cid for cid, _ in repo.search_ids("card", "minecraft", fts_cjk.match_expr(["闪电"]))] == [creeper["id"]]
    assert repo.search_ids("card", "minecraft", fts_cjk.match_expr(["金护甲"])) == [], "默认只查启用的"
    assert repo.rebuild_fts() == 3


def test_terms_crud(repo):
    repo.create_game({"id": "minecraft", "name": "我的世界"})
    term = repo.create_item("term", "minecraft", {"term": "苦力怕", "aliases": ["Creeper"], "explanation": "会爆炸的怪"})
    assert term["key"].startswith("t_") and term["status"] == "draft"
    with pytest.raises(GameKnowledgeError) as info:
        repo.create_item("term", "minecraft", {"term": "空解释", "explanation": ""})
    assert info.value.code == "term_explanation_required"
    updated = repo.update_item("term", term["id"], {"explanation": "靠近后会爆炸"}, expected_version=1)
    assert updated["version"] == 2
    assert repo.list_items("term", "minecraft", query="爆炸")["total"] == 1
    assert repo.list_items("term", "minecraft", query="creeper")["total"] == 1


def test_import_skip_update_merge_keep_status(repo):
    repo.create_game({"id": "oni", "name": "缺氧"})

    def cards(*items):
        return [{**normalize_card_fields(item), "status": item.get("status", "draft")} for item in items]

    first = repo.import_items("oni", cards=cards(
        {"key": "oni_ep08", "title": "EP08 制氧", "points": ["电解水制氧"], "keywords": ["制氧"], "conditions": ["cycle 30..100"], "status": "active"},
        {"key": "oni_ep08", "title": "重复键", "points": ["x"]},
    ), terms=[{**normalize_term_fields({"term": "藻类箱", "explanation": "早期制氧"}), "status": "draft"}])
    assert first["cards"] == {"created": 1, "updated": 0, "unchanged": 0, "skipped": 1}
    assert first["terms"]["created"] == 1
    card = repo.get_item_by_key("card", "oni", "oni_ep08")
    repo.set_status("card", "oni", [card["id"]], "archived")

    skipped = repo.import_items("oni", cards=cards({"key": "oni_ep08", "title": "EP08 制氧", "points": ["新内容"]}))
    assert skipped["cards"]["skipped"] == 1 and repo.get_item_by_key("card", "oni", "oni_ep08")["points"] == ["电解水制氧"]

    merged = repo.import_items("oni", cards=cards({"key": "oni_ep08", "title": "EP08 制氧模块", "points": ["分步：先挖好房间", "电解水制氧"], "keywords": ["氧气"], "conditions": ["cycle 30..100", "health > 0"]}), on_existing="merge")
    assert merged["cards"]["updated"] == 1
    after = repo.get_item_by_key("card", "oni", "oni_ep08")
    assert after["title"] == "EP08 制氧模块"
    assert after["points"] == ["分步：先挖好房间", "电解水制氧"], "本次导入的在前，旧条目去重保留"
    assert after["keywords"] == ["氧气", "制氧"]
    assert [c["key"] for c in after["conditions"]] == ["cycle", "health"]
    assert after["status"] == "archived", "导入不改已有条目的状态"

    again = repo.import_items("oni", cards=cards({"key": "oni_ep08", "title": "EP08 制氧模块", "points": ["分步：先挖好房间", "电解水制氧"], "keywords": ["氧气"], "conditions": ["cycle 30..100", "health > 0"]}), on_existing="merge")
    assert again["cards"]["unchanged"] == 1

    replaced = repo.import_items("oni", cards=cards({"key": "oni_ep08", "title": "EP08", "points": ["只剩这一条"]}), on_existing="update")
    assert replaced["cards"]["updated"] == 1 and repo.get_item_by_key("card", "oni", "oni_ep08")["points"] == ["只剩这一条"]
    with pytest.raises(GameKnowledgeError):
        repo.import_items("oni", cards=[], on_existing="overwrite")
    with pytest.raises(GameKnowledgeError):
        repo.import_items("nope", cards=[])


def test_revision_changes_on_any_write(repo):
    repo.create_game({"id": "oni", "name": "缺氧"})
    before = repo.revision("oni")
    card = repo.create_item("card", "oni", _card(key="k", title="t"))
    after_create = repo.revision("oni")
    repo.set_status("card", "oni", [card["id"]], "active")
    after_status = repo.revision("oni")
    repo.update_game("oni", {"enabled": False}, expected_version=1)
    assert len({before, after_create, after_status, repo.revision("oni")}) == 4


def test_legacy_memory_rows_are_read_only_and_safe_without_table(tmp_path):
    cm = ConnectionManager(str(tmp_path / "no_memories.sqlite"))
    try:
        repo = GameKnowledgeRepo(cm)
        assert repo.legacy_memory_rows("oni_lore") == []
    finally:
        cm.close()
    path = str(tmp_path / "with_memories.sqlite")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, timestamp REAL, memory_type TEXT, source TEXT, resolution_state TEXT)")
    con.executemany("INSERT INTO memories VALUES (?, ?, 0, ?, ?, ?)", [
        (1, "a", "message", "oni_lore", "resolved"), (2, "b", "deleted", "oni_lore", None), (3, "c", "message", "oni_lore", "deleted"), (4, "d", "message", "live", None),
    ])
    con.commit()
    con.close()
    cm = ConnectionManager(path)
    try:
        rows = GameKnowledgeRepo(cm).legacy_memory_rows("oni_lore")
        assert [r["id"] for r in rows] == [1]
        assert cm.execute_read("SELECT COUNT(*) FROM memories").fetchone()[0] == 4
    finally:
        cm.close()
