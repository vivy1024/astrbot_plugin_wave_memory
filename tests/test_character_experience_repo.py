"""角色经历仓储：幂等建表、去重、合并、版本冲突（整体回滚）、时间线阶段、全文索引与场景记录。全部虚构数据。"""

from __future__ import annotations

import pytest

from domain.bot_profile import BotProfile
from domain.character_experience import CharacterExperienceSettings, eligible_stage_keys
from engine.db.character_experience_repo import (
    CharacterExperienceConflict,
    CharacterExperienceError,
    CharacterExperienceRepo,
)
from engine.db.connection import ConnectionManager
from engine.db.fts_cjk import match_expr


@pytest.fixture
def repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    yield CharacterExperienceRepo(cm)
    cm.close()


def _item(repo, content, **fields):
    return repo.clean_item({"content": content, **fields})


def test_schema_is_idempotent_and_dedupes_by_content(repo):
    CharacterExperienceRepo(repo.cm)  # 再建一次表不报错
    items = [_item(repo, "那年夏天我在山脚下捡到一只受伤的小鹿。"), _item(repo, "我第一次坐马车进城。")]
    first = repo.add_many("oc_a", items, created_by="test")
    assert len(first["inserted"]) == 2
    # 空白差异算同一条；另一个 Bot 可以有同样的正文
    again = repo.add_many("oc_a", [_item(repo, "那年夏天 我在山脚下捡到一只受伤的小鹿。")])
    assert again == {"inserted": [], "duplicates": [first["inserted"][0]], "updated": []}
    other = repo.add_many("oc_b", [_item(repo, "我第一次坐马车进城。")])
    assert len(other["inserted"]) == 1
    assert repo.counts("oc_a")["total"] == 2 and repo.counts("oc_b")["total"] == 1


def test_fill_and_overwrite_merge_metadata_on_duplicates(repo):
    [eid] = repo.add_many("oc_a", [_item(repo, "师父教我第一套剑法。", story_time="十岁")])["inserted"]
    filled = repo.add_many("oc_a", [_item(repo, "师父教我第一套剑法。", story_time="九岁", emotion="感动", characters=["师父"])], on_duplicate="fill")
    assert filled["updated"] == [eid]
    row = repo.get("oc_a", eid)
    assert row["story_time"] == "十岁" and row["emotion"] == "感动" and row["characters"] == ["师父"] and row["version"] == 2
    repo.add_many("oc_a", [_item(repo, "师父教我第一套剑法。", story_time="九岁")], on_duplicate="overwrite")
    assert repo.get("oc_a", eid)["story_time"] == "九岁"


def test_validation_rejects_bad_values(repo):
    with pytest.raises(CharacterExperienceError) as err:
        repo.clean_item({"content": "  "})
    assert err.value.code == "content_required"
    with pytest.raises(CharacterExperienceError) as err:
        repo.clean_item({"content": "有内容", "canon_level": "rumor"})
    assert err.value.code == "invalid_canon_level"
    with pytest.raises(CharacterExperienceError) as err:
        repo.clean_item({"content": "有内容", "stage_key": "a/b"})
    assert err.value.code == "invalid_stage_key"


def test_update_checks_version_and_clears_vector_when_content_changes(repo):
    [eid] = repo.add_many("oc_a", [_item(repo, "我在海边捡贝壳。", vector=b"\x00" * 32)])["inserted"]
    assert repo.get("oc_a", eid)["has_vector"]
    updated = repo.update("oc_a", eid, {"content": "我在海边捡了一下午贝壳。", "emotion": "温馨"}, expected_version=1)
    assert updated["version"] == 2 and updated["emotion"] == "温馨" and not updated["has_vector"]
    with pytest.raises(CharacterExperienceConflict) as err:
        repo.update("oc_a", eid, {"emotion": "伤感"}, expected_version=1)
    assert err.value.code == "version_conflict" and err.value.ids == [str(eid)]
    with pytest.raises(CharacterExperienceError) as err:
        repo.update("oc_b", eid, {"emotion": "伤感"}, expected_version=2)  # 别的 Bot 的经历按不存在处理
    assert err.value.code == "experience_not_found"


def test_bulk_status_is_all_or_nothing(repo):
    ids = repo.add_many("oc_a", [_item(repo, "第一件事。"), _item(repo, "第二件事。")])["inserted"]
    with pytest.raises(CharacterExperienceConflict) as err:
        repo.set_status("oc_a", [(ids[0], 1), (ids[1], 99)], "active")
    assert err.value.ids == [str(ids[1])]
    assert {repo.get("oc_a", i)["status"] for i in ids} == {"draft"}, "有一条冲突就整体不写"
    assert repo.set_status("oc_a", [(ids[0], 1), (ids[1], 1)], "active") == ids
    assert repo.counts("oc_a")["by_status"]["active"] == 2


