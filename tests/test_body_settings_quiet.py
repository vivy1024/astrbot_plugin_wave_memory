"""BodySettings 的静默时段与静默话术：解析、默认话术、上限与自定义。"""

from __future__ import annotations

from domain.bot_profile import DEFAULT_QUIET_REPLIES, QUIET_REPLY_CHARS, QUIET_REPLY_MAX, BodySettings, BotProfile


def test_defaults_give_several_quiet_phrases_and_no_window():
    body = BodySettings()
    assert body.service_quiet_start is None and body.service_quiet_end is None
    assert len(body.quiet_replies) >= 5 and all("{end}" in p or p for p in body.quiet_replies)
    assert body.quiet_replies == list(DEFAULT_QUIET_REPLIES)


def test_window_and_custom_phrases_round_trip():
    body = BodySettings.from_dict({
        "service_quiet_start": 23, "service_quiet_end": 6,
        "quiet_replies": ["睡了，{end}见", "晚安", "睡了，{end}见"],
    })
    assert (body.service_quiet_start, body.service_quiet_end) == (23, 6)
    assert body.quiet_replies == ["睡了，{end}见", "晚安"], "去重并保持顺序"


def test_empty_or_missing_phrases_fall_back_to_defaults():
    for raw in ({}, {"quiet_replies": []}, {"quiet_replies": ["", "  "]}):
        assert BodySettings.from_dict(raw).quiet_replies == list(DEFAULT_QUIET_REPLIES)


def test_phrase_count_and_length_are_capped():
    body = BodySettings.from_dict({"quiet_replies": [f"第{i}句" + "长" * 200 for i in range(40)]})
    assert len(body.quiet_replies) == QUIET_REPLY_MAX
    assert all(len(p) <= QUIET_REPLY_CHARS for p in body.quiet_replies)


def test_invalid_hours_are_ignored_or_clamped():
    assert BodySettings.from_dict({"service_quiet_start": "x", "service_quiet_end": 6}).service_quiet_start is None
    assert BodySettings.from_dict({"service_quiet_start": 99, "service_quiet_end": -3}).service_quiet_start == 23
    assert BodySettings.from_dict({"service_quiet_start": "", "service_quiet_end": None}).service_quiet_end is None


def test_profile_serializes_the_quiet_settings():
    profile = BotProfile.from_dict({
        "db_id": "yushu", "name": "羽书", "qq_id": "2715127743",
        "body": {"service_quiet_start": 23, "service_quiet_end": 6, "quiet_replies": ["睡了，{end}见"]},
    })
    data = profile.to_dict()["body"]
    assert data["service_quiet_start"] == 23 and data["service_quiet_end"] == 6
    assert data["quiet_replies"] == ["睡了，{end}见"]
    again = BotProfile.from_dict(profile.to_dict())
    assert again.body.quiet_replies == ["睡了，{end}见"] and again.body.service_quiet_start == 23
