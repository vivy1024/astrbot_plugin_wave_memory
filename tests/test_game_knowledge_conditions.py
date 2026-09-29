"""游戏知识库的触发条件：解析（文字 / 对象）、判定（区间 / 比较 / 等值 / 属于 / 必须）、请求 game 解析、关键词命中与触发规则。"""

from __future__ import annotations

import pytest

from domain.game_knowledge import (
    FAIL,
    PASS,
    UNKNOWN,
    Condition,
    GameKnowledgeError,
    KeywordHaystack,
    card_trigger,
    evaluate_condition,
    normalize_card_fields,
    parse_condition,
    parse_conditions,
    parse_game_context,
    validate_game_id,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cycle 30..60", {"key": "cycle", "op": "between", "min": 30, "max": 60}),
        ("cycle 30-60", {"key": "cycle", "op": "between", "min": 30, "max": 60}),
        ("30 <= cycle <= 60", {"key": "cycle", "op": "between", "min": 30, "max": 60}),
        ("cycle 在 30 到 60 之间", {"key": "cycle", "op": "between", "min": 30, "max": 60}),
        ("y -64..0", {"key": "y", "op": "between", "min": -64, "max": 0}),
        ("cycle 200+", {"key": "cycle", "op": "gte", "value": 200}),
        ("cycle >= 200", {"key": "cycle", "op": "gte", "value": 200}),
        ("health < 8", {"key": "health", "op": "lt", "value": 8}),
        ("health ≤ 8", {"key": "health", "op": "lte", "value": 8}),
        ("health＜8", {"key": "health", "op": "lt", "value": 8}),
        ("health > 2.5", {"key": "health", "op": "gt", "value": 2.5}),
        ("time_of_day == night", {"key": "time_of_day", "op": "eq", "value": "night"}),
        ("time_of_day=night", {"key": "time_of_day", "op": "eq", "value": "night"}),
        ("time_of_day != day", {"key": "time_of_day", "op": "ne", "value": "day"}),
        ("dimension in nether, end", {"key": "dimension", "op": "in", "value": ["nether", "end"]}),
        ("biome 属于 沙漠、平原", {"key": "biome", "op": "in", "value": ["沙漠", "平原"]}),
        ("必须 dimension in nether, end", {"key": "dimension", "op": "in", "value": ["nether", "end"], "required": True}),
    ],
)
def test_parse_text_conditions_and_round_trip(text, expected):
    condition = parse_condition(text)
    assert condition.to_dict() == expected
    # 文字形式与对象形式都能原样解析回来（管理台编辑、导出再导入不走样）
    assert parse_condition(condition.text()) == condition
    assert parse_condition(condition.to_dict()) == condition


def test_parse_mapping_shorthands():
    assert parse_condition({"key": "cycle", "min": 30, "max": 60}).to_dict() == {"key": "cycle", "op": "between", "min": 30, "max": 60}
    assert parse_condition({"key": "hp", "max": 5}).to_dict() == {"key": "hp", "op": "lte", "value": 5}
    assert parse_condition({"key": "hp", "min": 5}).to_dict() == {"key": "hp", "op": "gte", "value": 5}
    assert parse_condition({"key": "d", "value": ["a", "b"]}).op == "in"
    assert parse_condition({"key": "d", "op": "==", "value": "x"}).op == "eq"
    assert parse_condition({"key": "c", "op": "between", "value": [1, 3]}).to_dict()["max"] == 3
    assert parse_condition({"key": "x", "op": "eq", "value": 1, "required": "true"}).required is True


@pytest.mark.parametrize(
    "bad",
    ["health < 低", "cycle 60..30", "ci cle > 3", "", "???", "health <", "x in ", "cycle between a..b", {"key": "health"}, {"key": "1bad", "op": "eq", "value": 1}, {"key": "x", "op": "like", "value": 1}, 42],
)
def test_invalid_conditions_raise_readable_error(bad):
    with pytest.raises(GameKnowledgeError) as info:
        parse_condition(bad)
    assert info.value.code == "invalid_condition"
    assert str(info.value)


def test_parse_conditions_accepts_lines_and_dedupes():
    conditions = parse_conditions("cycle 30..60\nhealth<8；time_of_day==night\ncycle 30-60")
    assert [c.text() for c in conditions] == ["cycle 30..60", "health < 8", "time_of_day == night"]
    assert parse_conditions(None) == [] and parse_conditions("") == [] and parse_conditions([" ", ""]) == []
    with pytest.raises(GameKnowledgeError):
        parse_conditions([f"k{i} < {i}" for i in range(9)])


STATE = {"cycle": 45, "health": 6, "time_of_day": "Night", "dimension": "nether", "hp_text": "low", "raining": True}


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("cycle 30..60", PASS),
        ("cycle 30..45", PASS),
        ("cycle 0..30", FAIL),
        ("cycle >= 200", FAIL),
        ("health < 8", PASS),
        ("health < 6", FAIL),
        ("health <= 6", PASS),
        ("health > 6", FAIL),
        ("health >= 6", PASS),
        ("time_of_day == night", PASS),  # 不分大小写
        ("time_of_day != night", FAIL),
        ("time_of_day != day", PASS),
        ("dimension in overworld, nether", PASS),
        ("dimension in overworld, end", FAIL),
        ("raining == true", PASS),
        ("health == 6", PASS),
        ("food < 3", UNKNOWN),  # 状态里没有这个键
        ("hp_text < 3", UNKNOWN),  # 数值比较遇到非数字
        ("必须 food < 3", FAIL),  # 标了必须：缺键算不满足
    ],
)
def test_evaluate_condition(text, outcome):
    state = {key.casefold(): value for key, value in STATE.items()}
    result, detail = evaluate_condition(parse_condition(text), state)
    assert result == outcome, detail
    assert detail


