"""纯图片/表情占位消息不触发召回，也不让纯占位历史挤占注入。"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if "astrbot.api" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("astrbot-test")
    sys.modules.setdefault("astrbot", _astrbot)
    sys.modules["astrbot.api"] = _api

from services.injection.channels.fts5 import FTS5Channel
from services.injection.channels.memory_recall import _filter_placeholder_only
from services.injection.query_gate import (
    is_placeholder_only,
    should_skip_retrieval,
    strip_placeholders,
)


class _Ctx:
    def __init__(self, message):
        self.message = message
        self.config = {}


@pytest.mark.parametrize("text", [
    "[图片]",
    "[图片] [图片] [图片]",
    "[表情:264]",
    "[动画表情]",
    "伊芙: [图片] 伊芙: [图片]",
    "时雨: [图片]\n时雨: [图片]",
    "[At:2715127743] [图片]",
    "[CQ:image,file=abc.jpg]",
])
def test_placeholder_only_detected(text):
    assert is_placeholder_only(text) is True


@pytest.mark.parametrize("text", [
    "大肥鱼: 才不叫爹妈呢 [图片]",
    "尘: @阿恒 等月底超实惠的0.01 kiro克() 尘: [图片]",
    "这张图好看 [图片]",
    "注意：",
    "",
    "普通的一句话",
])
def test_text_bearing_messages_are_not_placeholder_only(text):
    assert is_placeholder_only(text) is False


def test_strip_placeholders_keeps_real_text():
    assert strip_placeholders("伊芙: 好耶 [图片]") == "好耶"


@pytest.mark.parametrize("message", ["[图片]", "[表情:5] [图片]", "[At:123] [图片]"])
def test_gate_skips_placeholder_only_query(message):
    skip, reason = should_skip_retrieval(_Ctx(message))
    assert skip is True
    assert reason in {"placeholder_only_query", "empty_query_after_normalization"}


def test_gate_keeps_image_with_text():
    skip, _ = should_skip_retrieval(_Ctx("这张图里是谁 [图片]"))
    assert skip is False


def test_memory_filter_drops_placeholder_only_rows():
    rows = [
        {"id": 1, "content": "伊芙: [图片] 伊芙: [图片]"},
        {"id": 2, "content": "伊芙: 看这个 [图片]"},
    ]
    kept, filtered = _filter_placeholder_only(rows)
    assert [row["id"] for row in kept] == [2]
    assert filtered[0]["filter_reason"] == "placeholder_only"


def test_fts5_filter_drops_placeholder_only_rows():
    rows = [
        {"id": 1, "content": "游: [图片] 游: [图片]", "score": 1.0},
        {"id": 2, "content": "游: 这图绝了 [图片]", "score": 1.0},
    ]
    selected, filtered = FTS5Channel._filter_and_budget(rows, top_k=5, token_budget=999, min_score=0.0)
    assert [row["id"] for row in selected] == [2]
    assert any(item["filter_reason"] == "placeholder_only" for item in filtered)


def _classify():
    from astrbot_plugin_wave_memory.services.message_writer import classify_source

    return classify_source


def test_classify_source_marks_placeholder_only_as_noise():
    classify_source = _classify()
    assert classify_source("[图片] [图片] [图片] [图片]", "u1", set()) == "noise"
    assert classify_source("羽书 [图片]", "u1", {"羽书"}) == "core"
    assert classify_source("[At:2715127743] [图片]", "u1", set(), is_at_bot=True) == "noise"
    assert classify_source("[图片]", "bot", set()) == "noise"
    assert classify_source("哼 [图片]", "bot", set()) == "core"


def test_classify_source_keeps_text_with_image():
    classify_source = _classify()
    assert classify_source("今天的晚饭真不错吃饱了 [图片]", "u1", set()) == "chat"
