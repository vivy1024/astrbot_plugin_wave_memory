"""工具名与合并映射的唯一来源。

v5.2.0 把同族工具合并成「一个工具 + mode 参数」以减少每次请求的 schema 体积。
旧工具类全部保留（合并工具只是薄路由，逻辑一行不重复），内部按名调用旧工具的服务
（事后记账、每日日记、反思触发）不受影响；提示词与配置里出现旧名时统一经这里映射。
"""

from __future__ import annotations

from typing import Mapping

# ── 合并后的工具名 ──
RECALL = "wave_memory_recall"
LOOKUP = "wave_memory_lookup"
SOCIAL = "wave_memory_social"
NOTE = "wave_memory_note"
EPISODE = "wave_memory_episode"
HOLD_PEACE = "wave_memory_hold_peace"
DEBUG = "wave_memory_debug"

# ── 旧工具名（保留供内部调用与 legacy 模式） ──
SEARCH = "wave_memory_search"
FACTS = "wave_memory_facts"
PERSON_SEARCH = "wave_memory_person_search"
AFFINITY = "wave_memory_affinity"
BOOK_LORE_SEARCH = "wave_memory_book_lore_search"
NOVEL_SOURCE = "wave_memory_novel_source"
MEME_LOOKUP = "wave_memory_meme_lookup"
GAME_KNOWLEDGE = "wave_memory_game_knowledge"
RECORD_SOCIAL_IMPRESSION = "wave_memory_record_social_impression"
NOTE_SOCIAL_ANCHOR = "wave_memory_note_social_anchor"
NOTE_CONCERN = "wave_memory_note_concern"
NOTE_BOT_TAG = "wave_memory_note_bot_tag"
PROPOSE_FACT = "wave_memory_propose_fact"
MARK_CULTURAL_MOMENT = "wave_memory_mark_cultural_moment"
REMEMBER = "wave_memory_remember"
NOTE_EPISODE = "wave_memory_note_episode"
RECORD_DIARY_EPISODE = "wave_memory_record_diary_episode"
EXPLAIN_INJECTION = "wave_memory_explain_injection"
FEEDBACK_MEMORY = "wave_memory_feedback_memory"
SUGGEST_CONFIG = "wave_memory_suggest_config"
SUBMIT_REVIEW_CANDIDATE = "wave_memory_submit_review_candidate"

# 旧名 → (合并工具名, mode)。``tools_allow`` / ``tools_deny`` 里写旧名会按这里解释。
LEGACY_TO_MERGED: Mapping[str, tuple[str, str]] = {
    SEARCH: (RECALL, "search"),
    FACTS: (RECALL, "facts"),
    PERSON_SEARCH: (RECALL, "person"),
    AFFINITY: (RECALL, "relation"),
    BOOK_LORE_SEARCH: (LOOKUP, "lore"),
    NOVEL_SOURCE: (LOOKUP, "novel"),
    MEME_LOOKUP: (LOOKUP, "meme"),
    GAME_KNOWLEDGE: (LOOKUP, "game"),
    RECORD_SOCIAL_IMPRESSION: (SOCIAL, "impression"),
    NOTE_SOCIAL_ANCHOR: (SOCIAL, "anchor"),
    NOTE_CONCERN: (SOCIAL, "concern"),
    NOTE_BOT_TAG: (SOCIAL, "bot_check"),
    PROPOSE_FACT: (NOTE, "fact"),
    MARK_CULTURAL_MOMENT: (NOTE, "cultural"),
    REMEMBER: (NOTE, "memory"),
    NOTE_EPISODE: (EPISODE, "group"),
    RECORD_DIARY_EPISODE: (EPISODE, "diary"),
    EXPLAIN_INJECTION: (DEBUG, "explain"),
    FEEDBACK_MEMORY: (DEBUG, "feedback"),
    SUGGEST_CONFIG: (DEBUG, "suggest"),
    SUBMIT_REVIEW_CANDIDATE: (DEBUG, "candidate"),
}

MERGED_TOOLS = frozenset({RECALL, LOOKUP, SOCIAL, NOTE, EPISODE, DEBUG})


def merged_for(legacy_name: str) -> tuple[str, str] | None:
    return LEGACY_TO_MERGED.get(str(legacy_name or ""))


def normalize_tool_ref(ref: str) -> tuple[str, str | None]:
    """把配置里的一条工具引用解析成 (工具名, mode|None)。

    支持三种写法：``wave_memory_recall``（整个合并工具）、``wave_memory_social:concern``
    （只放开/关闭其中一个 mode）、旧名 ``wave_memory_note_concern``（等价于对应 mode）。
    """
    text = str(ref or "").strip()
    if ":" in text:
        name, _, mode = text.partition(":")
        return name.strip(), (mode.strip() or None)
    mapped = LEGACY_TO_MERGED.get(text)
    if mapped is not None:
        return mapped[0], mapped[1]
    return text, None


# ── 提示词里的工具引用：统一输出合并工具写法 ──
def tool_surface() -> str:
    return "merged"


def tool_ref(legacy_name: str, **args: str) -> str:
    """模型该怎么调用这个能力：统一渲染为 ``wave_memory_*(mode=..., ...)``。"""
    mapped = LEGACY_TO_MERGED.get(str(legacy_name or ""))
    if mapped is None:
        inner = ", ".join(f"{k}={v}" for k, v in args.items())
        return f"{legacy_name}({inner})" if inner else str(legacy_name)
    merged, mode = mapped
    inner = ", ".join([f"mode={mode}", *(f"{k}={v}" for k, v in args.items())])
    return f"{merged}({inner})"


__all__ = [
    "DEBUG", "EPISODE", "HOLD_PEACE", "LEGACY_TO_MERGED", "LOOKUP", "MERGED_TOOLS", "NOTE", "RECALL", "SOCIAL",
    "merged_for", "normalize_tool_ref", "tool_ref", "tool_surface",
] + [name for name in dir() if name.isupper() and name not in {"LEGACY_TO_MERGED", "MERGED_TOOLS"}]
