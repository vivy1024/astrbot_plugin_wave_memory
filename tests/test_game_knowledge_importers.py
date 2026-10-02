"""导入器：通用格式导入导出、缺氧策略卡 / 时间线适配（虚构样例，结构同原资产）、oni_lore 记忆迁移；仓库不带预设攻略。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from domain.game_knowledge import GameKnowledgeError, fold, parse_conditions, parse_game_context
from engine.database import WaveMemoryDB
from engine.db.connection import ConnectionManager
from engine.db.game_knowledge_repo import GameKnowledgeRepo
from services.game_knowledge import GameKnowledgeService
from services.game_knowledge.importers import (
    adapt_oni_timeline,
    adapt_oni_tutorial_cards,
    cycle_conditions,
    detect_format,
    parse_oni_lore_content,
    split_points,
    suggest_keywords,
    title_segments,
)

# ── 虚构样例：字段结构与 oni_tutorial_cards.json / oni_timeline.json 相同，内容是编的 ──

ONI_CARDS = [
    {
        "id": "oni_ep01_main", "title": "EP01 虚构开局要点", "stage": "early", "ep": 1, "phase": "foundation",
        "tags": ["ONI", "缺氧", "教程", "EP01", "EARLY", "FOUNDATION"],
        "trigger": ["第1集", "EP01", "虚构开局要点", "筑基开局", "一句很长的痛点描述不应该成为触发词。"],
        "prefer": ["先造【虚构厕所】再造床", "选挖掘兴趣的小人"],
        "avoid": ["一开局就搞复杂自动化"],
        "risk": "low", "mode_bias": "chaos_can_act_small",
        "source_urls": ["https://www.bilibili.com/video/BV1AAAAAAAAA"],
        "last_reviewed": "2026-07-09", "stability": "current", "approx_cycle_hint": "0-30",
    },
    {
        "id": "oni_ep08_main", "title": "EP08 虚构制氧：电解水", "stage": "mid", "ep": 8, "phase": "industry",
        "tags": ["ONI", "EP08"], "trigger": ["第8集", "EP08", "虚构制氧：电解水", "工业转型与自动化"],
        "prefer": ["电解水制氧，上方留空间收集氢气"], "avoid": ["管道漏气"], "risk": "medium", "mode_bias": "x",
        "source_urls": ["https://www.bilibili.com/video/BV1AAAAAAAAA"],
        "last_reviewed": "2026-07-09", "stability": "needs_review", "approx_cycle_hint": "30-100",
    },
    {
        "id": "oni_ep19_main", "title": "EP19 虚构飞天", "stage": "space", "ep": 19, "phase": "space",
        "tags": [], "trigger": ["第19集", "EP19"], "prefer": ["先把火箭造好"], "avoid": [], "risk": "high", "mode_bias": "x",
        "source_urls": ["https://www.bilibili.com/video/BV1BBBBBBBBB"],
        "last_reviewed": "2026-07-09", "stability": "current", "approx_cycle_hint": "200+",
    },
]

ONI_TIMELINE = {
    "version": 1,
    "source": "从新开始",
    "series_map_count": 2,
    "episodes": [
        {
            "ep": 1, "title": "虚构开局要点", "series_title": "【缺氧从新开始#01】虚构开局要点", "bvid": "BV1AAAAAAAAA",
            "source_url": "https://www.bilibili.com/video/BV1AAAAAAAAA", "phase": "foundation", "phase_label": "筑基开局",
            "stage": "early", "approx_cycle_hint": "0-30", "pain": "第一天没有厕所，全家生病。",
            "goals": ["开局先解决如厕", "先造【虚构厕所】再造床"], "prerequisites": [],
            "prefer": ["选挖掘兴趣的小人"], "avoid": ["开局搞复杂自动化"],
            "steps": [{
                "step": 1, "id": "ep01_s01", "title": "拍下两个茅厕和洗手盆", "prefer": ["拍下两个茅厕和洗手盆"], "avoid": ["洗手盆朝向装反"],
                "knowledge_point_ids": ["kp_001"], "done_signals": ["茅厕造好"],
            }],
            "knowledge_count": 1, "missing_source": False, "no_transcript_points": False, "knowledge_point_ids": ["kp_001", "kp_001"],
        },
        {
            "ep": 8, "title": "虚构制氧：电解水", "series_title": "【缺氧从新开始#08】虚构制氧：电解水", "bvid": "BV1CCCCCCCCC",
            "source_url": "https://www.bilibili.com/video/BV1CCCCCCCCC", "phase": "industry", "phase_label": "工业转型与自动化",
            "stage": "mid", "approx_cycle_hint": "30-100", "pain": "藻类耗尽，基地缺氧。",
            "goals": ["电解水制氧模块"], "prerequisites": [1, 2], "prefer": [], "avoid": [],
            "steps": [], "knowledge_count": 0, "missing_source": False, "no_transcript_points": True, "knowledge_point_ids": [],
        },
    ],
    "extras": [{"id": "extra_other", "title": "非主轴教程补充", "knowledge_count": 2, "knowledge_point_ids": ["x1", "x2"]}],
    "report": {"episode_count": 2},
}


@pytest.fixture
def service(tmp_path):
    cm = ConnectionManager(str(tmp_path / "gk.sqlite"))
    svc = GameKnowledgeService(GameKnowledgeRepo(cm))
    try:
        yield svc
    finally:
        cm.close()


def test_detect_formats():
    assert detect_format(ONI_CARDS) == "oni_tutorial_cards"
    assert detect_format(ONI_TIMELINE) == "oni_timeline"
    assert detect_format({"format": "wavememory.game_knowledge", "cards": []}) == "generic"
    assert detect_format({"cards": [{"title": "x"}]}) == "generic"
    for bad in ({"hello": 1}, [], [1, 2], "text", None):
        with pytest.raises(GameKnowledgeError) as info:
            detect_format(bad)
        assert info.value.code == "unknown_import_format"


def test_cycle_hint_conversion_and_keyword_helpers():
    # 教程每集的周期只是大致进度：写成「参考」条件，对不上时不挡、只排后
    assert cycle_conditions("0-30") == ["参考 cycle 0..30"]
    assert cycle_conditions("100 - 200") == ["参考 cycle 100..200"]
    assert cycle_conditions("200+") == ["参考 cycle >= 200"]
    assert cycle_conditions("早期") == [] and cycle_conditions(None) == []
    assert title_segments("入门第二道坎：制氧模块，包教包会") == ["制氧模块"]
    assert "我会出手" not in title_segments("冷却模块。无所谓，我会出手")
    words = suggest_keywords("虚构标题：液冷机", ["用【液体调节器】降温"], extra=["EP10", "第10集"])
    assert words[:3] == ["EP10", "第10集", "液体调节器"]
    assert split_points("第一句。第二句很长" + "长" * 200 + "。第三句。")[0] == "第一句。"
    assert all(len(point) <= 150 for point in split_points("字" * 400))


def test_oni_tutorial_cards_adapter():
    package = adapt_oni_tutorial_cards(copy.deepcopy(ONI_CARDS))
    assert package.format == "oni_tutorial_cards" and package.game["id"] == "oni"
    first, second, third = package.cards
    assert first["key"] == "oni_ep01" and first["title"] == "EP01 虚构开局要点" and first["stage"] == "筑基开局"
    assert first["conditions"] == ["参考 cycle 0..30"] and third["conditions"] == ["参考 cycle >= 200"]
    assert first["status"] == "draft" and first["source_type"] == "tutorial"
    assert first["source"] == "B站教程《缺氧从新开始》EP01「虚构开局要点」（BV1AAAAAAAAA）"
    assert first["source_url"] == "https://www.bilibili.com/video/BV1AAAAAAAAA"
    assert {"EP01", "第1集", "虚构厕所"} <= set(first["keywords"])
    assert not any("痛点描述" in word for word in first["keywords"]), "长句触发词不当关键词"
    assert first["points"] == ONI_CARDS[0]["prefer"] and first["pitfalls"] == ONI_CARDS[0]["avoid"]
    assert first["meta"]["ep"] == 1 and first["meta"]["bvid"] == "BV1AAAAAAAAA"
    assert "stability=needs_review" in second["note"]
    assert "同时标在 EP01、EP08 上" in first["note"] and "同时标在" in second["note"], "同一 bvid 标在多集上提示核对"
    assert "同时标在" not in third["note"]


def test_oni_timeline_adapter():
    package = adapt_oni_timeline(copy.deepcopy(ONI_TIMELINE))
    first, second = package.cards
    assert first["key"] == "oni_ep01" and first["title"] == "EP01 虚构开局要点" and first["stage"] == "筑基开局"
    assert first["points"] == ["开局先解决如厕", "先造【虚构厕所】再造床", "拍下两个茅厕和洗手盆", "选挖掘兴趣的小人"], "目标 → 分步 → 其余建议"
    assert first["pitfalls"] == ["典型痛点：第一天没有厕所，全家生病。", "开局搞复杂自动化", "洗手盆朝向装反"]
    assert first["conditions"] == ["参考 cycle 0..30"]
    assert first["source"] == "B站教程《缺氧从新开始》EP01「虚构开局要点」（BV1AAAAAAAAA）"
    assert first["meta"]["knowledge_point_ids"] == ["kp_001"] and first["meta"]["series_title"].startswith("【缺氧从新开始#01】")
    assert second["meta"]["prerequisites"] == [1, 2] and "字幕知识点偏少" in second["note"]
    assert any("extras" in warning and "2 个知识点编号" in warning for warning in package.warnings)


def test_oni_cards_then_timeline_merge_into_same_active_cards(service):
    service.create_game({"id": "oni", "name": "缺氧", "aliases": ["ONI", "Oxygen Not Included"]})
    preview = service.preview_import("oni", copy.deepcopy(ONI_CARDS))
    assert preview["format"] == "oni_tutorial_cards" and preview["cards"]["new"] == 3 and preview["default_status_mode"] == "auto"
    first = service.import_data("oni", copy.deepcopy(ONI_CARDS), status_mode="active")
    assert first["cards"]["created"] == 3
    preview_timeline = service.preview_import("oni", copy.deepcopy(ONI_TIMELINE))
    assert preview_timeline["cards"]["existing"] == 2 and preview_timeline["cards"]["new"] == 0
    merged = service.import_data("oni", copy.deepcopy(ONI_TIMELINE), status_mode="active", on_existing="merge")
    assert merged["cards"] == {"created": 0, "updated": 2, "unchanged": 0, "skipped": 0}
    card = service.repo.get_item_by_key("card", "oni", "oni_ep01")
    assert card["status"] == "active"
    assert card["points"][:3] == ["开局先解决如厕", "先造【虚构厕所】再造床", "拍下两个茅厕和洗手盆"], "时间线的目标与分步排在前面"
    assert "典型痛点：第一天没有厕所，全家生病。" in card["pitfalls"]
    assert card["meta"]["asset"] == "oni_timeline" and card["meta"]["risk"] == "low", "meta 合并保留策略卡字段"
    assert service.import_data("oni", copy.deepcopy(ONI_TIMELINE), status_mode="active", on_existing="merge")["cards"]["unchanged"] == 2
    # 周期条件 + 关键词：第 45 周期问「电解水」命中 EP08，不命中 EP01
    match = service.match(parse_game_context({"id": "oni", "state": {"cycle": 45}}), message="电解水怎么造")
    assert [hit.card["key"] for hit in match.cards] == ["oni_ep08"]
    # 周期对不上（第 12 周期就想造电解水）：「参考」条件不挡，照样给 EP08，只是分数低一些
    early = service.match(parse_game_context({"id": "oni", "state": {"cycle": 12}}), message="电解水怎么造")
    assert [hit.card["key"] for hit in early.cards] == ["oni_ep08"]
    assert early.cards[0].score < match.cards[0].score and "参考条件，不挡" in early.cards[0].reason


def test_generic_import_export_round_trip_and_validation(service):
    service.create_game({"id": "minecraft", "name": "我的世界", "aliases": ["MC"]})
    document = {
        "format": "wavememory.game_knowledge",
        "format_version": 1,
        "game": {"id": "minecraft", "name": "我的世界"},
        "cards": [
            {"key": "mc_night", "title": "第一夜", "stage": "前期", "points": ["天黑前封好房子"],
             "trigger": {"keywords": ["夜晚", "MC"], "conditions": ["time_of_day == night", {"key": "health", "op": "lt", "value": 8}]},
             "source": "测试", "status": "active"},
            {"title": "没有键的卡", "points": ["自动生成键"]},
            {"key": "bad", "title": "条件写错", "points": ["x"], "trigger": {"conditions": ["health < 很低"]}},
            {"key": "empty", "title": "空卡"},
        ],
        "terms": [{"term": "苦力怕", "explanation": "会爆炸"}, {"term": "", "explanation": "没有术语"}],
    }
    preview = service.preview_import("minecraft", document)
    assert preview["default_status_mode"] == "draft", "通用文件默认先当草稿审核"
    assert preview["cards"] == {"total": 4, "new": 2, "existing": 0, "invalid": 2, "by_status": {"active": 1, "draft": 1}}
    assert {e["code"] for e in preview["errors"]} == {"invalid_condition", "card_content_required", "term_required"}
    assert any("游戏名" in w for w in preview["warnings"]), "游戏名当触发词被去掉并提示"
    forced = service.import_data("minecraft", document, status_mode="draft")
    assert forced["cards"]["created"] == 2 and forced["invalid"] == {"cards": 2, "terms": 1}
    night = service.repo.get_item_by_key("card", "minecraft", "mc_night")
    assert night["status"] == "draft" and night["keywords"] == ["夜晚"] and night["source_type"] == "import"
    exported = service.export_game("minecraft")
    assert exported["format"] == "wavememory.game_knowledge" and exported["game"]["aliases"] == ["MC"]
    card = next(c for c in exported["cards"] if c["key"] == "mc_night")
    assert card["trigger"]["conditions"] == [{"key": "time_of_day", "op": "eq", "value": "night"}, {"key": "health", "op": "lt", "value": 8}]
    service.create_game({"id": "copy_game", "name": "复制"})
    again = service.import_data("copy_game", exported, status_mode="auto")
    assert again["cards"]["created"] == 2 and again["terms"]["created"] == 1
    copied = service.repo.get_item_by_key("card", "copy_game", "mc_night")
    assert {k: copied[k] for k in ("title", "stage", "points", "keywords", "conditions", "status")} == {k: night[k] for k in ("title", "stage", "points", "keywords", "conditions", "status")}
    assert any("导入到当前游戏库 copy_game" in w for w in again["warnings"])
    assert service.export_game("minecraft", statuses=["active"])["cards"] == []
    with pytest.raises(GameKnowledgeError):
        service.import_data("minecraft", {"hello": "world"})
    with pytest.raises(GameKnowledgeError):
        service.import_data("nope", document)


def test_parse_oni_lore_content():
    parsed = parse_oni_lore_content("[EP06·挖向原油区，野外炼钢详细注意事项 · #18/30 · mid] 水的相变温度：零度结冰，一百度沸腾。")
    assert parsed == {
        "ep": 6, "episode_title": "挖向原油区，野外炼钢详细注意事项", "index": 18, "total": 30, "stage_code": "mid",
        "title": "水的相变温度", "text": "零度结冰，一百度沸腾。",
    }
    spaced = parse_oni_lore_content("[ EP 1 ・ 虚构标题 ・ # 3 / 29 ・ early ]没有冒号的一段知识点")
    assert spaced["ep"] == 1 and spaced["index"] == 3 and spaced["title"] == "" and spaced["text"] == "没有冒号的一段知识点"
    assert parse_oni_lore_content("没有前缀的一段话") is None


def _memory_db(tmp_path, rows):
    db = WaveMemoryDB(str(tmp_path / "wave.sqlite"), dimension=3)
    for memory_id, content, source in rows:
        db._cm.execute_write(
            "INSERT INTO memories (id, group_id, sender_id, sender_name, content, timestamp, memory_type, source) VALUES (?, '', '', '', ?, 1.0, 'message', ?)",
            (memory_id, content, source),
        )
    db._cm.commit()
    return db


def test_oni_lore_migration_to_drafts_is_idempotent_and_keeps_memories(tmp_path):
    rows = [
        (101, "[EP06·挖向原油区，野外炼钢详细注意事项 · #18/30 · mid] 水的相变温度：零度以下结冰，一百度沸腾。", "oni_lore"),
        (102, "[EP01·虚构开局要点 · #3/29 · early] 选人技巧：优先挑挖掘兴趣的小人。", "oni_lore"),
        (103, "[EP01·虚构开局要点 · #3/29 · early] 选人技巧：优先挑挖掘兴趣的小人。", "oni_lore"),
        (104, "没有前缀的缺氧知识点：藻类会被制氧机消耗。", "oni_lore"),
        (105, "测试群友甲说今晚一起玩缺氧", "live"),
    ]
    db = _memory_db(tmp_path, rows)
    try:
        service = GameKnowledgeService(db.game_knowledge)
        service.create_game({"id": "oni", "name": "缺氧"})
        service.create_item("card", "oni", {"key": "oni_ep06", "title": "EP06 挖向原油区", "points": ["x"], "source_url": "https://www.bilibili.com/video/BV1DDDDDDDDD", "meta": {"ep": 6}})
        assert service.scan_legacy("oni_lore") == {"source": "oni_lore", "rows": 4, "parsed": 3}
        result = service.migrate_legacy("oni_lore")
        assert (result["rows"], result["parsed"], result["parse_failed"], result["duplicates"]) == (4, 3, 1, 1)
        assert result["cards"]["created"] == 3 and result["created_game"] is False
        cards = {c["key"]: c for c in service.list_items("card", "oni", source_type="memory_migration")["items"]}
        assert set(cards) == {"oni_lore_m101", "oni_lore_m102", "oni_lore_m104"}
        point = cards["oni_lore_m101"]
        assert point["status"] == "draft" and point["title"] == "水的相变温度" and point["stage"] == "工业转型与自动化"
        assert point["conditions"] == [{"key": "cycle", "op": "between", "min": 30, "max": 100, "soft": True}]
        assert point["source"] == "B站教程《缺氧从新开始》EP06「挖向原油区，野外炼钢详细注意事项」字幕知识点 #18/30"
        assert point["source_url"] == "https://www.bilibili.com/video/BV1DDDDDDDDD", "同集的教程卡有出处链接时沿用"
        assert point["meta"]["memory_id"] == 101 and "错字较多" in point["note"]
        assert cards["oni_lore_m104"]["meta"]["parse_failed"] and "前缀没解析出集数" in cards["oni_lore_m104"]["note"]
        again = service.migrate_legacy("oni_lore")
        assert again["cards"] == {"created": 0, "updated": 0, "unchanged": 0, "skipped": 3}
        remaining = db._cm.execute_read("SELECT id, content FROM memories ORDER BY id").fetchall()
        assert [tuple(r) for r in remaining] == [(mid, content) for mid, content, _ in rows], "原记忆行不删不改"
        legacy = service.legacy_sources()[0]
        assert legacy["rows"] == 4 and legacy["migrated"] == 3 and legacy["scanned"]
        # 草稿不注入
        assert service.match(parse_game_context({"id": "oni", "state": {"cycle": 45}}), message="水的相变温度").cards == []
    finally:
        db.close()


def test_oni_lore_migration_skips_safely_without_rows(tmp_path):
    db = _memory_db(tmp_path, [(1, "普通聊天", "live")])
    try:
        service = GameKnowledgeService(db.game_knowledge)
        result = service.migrate_legacy("oni_lore")
        assert result["skipped"] and result["reason"] == "no_rows" and result["rows"] == 0
        assert service.get_game("oni") is None, "没有旧行时不新建游戏库"
        with pytest.raises(GameKnowledgeError):
            service.migrate_legacy("unknown_source")
    finally:
        db.close()


def test_bundled_topic_pack_is_draft_only():
    """仓库附带的缺氧主题整理只能作为草稿素材，不能变成默认启用的预设攻略。"""
    root = Path(__file__).resolve().parents[1]
    pack = root / "assets" / "game_knowledge" / "oni_topic_cards.v1.json"
    assert pack.exists()
    document = json.loads(pack.read_text(encoding="utf-8"))
    assert document["pack"]["force_status"] == "draft"
    assert document["game"]["id"] == "oni"
    assert document["cards"] and all(card.get("status") == "draft" for card in document["cards"])
