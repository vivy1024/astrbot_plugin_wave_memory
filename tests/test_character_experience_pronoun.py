"""角色经历的代词设置：她 / 他 / ta（默认 ta，不假定性别）。

- 三种取值渲染进从原著提取、起草时间线、展开经历的提示词，不再写死「她」；
- 后台任务按 Bot 资料里的代词写提示词；
- 旧配置缺字段（或值不认识）时按默认值；工作台改代词带版本检查、只接受三种取值；
- 注入说明对 Bot 用第二人称「你」，不带第三人称代词。

虚构角色、虚构 QQ 号与虚构内容。
"""

from __future__ import annotations

import asyncio

import pytest

from character_experience_support import FakeJobs, FakeLLM, FakeRunner, bot, job, make_registry, make_service
from domain.bot_profile import BotProfile
from domain.character_experience import DEFAULT_PRONOUN, CharacterExperienceSettings, normalize_pronoun
from engine.db.character_experience_repo import CharacterExperienceError
from engine.db.connection import ConnectionManager
from services.character_experience import EXPAND_KIND, EXTRACT_KIND, TIMELINE_KIND
from services.character_experience_extract import expand_prompts, extract_prompts, timeline_prompts
from services.injection.channels.character_experience import render_block, render_line

PRONOUNS = ("她", "他", "ta")


def _prompts(pronoun: str | None = None) -> tuple[str, str, str]:
    extra = {} if pronoun is None else {"pronoun": pronoun}
    extract_system, _ = extract_prompts(name="青禾", aliases=["阿禾"], description="", chapter_title="第1章 渔村", scene_text="正文", **extra)
    timeline_system, _ = timeline_prompts(name="青禾", paragraphs=["青禾出生在海边的渔村。"], **extra)
    expand_system, _ = expand_prompts(
        name="青禾", setting="青禾出生在海边的渔村。", stage_title="渔村童年", stage_time="小时候",
        stage_description="", event="跟父亲学着修补渔船", **extra,
    )
    return extract_system, timeline_system, expand_system


@pytest.mark.parametrize("pronoun", PRONOUNS)
def test_each_pronoun_is_rendered_into_extract_timeline_and_expand_prompts(pronoun):
    extract_system, timeline_system, expand_system = _prompts(pronoun)
    note = f"指代青禾时用「{pronoun}」"
    assert note in extract_system
    assert f"用{pronoun}的视角和语气重述" in extract_system and f"与{pronoun}有关的其他人物" in extract_system
    assert f"整理{pronoun}进入群聊之前的人生经历" in timeline_system and f"起草{pronoun}的人生时间线" in timeline_system
    assert f"{pronoun}自己的时间" in timeline_system and f"不要写{pronoun}进群之后的事" in timeline_system and note in timeline_system
    assert f"里面的青禾（{pronoun}" in expand_system
    for system in (extract_system, timeline_system, expand_system):
        if pronoun != "她":
            assert "她" not in system, "提示词里不再写死「她」"
        assert ("不假定性别" in system) is (pronoun == "ta"), "只有 ta 才说明不假定性别"


def test_prompts_default_to_ta_and_unknown_values_fall_back():
    assert DEFAULT_PRONOUN == "ta"
    assert _prompts() == _prompts("ta") == _prompts("TA") == _prompts("它"), "不传 / 大写 / 不认识的值都按 ta"
    assert all("不假定性别" in system for system in _prompts())


def test_missing_or_unknown_pronoun_in_old_config_uses_default():
    assert CharacterExperienceSettings().pronoun == "ta"
    assert CharacterExperienceSettings.from_dict({"enabled": True, "current_stage": "s1"}).pronoun == "ta", "旧配置缺字段"
    assert CharacterExperienceSettings.from_dict(None).pronoun == "ta"
    assert CharacterExperienceSettings.from_dict({"pronoun": "它"}).pronoun == "ta"
    assert CharacterExperienceSettings.from_dict({"pronoun": " TA "}).pronoun == "ta"
    assert [CharacterExperienceSettings.from_dict({"pronoun": p}).pronoun for p in PRONOUNS] == list(PRONOUNS)
    assert normalize_pronoun("她") == "她" and normalize_pronoun("") is None

    old = BotProfile.from_dict({"db_id": "oc_old", "name": "旧角色", "character_experience": {"enabled": True}})
    assert old.character_experience.pronoun == "ta"
    assert old.to_dict()["character_experience"]["pronoun"] == "ta", "保存时补上默认值"
    assert BotProfile.from_dict({"db_id": "oc_none", "name": "没有这一段"}).character_experience.pronoun == "ta"


SETTING = "石青在山里长大，跟着师父采药。\n\n十五岁那年他下山去了县城。"
TIMELINE = {"stages": [{"title": "山里的日子", "story_time": "小时候", "description": "跟师父采药",
                        "key_events": [{"text": "第一次跟师父进山采药", "basis": [1]}], "basis": [1]}]}
NOVEL = "\n".join(["第1章 进山", "石青跟着师父进山采药，走了一整天也没喊累。" * 6])


