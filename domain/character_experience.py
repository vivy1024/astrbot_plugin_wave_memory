"""角色经历：一个 Bot 进群之前「过去的人生」，按她自己的时间线存成一条条第一人称记忆。

羽书这类 Bot 的经历从群聊里长出来（记忆表）；书中角色 / 原创角色（OC）还有一段进群前的人生，
需要先设定成她自己的第一人称经历。本模块只定义纯数据：Bot Profile 里的设置段、状态与可信级别
等枚举、正文指纹与阶段排序，不依赖任何其他层。

- 可信级别：canon 原著 / inferred 推演 / invented 编造；
- 状态：draft 草稿（不召回）/ active 启用 / archived 归档（不召回）；
- 当前时间点：Bot 当前处在时间线的哪个阶段，只召回这个阶段及之前的经历（避免剧透、自相矛盾）；
  没有归入任何阶段的经历不受时间点限制。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

STATUSES = ("draft", "active", "archived")
STATUS_LABELS = {"draft": "草稿", "active": "启用", "archived": "归档"}

CANON_LEVELS = ("canon", "inferred", "invented")
CANON_LABELS = {"canon": "原著", "inferred": "推演", "invented": "编造"}

# 创建方式
SOURCE_KINDS = ("novel_extract", "setting_compose", "manual", "jsonl_import", "legacy_migration")
SOURCE_KIND_LABELS = {
    "novel_extract": "从原著提取",
    "setting_compose": "按设定编写",
    "manual": "手写",
    "jsonl_import": "导入 JSONL",
    "legacy_migration": "旧数据迁移",
}

STAGE_STATUSES = ("draft", "confirmed")

# 提示词里给模型的情绪标签（与 v5 白真真经历的标签一致）；导入的其他情绪词原样保留。
EMOTIONS = ("日常", "温馨", "战斗", "紧张", "伤感", "搞笑", "愤怒", "感动")

# 提示词里指代角色用的代词；ta 不假定性别（默认）。
PRONOUNS = ("她", "他", "ta")
DEFAULT_PRONOUN = "ta"

_STAGE_KEY_RE = re.compile(r"^[^\s/\\:?#]{1,64}$")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set, frozenset)) else re.split(r"[,，\n]", str(value))
    out: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "否", "关闭"}:
            return False
    return bool(value)


def normalize_pronoun(value: Any) -> str | None:
    """「她」「他」「ta」（ta 不分大小写）→ 规范值；其他值返回 None。"""
    text = _text(value)
    if text.lower() == "ta":
        return "ta"
    return text if text in PRONOUNS else None


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, parsed))


def _float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return min(high, max(low, parsed))


@dataclass
class CharacterExperienceSettings:
    """Bot Profile 的 ``character_experience`` 段；没有这一段（旧 Profile）= 关闭。"""

    # 是否召回这个 Bot 的角色经历。只有打开的 Bot 才会想起自己的角色经历，其他 Bot 完全看不到。
    enabled: bool = False
    # 当前时间点：时间线阶段的 key。只召回这个阶段及之前的经历；空 = 不限（能想起所有阶段）。
    current_stage: str = ""
    # 提取、起草、展开的提示词里指代这个角色用的代词：她 / 他 / ta（默认 ta，不假定性别）。
    # 旧配置没有这个字段、或值不认识时按默认值。
    pronoun: str = DEFAULT_PRONOUN
    # 每次最多想起几条。
    top_k: int = 3
    # 注入正文上限（字）。
    max_chars: int = 600
    # 向量相似度下限（关键词命中的不受此限制）。
    min_score: float = 0.35
    # 记忆表里已由角色经历接管的旧来源值（如 v5 的 bzz_experience）：这些行不再走普通记忆召回，
    # 避免同一条经历出现两次。旧行本身不删除。
    legacy_sources: list[str] = field(default_factory=list)
    # 书设库里属于她个人经历的笔记类别（如「白真真经历」）：书设通道不把它们当世界观注入。
    lore_categories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "CharacterExperienceSettings":
        data = data if isinstance(data, Mapping) else {}
        defaults = cls()
        return cls(
            enabled=_bool(data.get("enabled"), defaults.enabled),
            current_stage=_text(data.get("current_stage")),
            pronoun=normalize_pronoun(data.get("pronoun")) or defaults.pronoun,
            top_k=_int(data.get("top_k"), defaults.top_k, 1, 10),
            max_chars=_int(data.get("max_chars"), defaults.max_chars, 100, 4000),
            min_score=_float(data.get("min_score"), defaults.min_score, 0.0, 1.0),
            legacy_sources=_text_list(data.get("legacy_sources")),
            lore_categories=_text_list(data.get("lore_categories")),
        )


def normalize_content(text: Any) -> str:
    """正文比较用的规范形：NFKC、去空白。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", "", normalized)


def content_hash(text: Any) -> str:
    """同一个 Bot 内判断「同一条经历」的指纹（规范化后正文的 sha1）。"""
    return hashlib.sha1(normalize_content(text).encode("utf-8")).hexdigest()


def natural_key(text: Any) -> tuple[Any, ...]:
    """自然排序：「arc2」排在「arc10」前面。"""
    parts = re.split(r"(\d+)", str(text or ""))
    return tuple(int(part) if part.isdigit() else part for part in parts)


def validate_stage_key(value: Any) -> str:
    key = _text(value)
    if not key or not _STAGE_KEY_RE.match(key):
        raise ValueError("阶段 key 不能为空，最长 64 字，不能含空白和 / \\ : ? #")
    return key


def stage_title_from_key(key: str) -> str:
    """「arc01_炼气高中期」→「炼气高中期」；没有编号前缀时原样返回。"""
    match = re.match(r"^[A-Za-z]*\d+[_\-\s]+(.+)$", key or "")
    return match.group(1).strip() if match else (key or "")


def eligible_stage_keys(
    stages: Sequence[Mapping[str, Any]],
    current_stage: str,
) -> tuple[set[str] | None, str]:
    """当前时间点及之前的阶段。

    返回 ``(阶段 key 集合或 None, 警告)``：None 表示不限（没设当前时间点）；
    设了但时间线里找不到这个阶段时失败关闭——只剩不分阶段的经历可以召回。
    """
    current = _text(current_stage)
    if not current:
        return None, ""
    ordinal = next((stage.get("ordinal") for stage in stages if stage.get("stage_key") == current), None)
    if ordinal is None:
        return set(), "current_stage_missing"
    return {
        str(stage.get("stage_key"))
        for stage in stages
        if stage.get("ordinal") is not None and stage.get("ordinal") <= ordinal
    }, ""


def union_values(groups: Iterable[Iterable[str]]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for value in group or ():
            text = _text(value)
            if text and text not in out:
                out.append(text)
    return out


__all__ = [
    "CANON_LABELS",
    "CANON_LEVELS",
    "CharacterExperienceSettings",
    "DEFAULT_PRONOUN",
    "EMOTIONS",
    "PRONOUNS",
    "SOURCE_KINDS",
    "SOURCE_KIND_LABELS",
    "STAGE_STATUSES",
    "STATUSES",
    "STATUS_LABELS",
    "content_hash",
    "eligible_stage_keys",
    "natural_key",
    "normalize_content",
    "normalize_pronoun",
    "stage_title_from_key",
    "union_values",
    "validate_stage_key",
]
