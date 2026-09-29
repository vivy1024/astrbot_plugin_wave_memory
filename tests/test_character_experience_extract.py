"""从原著提取：切章、找出角色出场的场景、预览与确认、假模型跑任务、续跑与失败重试。虚构小说与虚构角色。"""

from __future__ import annotations

import asyncio

import pytest

import services.character_experience as ce_module
from character_experience_support import FakeJobs, FakeLLM, FakeRunner, bot, job, make_registry, make_service
from engine.db.character_experience_repo import CharacterExperienceError
from engine.db.connection import ConnectionManager
from services.character_experience import EXTRACT_KIND
from services.character_experience_extract import extract_prompts, extract_scenes, parse_memory_list, plan_scenes

FILLER = "山风吹过竹林，远处的钟声一下一下地响着。"


def _para(text: str, times: int = 6) -> str:
    return text * times


NOVEL = "\n".join([
    "第1章 入门",
    _para("林小满第一次走进山门，手里攥着师父给的木牌。"),
    FILLER,
    _para("她把木牌挂在床头，心想一定要学会御剑。"),
    "第2章 旁人",
    _para("门里的人都在议论小满，说她是走后门进来的。"),  # 只提到别名：默认不算她的场景
    "第3章 两件事",
    _para("林小满在后山练剑，一不小心削断了师兄的发带。"),
    FILLER, FILLER, FILLER, FILLER, FILLER,  # 隔开超过 3 段：后面是另一个场景
    _para("傍晚林小满去膳堂帮忙，把一锅粥煮糊了。"),
    "第4章 无关",
    _para("这一章讲的是别的门派的事情，没有她。"),
    "第5章 下山",
    _para("林小满第一次下山，在镇上迷了路，被一个卖糖人的老伯送回来。"),
])

MEMORY_BY_CHAPTER = {
    "第1章": [{"memory": "我第一次走进山门的时候，手心里全是汗，木牌都被我攥热了。", "emotion": "紧张", "characters": ["师父"], "time": "入门那年"}],
    "第3章": [{"memory": "那天在后山练剑，我一剑削断了师兄的发带，他瞪了我半天。", "emotion": "搞笑", "characters": ["师兄"]}],
    "第5章": [{"memory": "第一次下山就迷了路，是卖糖人的老伯把我送回来的。", "emotion": "温馨", "characters": ["卖糖人的老伯"]}],
}


def _llm_output(prompt: str):
    if "粥煮糊" in prompt:
        return []  # 模型判定这一段不值得写：空数组
    for chapter, memories in MEMORY_BY_CHAPTER.items():
        if f"来源：{chapter}" in prompt:
            return memories
    return []


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    registry = make_registry(cm, bot("oc_a", "林小满", "10000001", aliases=["小满"]))
    source_root = tmp_path / "novel_docs"
    source_root.mkdir()
    novel = source_root / "虚构小说.txt"
    novel.write_text(NOVEL, encoding="utf-8")
    jobs = FakeJobs()
    llm = FakeLLM(default=_llm_output)
    service = make_service(cm, registry, llm=llm, jobs=jobs, source_roots=[str(source_root)])
    service.repo.save_stage("oc_a", "s1", {"title": "入门", "chapter_from": 1, "chapter_to": 2}, expected_version=0)
    service.repo.save_stage("oc_a", "s2", {"title": "学艺", "chapter_from": 3, "chapter_to": 4}, expected_version=0)
    yield {"cm": cm, "service": service, "jobs": jobs, "llm": llm, "novel": novel, "tmp": tmp_path}
    cm.close()


def _body(env, **extra):
    return {"path": str(env["novel"]), "name": "林小满", "aliases": "小满", "description": "嘴硬心软，做事毛躁但很认真", "concurrency": 1, **extra}


def test_scene_extraction_filters_alias_only_and_splits_by_gap():
    scenes, stats = plan_scenes(NOVEL, name="林小满", aliases=["小满"])
    assert stats == {"chapters_total": 5, "chapters_in_range": 5, "chapters_with_character": 3, "scenes": 4}
    assert [(s.chapter, s.index) for s in scenes] == [(1, 0), (3, 0), (3, 1), (5, 0)]
    first = scenes[0].text.split("\n")
    assert first[1] == FILLER and len(first) == len(set(first)), "中间段只带一次，不重复收录"
    assert all("走后门" not in s.text for s in scenes), "只提到别名的章节默认不算"
    loose, _ = plan_scenes(NOVEL, name="林小满", aliases=["小满"], require_full_name=False)
    assert any("走后门" in s.text for s in loose)
    ranged, ranged_stats = plan_scenes(NOVEL, name="林小满", chapter_from=3, chapter_to=4)
    assert ranged_stats["chapters_in_range"] == 2 and {s.chapter for s in ranged} == {3}
    assert extract_scenes("短短一句林小满。", ["林小满"]) == [], "不足最少字数的不算场景"


def test_prompt_uses_parameters_not_hardcoded_character():
    system, user = extract_prompts(name="林小满", aliases=["小满"], description="嘴硬心软", chapter_title="第1章 入门", scene_text="正文")
    assert "「林小满」本人的第一人称记忆" in system and "别名：小满" in system and "角色设定：嘴硬心软" in system
    assert "白真真" not in system and "张羽" not in system
    assert user == "来源：第1章 入门\n\n正文"
    assert parse_memory_list('```json\n[{"memory": "我记得那天下雨了，雨一直没停。", "emotion": "伤感"}]\n```')[0]["emotion"] == "伤感"
    assert parse_memory_list("[]") == [] and parse_memory_list("不是 JSON") is None


