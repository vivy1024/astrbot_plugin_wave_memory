"""七个合并工具的定义：每一族「一个工具 + mode」，子工具仍是原来的类与实例。

``tool_surface = merged``（默认）注册这些；``legacy`` 注册原来的 17 个。两种模式下
内部按旧名调用工具的服务（事后记账、每日日记、反思触发）都能工作：合并模式下旧工具仍会被
实例化并登记为**内部工具**（不交给模型），只是不再出现在模型的工具清单里。
"""

from __future__ import annotations

from typing import Any, Callable

from .merged import MergedTool, ModeSpec, as_function_tool
from . import names as N

# ── 各族的 mode 声明 ──

RECALL_MODES = [
    ModeSpec("search", N.SEARCH, "按内容检索长期记忆（query 必填）"),
    ModeSpec("facts", N.FACTS, "查已确认的事实三元组（query 必填）"),
    ModeSpec("person", N.PERSON_SEARCH, "按人查：最近发言、被提及、常互动对象、画像、完整印象时间线（person 必填，query_type 选视图）",
             hide=frozenset({"cross_group"})),
    ModeSpec(
        "relation", N.AFFINITY,
        "查对某人的好感与关系；target_user 留空是当前发言者，view=ranking/blacklist/active 看排行",
        rename={"view": "mode"}, hide=frozenset({"user_id"}),
    ),
]

LOOKUP_MODES = [
    ModeSpec("lore", N.BOOK_LORE_SEARCH, "查书设与世界观（query 必填）"),
    ModeSpec("novel", N.NOVEL_SOURCE, "查原著原文：忘记桥段、原话或章节位置时直接回原文（keyword 或 chapter）"),
    ModeSpec("meme", N.MEME_LOOKUP, "查梗百科：看不懂梗或想接梗时查释义与例句（query 必填）"),
    ModeSpec("game", N.GAME_KNOWLEDGE, "查游戏攻略与机制常识（game 与 query 必填）；查自己玩过的事用 recall"),
]

SOCIAL_MODES = [
    ModeSpec("impression", N.RECORD_SOCIAL_IMPRESSION, "对某人形成或更新看法，并在系统给的范围内给出好感变化（target_user 必填）",
             hide=frozenset({"shift_reason", "affinity_delta"})),
    ModeSpec("anchor", N.NOTE_SOCIAL_ANCHOR, "记下一件值得记住的交往：帮过忙、受过帮助、许过诺、被冒犯（target_user、anchor_type、summary 必填）"),
    ModeSpec("concern", N.NOTE_CONCERN, "记录/跟进/了结一件挂念的事（action 必填）"),
]

NOTE_MODES = [
    ModeSpec("fact", N.PROPOSE_FACT, "提交一条关于某人或某事的事实待审（需要 source_quote 逐字原话）"),
    ModeSpec("cultural", N.MARK_CULTURAL_MOMENT, "标记一个值得学的梗或一句好回复（moment_type、target_phrase_or_snippet、context_note）"),
    ModeSpec("memory", N.REMEMBER, "把一件事主动记进长期记忆（content 必填）"),
]

EPISODE_MODES = [
    ModeSpec("group", N.NOTE_EPISODE, "记录一次互动经历：触发、你做了什么、对方反应、结果"),
    ModeSpec("diary", N.RECORD_DIARY_EPISODE, "写下今天的日记并记一条经历（仅日记流程使用）"),
]

DEBUG_MODES = [
    ModeSpec("explain", N.EXPLAIN_INJECTION, "解释某次注入为什么注入了这些内容（trace_id）"),
    ModeSpec("feedback", N.FEEDBACK_MEMORY, "给某条被注入的记忆打反馈（trace_id、memory_id、feedback）"),
    ModeSpec("suggest", N.SUGGEST_CONFIG, "针对注入问题提配置建议（scope、problem、evidence_trace_ids）"),
    ModeSpec("candidate", N.SUBMIT_REVIEW_CANDIDATE, "提交一条待审候选（type、content、evidence、reason）"),
]

# 合并工具名 → (一句话说明, mode 列表, 对应子工具所属的能力开关组)
FAMILIES: dict[str, tuple[str, list[ModeSpec], str]] = {
    N.RECALL: ("查记忆与关系。用 mode 选查什么。", RECALL_MODES, "memory"),
    N.LOOKUP: ("查外部知识：书设、原著、梗、游戏。用 mode 选查哪一类。", LOOKUP_MODES, "knowledge"),
    N.SOCIAL: ("记录你对人的看法、交往与挂念。用 mode 选记什么。", SOCIAL_MODES, "social"),
    N.NOTE: ("提交要沉淀下来的东西：事实、梗、主动记忆。用 mode 选类型。", NOTE_MODES, "note"),
    N.EPISODE: ("记录经历。用 mode 选类型。", EPISODE_MODES, "episode"),
    N.DEBUG: ("诊断注入质量（调试用）。", DEBUG_MODES, "debug"),
}

# diary 只给日记流程内部调用，不暴露给模型
INTERNAL_ONLY_MODES = {(N.EPISODE, "diary")}


def build_merged_tool(
    name: str,
    resolve_child: Callable[[str], Any | None],
    *,
    is_allowed: Callable[[Any, str, str], bool] | None = None,
    expose_internal: bool = False,
) -> Any | None:
    """构造一个合并工具的 AstrBot FunctionTool；没有任何可用子工具时返回 None。"""
    intro, modes, _group = FAMILIES[name]
    if not expose_internal:
        modes = [spec for spec in modes if (name, spec.mode) not in INTERNAL_ONLY_MODES]
    router = MergedTool(name=name, intro=intro, modes=modes, resolve_child=resolve_child, is_allowed=is_allowed)
    if not router.available_modes():
        return None
    return as_function_tool(router)


__all__ = ["FAMILIES", "INTERNAL_ONLY_MODES", "build_merged_tool"]
