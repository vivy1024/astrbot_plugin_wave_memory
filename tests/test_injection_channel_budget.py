"""编排器按单通道执行 token_budget：按条目/行截断，截不动就跳过并留下可观测的原因。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from services.config.channel_config import KNOWN_CHANNELS, apply_channel_overrides, build_default_channel_config
from services.injection.channel_base import InjectionResult, estimate_injection_tokens
from services.injection.context import InjectionContext
from services.injection.orchestrator import InjectionOrchestrator
from services.injection.trace_store import InjectionTraceStore


class _Req:
    def __init__(self):
        self.extra_user_content_parts = []


class _Channel:
    def __init__(self, result: InjectionResult):
        self.name = result.channel
        self._result = result

    async def build(self, ctx):
        return self._result


def _config(**channels):
    only = {name: {"enabled": False} for name in KNOWN_CHANNELS if name not in {"safety", *channels}}
    patch = {name: {"enabled": True, **values} for name, values in channels.items()}
    return apply_channel_overrides(build_default_channel_config(runtime_mode="full"), {"channels": {**only, **patch}})


def _run(results, config, store=None, trace_id="trace-budget"):
    orchestrator = InjectionOrchestrator(
        channels=[_Channel(result) for result in results],
        config=config,
        trace_store=store,
        text_part_factory=lambda text: text,
    )
    ctx = InjectionContext(
        event=None, req=_Req(), message="hello", group_id="20000001", sender_id="10000001",
        sender_name="测试群友", bot_id="bot-test", bot_profile_id="bot-test", trace_id=trace_id,
    )
    return asyncio.run(orchestrator.run(ctx))


def _fact_lines(count: int) -> list[str]:
    return [f"- 测试群友{i:02d} 喜欢 虚构的第{i:02d}种水果和第{i:02d}种颜色" for i in range(count)]


def _facts_result(count: int = 10) -> InjectionResult:
    lines = _fact_lines(count)
    text = "<known_facts>\n" + "\n".join(lines) + "\n</known_facts>"
    items = [{"rowid": i, "rendered_text": line, "dedupe_key": f"fact|{i}", "preview": line} for i, line in enumerate(lines)]
    return InjectionResult.hit("facts", text, items=items)


def test_channel_within_its_budget_is_untouched():
    result = _facts_result(3)
    original = result.text
    out = _run([result], _config(facts={"token_budget": 500}))
    assert out.final_text == original
    assert not any("channel_token_budget" in warning for warning in result.warnings)


def test_oversized_channel_is_truncated_by_items_and_keeps_wrapper():
    result = _facts_result(10)
    assert estimate_injection_tokens(result.text) > 60
    out = _run([result], _config(facts={"token_budget": 60}))
    assert result.status == "hit"
    assert estimate_injection_tokens(out.final_text) <= 60
    assert out.final_text.startswith("<known_facts>") and out.final_text.endswith("</known_facts>")
    kept_lines = [line for line in out.final_text.splitlines() if line.startswith("- ")]
    assert kept_lines == _fact_lines(10)[: len(kept_lines)] and 1 <= len(kept_lines) < 10
    assert len(result.items) == len(kept_lines)
    assert result.tokens == estimate_injection_tokens(result.text)
    dropped = [item for item in result.filtered if item.get("filter_reason") == "channel_token_budget"]
    assert len(dropped) == 10 - len(kept_lines)
    assert any(warning.startswith("channel_token_budget_truncated") and "条目" in warning for warning in result.warnings)


def test_oversized_channel_without_item_rendering_is_truncated_by_lines():
    lines = [f"[其他群友历史] [记忆] 测试群友{i:02d}(09-28 10:{i:02d}): 第{i:02d}条虚构的聊天记录内容比较长 (relevance: 0.80)" for i in range(8)]
    text = "<wave_memory>\n" + "\n".join(lines) + "\n</wave_memory>"
    items = [{"id": i, "preview": f"第{i:02d}条虚构的聊天记录内容比较长"} for i in range(8)]
    result = InjectionResult.hit("memory", text, items=items)
    out = _run([result], _config(memory={"token_budget": 70}))
    assert estimate_injection_tokens(out.final_text) <= 70
    assert out.final_text.endswith("</wave_memory>")
    kept = [line for line in out.final_text.splitlines() if line.startswith("[其他群友历史]")]
    assert kept == lines[: len(kept)] and kept
    # 被截掉的记忆不再出现在 trace 的 items 里，而是记为 filtered
    assert [item["id"] for item in result.items] == list(range(len(kept)))
    assert {item["id"] for item in result.filtered} == set(range(len(kept), 8))
    assert any("按行截断" in warning for warning in result.warnings)


def test_empty_sections_are_removed_after_truncation():
    header = "[黑话理解参考：只解释用户消息中已经出现的词条]"
    local = "【本群私域黑话】\n" + "\n".join(f'- "本群词{i}" → 虚构的本群释义第{i}条内容' for i in range(2))
    shared = "【广域黑话】\n" + "\n".join(f'- "广域词{i}" → 虚构的广域释义第{i}条内容比较长比较长' for i in range(4))
    result = InjectionResult.hit("jargon", "\n\n".join([header, local, shared]))
    out = _run([result], _config(jargon={"token_budget": 32}))
    assert estimate_injection_tokens(out.final_text) <= 32
    assert out.final_text.startswith(header)
    assert not out.final_text.rstrip().endswith("【广域黑话】")


def test_single_block_that_cannot_be_cut_is_skipped_with_visible_reason(caplog):
    store = InjectionTraceStore(sqlite3.connect(":memory:"))
    store.ensure_schema()
    long_text = "这是一整段无法按行拆开的超长人格描述" * 20
    result = InjectionResult.hit("persona", long_text, items=[{"rendered_text": long_text}])
    short = InjectionResult.hit("facts", "<known_facts>\n- 测试群友 喜欢 苹果\n</known_facts>")
    with caplog.at_level(logging.WARNING, logger="services.injection.orchestrator"):
        out = _run([result, short], _config(persona={"token_budget": 50}, facts={"token_budget": 100}), store, "trace-skip")
    assert result.status == "skipped"
    assert long_text not in out.final_text
    assert "测试群友 喜欢 苹果" in out.final_text
    assert any(warning.startswith("channel_token_budget_exceeded") for warning in result.warnings)
    assert "超出自身预算" in caplog.text
    row = next(item for item in store.get("trace-skip")["channels"] if item["channel"] == "persona")
    assert row["status"] == "skipped"
    assert "channel_token_budget_exceeded" in str(row)


def test_zero_budget_channel_is_skipped():
    result = InjectionResult.hit("book_lore", "短书设")
    _run([result], _config(book_lore={"token_budget": 0}))
    assert result.status == "skipped"


def test_preserved_affinity_keeps_independent_budget_semantics():
    timeline = "印象时间线\n" + "\n".join(f"- 事件#{i:02d} 旧摘要{i:02d}" for i in range(40))
    affinity = InjectionResult.hit("affinity", timeline, preserve_full_text=True)
    out = _run([affinity], _config(affinity={"token_budget": 1}))
    assert affinity.text == timeline
    assert "事件#39 旧摘要39" in out.final_text
    assert "full_timeline_exceeds_injection_budget" in affinity.warnings
    assert not any("channel_token_budget" in warning for warning in affinity.warnings)


def test_safety_guard_is_exempt_from_channel_budget():
    # safety 自身 token_budget=0，只受全局总预算约束（沿用原语义），不受单通道预算约束。
    guard = InjectionResult.hit("safety", "身份安全守卫：" + "不要认爹认主。" * 30)
    out = _run([guard], _config(memory={"token_budget": 600}))
    assert guard.status == "hit"
    assert out.final_text == guard.text


# ── 去重（修复前 \s 写成字面反斜杠，去重从未生效） ───────────────


def test_duplicate_item_lines_are_removed_from_later_channels():
    first_lines = ["- 测试群友 喜欢 苹果", "- 测试群友 住在 虚构市"]
    facts = InjectionResult.hit(
        "facts",
        "<known_facts>\n" + "\n".join(first_lines) + "\n</known_facts>",
        items=[{"rendered_text": line, "dedupe_key": f"fact|{i}"} for i, line in enumerate(first_lines)],
    )
    later_lines = ["- 测试群友 住在 虚构市", "- 测试群友 喜欢 苹果派", "- 测试群友 养了 一只猫"]
    belief = InjectionResult.hit(
        "belief",
        "[相关信念]\n" + "\n".join(later_lines),
        items=[
            {"rendered_text": later_lines[0], "dedupe_key": "fact|1"},
            {"rendered_text": later_lines[1], "dedupe_key": "fact|9"},
            {"rendered_text": later_lines[2], "dedupe_key": "fact|10"},
        ],
    )
    out = _run([facts, belief], _config(facts={"token_budget": 500}, belief={"token_budget": 500}))
    assert out.final_text.count("- 测试群友 住在 虚构市") == 1
    # 只删整行相同的重复条目；「苹果派」与「苹果」只是前缀相同，不受影响
    assert "- 测试群友 喜欢 苹果派" in out.final_text
    assert "- 测试群友 养了 一只猫" in out.final_text
    assert "[相关信念]" in out.final_text


def test_channel_whose_items_are_all_duplicates_is_dropped():
    line = "- 测试群友 喜欢 苹果"
    first = InjectionResult.hit("facts", f"<known_facts>\n{line}\n</known_facts>", items=[{"rendered_text": line, "dedupe_key": "fact|1"}])
    second = InjectionResult.hit("belief", f"[相关信念]\n{line}", items=[{"rendered_text": line, "dedupe_key": "fact|1"}])
    out = _run([first, second], _config(facts={"token_budget": 500}, belief={"token_budget": 500}))
    assert out.final_text.count(line) == 1
    assert "[相关信念]" not in out.final_text


def test_whole_text_dedupe_ignores_whitespace_differences_only():
    a = InjectionResult.hit("facts", "同一段  注入\n文本")
    b = InjectionResult.hit("belief", "同一段 注入 文本")
    c = InjectionResult.hit("book_lore", "同一段 注入 文本，但多了一句")
    out = _run([a, b, c], _config(facts={"token_budget": 500}, belief={"token_budget": 500}, book_lore={"token_budget": 500}))
    assert out.final_text.count("注入") == 2
    assert "同一段  注入\n文本" in out.final_text
    assert "同一段 注入 文本，但多了一句" in out.final_text


# ── tokens 为 0 的结果按正文补估算 ────────────────────────────


class _LateTextChannel:
    """构造结果后才填正文的通道：tokens / chars 停在 0。"""

    def __init__(self, name: str, text: str):
        self.name = name
        self.text = text

    async def build(self, ctx):
        result = InjectionResult(channel=self.name, status="hit")
        result.text = self.text
        return result


def test_missing_tokens_are_estimated_so_budgets_and_trace_apply():
    store = InjectionTraceStore(sqlite3.connect(":memory:"))
    store.ensure_schema()
    lines = [f"[其他群友历史] [记忆] 测试群友{i:02d}(09-28 10:{i:02d}): 第{i:02d}条比较长的虚构聊天记录 (relevance: 0.80)" for i in range(10)]
    long_text = "<wave_memory>\n" + "\n".join(lines) + "\n</wave_memory>"
    orchestrator = InjectionOrchestrator(
        channels=[_LateTextChannel("memory", long_text), _LateTextChannel("facts", "<known_facts>\n- 测试群友 喜欢 苹果\n</known_facts>")],
        config=_config(memory={"token_budget": 60}, facts={"token_budget": 100}),
        trace_store=store,
        text_part_factory=lambda text: text,
    )
    ctx = InjectionContext(
        event=None, req=_Req(), message="hello", group_id="20000001", sender_id="10000001",
        sender_name="测试群友", bot_id="bot-test", bot_profile_id="bot-test", trace_id="trace-late-text",
    )
    out = asyncio.run(orchestrator.run(ctx))
    memory = next(result for result in out.channel_results if result.channel == "memory")
    assert 0 < memory.tokens <= 60
    assert memory.chars == len(memory.text)
    assert any("channel_token_budget_truncated" in warning for warning in memory.warnings)
    rows = {row["channel"]: row for row in store.get("trace-late-text")["channels"]}
    assert rows["memory"]["tokens"] > 0 and rows["facts"]["tokens"] > 0