def test_preview_requires_confirmation_then_job_writes_drafts(env):
    service, jobs = env["service"], env["jobs"]
    plan = service.extraction_plan("oc_a", _body(env))
    assert plan["scenes"] == 4 and plan["calls"] == 4 and plan["already_processed"] == 0
    assert plan["by_stage"] == {"s1": 1, "s2": 2, "": 1} and plan["unassigned_scenes"] == 1
    with pytest.raises(CharacterExperienceError) as err:
        asyncio.run(service.start_extraction("oc_a", _body(env)))
    assert err.value.code == "confirmation_required" and err.value.plan["calls"] == 4 and "scene_keys" not in err.value.plan
    with pytest.raises(CharacterExperienceError):
        asyncio.run(service.start_extraction("oc_a", _body(env, confirm=True, expected_calls=3)))
    started = asyncio.run(service.start_extraction("oc_a", _body(env, confirm=True, expected_calls=4)))
    assert started["calls"] == 4
    request = next(iter(jobs.requests.values()))
    assert request.kind == EXTRACT_KIND and len(request.payload["scene_keys"]) == 4 and request.scope == {"kind": "character_experience", "bot_id": "oc_a"}

    run, req = job(EXTRACT_KIND, request.payload, request_id=request.request_id)
    result = asyncio.run(service._run_extract(run, req, FakeRunner(jobs)))
    assert result["done"] == 4 and result["memories"] == 3 and result["empty"] == 1 and result["failed"] == 0
    items, total = service.repo.list("oc_a", limit=10)
    assert total == 3 and {i["status"] for i in items} == {"draft"} and {i["canon_level"] for i in items} == {"canon"}
    first = next(i for i in items if i["source_ref"]["chapter"] == 1)
    assert first["stage_key"] == "s1" and first["story_time"] == "入门那年" and first["characters"] == ["师父"]
    assert first["source_ref"]["chapter_title"] == "第1章 入门" and first["source_ref"]["source"] == "虚构小说.txt"
    assert "林小满第一次走进山门" in first["source_ref"]["scene_excerpt"]
    assert next(i for i in items if i["source_ref"]["chapter"] == 5)["stage_key"] == "", "不在任何阶段章节范围内：未分阶段"
    # 全部处理过：再预览不需要调用；勾选重新提取才会重跑
    assert service.extraction_plan("oc_a", _body(env))["calls"] == 0
    assert service.extraction_plan("oc_a", _body(env, redo=True))["calls"] == 4


def test_cancel_then_resume_skips_processed_scenes(env):
    service, jobs, llm = env["service"], env["jobs"], env["llm"]
    asyncio.run(service.start_extraction("oc_a", _body(env, confirm=True, expected_calls=4)))
    request = next(iter(jobs.requests.values()))
    run, req = job(EXTRACT_KIND, request.payload, request_id=request.request_id)
    jobs.cancelled.add(run.run_id)  # 第一批处理完就发现取消了
    stopped = asyncio.run(service._run_extract(run, req, FakeRunner(jobs)))
    assert stopped["stopped"] is True and stopped["done"] == 1 and len(llm.prompts) == 1
    resumed_run, _ = job(EXTRACT_KIND, request.payload, request_id=request.request_id)
    resumed_run.run_id = "run-2"
    finished = asyncio.run(service._run_extract(resumed_run, req, FakeRunner(jobs)))
    assert finished["stopped"] is False and finished["done"] == 4
    assert len(llm.prompts) == 4, "续跑只处理剩下的 3 个场景，已处理的不重复调用"
    assert service.repo.counts("oc_a")["total"] == 3


def test_failed_scenes_are_retried_and_repeated_failures_stop_the_job(env, monkeypatch):
    service, jobs = env["service"], env["jobs"]
    env["llm"].script = [("来源：第3章", RuntimeError("upstream 503"))]
    asyncio.run(service.start_extraction("oc_a", _body(env, confirm=True, expected_calls=4)))
    request = next(iter(jobs.requests.values()))
    run, req = job(EXTRACT_KIND, request.payload, request_id=request.request_id)
    result = asyncio.run(service._run_extract(run, req, FakeRunner(jobs)))
    assert result["failed"] == 2 and result["done"] == 2
    assert service.repo.scene_stats("oc_a")["novel"]["failed"] == 2
    assert service.extraction_plan("oc_a", _body(env))["calls"] == 2, "失败的场景下次还会处理"

    env["llm"].script = [("来源", RuntimeError("quota exceeded"))]
    monkeypatch.setattr(ce_module, "MAX_CONSECUTIVE_FAILURES", 2)
    rerun, _ = job(EXTRACT_KIND, request.payload, request_id="req-other")
    rerun.run_id = "run-9"
    with pytest.raises(RuntimeError, match="连续 2 个场景"):
        asyncio.run(service._run_extract(rerun, job(EXTRACT_KIND, request.payload, request_id="req-other")[1], FakeRunner(jobs)))


def test_paths_outside_allowed_roots_are_rejected(env):
    outside = env["tmp"] / "outside.txt"
    outside.write_text(NOVEL, encoding="utf-8")
    with pytest.raises(Exception) as err:
        env["service"].extraction_plan("oc_a", _body(env, path=str(outside)))
    assert getattr(err.value, "code", "") == "path_not_allowed"
    with pytest.raises(Exception) as err:
        env["service"].extraction_plan("oc_a", _body(env, path=str(env["novel"].parent / ".." / "outside.txt")))
    assert getattr(err.value, "code", "") == "path_not_allowed"
    with pytest.raises(CharacterExperienceError) as err:
        env["service"].extraction_plan("oc_a", _body(env, name=""))
    assert err.value.code == "name_required"