def _memory(prompt: str):
    if "要回忆的事" in prompt:  # 展开关键事件
        return [{"memory": "第一次跟师父进山，我背着比自己还高的药篓，一路数着山上的石阶。", "emotion": "日常", "characters": ["师父"]}]
    if "进山" in prompt:  # 从原著提取
        return [{"memory": "那天我跟着师父进山采药，脚底磨出了泡也没吭声。", "emotion": "日常", "characters": ["师父"]}]
    return []


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    registry = make_registry(
        cm,
        bot("oc_he", "石青", "10000011", character_experience={"pronoun": "他"}),
        bot("oc_ta", "未定", "10000012"),  # 没设代词：按 ta
    )
    root = tmp_path / "novel_docs"
    root.mkdir()
    (root / "虚构山村记.txt").write_text(NOVEL, encoding="utf-8")
    jobs = FakeJobs()
    llm = FakeLLM([("设定：\n[1]", TIMELINE)], default=_memory)
    service = make_service(cm, registry, llm=llm, jobs=jobs, source_roots=[str(root)])
    yield {"service": service, "jobs": jobs, "llm": llm, "registry": registry, "novel": str(root / "虚构山村记.txt")}
    cm.close()


def _run_last(env, kind):
    request = [r for r in env["jobs"].requests.values() if r.kind == kind][-1]
    run, req = job(kind, request.payload, request_id=request.request_id)
    handler = {
        EXTRACT_KIND: env["service"]._run_extract,
        TIMELINE_KIND: env["service"]._run_timeline,
        EXPAND_KIND: env["service"]._run_expand,
    }[kind]
    return asyncio.run(handler(run, req, FakeRunner(env["jobs"])))


def test_jobs_write_prompts_with_the_bot_pronoun(env):
    service, llm = env["service"], env["llm"]
    asyncio.run(service.start_extraction("oc_he", {"path": env["novel"], "name": "石青", "concurrency": 1, "confirm": True, "expected_calls": 1}))
    assert _run_last(env, EXTRACT_KIND)["memories"] == 1
    asyncio.run(service.start_timeline_draft("oc_he", {"setting": SETTING, "confirm": True, "expected_calls": 1}))
    assert _run_last(env, TIMELINE_KIND)["stages_created"] == ["oc01"]
    stage = service.repo.get_stage("oc_he", "oc01")
    service.repo.confirm_stages("oc_he", [("oc01", stage["version"])])
    asyncio.run(service.start_expansion("oc_he", {"setting": SETTING, "confirm": True, "expected_calls": 1}))
    assert _run_last(env, EXPAND_KIND)["memories"] == 1

    extract_system, timeline_system, expand_system = (system for system, _user in llm.prompts)
    assert "指代石青时用「他」" in extract_system and "用他的视角和语气重述" in extract_system
    assert "整理他进入群聊之前的人生经历" in timeline_system
    assert "里面的石青（他）就是你自己" in expand_system
    assert all("她" not in system for system, _user in llm.prompts)

    # 没设代词的 Bot 按 ta；改了代词之后的任务用新值
    asyncio.run(service.start_timeline_draft("oc_ta", {"setting": "未定在城里长大。", "confirm": True, "expected_calls": 1}))
    _run_last(env, TIMELINE_KIND)
    assert "整理ta进入群聊之前的人生经历" in llm.prompts[-1][0] and "不假定性别" in llm.prompts[-1][0]
    profile = env["registry"].get_any("oc_ta")
    service.update_settings("oc_ta", {"pronoun": "她"}, expected_version=profile.version)
    asyncio.run(service.start_timeline_draft("oc_ta", {"setting": "未定在城里长大。", "confirm": True, "expected_calls": 1}))
    _run_last(env, TIMELINE_KIND)
    assert "整理她进入群聊之前的人生经历" in llm.prompts[-1][0]


def test_workbench_settings_validate_pronoun_and_check_version(env):
    service, registry = env["service"], env["registry"]
    assert service.overview("oc_he")["settings"]["pronoun"] == "他"
    assert service.overview("oc_ta")["settings"]["pronoun"] == "ta"
    profile = registry.get_any("oc_ta")
    with pytest.raises(CharacterExperienceError) as err:
        service.update_settings("oc_ta", {"pronoun": "它"}, expected_version=profile.version)
    assert err.value.code == "invalid_pronoun"
    saved = service.update_settings("oc_ta", {"pronoun": "他"}, expected_version=profile.version)
    assert saved.character_experience.pronoun == "他" and saved.version == profile.version + 1
    with pytest.raises(Exception) as stale:
        service.update_settings("oc_ta", {"pronoun": "她"}, expected_version=profile.version)
    assert getattr(stale.value, "code", "") == "version_conflict"
    assert registry.get_any("oc_ta").character_experience.pronoun == "他"


def test_injection_text_addresses_the_bot_as_you():
    item = {"content": "那年冬天我第一次下山。", "story_time": "第1年冬"}
    block = render_block("青禾", [render_line(item)])
    assert "【你过去的经历·第1年冬】那年冬天我第一次下山。" in block and "你（青禾）" in block
    assert "她" not in block and "他" not in block, "注入说明对 Bot 用第二人称，不用第三人称代词"
