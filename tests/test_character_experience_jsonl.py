"""JSONL 导入导出：兼容 v5 bzz_memories_raw.jsonl 的字段格式（这里是虚构内容），导出可原样再导入。"""

from __future__ import annotations

import json

import pytest

from character_experience_support import bot, make_registry, make_service
from engine.db.character_experience_repo import CharacterExperienceError
from engine.db.connection import ConnectionManager

# v5 格式：chapter, title, scene_idx, memory, emotion, characters, arc, stage, approx_time
V5_LINES = [
    {"chapter": 12, "title": "第12章 雨夜", "scene_idx": 30, "memory": "那晚下着大雨，我在破庙里躲雨，遇到了后来的师姐。",
     "emotion": "紧张", "characters": ["师姐"], "arc": "arc02_下山游历", "stage": "雨夜破庙", "approx_time": "第2年夏"},
    {"chapter": 2, "title": "第2章 山门", "scene_idx": 1, "memory": "我拜师那天起得很早，把山门前的石阶扫了三遍。",
     "emotion": "日常", "characters": [], "arc": "arc01_山门岁月", "stage": "拜师", "approx_time": "第1年春"},
    {"chapter": 2, "title": "第2章 山门", "scene_idx": 1, "memory": "师父收下我的时候只说了一句：先把字练好。",
     "emotion": "感动", "characters": ["师父"], "arc": "arc01_山门岁月", "stage": "拜师", "approx_time": "第1年春"},
    {"chapter": 40, "title": "第40章 归来", "scene_idx": 99, "memory": "回山那天我在山门口站了很久，石阶还是那么长。",
     "emotion": "伤感", "characters": [], "arc": "arc10_归山", "stage": "归来", "approx_time": "第5年冬"},
]


@pytest.fixture
def service(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    registry = make_registry(cm, bot("oc_a", "云栖", "10000005"), bot("oc_b", "云岫", "10000006"))
    yield make_service(cm, registry)
    cm.close()


def _jsonl(lines):
    return "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)


def test_import_v5_format_builds_stages_and_maps_fields(service):
    result = service.import_jsonl("oc_a", _jsonl(V5_LINES) + "\n不是JSON\n" + json.dumps({"emotion": "日常"}))
    assert result["lines"] == 6 and result["valid"] == 4 and result["inserted"] == 4 and result["error_count"] == 2
    assert [e["line"] for e in result["errors"]] == [5, 6]
    assert result["stages_created"] == ["arc01_山门岁月", "arc02_下山游历", "arc10_归山"], "arc 按自然顺序建阶段（arc10 在 arc2 之后）"
    stages = service.repo.list_stages("oc_a")
    assert [(s["title"], s["ordinal"]) for s in stages] == [("山门岁月", 1), ("下山游历", 2), ("归山", 3)]
    items, _ = service.repo.list("oc_a", limit=10)
    assert [i["content"][:4] for i in items] == ["我拜师那", "师父收下", "那晚下着", "回山那天"], "按阶段、章节、场景内顺序"
    first = items[0]
    assert first["status"] == "draft" and first["canon_level"] == "canon" and first["source_kind"] == "jsonl_import"
    assert first["stage_key"] == "arc01_山门岁月" and first["plot_stage"] == "拜师" and first["story_time"] == "第1年春"
    assert first["source_ref"]["chapter"] == 2 and first["source_ref"]["chapter_title"] == "第2章 山门" and first["source_ref"]["scene_idx"] == 1


def test_duplicate_handling_skip_fill_overwrite(service):
    service.import_jsonl("oc_a", _jsonl([{"memory": V5_LINES[0]["memory"]}]))
    skipped = service.import_jsonl("oc_a", _jsonl(V5_LINES[:1]))
    assert skipped["inserted"] == 0 and skipped["duplicates"] == 1
    assert service.repo.list("oc_a")[0][0]["emotion"] == ""
    filled = service.import_jsonl("oc_a", _jsonl(V5_LINES[:1]), on_duplicate="fill")
    assert filled["updated"] == 1
    row = service.repo.list("oc_a")[0][0]
    assert row["emotion"] == "紧张" and row["characters"] == ["师姐"] and row["story_time"] == "第2年夏"
    changed = dict(V5_LINES[0], approx_time="第2年秋")
    service.import_jsonl("oc_a", _jsonl([changed]), on_duplicate="fill")
    assert service.repo.list("oc_a")[0][0]["story_time"] == "第2年夏", "补空字段不覆盖已有值"
    service.import_jsonl("oc_a", _jsonl([changed]), on_duplicate="overwrite")
    assert service.repo.list("oc_a")[0][0]["story_time"] == "第2年秋"


def test_import_options_are_validated(service):
    with pytest.raises(CharacterExperienceError) as err:
        service.import_jsonl("oc_a", _jsonl(V5_LINES), status="published")
    assert err.value.code == "invalid_status"
    with pytest.raises(CharacterExperienceError) as err:
        service.import_jsonl("nobody", _jsonl(V5_LINES))
    assert err.value.code == "bot_not_found"
    active = service.import_jsonl("oc_a", _jsonl(V5_LINES[:1]), status="active", canon_level="inferred")
    row = service.repo.list("oc_a")[0][0]
    assert active["inserted"] == 1 and row["status"] == "active" and row["canon_level"] == "inferred"


def test_export_is_v5_compatible_and_round_trips(service):
    service.import_jsonl("oc_a", _jsonl(V5_LINES), status="active")
    exported = service.export_jsonl("oc_a")
    records = [json.loads(line) for line in exported.splitlines()]
    assert len(records) == 4
    for key in ("memory", "emotion", "characters", "arc", "stage", "approx_time", "chapter", "title", "scene_idx"):
        assert key in records[0], key
    assert records[0]["arc"] == "arc01_山门岁月" and records[0]["stage_title"] == "山门岁月" and records[0]["status"] == "active"
    assert service.export_jsonl("oc_a", statuses=["draft"]) == ""

    again = service.import_jsonl("oc_b", exported, status="as_file")
    assert again["inserted"] == 4 and again["stages_created"] == ["arc01_山门岁月", "arc02_下山游历", "arc10_归山"]
    a_items, _ = service.repo.list("oc_a", limit=10)
    b_items, _ = service.repo.list("oc_b", limit=10)
    fields = ("content", "story_time", "stage_key", "seq", "plot_stage", "emotion", "characters", "canon_level", "status")
    assert [{f: i[f] for f in fields} for i in a_items] == [{f: i[f] for f in fields} for i in b_items]
