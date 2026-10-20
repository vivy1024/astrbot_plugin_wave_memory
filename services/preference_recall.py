"""把 Bot 的喜恶预设按当前话题挑出 1–2 条，供注入通道使用。

喜恶是 Bot 自己的稳定倾向，不属于某个群；这里只做词面相关性筛选，不调用模型，
也不生成新内容。没有相关条目时返回空，不把整套预设每轮复读一遍。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

try:
    from ..domain.bot_profile import PreferenceItem, PreferenceSettings, ReactionExample
except ImportError:  # pragma: no cover - 独立测试
    from domain.bot_profile import PreferenceItem, PreferenceSettings, ReactionExample

_MIN_TEXT_RELEVANCE = 0.2
_TAG_HIT = 3.0


@dataclass(frozen=True)
class SelectedPreference:
    kind: str
    text: str
    tags: tuple[str, ...]
    strength: int
    score: float
    situation: str = ""
    reaction: str = ""

    def to_audit(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "situation": self.situation,
            "reaction": self.reaction,
            "tags": list(self.tags),
            "strength": self.strength,
            "score": round(self.score, 3),
        }


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\s\W_]+", "", _normalize(text))
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _context_matches(rule_context: str, visibility: str) -> bool:
    context = str(rule_context or "all").strip().lower()
    return context in {"", "all"} or context == str(visibility or "").strip().lower()


def _relevance(message: str, message_bigrams: set[str], text: str, tags: Iterable[str]) -> float:
    normalized = _normalize(message)
    tag_hits = sum(1 for tag in tags if tag and _normalize(tag) in normalized)
    score = tag_hits * _TAG_HIT
    target = _bigrams(text)
    if target and message_bigrams:
        overlap = len(target & message_bigrams) / len(target)
        score += overlap * 2.0
        if not tag_hits and overlap < _MIN_TEXT_RELEVANCE:
            return 0.0
    return score


def _item_score(message: str, message_bigrams: set[str], item: PreferenceItem) -> float:
    score = _relevance(message, message_bigrams, item.text, item.tags)
    if score <= 0:
        return 0.0
    return score + min(5, max(1, int(item.strength or 3))) * 0.1


def _example_score(message: str, message_bigrams: set[str], item: ReactionExample) -> float:
    score = max(
        _relevance(message, message_bigrams, item.situation, item.tags),
        _relevance(message, message_bigrams, item.reaction, item.tags) * 0.6,
    )
    if score <= 0:
        return 0.0
    return score + min(5, max(1, int(item.strength or 3))) * 0.1


def select_preferences(
    settings: PreferenceSettings | None,
    message: str,
    *,
    visibility: str = "group",
    max_items: int = 2,
) -> list[SelectedPreference]:
    """按当前消息挑选相关喜恶；没有相关项时返回空列表。"""
    if settings is None or settings.is_empty() or max_items <= 0:
        return []
    text = str(message or "").strip()
    if not text:
        return []
    message_bigrams = _bigrams(text)
    candidates: list[SelectedPreference] = []
    for item in settings.likes:
        if not _context_matches(item.context, visibility):
            continue
        score = _item_score(text, message_bigrams, item)
        if score > 0:
            candidates.append(SelectedPreference(
                kind="like", text=item.text, tags=tuple(item.tags),
                strength=item.strength, score=score,
            ))
    for item in settings.dislikes:
        if not _context_matches(item.context, visibility):
            continue
        score = _item_score(text, message_bigrams, item)
        if score > 0:
            candidates.append(SelectedPreference(
                kind="dislike", text=item.text, tags=tuple(item.tags),
                strength=item.strength, score=score,
            ))
    for item in settings.reaction_examples:
        if not _context_matches(item.context, visibility):
            continue
        score = _example_score(text, message_bigrams, item)
        if score > 0:
            candidates.append(SelectedPreference(
                kind="reaction", text=item.reaction, tags=tuple(item.tags),
                strength=item.strength, score=score,
                situation=item.situation, reaction=item.reaction,
            ))
    candidates.sort(key=lambda item: (-item.score, -item.strength, item.kind, item.text))
    return candidates[:max(1, min(3, int(max_items)))]


__all__ = ["SelectedPreference", "select_preferences"]
