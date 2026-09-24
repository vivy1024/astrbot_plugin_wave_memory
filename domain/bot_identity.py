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

# v5 起第一人称经历记忆沿用的来源值，库里已有大量行使用它，不能改名。
LEGACY_EXPERIENCE_SOURCE = "bzz_experience"

# 平台回推的 Bot 自身消息在库里统一记为 sender_id="bot"。
BOT_SENDER_SENTINEL = "bot"

_lock = Lock()
_revision = 0
_sender_ids: frozenset[str] = frozenset({BOT_SENDER_SENTINEL})
_identity_terms: tuple[str, ...] = ()
_experience_sources: frozenset[str] = frozenset({LEGACY_EXPERIENCE_SOURCE})


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
) -> int:
    """替换整份快照并返回新的版本号。"""
    global _revision, _sender_ids, _identity_terms, _experience_sources
    with _lock:
        _sender_ids = frozenset({BOT_SENDER_SENTINEL, *_clean(sender_ids)})
        # 长词在前：正则交替匹配时优先命中「羽书bot」而不是「羽书」。
        _identity_terms = tuple(sorted(_clean(identity_terms), key=len, reverse=True))
        _experience_sources = frozenset({LEGACY_EXPERIENCE_SOURCE, *_clean(experience_sources)})
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
    """第一人称经历记忆的来源值集合（始终包含 v5 的旧来源值）。"""
    return _experience_sources


def is_experience_source(source: object) -> bool:
    return str(source or "").strip() in _experience_sources


__all__ = [
    "BOT_SENDER_SENTINEL",
    "GENERIC_SELF_TERMS",
    "LEGACY_EXPERIENCE_SOURCE",
    "bot_sender_ids",
    "experience_sources",
    "identity_terms",
    "is_experience_source",
    "publish",
    "revision",
]
