"""进程内已注册 Bot 的身份快照。

BotRegistry 在加载与热重载时调用 :func:`publish`；身份安全、黑话过滤、经历分段等
没有 Bot 上下文的模块从这里读取，而不是在代码里写死某个 Bot 的名字或账号。
本模块只保存纯数据，不依赖任何其他层，可以被 engine/services/webui 任意引用。
"""

from __future__ import annotations

from collections.abc import Iterable
from threading import Lock

# 与具体 Bot 无关的自指/指向 Bot 的词。
GENERIC_SELF_TERMS: tuple[str, ...] = ("我", "你", "bot", "机器人", "AI")

# 平台回推的 Bot 自身消息在库里统一记为 sender_id="bot"。
BOT_SENDER_SENTINEL = "bot"

_lock = Lock()
_revision = 0
_sender_ids: frozenset[str] = frozenset({BOT_SENDER_SENTINEL})
_identity_terms: tuple[str, ...] = ()
_experience_sources: frozenset[str] = frozenset()
_taken_over_sources: frozenset[str] = frozenset()
_personal_lore_categories: frozenset[str] = frozenset()


def _clean(values: Iterable[object] | None) -> list[str]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def publish(
    *,
    sender_ids: Iterable[object] = (),
    identity_terms: Iterable[object] = (),
    experience_sources: Iterable[object] = (),
    taken_over_sources: Iterable[object] = (),
    personal_lore_categories: Iterable[object] = (),
) -> int:
    """替换整份快照并返回新的版本号。"""
    global _revision, _sender_ids, _identity_terms, _experience_sources
    global _taken_over_sources, _personal_lore_categories
    with _lock:
        _sender_ids = frozenset({BOT_SENDER_SENTINEL, *_clean(sender_ids)})
        # 长词在前：正则交替匹配时优先命中「羽书bot」而不是「羽书」。
        _identity_terms = tuple(sorted(_clean(identity_terms), key=len, reverse=True))
        _experience_sources = frozenset(_clean(experience_sources))
        _taken_over_sources = frozenset(_clean(taken_over_sources))
        _personal_lore_categories = frozenset(_clean(personal_lore_categories))
        _revision += 1
        return _revision


def revision() -> int:
    return _revision


def bot_sender_ids() -> frozenset[str]:
    """所有已注册 Bot 的平台账号，外加库内的 ``bot`` 哨兵值。"""
    return _sender_ids


def identity_terms() -> tuple[str, ...]:
    """所有已注册 Bot 的名字、别名与自称词（不含通用词）。"""
    return _identity_terms


def experience_sources() -> frozenset[str]:
    """（旧版）各 Bot 在 ``persona.experience_source`` 里登记的第一人称经历记忆来源值。

    v5 起写死的 ``bzz_experience`` 已去掉：它现在由白真真的角色经历接管（见
    :func:`taken_over_sources`），不再作为所有 Bot 共用的默认来源。
    """
    return _experience_sources


def is_experience_source(source: object) -> bool:
    return str(source or "").strip() in _experience_sources


def taken_over_sources() -> frozenset[str]:
    """记忆表里已由某个 Bot 的角色经历接管的旧来源值（``character_experience.legacy_sources``）。

    这些行的内容已经迁进角色经历，普通记忆召回（向量、原词检索、记忆搜索工具）不再返回它们，
    避免同一条经历出现两次，也避免别的 Bot 读到不属于自己的经历。旧行不删除。
    """
    return _taken_over_sources


def is_taken_over_source(source: object) -> bool:
    return str(source or "").strip() in _taken_over_sources


def personal_lore_categories() -> frozenset[str]:
    """书设库里属于某个角色个人经历的笔记类别（``character_experience.lore_categories``），不当世界观书设用。"""
    return _personal_lore_categories


__all__ = [
    "BOT_SENDER_SENTINEL",
    "GENERIC_SELF_TERMS",
    "bot_sender_ids",
    "experience_sources",
    "identity_terms",
    "is_experience_source",
    "is_taken_over_source",
    "personal_lore_categories",
    "publish",
    "revision",
    "taken_over_sources",
]
