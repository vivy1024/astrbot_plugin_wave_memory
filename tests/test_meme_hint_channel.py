"""用梗提示通道：默认关闭、按 Bot 与按场合开关、直播默认关、严肃场合不提示。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from domain.scope import RuntimeScope, SessionRef
from services.config.channel_config import (
    apply_channel_overrides,
    build_channel_config_from_plugin_config,
    build_default_channel_config,
    resolve_effective_channel_config,
)
from services.injection.channels.meme_hint import (
    HINT_HEADER,
    MemeHintChannel,
    meme_hint_scene_switches,
    resolve_scene,
)
from services.injection.context import InjectionContext
from services.injection.orchestrator import InjectionOrchestrator


GROUP = RuntimeScope("bot-test", "group", SessionRef("qq:group:20000001", "qq", "group", "20000001"))
PRIVATE = RuntimeScope("bot-test", "private", SessionRef("qq:private:10000001", "qq", "private", "10000001"))
TOPIC_MESSAGE = "最近投简历一直找不到工作，面试挂了好几次"


class _Req:
    def __init__(self):
        self.extra_user_content_parts = []


def _ctx(message=TOPIC_MESSAGE, *, scope=GROUP, source="astrbot", enabled=True, max_items=2, mode="full"):
    return InjectionContext(
        event=None,
        req=_Req(),
        message=message,
        group_id="20000001" if scope is GROUP else None,
        sender_id="10000001",
        sender_name="测试群友",
        bot_id="bot-test",
        bot_profile_id="bot-test",
        scope=scope,
        mode=mode,
        channel_options={"meme_hint": {"enabled": enabled, "max_items": max_items}},
        source=source,
        trace_id="trace-meme-hint",
    )


def _build(channel, ctx):
    return asyncio.run(channel.build(ctx))


# ── 默认关闭 ────────────────────────────────────────────────


def test_default_config_keeps_meme_hint_off_even_with_legacy_persona_switch():
    for inject_cfg in ({}, {"enable_holyman_persona": True}):
        config = build_default_channel_config(runtime_mode="full", inject_cfg=inject_cfg)
        assert config.channels["meme_hint"].enabled is False
        assert config.channels["meme_hint"].max_items == 2
        assert "holyman_persona" not in config.channels


def test_channel_is_disabled_unless_explicitly_enabled():
    result = _build(MemeHintChannel(), _ctx(enabled=False))
    assert result.status == "disabled"
    result = _build(MemeHintChannel(), InjectionContext(
        event=None, req=_Req(), message=TOPIC_MESSAGE, group_id="20000001", sender_id="10000001",
        sender_name="测试群友", bot_id="bot-test", bot_profile_id="bot-test", scope=GROUP,
    ))
    assert result.status == "disabled"


def test_memory_only_mode_never_hints():
    assert _build(MemeHintChannel(), _ctx(mode="memory_only")).status == "disabled"


# ── 场合开关 ────────────────────────────────────────────────


def test_scene_switch_defaults_and_parsing():
    assert meme_hint_scene_switches({}) == {"group": True, "private": False, "live": False}
    assert meme_hint_scene_switches(
        {"Jargon_Settings": {"meme_hint_group": False, "meme_hint_private": "true", "meme_hint_live": None}}
    ) == {"group": False, "private": True, "live": False}


def test_scene_resolution():
    assert resolve_scene(_ctx()) == "group"
    assert resolve_scene(_ctx(scope=PRIVATE)) == "private"
    assert resolve_scene(_ctx(source="cortico")) == "live"
    assert resolve_scene(_ctx(source="runtime")) == "live"


def test_group_scene_hints_with_optional_wording():
    result = _build(MemeHintChannel(), _ctx())
    assert result.status == "hit"
    assert result.text.startswith(HINT_HEADER)
    assert "可以用，不是必须用" in result.text
    assert "「找不到工作也没关系」" in result.text
    assert 1 <= len(result.items) <= 2
    assert all(item["dedupe_key"].startswith("meme_hint:") for item in result.items)
    # 只给释义，不带例句
    assert "就已经安排好工作了。" not in result.text


def test_live_scene_is_off_by_default_and_reports_reason():
    result = _build(MemeHintChannel(), _ctx(source="cortico"))
    assert result.status == "skipped"
    assert "Jargon_Settings.meme_hint_live" in result.warnings[0]
    enabled = MemeHintChannel(scene_switches={"live": True})
    assert _build(enabled, _ctx(source="cortico")).status == "hit"


def test_private_scene_is_off_by_default_and_can_be_enabled():
    assert _build(MemeHintChannel(), _ctx(scope=PRIVATE)).status == "skipped"
    assert _build(MemeHintChannel(scene_switches={"private": True}), _ctx(scope=PRIVATE)).status == "hit"


def test_group_scene_can_be_turned_off():
    channel = MemeHintChannel(scene_switches=meme_hint_scene_switches({"Jargon_Settings": {"meme_hint_group": False}}))
    result = _build(channel, _ctx())
    assert result.status == "skipped"
    assert "meme_hint_group" in result.warnings[0]


# ── 按 Bot 开关（沿用通道覆盖） ─────────────────────────────


def test_bot_channel_override_enables_only_that_bot():
    plugin_config = {"Runtime_Settings": {"runtime_mode": "full"}}
    on, _ = resolve_effective_channel_config(plugin_config, scope=GROUP, bot_channels={"meme_hint": {"enabled": True}})
    off, _ = resolve_effective_channel_config(plugin_config, scope=GROUP)
    assert on.channels["meme_hint"].enabled is True
    assert off.channels["meme_hint"].enabled is False


def test_private_envelope_keeps_meme_hint_setting_but_still_disables_data_channels():
    config = build_channel_config_from_plugin_config(
        {"Channel_Settings": {"channels": {"meme_hint": {"enabled": True}, "jargon": {"enabled": True}}}},
        scope=PRIVATE,
    )
    assert config.channels["meme_hint"].enabled is True
    assert config.channels["jargon"].enabled is False
    assert config.channels["persona"].enabled is False


# ── 挑选规则 ────────────────────────────────────────────────


def test_hints_are_capped_at_two_even_if_config_asks_for_more():
    message = "找不到工作还天天熬夜，投简历被针对，运气差到被做局"
    result = _build(MemeHintChannel(), _ctx(message, max_items=9))
    assert result.status == "hit"
    assert len(result.items) == 2


def test_serious_context_gets_no_hint():
    for message in ("找不到工作，真的好难过想哭", "朋友住院了，找不到工作也没心思", "我只有14岁找不到工作"):
        result = _build(MemeHintChannel(), _ctx(message))
        assert result.status == "empty", message


def test_meme_already_used_is_left_to_jargon_explanation():
    result = _build(MemeHintChannel(), _ctx("找不到工作也没关系，反正面试挂了"))
    assert all(item["word"] != "找不到工作也没关系" for item in result.items)


def test_no_topic_no_hint():
    assert _build(MemeHintChannel(), _ctx("今天天气不错")).status == "empty"


# ── 编排器集成 ──────────────────────────────────────────────


def _orchestrate(ctx, config, store=None):
    orchestrator = InjectionOrchestrator(channels=[MemeHintChannel()], config=config, trace_store=store, text_part_factory=lambda t: t)
    return asyncio.run(orchestrator.run(ctx))


def test_orchestrator_skips_disabled_channel_and_injects_when_enabled():
    base = build_default_channel_config(runtime_mode="full")
    off = _orchestrate(_ctx(), base)
    assert off.final_text == "" and off.channel_results == []
    on = _orchestrate(_ctx(), apply_channel_overrides(base, {"channels": {"meme_hint": {"enabled": True}}}))
    assert "可以用，不是必须用" in on.final_text


def test_live_skip_reason_is_visible_in_trace():
    from services.injection.trace_store import InjectionTraceStore

    conn = sqlite3.connect(":memory:")
    store = InjectionTraceStore(conn)
    store.ensure_schema()
    config = apply_channel_overrides(build_default_channel_config(runtime_mode="full"), {"channels": {"meme_hint": {"enabled": True}}})
    _orchestrate(_ctx(source="cortico"), config, store)
    detail = store.get("trace-meme-hint")
    row = next(item for item in detail["channels"] if item["channel"] == "meme_hint")
    assert row["status"] == "skipped"
    assert "meme_hint_live" in str(row)


@pytest.mark.parametrize("tier, expected", [("full", True), ("light", True), ("minimal", False)])
def test_runtime_tiers_include_meme_hint_except_minimal(tier, expected):
    from services.injection.runtime_prepare import TIER_CHANNELS

    allowed = TIER_CHANNELS[tier]
    assert (allowed is None or "meme_hint" in allowed) is expected