def test_game_id_validation_is_lowercase_and_strict():
    assert validate_game_id(" MineCraft ") == "minecraft"
    assert validate_game_id("slay_the_spire") == "slay_the_spire"
    for bad in ("", "我的世界", "has space", "-x", "a" * 41):
        with pytest.raises(GameKnowledgeError):
            validate_game_id(bad)


def test_parse_game_context_is_lenient_and_reports_warnings():
    assert parse_game_context(None) is None
    game = parse_game_context({
        "id": "Minecraft",
        "state": {"time_of_day": "night", "health": 6, "inventory": {"apple": 2}, "bad key": 1, "ok": True, "nan": float("nan"), "long": "x" * 200},
        "keywords": ["夜晚", "河边", "", "夜晚"],
    })
    assert game.id == "minecraft" and not game.error
    assert game.state == {"time_of_day": "night", "health": 6, "ok": True, "long": "x" * 60}
    assert game.keywords == ("夜晚", "河边")
    assert any("inventory" in w for w in game.warnings) and any("bad key" in w for w in game.warnings)
    assert parse_game_context("oni").id == "oni"
    assert parse_game_context({"id": "oni", "keywords": "夜晚，河边"}).keywords == ("夜晚", "河边")
    assert parse_game_context({"id": "Bad Id"}).error.startswith("invalid_game_id")
    assert parse_game_context({"state": {}}).error.startswith("game_id_required")
    assert parse_game_context(3).error.startswith("invalid_game_context")
    assert "game.state 不是对象" in parse_game_context({"id": "oni", "state": [1, 2]}).warnings[0]


def test_keyword_haystack_sources_and_boundaries():
    haystack = KeywordHaystack(
        message="苦力怕在我家门口，embedded 也不算 bed 吗",
        keywords=["夜晚降临", "河边"],
        state={"time_of_day": "night", "health": 6},
    )
    assert haystack.find("苦力怕").source == "message"
    assert haystack.find("bed").source == "message"
    assert haystack.find("embed") is None  # 英文按词边界
    assert haystack.find("夜晚").source == "keywords"  # 两字以上互相包含
    assert haystack.find("夜晚").detail == "keywords「夜晚降临」"
    assert haystack.find("河边").detail == "keywords"
    assert haystack.find("night").source == "state"
    assert haystack.find("河") is None  # 单字只在相等时命中 keywords / state
    assert haystack.find("门").source == "message"  # 单个汉字的触发词在消息里按原样匹配
    assert haystack.find("6") is None  # 数字状态值不当关键词
    assert haystack.find("僵尸") is None
    assert KeywordHaystack().empty()


def test_card_trigger_rules():
    state = {"time_of_day": "night", "health": 5, "cycle": 12}
    haystack = KeywordHaystack(message="晚上好，有苦力怕", keywords=["夜晚"], state=state)
    night = parse_conditions("time_of_day == night")
    # 有关键词：命中才触发，条件用来限定
    hit = card_trigger(["夜晚"], night, haystack, state)
    assert hit.triggered and hit.passed_conditions == 1 and "time_of_day=night" in hit.reason
    assert not card_trigger(["僵尸"], night, haystack, state).triggered
    blocked = card_trigger(["苦力怕"], parse_conditions("cycle 30..100"), haystack, state)
    assert not blocked.triggered and blocked.reason.startswith("条件不满足")
    # 条件缺键时不挡关键词；标了必须就挡
    assert card_trigger(["苦力怕"], parse_conditions("dimension == nether"), haystack, state).triggered
    assert not card_trigger(["苦力怕"], parse_conditions("必须 dimension == nether"), haystack, state).triggered
    # 没有关键词：条件全部满足才触发，缺键不触发
    assert card_trigger([], parse_conditions("health <= 6"), haystack, state).triggered
    assert not card_trigger([], parse_conditions("health <= 6\nfood < 3"), haystack, state).triggered
    # 两样都没有：只能通过工具查到
    none = card_trigger([], [], haystack, state)
    assert not none.triggered and "工具" in none.reason


def test_normalize_card_fields_accepts_trigger_block_and_requires_content():
    fields = normalize_card_fields({
        "key": "mc_x", "title": " 第一夜 ", "points": "天黑前封好房子\n- 有床就睡", "pitfalls": ["别乱跑", "别乱跑"],
        "trigger": {"keywords": "夜晚、天黑", "conditions": ["time_of_day == night"]},
        "status": "active", "source_url": "https://example.com/x",
    })
    assert fields["title"] == "第一夜" and fields["card_key"] == "mc_x" and fields["status"] == "active"
    assert fields["points"] == ["天黑前封好房子", "有床就睡"] and fields["pitfalls"] == ["别乱跑"]
    assert fields["keywords"] == ["夜晚", "天黑"]
    assert fields["conditions"] == [{"key": "time_of_day", "op": "eq", "value": "night"}]
    with pytest.raises(GameKnowledgeError) as info:
        normalize_card_fields({"title": "空卡"})
    assert info.value.code == "card_content_required"
    with pytest.raises(GameKnowledgeError) as info:
        normalize_card_fields({"title": "x", "points": ["a"], "source_url": "javascript:alert(1)"})
    assert info.value.code == "invalid_source_url"
    with pytest.raises(GameKnowledgeError):
        normalize_card_fields({"title": "x", "points": ["a"], "status": "published"})


def test_condition_is_hashable_value_object():
    a = parse_condition("dimension in a, b")
    assert isinstance(a, Condition) and a == parse_condition("dimension in a,b") and hash(a) == hash(parse_condition("dimension in a,b"))