def test_stages_crud_reorder_and_in_use(repo):
    s1 = repo.save_stage("oc_a", "s1", {"title": "童年", "story_time": "七岁到十二岁"}, expected_version=0)
    repo.save_stage("oc_a", "s2", {"title": "求学"}, expected_version=0)
    assert [s["ordinal"] for s in repo.list_stages("oc_a")] == [1, 2]
    with pytest.raises(CharacterExperienceConflict):
        repo.save_stage("oc_a", "s1", {"title": "改名"}, expected_version=0)
    renamed = repo.save_stage("oc_a", "s1", {"title": "小时候", "key_events": ["捡到小鹿", {"text": "拜师", "basis": [2, "3"]}]}, expected_version=s1["version"])
    assert renamed["title"] == "小时候" and renamed["key_events"] == [{"text": "捡到小鹿", "basis": []}, {"text": "拜师", "basis": [2, 3]}]
    stages = repo.reorder_stages("oc_a", [("s2", 1), ("s1", renamed["version"])])
    assert [s["stage_key"] for s in stages] == ["s2", "s1"]
    with pytest.raises(CharacterExperienceError) as err:
        repo.reorder_stages("oc_a", [("s2", 2)])
    assert err.value.code == "stage_list_mismatch"
    [eid] = repo.add_many("oc_a", [_item(repo, "拜师那天下着雨。", stage_key="s1")])["inserted"]
    with pytest.raises(CharacterExperienceError) as err:
        repo.delete_stage("oc_a", "s1", expected_version=repo.get_stage("oc_a", "s1")["version"])
    assert err.value.code == "stage_in_use"
    with pytest.raises(CharacterExperienceError) as err:
        repo.set_stage("oc_a", [(eid, 1)], "missing")
    assert err.value.code == "stage_not_found"
    assert repo.set_stage("oc_a", [(eid, 1)], "") == [eid]
    repo.delete_stage("oc_a", "s1", expected_version=repo.get_stage("oc_a", "s1")["version"])
    assert [s["stage_key"] for s in repo.list_stages("oc_a")] == ["s2"]


def test_fts_search_only_active_rows_of_the_bot(repo):
    ids = repo.add_many("oc_a", [
        _item(repo, "灯会那晚我走丢了，是师父把我找回来的。", status="active"),
        _item(repo, "灯会上我第一次吃糖人。"),  # 草稿：不召回
    ])["inserted"]
    repo.add_many("oc_b", [_item(repo, "灯会我没去成。", status="active")])
    hits = repo.fts_search("oc_a", match_expr(["灯会"]))
    assert [hit[0] for hit in hits] == [ids[0]]
    assert repo.fts_search("oc_a", match_expr(["马车"])) == []


def test_scene_log_records_processed_and_failed(repo):
    repo.record_scene_failure("oc_a", "novel:ch1:x", kind="novel", job_id="req-1", error="boom")
    assert repo.processed_scene_keys("oc_a", ["novel:ch1:x"]) == set()
    result = repo.record_scene("oc_a", "novel:ch1:x", [_item(repo, "那一章里我救了一只猫。")], kind="novel", job_id="req-1")
    assert len(result["inserted"]) == 1
    repo.record_scene("oc_a", "novel:ch2:y", [], kind="novel", job_id="req-1")
    assert repo.processed_scene_keys("oc_a", ["novel:ch1:x", "novel:ch2:y", "novel:ch3:z"]) == {"novel:ch1:x", "novel:ch2:y"}
    # 已处理的场景不会被一次迟到的失败改回失败
    repo.record_scene_failure("oc_a", "novel:ch1:x", kind="novel", job_id="req-2", error="late")
    assert repo.scene_stats("oc_a") == {"novel": {"done": 1, "empty": 1}}


def test_settings_defaults_off_and_round_trip_through_profile():
    old = BotProfile.from_dict({"db_id": "oc_a", "name": "小鹿", "qq_id": "10000001"})
    assert old.character_experience == CharacterExperienceSettings()
    assert old.character_experience.enabled is False, "旧 Profile 没有这一段 = 关闭"
    data = old.to_dict()
    data["character_experience"] = {"enabled": "true", "current_stage": " s2 ", "top_k": 99, "max_chars": 10, "min_score": "0.5",
                                    "legacy_sources": "old_exp, old_exp", "lore_categories": ["小鹿经历"]}
    parsed = BotProfile.from_dict(data).character_experience
    assert parsed.enabled is True and parsed.current_stage == "s2"
    assert parsed.top_k == 10 and parsed.max_chars == 100 and parsed.min_score == 0.5
    assert parsed.legacy_sources == ["old_exp"] and parsed.lore_categories == ["小鹿经历"]
    assert BotProfile.from_dict(BotProfile.from_dict(data).to_dict()).character_experience == parsed


def test_eligible_stages_follow_current_time_point():
    stages = [{"stage_key": "s1", "ordinal": 1}, {"stage_key": "s2", "ordinal": 2}, {"stage_key": "s3", "ordinal": 3}]
    assert eligible_stage_keys(stages, "") == (None, "")
    assert eligible_stage_keys(stages, "s2") == ({"s1", "s2"}, "")
    assert eligible_stage_keys(stages, "gone") == (set(), "current_stage_missing")
