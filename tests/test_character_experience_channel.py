"""角色经历注入通道：按 Bot 隔离、只召回当前时间点及之前、渲染格式、未启用的 Bot 完全看不到。全部虚构数据。"""

from __future__ import annotations

import asyncio

import pytest

from character_experience_support import FakeEmbedding, bot, make_registry, make_service, vector_for
from domain.bot_profile import BotProfile
from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from services.config.channel_config import build_channel_config_from_plugin_config, build_default_channel_config
from services.injection.channels.character_experience import CharacterExperienceChannel, render_line
from services.injection.context import InjectionContext


def _scope(bot_id: str, *, private: bool = False) -> RuntimeScope:
    if private:
        return RuntimeScope(bot_id, "private", SessionRef("qq:private:10000009", "qq", "private", "10000009"),
                            subject_principal_id="qq:user:10000009")
    return RuntimeScope(bot_id, "group", SessionRef("qq:group:20000001", "qq", "group", "20000001"),
                        subject_principal_id="qq:user:10000009")


def _ctx(bot_id: str, message: str, *, private: bool = False, mode: str = "full") -> InjectionContext:
    return InjectionContext(
        event=None, req=None, message=message, group_id=None if private else "20000001",
        sender_id="10000009", sender_name="测试群友", bot_id=bot_id, bot_profile_id=bot_id,
        scope=_scope(bot_id, private=private), mode=mode, config={}, now=1.0,
    )


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "ce.sqlite3"))
    registry = make_registry(
        cm,
        bot("oc_a", "青芷", "10000001", character_experience={"enabled": True, "current_stage": "s2", "top_k": 3}),
        bot("oc_b", "阿木", "10000002"),  # 没开启角色经历
        bot("oc_c", "青禾", "10000003", character_experience={"enabled": True}),  # 开启了但自己没有经历
    )
    embedding = FakeEmbedding()
    service = make_service(cm, registry, embedding=embedding)
    repo = service.repo
    for key, title, story_time, ordinal in (("s1", "童年", "七岁到十二岁", 1), ("s2", "求学", "", 2), ("s3", "下山之后", "十八岁", 3)):
        repo.save_stage("oc_a", key, {"title": title, "story_time": story_time, "ordinal": ordinal}, expected_version=0)
    rows = [
        ("我七岁那年在柴房里偷偷养过一只小鹿。", "s1", "七岁"),
        ("进书院第一天我就被师父罚抄书。", "s2", ""),
        ("下山后我在海边第一次见到灯会。", "s3", "十八岁"),  # 当前时间点之后：不能召回
        ("我从小就怕打雷，这件事不分阶段。", "", ""),
    ]
    items = [repo.clean_item({"content": c, "stage_key": s, "story_time": t, "status": "active", "vector": vector_for(c).tobytes()})
             for c, s, t in rows]
    ids = repo.add_many("oc_a", items)["inserted"]
    repo.add_many("oc_b", [repo.clean_item({"content": "我在柴房里藏过一只小鹿。", "status": "active", "vector": vector_for("小鹿 柴房").tobytes()})])
    channel = CharacterExperienceChannel(service=service, embedding_service=embedding, profile_lookup=registry.get)
    yield {"cm": cm, "registry": registry, "service": service, "channel": channel, "ids": ids}
    cm.close()


def _run(channel, ctx):
    return asyncio.run(channel.build(ctx))


def test_recalls_own_past_and_renders_story_time(env):
    result = _run(env["channel"], _ctx("oc_a", "你小时候在柴房里养过小鹿吗"))
    assert result.status == "hit"
    assert "【你过去的经历·七岁】我七岁那年在柴房里偷偷养过一只小鹿。" in result.text
    assert result.text.startswith("<your_past>") and result.text.rstrip().endswith("</your_past>")
    assert "不是群友的事" in result.text and "小鹿" in result.text
    item = result.items[0]
    assert item["experience_id"] == env["ids"][0] and item["dedupe_key"] == f"character_experience:oc_a:{env['ids'][0]}"
    assert item["rendered_text"] in result.text


