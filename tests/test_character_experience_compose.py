"""按设定编写（原创角色）：起草人生时间线 → 工作台确认 → 逐条展开成第一人称经历（标为编造、记下依据段落）。虚构设定。"""

from __future__ import annotations

import asyncio

import pytest

from character_experience_support import FakeJobs, FakeLLM, FakeRunner, bot, job, make_registry, make_service
from engine.db.character_experience_repo import CharacterExperienceError
from engine.db.connection import ConnectionManager
from services.character_experience import EXPAND_KIND, TIMELINE_KIND
from services.character_experience_extract import parse_timeline, split_setting_paragraphs, timeline_prompts

SETTING = """青禾出生在海边的渔村，父亲是船匠。

十二岁那年村子遭了风暴，她跟着一位游方的药师离开了家乡。

她跟药师学了六年医术，性子安静，说话慢，但认定的事不会改。"""

TIMELINE = {
    "stages": [
        {"title": "渔村童年", "story_time": "出生到十二岁", "description": "在海边长大",
         "key_events": [{"text": "跟父亲学着修补渔船", "basis": [1]}, {"text": "风暴那一夜", "basis": [2, 9]}], "basis": [1, 2]},
        {"title": "随师学医", "story_time": "十二岁到十八岁", "description": "跟药师四处行医",
         "key_events": ["第一次独自给人看病"], "basis": [3]},
    ]
}


def _expand_output(prompt: str):
    if "修补渔船" in prompt:
        return [{"memory": "我小时候总蹲在船坞边上看父亲刨木头，后来他让我帮着递钉子。", "emotion": "温馨", "characters": ["父亲"]}]
    if "风暴那一夜" in prompt:
        return [{"memory": "风暴那一夜屋顶被掀开了一角，我抱着弟弟缩在灶台后面，一夜没合眼。", "emotion": "紧张", "characters": ["弟弟"]}]
    if "独自给人看病" in prompt:
        return [{"memory": "第一次独自给人看病，我把脉把了很久，手一直在抖。", "emotion": "紧张", "characters": []}]
    return []


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    registry = make_registry(cm, bot("oc_q", "青禾", "10000004", persona={"lore_title": "青禾的设定", "lore_lines": ["海边渔村长大"]}))
    jobs = FakeJobs()
    llm = FakeLLM([("设定：\n[1]", TIMELINE)], default=_expand_output)
    service = make_service(cm, registry, llm=llm, jobs=jobs)
    yield {"service": service, "jobs": jobs, "llm": llm}
    cm.close()


def _run_last(env, kind):
    request = [r for r in env["jobs"].requests.values() if r.kind == kind][-1]
    run, req = job(kind, request.payload, request_id=request.request_id)
    runner = FakeRunner(env["jobs"])
    handler = {TIMELINE_KIND: env["service"]._run_timeline, EXPAND_KIND: env["service"]._run_expand}[kind]
    return asyncio.run(handler(run, req, runner)), request


def test_setting_paragraphs_and_timeline_parsing():
    paragraphs = split_setting_paragraphs(SETTING)
    assert len(paragraphs) == 3
    system, user = timeline_prompts(name="青禾", paragraphs=paragraphs)
    assert "「青禾」" in system and "[2] 十二岁那年" in user
    stages = parse_timeline(TIMELINE, paragraph_count=3)
    assert stages[0]["key_events"][1] == {"text": "风暴那一夜", "basis": [2]}, "越界的段落编号丢掉"
    assert stages[1]["key_events"] == [{"text": "第一次独自给人看病", "basis": []}]
    assert parse_timeline("乱七八糟", paragraph_count=3) is None


