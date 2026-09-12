"""holyman_persona 通道：默认关、显式开启才注入、身份守卫、缺文件 fail-closed。"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

if "astrbot.api" not in sys.modules:
    _astrbot_mod = types.ModuleType("astrbot")
    _api_mod = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass

    _api_mod.logger = _Logger()
    sys.modules["astrbot"] = _astrbot_mod
    sys.modules["astrbot.api"] = _api_mod

from domain.scope import RuntimeScope, SessionRef
from services.identity_safety import is_identity_contamination
from services.injection.channels.holyman_persona import HolymanPersonaChannel
from services.jargon.holyman_persona import HolymanPersonaPack


def _ctx(config=None, message="", scope=None):
    scope = scope or RuntimeScope("bot-alpha", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
    return types.SimpleNamespace(
        mode="full",
        config=config or {},
        message=message,
        scope=scope,
        trace_id="t1",
    )


def _channel_cfg(enabled: bool):
    return {"channels": {"holyman_persona": {"enabled": enabled}}}


class PersonaPackTest:
    pass


def test_persona_pack_available_from_real_assets():
    pack = HolymanPersonaPack()

    assert pack.available is True, "本地 assets/holyman/raw 应能组装人格包"


def test_persona_pack_covers_persona_and_core_blocks():
    pack = HolymanPersonaPack()

    result = pack.build_injection("随便聊聊")
    blocks = {block["block"] for block in result["blocks"]}

    # SKILL.md 的 Activation 语义：persona 常载，含 Hard Boundaries 安全约束。
    assert "expression" in blocks
    assert "values" in blocks
    assert "frameworks" in blocks
    assert "core_rules" in blocks
    assert "hard_boundaries" in blocks


def test_persona_pack_attaches_knowledge_only_on_topic_match():
    pack = HolymanPersonaPack()

    gaming = {b["block"] for b in pack.build_injection("今天原神抽卡")["blocks"]}
    culture = {b["block"] for b in pack.build_injection("这也太抽象了吧")["blocks"]}
    plain = {b["block"] for b in pack.build_injection("帮我算个加法")["blocks"]}

    assert "knowledge_gaming" in gaming
    assert "knowledge_culture" in culture
    assert "knowledge_gaming" not in plain and "knowledge_culture" not in plain


def test_persona_pack_output_never_trips_identity_guard():
    """整包必须过身份守卫：逐块过滤后不得再构成身份/服从声明。"""
    pack = HolymanPersonaPack()

    assert pack.available is True
    text = pack.build_injection("原神")["text"]
    assert text
    assert is_identity_contamination(text) is False


def test_persona_pack_keeps_safety_boundaries_text():
    """Hard Boundaries 是安全约束，过滤不得把它丢掉。"""
    pack = HolymanPersonaPack()

    text = pack.build_injection("")["text"]

    assert "真实" in text and "灾难" in text


def test_persona_pack_missing_assets_is_fail_closed(tmp_path):
    pack = HolymanPersonaPack(root_path=tmp_path)

    assert pack.available is False
    assert pack.build_injection("你好")["text"] == ""


def test_channel_disabled_by_default():
    """核心契约：未显式开启时通道必须 disabled，不发生任何注入。"""
    channel = HolymanPersonaChannel(persona_pack=HolymanPersonaPack())

    result = asyncio.run(channel.build(_ctx()))

    assert result.status == "disabled"
    assert result.text == ""


def test_channel_explicit_disable_stays_disabled():
    channel = HolymanPersonaChannel(persona_pack=HolymanPersonaPack())

    result = asyncio.run(channel.build(_ctx(config=_channel_cfg(False))))

    assert result.status == "disabled"


def test_channel_enabled_injects_persona_pack():
    channel = HolymanPersonaChannel(persona_pack=HolymanPersonaPack())

    result = asyncio.run(channel.build(_ctx(config=_channel_cfg(True), message="今天原神抽卡")))

    assert result.status == "hit"
    assert "神人风格参考" in result.text
    blocks = {item["block"] for item in result.items}
    assert "hard_boundaries" in blocks
    assert "knowledge_gaming" in blocks
    # 注入项须声明为风格层，而非只读参考。
    assert all(item["source_layer"] == "holyman_persona" for item in result.items)
    assert all(item["reference_only"] is False for item in result.items)


def test_channel_enabled_output_passes_identity_guard():
    channel = HolymanPersonaChannel(persona_pack=HolymanPersonaPack())

    result = asyncio.run(channel.build(_ctx(config=_channel_cfg(True), message="太抽象了")))

    assert result.status == "hit"
    assert is_identity_contamination(result.text) is False


def test_channel_unavailable_pack_is_empty_not_error():
    channel = HolymanPersonaChannel(persona_pack=None)

    result = asyncio.run(channel.build(_ctx(config=_channel_cfg(True))))

    assert result.status == "empty"
    assert result.error == ""


def test_channel_max_items_zero_stops_injection():
    config = {"channels": {"holyman_persona": {"enabled": True, "max_items": 0}}}
    channel = HolymanPersonaChannel(persona_pack=HolymanPersonaPack())

    result = asyncio.run(channel.build(_ctx(config=config)))

    assert result.status == "empty"


def test_channel_rejects_non_group_scope():
    channel = HolymanPersonaChannel(persona_pack=HolymanPersonaPack())
    bot_scope = RuntimeScope("bot-alpha", "bot_private", None)

    result = asyncio.run(channel.build(_ctx(config=_channel_cfg(True), scope=bot_scope)))

    assert result.status == "empty"
    assert result.text == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