def test_never_recalls_experiences_after_current_time_point(env):
    # 精确命中下山之后的经历（关键词 + 向量都对得上），当前时间点在「求学」，仍然不能想起
    result = _run(env["channel"], _ctx("oc_a", "你下山后在海边看过灯会吗"))
    assert "海边第一次见到灯会" not in result.text
    recalled = asyncio.run(env["service"].recall("oc_a", "海边 灯会", query_vector=vector_for("海边 灯会")))
    assert env["ids"][2] not in [item["id"] for item in recalled["items"]]
    assert recalled["eligible_stages"] == ["s1", "s2"]
    # 时间点推到「下山之后」就能想起
    profile = env["registry"].get("oc_a")
    data = profile.to_dict()
    data["character_experience"]["current_stage"] = "s3"
    env["registry"].save(BotProfile.from_dict(data), expected_version=profile.version)
    later = _run(env["channel"], _ctx("oc_a", "你下山后在海边看过灯会吗"))
    assert "【你过去的经历·十八岁】下山后我在海边第一次见到灯会。" in later.text


def test_stage_time_and_title_fall_back_when_experience_has_no_time(env):
    result = _run(env["channel"], _ctx("oc_a", "你在书院被师父罚过吗"))
    assert "【你过去的经历·求学】进书院第一天我就被师父罚抄书。" in result.text, "没有自己的时间用阶段名"
    assert render_line({"content": "正文", "stage_story_time": "第2年"}) == "【你过去的经历·第2年】正文"
    assert render_line({"content": "正文"}) == "【你过去的经历】正文"


def test_unstaged_experiences_are_not_limited_by_time_point(env):
    result = _run(env["channel"], _ctx("oc_a", "你怕不怕打雷"))
    assert "【你过去的经历】我从小就怕打雷，这件事不分阶段。" in result.text


def test_other_bots_see_nothing(env):
    disabled = _run(env["channel"], _ctx("oc_b", "你小时候在柴房里养过小鹿吗"))
    assert disabled.status == "disabled" and disabled.text == ""
    assert "character_experience_not_enabled_for_bot" in disabled.warnings[0]
    other = _run(env["channel"], _ctx("oc_c", "你小时候在柴房里养过小鹿吗"))
    assert other.status == "empty" and "小鹿" not in other.text, "开启了也只看得到自己的经历"
    unknown = _run(env["channel"], _ctx("oc_zz", "你小时候在柴房里养过小鹿吗"))
    assert unknown.status == "disabled"


def test_missing_current_stage_fails_closed(env):
    profile = env["registry"].get("oc_a")
    data = profile.to_dict()
    data["character_experience"]["current_stage"] = "gone"
    env["registry"].save(BotProfile.from_dict(data), expected_version=profile.version)
    result = _run(env["channel"], _ctx("oc_a", "你小时候在柴房里养过小鹿吗"))
    assert "小鹿" not in result.text
    recalled = asyncio.run(env["service"].recall("oc_a", "打雷", query_vector=vector_for("打雷")))
    assert recalled["warning"] == "current_stage_missing"
    assert [item["stage_key"] for item in recalled["items"]] in ([""], [])


def test_private_chat_and_keyword_only_without_embedding(env):
    result = _run(env["channel"], _ctx("oc_a", "你小时候在柴房里养过小鹿吗", private=True))
    assert result.status == "hit" and "小鹿" in result.text
    env["channel"].embedding_service = FakeEmbedding(fail=True)
    keyword = _run(env["channel"], _ctx("oc_a", "说说柴房的事吧"))
    assert keyword.status == "hit" and "柴房" in keyword.text and keyword.items[0]["keyword_hit"]


def test_max_chars_limits_injection(env):
    profile = env["registry"].get("oc_a")
    data = profile.to_dict()
    data["character_experience"].update({"current_stage": "", "max_chars": 100, "min_score": 0.0, "top_k": 5})
    env["registry"].save(BotProfile.from_dict(data), expected_version=profile.version)
    result = _run(env["channel"], _ctx("oc_a", "讲讲你以前的事，小鹿、书院、海边、打雷"))
    body = [line for line in result.text.splitlines() if line.startswith("【")]
    assert 1 <= len(body) < 4 and sum(len(line) for line in body) <= 100
    assert any(item["filter_reason"] == "max_chars" for item in result.filtered)


def test_channel_config_modes_and_private_envelope():
    defaults = build_default_channel_config()
    assert defaults.channels["character_experience"].enabled and defaults.channels["character_experience"].priority == 69
    memory_only = build_default_channel_config(runtime_mode="memory_only")
    assert not memory_only.channels["character_experience"].enabled
    private = build_channel_config_from_plugin_config({}, scope=_scope("oc_a", private=True))
    assert private.channels["character_experience"].enabled, "只读 Bot 自己的静态经历，私聊安全包络里保留"
    assert not private.channels["persona"].enabled