def test_timeline_draft_confirm_then_expand(env):
    service, repo = env["service"], env["service"].repo
    plan = service.timeline_plan("oc_q", {"setting": SETTING})
    assert plan["calls"] == 1 and plan["paragraphs"] == 3
    with pytest.raises(CharacterExperienceError) as err:
        asyncio.run(service.start_timeline_draft("oc_q", {"setting": SETTING}))
    assert err.value.code == "confirmation_required"
    asyncio.run(service.start_timeline_draft("oc_q", {"setting": SETTING, "confirm": True, "expected_calls": 1}))
    result, _ = _run_last(env, TIMELINE_KIND)
    assert result["stages_created"] == ["oc01", "oc02"]
    stages = repo.list_stages("oc_q")
    assert [(s["stage_key"], s["title"], s["status"], s["source_kind"]) for s in stages] == [
        ("oc01", "渔村童年", "draft", "setting_compose"), ("oc02", "随师学医", "draft", "setting_compose"),
    ]
    assert stages[0]["source_ref"]["basis"] == [1, 2] and "渔村" in stages[0]["source_ref"]["setting_excerpt"]

    # 草稿阶段不能展开；管理员改一改、确认后才行
    assert service.expansion_plan("oc_q", {})["calls"] == 0
    edited = repo.save_stage("oc_q", "oc02", {"description": "跟药师走南闯北"}, expected_version=stages[1]["version"])
    repo.confirm_stages("oc_q", [("oc01", stages[0]["version"]), ("oc02", edited["version"])])
    plan = service.expansion_plan("oc_q", {})
    assert plan["events"] == 3 and plan["calls"] == 3
    with pytest.raises(CharacterExperienceError):
        asyncio.run(service.start_expansion("oc_q", {"setting": SETTING, "confirm": True, "expected_calls": 2}))
    asyncio.run(service.start_expansion("oc_q", {"setting": SETTING, "confirm": True, "expected_calls": 3}))
    result, request = _run_last(env, EXPAND_KIND)
    assert result["done"] == 3 and result["memories"] == 3
    items, total = repo.list("oc_q", limit=10)
    assert total == 3 and {i["canon_level"] for i in items} == {"invented"} and {i["status"] for i in items} == {"draft"}
    storm = next(i for i in items if "风暴" in i["content"])
    assert storm["stage_key"] == "oc01" and storm["story_time"] == "出生到十二岁" and storm["plot_stage"] == "风暴那一夜"
    assert storm["source_ref"]["basis"] == [2] and "风暴" in storm["source_ref"]["setting_excerpt"], "记下依据的设定段落"
    assert [i["stage_key"] for i in items] == ["oc01", "oc01", "oc02"], "按时间线顺序"
    expand_prompt = next(p for p in env["llm"].prompts if "独自给人看病" in p[1])
    assert "跟药师走南闯北" in expand_prompt[1], "展开用的是管理员改过的阶段说明"
    assert service.expansion_plan("oc_q", {})["calls"] == 0, "展开过的事件不再重复"


def test_redraft_replaces_only_unused_draft_stages(env):
    service, repo = env["service"], env["service"].repo
    asyncio.run(service.start_timeline_draft("oc_q", {"setting": SETTING, "confirm": True, "expected_calls": 1}))
    _run_last(env, TIMELINE_KIND)
    oc01 = repo.get_stage("oc_q", "oc01")
    repo.confirm_stages("oc_q", [("oc01", oc01["version"])])
    asyncio.run(service.start_timeline_draft("oc_q", {"setting": SETTING, "confirm": True, "expected_calls": 1, "replace_drafts": True}))
    result, _ = _run_last(env, TIMELINE_KIND)
    assert result["removed_drafts"] == ["oc02"]
    assert [s["stage_key"] for s in repo.list_stages("oc_q")] == ["oc01", "oc02", "oc03"], "确认过的阶段保留，新草稿接在后面"


def test_setting_defaults_to_profile_persona(env):
    plan = env["service"].timeline_plan("oc_q", {})
    assert plan["setting"].startswith("青禾的设定") and "海边渔村长大" in plan["setting"]
    with pytest.raises(CharacterExperienceError) as err:
        env["service"].timeline_plan("nobody", {"setting": SETTING})
    assert err.value.code == "bot_not_found"
