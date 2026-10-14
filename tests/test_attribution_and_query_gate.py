"""人称归属标注与短查询门控回归测试。

背景一（归属）：注入模板原为 `[记忆] 某人(时间): 内容`，模型无法分辨这句是不是
当前对话者说的，于是把群友的历史当成对方说过的话（第一次答错、被提醒后才纠正）。

背景二（门控）：像「如何评价？」「这个视频」这类无目标短句仍会触发向量与全文召回，
结果与意图无关却占注入预算。但真实注入样本显示 4-5 字消息里大量是有检索目标的
（「我什么星座」「查询好感度」「有没有我的」「你玩原神吗」），因此门控只拦
白名单化的功能性短句，不做长度粗筛。
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

if "astrbot.api" not in sys.modules:  # pragma: no cover - test harness shim
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from engine.query_engine import QueryEngine
from services.injection.query_gate import normalize_query, should_skip_retrieval

SPEAKER = "1465892206"
BOT_QQ = "1923563505"
BOT_DB = "yushu"


def _memory(sender_id, content, *, sender_name="", score=1.0, source="chat"):
    return {
        "id": abs(hash((sender_id, content))) % 100000,
        "sender_id": sender_id,
        "sender_name": sender_name or sender_id,
        "content": content,
        "timestamp": 1785600000.0,
        "score": score,
        "source": source,
        "group_id": "100000001",
    }


class _EngineStub:
    """只借用 QueryEngine 的渲染逻辑，绕开其重型依赖构造。"""

    format_injection = QueryEngine.format_injection
    _attribution_tag = staticmethod(QueryEngine._attribution_tag)

    @staticmethod
    def _prefer_current_group_and_dedupe(memories, current_group_id=""):
        return list(memories)


def _render(memories, *, speaker_id=SPEAKER, bot_ids=(BOT_QQ, BOT_DB)):
    return _EngineStub().format_injection(
        memories,
        speaker_id=speaker_id,
        bot_ids=set(bot_ids),
    )


class TestAttributionTags:
    def test_speaker_own_history_is_labelled(self):
        text = _render([_memory(SPEAKER, "我在Duke做康普顿散射实验", sender_name="练体博导")])

        assert "[对话者本人历史]" in text
        assert "康普顿散射" in text

    def test_other_member_history_is_labelled(self):
        """回归核心：别人说的话必须显式标注，否则会被当成对话者说过的。"""
        text = _render([_memory("999", "你跑不过我你信吗", sender_name="苍")])

        assert "[其他群友历史]" in text
        assert "[对话者本人历史]" not in text

    def test_bot_own_reply_is_labelled_by_qq_id(self):
        text = _render([_memory(BOT_QQ, "高端龙井的味道层次", sender_name="羽书")])

        assert "[你的历史回复]" in text

    def test_bot_own_reply_is_labelled_by_db_id(self):
        text = _render([_memory(BOT_DB, "我继续潜水", sender_name="羽书")])

        assert "[你的历史回复]" in text

    def test_legacy_bot_sender_literal_is_labelled(self):
        """旧数据里 bot 自己的记忆 sender_id 写作字面量 'bot'。"""
        text = _render([_memory("bot", "那也是bug害的", sender_name="羽书")])

        assert "[你的历史回复]" in text

    def test_all_three_kinds_are_distinguished_together(self):
        text = _render([
            _memory(SPEAKER, "我下个月就回国"),
            _memory("999", "你跑不过我你信吗"),
            _memory(BOT_QQ, "我看不了视频"),
        ])

        assert "[对话者本人历史]" in text
        assert "[其他群友历史]" in text
        assert "[你的历史回复]" in text

    def test_no_tags_when_attribution_context_absent(self):
        """未提供归属上下文时保持旧格式，确保向后兼容。"""
        text = _render([_memory("999", "普通记忆")], speaker_id="", bot_ids=())

        assert "[其他群友历史]" not in text
        assert "[记忆]" in text

    def test_missing_sender_id_produces_no_tag(self):
        memory = _memory("", "来源不明的记忆")
        memory["sender_id"] = ""

        text = _render([memory])

        assert "[其他群友历史]" not in text
        assert "[对话者本人历史]" not in text

    def test_relevance_and_content_are_preserved(self):
        text = _render([_memory(SPEAKER, "斑衣蜡蝉对人没害吧", score=0.87)])

        assert "relevance: 0.87" in text
        assert "斑衣蜡蝉对人没害吧" in text


class _GateCtx:
    def __init__(self, message, config=None):
        self.message = message
        self.config = config if config is not None else {}


class TestQueryGateSkips:
    @pytest.mark.parametrize("message", [
        "如何评价？", "评价一下", "这个视频", "这个", "确实", "好的", "嗯",
        "怎么看", "然后呢", "听到没有",
    ])
    def test_generic_functional_short_queries_are_skipped(self, message):
        skip, reason = should_skip_retrieval(_GateCtx(message))

        assert skip is True
        assert reason == "generic_short_query"

    def test_punctuation_only_message_is_skipped(self):
        skip, reason = should_skip_retrieval(_GateCtx("？？？！"))

        assert skip is True
        assert reason == "empty_query_after_normalization"

    def test_single_character_without_entity_is_skipped(self):
        skip, reason = should_skip_retrieval(_GateCtx("哦"))

        assert skip is True


class TestQueryGateKeeps:
    @pytest.mark.parametrize("message", [
        "我什么星座",
        "我的成分呢",
        "查询好感度",
        "有没有我的",
        "你玩原神吗？",
        "这是真的吗",
        "咖啡",
        "聊聊咖啡",
        "fw羽书",
    ])
    def test_real_short_queries_with_intent_are_kept(self, message):
        """取自真实注入样本：4-5 字里大量是有检索目标的，不能按长度粗筛。"""
        skip, _ = should_skip_retrieval(_GateCtx(message))

        assert skip is False

    @pytest.mark.parametrize("message", [
        "还记得吗", "之前说过什么", "上次那个", "昨天聊了啥", "你忘了",
    ])
    def test_explicit_recall_intent_always_retrieves(self, message):
        skip, _ = should_skip_retrieval(_GateCtx(message))

        assert skip is False

    def test_entity_hint_rescues_short_message(self):
        skip, _ = should_skip_retrieval(_GateCtx("查 HIGS"))

        assert skip is False

    def test_at_mention_rescues_short_message(self):
        skip, _ = should_skip_retrieval(_GateCtx("@练体博导 在吗"))

        assert skip is False

    def test_long_message_is_never_skipped(self):
        skip, _ = should_skip_retrieval(
            _GateCtx("我在家泳池游泳，遇到了很多斑衣蜡蝉，对人没害吧")
        )

        assert skip is False


class TestQueryGateConfig:
    def test_gate_can_be_disabled(self):
        ctx = _GateCtx("如何评价？", {"query_gate": {"enabled": False}})

        skip, reason = should_skip_retrieval(ctx)

        assert skip is False
        assert reason == ""

    def test_normalize_strips_punctuation_and_whitespace(self):
        assert normalize_query(" 如何评价？！ ") == "如何评价"

    def test_normalize_handles_none(self):
        assert normalize_query(None) == ""


class TestChannelGateWiring:
    def test_memory_channel_reports_skip_reason(self):
        import asyncio

        from services.injection.channels.memory_recall import MemoryRecallChannel
        from services.injection.context import InjectionContext
        from domain.scope import RuntimeScope, SessionRef

        class _Engine:
            def __init__(self):
                self.called = False

            async def query(self, **kwargs):
                self.called = True
                return []

            def format_injection(self, memories, template="", current_group_id="", **kwargs):
                return "should not be reached"

        engine = _Engine()
        ctx = InjectionContext(
            event="event",
            req=object(),
            message="如何评价？",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id=BOT_QQ,
            bot_profile_id=BOT_DB,
            scope=RuntimeScope(
                bot_id=BOT_DB,
                visibility="group",
                session=SessionRef("qq:group:g1", "qq", "group", "g1"),
            ),
            config={"channels": {"memory": {"top_k": 5}}},
        )

        result = asyncio.run(MemoryRecallChannel(query_engine=engine).build(ctx))

        assert result.status == "empty"
        assert "generic_short_query" in result.warnings
        assert engine.called is False
