"""游戏知识库的纯逻辑：游戏标识、知识卡 / 词条字段规整、触发条件的解析与判定、请求里 ``game`` 字段的解析。

这里不写任何针对具体游戏的规则：游戏只是一个小写标识，阶段是自由文本，``state`` 的键由调用方
（Cortico 等）决定，触发条件只认「键 + 比较」。新增一个游戏 = 管理台建一个库、导入知识。

触发条件（``conditions``）
--------------------------
存储为对象列表（导出也用这个形式）::

    {"key": "cycle", "op": "between", "min": 30, "max": 60}
    {"key": "health", "op": "lt", "value": 8}
    {"key": "time_of_day", "op": "eq", "value": "night"}
    {"key": "dimension", "op": "in", "value": ["nether", "end"]}

管理台与导入文件也可以写成一行文字（:func:`parse_condition`）::

    cycle 30..60        cycle 30-60        30 <= cycle <= 60     区间（含两端）
    cycle 200+          cycle >= 200                              大于等于
    health < 8          health <= 8        health > 8            比较（只接受数字）
    time_of_day == night                   time_of_day != day    等值（数字或短字符串，不分大小写）
    dimension in nether, end                                      属于其中之一
    必须 dimension in nether, end                                 前面加「必须」：状态里没有这个键时算不满足
    参考 cycle 30..100                                            前面加「参考」：只影响排序，不满足也不挡

判定（:func:`evaluate_condition`）：状态里没有这个键、或数值比较遇到非数字时记为「未判定」，
不算满足也不算不满足（标了「必须」的条件缺键时算不满足）；知识卡怎么用这三种结果见 :func:`card_trigger`。
「参考」条件适合「大致在什么阶段用得上」这类提示（如缺氧教程每集的周期范围）：眼前的周期对不上时，
这张卡照样能被关键词触发，只是排在对得上的卡后面。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

STATUSES = ("draft", "active", "archived")
STATUS_LABELS = {"draft": "草稿", "active": "启用", "archived": "归档"}
SOURCE_TYPE_LABELS = {
    "manual": "手工录入",
    "import": "文件导入",
    "tutorial": "教程整理",
    "memory_migration": "记忆表迁移",
    "experience": "自己玩出来的经验",
}
# Bot 从自己的游戏经历里写下的经验卡（services/game_knowledge/lessons.py）：只属于这个 Bot，不走导入。
EXPERIENCE_SOURCE_TYPE = "experience"

GAME_ID_MAX = 40
KEY_MAX = 80
TITLE_MAX = 80
STAGE_MAX = 40
LIST_ITEM_MAX = 300
LIST_MAX = 40
KEYWORD_MAX = 24
KEYWORDS_MAX = 30
CONDITIONS_MAX = 8
CONDITION_VALUE_MAX = 40
CONDITION_IN_MAX = 20
TEXT_MAX = 2000
URL_MAX = 500
STATE_KEY_MAX = 40
STATE_VALUE_MAX = 60
STATE_MAX_KEYS = 40
REQUEST_KEYWORDS_MAX = 20

_GAME_ID_RE = re.compile(rf"^[a-z0-9][a-z0-9_.-]{{0,{GAME_ID_MAX - 1}}}$")
_ITEM_KEY_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{KEY_MAX - 1}}}$")
_SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_STATE_KEY = r"[A-Za-z_][A-Za-z0-9_.-]{0,39}"
_STATE_KEY_RE = re.compile(rf"^{_STATE_KEY}$")
_NUM = r"[-+]?\d+(?:\.\d+)?"
_LIST_SPLIT_RE = re.compile(r"[\n,，、;；|]+")
_CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏‪-‮⁦-⁩]")


class GameKnowledgeError(ValueError):
    """输入不合法或操作不允许；``code`` 是稳定错误码，API 据此返回状态码。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.reason_code = code


class GameKnowledgeConflict(GameKnowledgeError):
    """版本号对不上（管理台两处同时编辑、导入期间有人改过）。"""

    def __init__(self, code: str, *, expected: int | None = None, actual: int | None = None, message: str = "") -> None:
        super().__init__(code, message or f"{code}（期望版本 {expected}，当前 {actual}）")
        self.expected = expected
        self.actual = actual


# ─────────────────────────── 文本规整 ───────────────────────────


def fold(text: Any) -> str:
    """比较用的规整形：NFKC + 小写 + 合并空白（全角半角、大小写都视为相同）。"""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"\s+", " ", value).strip()


def clean_text(value: Any, *, limit: int = TEXT_MAX) -> str:
    """去掉控制字符与首尾空白，按上限截断。"""
    text = _CONTROL_RE.sub("", str(value if value is not None else "")).strip()
    return text[:limit].rstrip() if len(text) > limit else text


def clean_list(value: Any, *, item_limit: int = LIST_ITEM_MAX, max_items: int = LIST_MAX, split: bool = True) -> list[str]:
    """字符串列表：接受列表或（``split`` 时）按换行 / 逗号 / 顿号分隔的一段文字；去空、去重、限长。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw: Iterable[Any] = _LIST_SPLIT_RE.split(value) if split else value.splitlines()
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        raw = value
    else:
        raw = [value]
    items: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, (Mapping, list, tuple, set)):
            continue
        text = clean_text(item, limit=item_limit)
        key = fold(text)
        if not text or key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= max_items:
            break
    return items


def clean_lines(value: Any, *, item_limit: int = LIST_ITEM_MAX, max_items: int = LIST_MAX) -> list[str]:
    """要点 / 注意这类句子列表：一段文字按行拆（句子里常有逗号，不能按逗号拆）。"""
    if isinstance(value, str):
        value = [line.strip().lstrip("-·•*").strip() for line in value.splitlines()]
    return clean_list(value, item_limit=item_limit, max_items=max_items, split=False)


def validate_game_id(value: Any) -> str:
    """游戏标识：小写字母 / 数字开头，只含小写字母、数字、下划线、点、短横线，最长 40。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not text:
        raise GameKnowledgeError("game_id_required", "游戏标识不能为空（小写英文，如 minecraft）")
    if not _GAME_ID_RE.match(text):
        raise GameKnowledgeError(
            "invalid_game_id",
            f"游戏标识只能用小写字母、数字、下划线、点和短横线，且以字母或数字开头，最长 {GAME_ID_MAX}：{value!r}",
        )
    return text


def validate_item_key(value: Any) -> str:
    text = clean_text(value, limit=KEY_MAX + 1)
    if not _ITEM_KEY_RE.match(text):
        raise GameKnowledgeError(
            "invalid_item_key",
            f"条目键只能用字母、数字、下划线、点、冒号和短横线，最长 {KEY_MAX}：{value!r}",
        )
    return text


def validate_status(value: Any, *, default: str | None = None) -> str:
    text = str(value or "").strip().lower()
    if not text and default is not None:
        return default
    if text not in STATUSES:
        raise GameKnowledgeError("invalid_status", f"状态只能是 draft（草稿）、active（启用）或 archived（归档）：{value!r}")
    return text


def validate_source_type(value: Any, *, default: str = "manual") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if not _SOURCE_TYPE_RE.match(text):
        raise GameKnowledgeError("invalid_source_type", f"来源类型只能是小写英文标识：{value!r}")
    return text


def validate_url(value: Any) -> str:
    text = clean_text(value, limit=URL_MAX)
    if text and not re.match(r"^https?://", text, re.I):
        raise GameKnowledgeError("invalid_source_url", f"出处链接要以 http:// 或 https:// 开头：{text[:60]}")
    return text


# ─────────────────────────── 触发条件 ───────────────────────────

CONDITION_OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "between", "in")
_NUMERIC_OPS = frozenset({"lt", "lte", "gt", "gte", "between"})
_OP_ALIASES = {
    "==": "eq", "=": "eq", "eq": "eq", "is": "eq",
    "!=": "ne", "<>": "ne", "ne": "ne", "not": "ne",
    "<": "lt", "lt": "lt",
    "<=": "lte", "lte": "lte", "le": "lte",
    ">": "gt", "gt": "gt",
    ">=": "gte", "gte": "gte", "ge": "gte",
    "between": "between", "range": "between", "..": "between",
    "in": "in", "one_of": "in",
}
_OP_TEXT = {"eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
# NFKC 不处理的数学符号与各种破折号 / 波浪号，先换成 ASCII 写法
_TEXT_REPLACEMENTS = (("≤", "<="), ("≥", ">="), ("≠", "!="), ("～", "~"), ("〜", "~"), ("–", "-"), ("—", "-"), ("－", "-"))

_RANGE_CHAIN_RE = re.compile(rf"^(?P<lo>{_NUM})\s*<=\s*(?P<key>{_STATE_KEY})\s*<=\s*(?P<hi>{_NUM})$")
_RANGE_DOTS_RE = re.compile(
    rf"^(?P<key>{_STATE_KEY})\s+(?:(?:between|在)\s*)?(?P<lo>{_NUM})\s*(?:\.\.|~|到|至)\s*(?P<hi>{_NUM})\s*(?:之间)?$",
    re.I,
)
_RANGE_DASH_RE = re.compile(r"^(?P<key>" + _STATE_KEY + r")\s+(?P<lo>\d+(?:\.\d+)?)\s*-\s*(?P<hi>\d+(?:\.\d+)?)$")
_PLUS_RE = re.compile(rf"^(?P<key>{_STATE_KEY})\s+(?P<lo>{_NUM})\s*\+$")
_IN_RE = re.compile(rf"^(?P<key>{_STATE_KEY})\s+(?:in|属于)\s+(?P<values>.+)$", re.I)
_REQUIRED_RE = re.compile(r"^(?:必须|required|must)\s*[:：]?\s*", re.I)
_SOFT_RE = re.compile(r"^(?:参考|soft|prefer)\s*[:：]?\s*", re.I)
_COMPARE_RE = re.compile(rf"^(?P<key>{_STATE_KEY})\s*(?P<op>==|!=|<>|<=|>=|=|<|>)\s*(?P<value>.+)$")


@dataclass(frozen=True)
class Condition:
    """一个数值 / 等值条件：``key`` 对应请求 ``game.state`` 里的键（不分大小写）。"""

    key: str
    op: str
    value: Any = None
    min: float | None = None
    max: float | None = None
    # 必须判定：状态里没有这个键时算不满足（默认缺键不判定、不挡关键词）
    required: bool = False
    # 参考条件：只影响排序（满足排前、不满足排后），不挡住关键词触发；与「必须」互斥
    soft: bool = False

    @property
    def folded_key(self) -> str:
        return self.key.casefold()

    def to_dict(self) -> dict[str, Any]:
        if self.op == "between":
            payload: dict[str, Any] = {"key": self.key, "op": "between", "min": _plain_number(self.min), "max": _plain_number(self.max)}
        elif self.op == "in":
            payload = {"key": self.key, "op": "in", "value": [_plain_number(v) for v in self.value]}
        else:
            payload = {"key": self.key, "op": self.op, "value": _plain_number(self.value)}
        if self.required:
            payload["required"] = True
        if self.soft:
            payload["soft"] = True
        return payload

    def text(self) -> str:
        """一行文字形式（管理台展示与编辑）；能被 :func:`parse_condition` 原样解析回来。"""
        prefix = "必须 " if self.required else "参考 " if self.soft else ""
        if self.op == "between":
            return f"{prefix}{self.key} {_number_text(self.min)}..{_number_text(self.max)}"
        if self.op == "in":
            return f"{prefix}{self.key} in {', '.join(_value_text(v) for v in self.value)}"
        return f"{prefix}{self.key} {_OP_TEXT[self.op]} {_value_text(self.value)}"

    def describe(self) -> str:
        """给人看的说明，如「cycle 在 30–60 之间」。"""
        if self.op == "between":
            text = f"{self.key} 在 {_number_text(self.min)}–{_number_text(self.max)} 之间"
        elif self.op == "in":
            text = f"{self.key} 是 {' / '.join(_value_text(v) for v in self.value)} 之一"
        else:
            words = {"eq": "是", "ne": "不是", "lt": "小于", "lte": "不超过", "gt": "大于", "gte": "至少"}
            text = f"{self.key} {words[self.op]} {_value_text(self.value)}"
        return text + ("（必须）" if self.required else "（参考）" if self.soft else "")


def _plain_number(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _number_text(value: Any) -> str:
    value = _plain_number(value)
    return str(value)


def _value_text(value: Any) -> str:
    return _number_text(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def as_number(value: Any) -> float | None:
    """状态值 / 条件值转数字；布尔值、非有限数、非数字字符串返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
        if not re.fullmatch(_NUM, text):
            return None
        number = float(text)
    return number if math.isfinite(number) else None


def _condition_value(raw: Any, *, numeric: bool, source: str) -> Any:
    if isinstance(raw, bool):
        raw = "true" if raw else "false"
    number = as_number(raw)
    if numeric:
        if number is None:
            raise GameKnowledgeError("invalid_condition", f"条件「{source}」的比较值必须是数字")
        return _plain_number(number)
    if number is not None:
        return _plain_number(number)
    text = clean_text(str(raw).strip().strip("'\"“”‘’「」"), limit=CONDITION_VALUE_MAX + 1)
    if not text:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」缺少比较值")
    if len(text) > CONDITION_VALUE_MAX:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」的值太长（最多 {CONDITION_VALUE_MAX} 字）")
    return text


def _condition_key(raw: Any, source: str) -> str:
    key = str(raw or "").strip()
    if not _STATE_KEY_RE.match(key):
        raise GameKnowledgeError(
            "invalid_condition",
            f"条件「{source}」的键不合法：要用状态里的英文键名（字母、数字、下划线），如 cycle、health、time_of_day",
        )
    return key


def _between(key: str, lo: Any, hi: Any, source: str) -> Condition:
    low, high = as_number(lo), as_number(hi)
    if low is None or high is None:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」的区间两端必须是数字")
    if low > high:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」的区间下限大于上限")
    return Condition(key=key, op="between", min=_plain_number(low), max=_plain_number(high))


def _in_values(raw: Any, source: str) -> tuple[Any, ...]:
    if isinstance(raw, str):
        stripped = raw.strip().strip("[]()（）{}")
        parts: Iterable[Any] = [part for part in re.split(r"[,，、|/]", stripped)]
    elif isinstance(raw, Iterable) and not isinstance(raw, (bytes, Mapping)):
        parts = raw
    else:
        parts = [raw]
    values: list[Any] = []
    for part in parts:
        if isinstance(part, str) and not part.strip():
            continue
        value = _condition_value(part, numeric=False, source=source)
        if value not in values:
            values.append(value)
    if not values:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」没有给出可选值")
    if len(values) > CONDITION_IN_MAX:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」的可选值太多（最多 {CONDITION_IN_MAX} 个）")
    return tuple(values)


def parse_condition(raw: Any) -> Condition:
    """解析一个条件：对象形式或一行文字（见模块说明）；不合法时抛 ``GameKnowledgeError(invalid_condition)``。"""
    if isinstance(raw, Condition):
        return raw
    if isinstance(raw, Mapping):
        return _parse_condition_mapping(raw)
    if not isinstance(raw, str):
        raise GameKnowledgeError("invalid_condition", f"条件必须是对象或一行文字：{raw!r}")
    source = raw.strip()
    text = source
    for old, new in _TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    text = unicodedata.normalize("NFKC", text).strip()
    required = soft = False
    marker = _REQUIRED_RE.match(text)
    if marker:
        required, text = True, text[marker.end():].strip()
    marker = _SOFT_RE.match(text)
    if marker:
        soft, text = True, text[marker.end():].strip()
    if required and soft:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」不能同时是「必须」和「参考」")
    if not text:
        raise GameKnowledgeError("invalid_condition", "条件不能为空")
    return replace(_parse_condition_text(text, source), required=required, soft=soft)


def _parse_condition_text(text: str, source: str) -> Condition:
    match = _RANGE_CHAIN_RE.match(text)
    if match:
        return _between(_condition_key(match["key"], source), match["lo"], match["hi"], source)
    match = _RANGE_DOTS_RE.match(text) or _RANGE_DASH_RE.match(text)
    if match:
        return _between(_condition_key(match["key"], source), match["lo"], match["hi"], source)
    match = _PLUS_RE.match(text)
    if match:
        return Condition(key=_condition_key(match["key"], source), op="gte", value=_condition_value(match["lo"], numeric=True, source=source))
    match = _IN_RE.match(text)
    if match:
        return Condition(key=_condition_key(match["key"], source), op="in", value=_in_values(match["values"], source))
    match = _COMPARE_RE.match(text)
    if match:
        op = _OP_ALIASES[match["op"]]
        return Condition(
            key=_condition_key(match["key"], source),
            op=op,
            value=_condition_value(match["value"], numeric=op in _NUMERIC_OPS, source=source),
        )
    raise GameKnowledgeError(
        "invalid_condition",
        f"看不懂条件「{source}」：可以写 cycle 30..60、health < 8、time_of_day == night、dimension in nether, end",
    )


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parse_condition_mapping(raw: Mapping[str, Any]) -> Condition:
    required, soft = _flag(raw.get("required")), _flag(raw.get("soft"))
    if required and soft:
        raise GameKnowledgeError("invalid_condition", "条件不能同时是「必须」（required）和「参考」（soft）")
    return replace(_parse_condition_fields(raw), required=required, soft=soft)


def _parse_condition_fields(raw: Mapping[str, Any]) -> Condition:
    source = ", ".join(f"{k}={v}" for k, v in raw.items())
    key = _condition_key(raw.get("key"), source)
    op_raw = str(raw.get("op") or "").strip().lower()
    if not op_raw:
        has_min, has_max = raw.get("min") is not None, raw.get("max") is not None
        if has_min and has_max:
            op_raw = "between"
        elif has_min:
            return Condition(key=key, op="gte", value=_condition_value(raw["min"], numeric=True, source=source))
        elif has_max:
            return Condition(key=key, op="lte", value=_condition_value(raw["max"], numeric=True, source=source))
        elif isinstance(raw.get("value"), (list, tuple)):
            op_raw = "in"
        else:
            op_raw = "eq"
    op = _OP_ALIASES.get(op_raw)
    if op is None:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」的比较方式不认识：{op_raw}（可用 {', '.join(CONDITION_OPS)}）")
    if op == "between":
        low, high = raw.get("min"), raw.get("max")
        if (low is None or high is None) and isinstance(raw.get("value"), (list, tuple)) and len(raw["value"]) == 2:
            low, high = raw["value"]
        return _between(key, low, high, source)
    if op == "in":
        return Condition(key=key, op="in", value=_in_values(raw.get("value", raw.get("values")), source))
    if raw.get("value") is None:
        raise GameKnowledgeError("invalid_condition", f"条件「{source}」缺少比较值")
    return Condition(key=key, op=op, value=_condition_value(raw["value"], numeric=op in _NUMERIC_OPS, source=source))


def parse_conditions(raw: Any) -> list[Condition]:
    """条件列表：对象 / 文字混合的列表，或一段按行（或分号）分隔的文字。空值返回空列表。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        items: list[Any] = [part.strip() for part in re.split(r"[\n;；]+", raw) if part.strip()]
    elif isinstance(raw, Mapping):
        items = [raw]
    elif isinstance(raw, Iterable) and not isinstance(raw, bytes):
        items = [item for item in raw if not (isinstance(item, str) and not item.strip())]
    else:
        raise GameKnowledgeError("invalid_condition", "条件必须是列表或文字")
    conditions: list[Condition] = []
    seen: set[str] = set()
    for item in items:
        condition = parse_condition(item)
        marker = condition.text().casefold()
        if marker in seen:
            continue
        seen.add(marker)
        conditions.append(condition)
    if len(conditions) > CONDITIONS_MAX:
        raise GameKnowledgeError("invalid_condition", f"一张卡最多 {CONDITIONS_MAX} 个条件")
    return conditions


PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


def _state_value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    number = as_number(value)
    return _number_text(_plain_number(number)) if number is not None else str(value)


def _equal(state_value: Any, expected: Any) -> bool:
    left, right = as_number(state_value), as_number(expected)
    if left is not None and right is not None:
        return left == right
    return fold(_state_value_text(state_value)) == fold(_value_text(expected))


def evaluate_condition(condition: Condition, state: Mapping[str, Any]) -> tuple[str, str]:
    """对 ``state``（键已 casefold）判定一个条件，返回 (pass|fail|unknown, 说明)。"""
    if condition.folded_key not in state or state[condition.folded_key] is None:
        if condition.required:
            return FAIL, f"状态里没有 {condition.key}（标了必须，不触发）"
        return UNKNOWN, f"状态里没有 {condition.key}，未判定"
    raw = state[condition.folded_key]
    shown = f"{condition.key}={_state_value_text(raw)}"
    if condition.op in _NUMERIC_OPS:
        number = as_number(raw)
        if number is None:
            return UNKNOWN, f"{shown} 不是数字，未判定"
        if condition.op == "between":
            ok = float(condition.min) <= number <= float(condition.max)  # type: ignore[arg-type]
        else:
            target = float(condition.value)
            ok = {
                "lt": number < target,
                "lte": number <= target,
                "gt": number > target,
                "gte": number >= target,
            }[condition.op]
    elif condition.op == "in":
        ok = any(_equal(raw, option) for option in condition.value)
    elif condition.op == "eq":
        ok = _equal(raw, condition.value)
    else:  # ne
        ok = not _equal(raw, condition.value)
    described = replace(condition, required=False, soft=False).describe()
    return (PASS, f"{shown}（{described}）") if ok else (FAIL, f"{shown}，不满足「{described}」")


# ─────────────────────────── 请求里的 game ───────────────────────────


@dataclass(frozen=True)
class GameContext:
    """``/context/prepare`` 请求里的 ``game``：游戏标识、扁平状态与场景关键词。"""

    id: str
    state: Mapping[str, Any] = field(default_factory=dict)
    keywords: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # 整个 game 字段不可用的原因（缺 id、id 不合法）；有值时通道只报告、不注入。
    error: str = ""

    def folded_state(self) -> dict[str, Any]:
        return {str(key).casefold(): value for key, value in self.state.items()}

    def summary(self) -> dict[str, Any]:
        """写进 trace 的精简形式（不含任何会话数据）。"""
        return {"id": self.id, "state": dict(self.state), "keywords": list(self.keywords)}


def _state_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return value if isinstance(value, int) else _plain_number(number)
    if isinstance(value, str):
        text = clean_text(value, limit=STATE_VALUE_MAX)
        return text or None
    return None


def parse_game_context(raw: Any) -> GameContext | None:
    """规整请求里的 ``game``；不带 ``game`` 时返回 None（行为与没有游戏知识库时完全一致）。

    宽松接受：``game`` 可以直接是游戏标识字符串；``state`` 里嵌套对象 / 列表、超长值会被忽略并记警告；
    ``keywords`` 可以是列表或逗号分隔的文字。游戏标识缺失或不合法时返回带 ``error`` 的上下文，
    通道据此在观测台报告原因，不影响其他通道。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, Mapping):
        return GameContext(id="", error="invalid_game_context：game 必须是对象（id、state、keywords）")
    warnings: list[str] = []
    try:
        game_id = validate_game_id(raw.get("id"))
        error = ""
    except GameKnowledgeError as exc:
        game_id, error = "", f"{exc.code}：{exc}"

    state: dict[str, Any] = {}
    raw_state = raw.get("state")
    if raw_state is not None and not isinstance(raw_state, Mapping):
        warnings.append("game.state 不是对象，已忽略")
        raw_state = None
    for key, value in (raw_state or {}).items():
        name = str(key or "").strip()
        if not _STATE_KEY_RE.match(name):
            warnings.append(f"game.state 的键 {name[:STATE_KEY_MAX]!r} 不合法（要用英文键名），已忽略")
            continue
        normalized = _state_value(value)
        if normalized is None:
            if value is not None:
                warnings.append(f"game.state.{name} 不是数字或短字符串，已忽略")
            continue
        if len(state) >= STATE_MAX_KEYS:
            warnings.append(f"game.state 超过 {STATE_MAX_KEYS} 个键，其余已忽略")
            break
        state[name] = normalized

    raw_keywords = raw.get("keywords")
    if raw_keywords is not None and not isinstance(raw_keywords, (str, list, tuple)):
        warnings.append("game.keywords 不是列表，已忽略")
        raw_keywords = None
    keywords = clean_list(raw_keywords, item_limit=KEYWORD_MAX, max_items=REQUEST_KEYWORDS_MAX)
    return GameContext(id=game_id, state=state, keywords=tuple(keywords), warnings=tuple(warnings), error=error)


# ─────────────────────────── 关键词命中 ───────────────────────────

_ASCII_WORD_RE = re.compile(r"^[a-z0-9][a-z0-9 _.+-]*$")
SOURCE_LABELS = {"keywords": "keywords", "state": "state", "message": "消息"}


@dataclass(frozen=True)
class KeywordHit:
    keyword: str
    source: str  # keywords | state | message
    detail: str

    def describe(self) -> str:
        return f"「{self.keyword}」（{self.detail}）"


class KeywordHaystack:
    """命中关键词的来源：当前消息、请求 ``game.keywords``、``game.state`` 的字符串值（按这个顺序找）。

    - 消息：词出现在消息里就算（英文数字词至少两个字母、按词边界，不会把 bed 命中到 embedded）；
    - keywords / state：相等即命中；两边都至少两个字时，互相包含也算（「夜晚」命中「夜晚降临」）。
    """

    def __init__(self, *, message: str = "", keywords: Sequence[str] = (), state: Mapping[str, Any] | None = None) -> None:
        self.message = fold(message)
        self.keywords = [(fold(word), str(word)) for word in keywords if fold(word)]
        self.state_values = [
            (fold(value), str(key), str(value))
            for key, value in (state or {}).items()
            if isinstance(value, str) and fold(value)
        ]

    def empty(self) -> bool:
        return not (self.message or self.keywords or self.state_values)

    @staticmethod
    def _short_match(needle: str, candidate: str) -> bool:
        if needle == candidate:
            return True
        if len(needle) >= 2 and len(candidate) >= 2:
            return needle in candidate or candidate in needle
        return False

    def _in_message(self, needle: str) -> bool:
        if not needle or not self.message:
            return False
        if _ASCII_WORD_RE.match(needle):
            if len(needle) < 2:
                return False
            return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", self.message) is not None
        # 知识卡的触发词是人挑的：单个汉字（如「床」）也按原样匹配；全文检索那边的「单字不参与」不适用于这里
        return needle in self.message

    def find(self, keyword: str) -> KeywordHit | None:
        needle = fold(keyword)
        if not needle:
            return None
        if self._in_message(needle):
            return KeywordHit(keyword, "message", "消息")
        for folded, original in self.keywords:
            if self._short_match(needle, folded):
                return KeywordHit(keyword, "keywords", "keywords" if folded == needle else f"keywords「{original}」")
        for folded, key, original in self.state_values:
            if self._short_match(needle, folded):
                return KeywordHit(keyword, "state", f"state {key}={original}")
        return None


# ─────────────────────────── 知识卡触发 ───────────────────────────


@dataclass(frozen=True)
class TriggerResult:
    triggered: bool
    keyword_hits: tuple[KeywordHit, ...] = ()
    conditions: tuple[tuple[Condition, str, str], ...] = ()
    reason: str = ""

    @property
    def passed_conditions(self) -> int:
        return sum(1 for _, outcome, _ in self.conditions if outcome == PASS)

    @property
    def soft_failed(self) -> int:
        """没满足的「参考」条件数：不挡触发，只让这张卡排在后面。"""
        return sum(1 for condition, outcome, _ in self.conditions if outcome == FAIL and condition.soft)


TRIGGER_RULE_TEXT = (
    "写了关键词：关键词命中才触发，数值条件用来限定（状态里没有该键时不判定；条件前加「必须」则缺键时不触发；"
    "条件前加「参考」则只影响排序，不满足也照样触发、排在后面）；"
    "没写关键词：数值条件全部满足才触发；两样都没写：只能通过工具查到，不会自动注入。"
)


def card_trigger(keywords: Sequence[str], conditions: Sequence[Condition], haystack: KeywordHaystack, state: Mapping[str, Any]) -> TriggerResult:
    """一张知识卡是否被这次请求触发（规则见 :data:`TRIGGER_RULE_TEXT`）。``state`` 的键已 casefold。"""
    results = tuple((condition, *evaluate_condition(condition, state)) for condition in conditions)
    # 没写关键词的卡只靠条件触发：「参考」条件在这里也得满足（否则什么都挡不住，每轮都会触发）
    failed = [detail for condition, outcome, detail in results if outcome == FAIL and (not condition.soft or not keywords)]
    if failed:
        return TriggerResult(False, (), results, "条件不满足：" + "；".join(failed))
    if keywords:
        hits: list[KeywordHit] = []
        seen: set[str] = set()
        for keyword in keywords:
            hit = haystack.find(keyword)
            if hit is not None and fold(keyword) not in seen:
                seen.add(fold(keyword))
                hits.append(hit)
        if not hits:
            return TriggerResult(False, (), results, "关键词没有命中")
        parts = ["关键词 " + "、".join(hit.describe() for hit in hits[:4])]
        if results:
            parts.append("条件 " + "；".join(
                detail + ("（参考条件，不挡，排在后面）" if condition.soft and outcome == FAIL else "")
                for condition, outcome, detail in results
            ))
        return TriggerResult(True, tuple(hits), results, "；".join(parts))
    if results and all(outcome == PASS for _, outcome, _ in results):
        return TriggerResult(True, (), results, "条件全部满足：" + "；".join(detail for _, _, detail in results))
    if not results:
        return TriggerResult(False, (), results, "没有触发关键词或条件，只能通过工具查到")
    return TriggerResult(False, (), results, "条件未全部判定：" + "；".join(detail for _, _, detail in results))


# ─────────────────────────── 字段规整（存储 / 导入 / API 共用） ───────────────────────────

META_MAX_CHARS = 4000


def _meta(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if not isinstance(value, Mapping):
        raise GameKnowledgeError("invalid_meta", "meta 必须是对象")
    import json

    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        raise GameKnowledgeError("invalid_meta", f"meta 不能序列化：{exc}") from exc
    if len(encoded) > META_MAX_CHARS:
        raise GameKnowledgeError("invalid_meta", f"meta 太大（最多 {META_MAX_CHARS} 字符）")
    return json.loads(encoded)


def auto_item_key(prefix: str, text: str) -> str:
    """没有给键时按正文生成稳定键：同一个标题 / 术语重复导入会落到同一个键上。"""
    import hashlib

    digest = hashlib.sha1(fold(text).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def normalize_game_fields(payload: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """游戏库字段：``id``（新建时）、``name``、``aliases``、``description``、``enabled``、``lessons_enabled``。

    ``lessons_enabled``：允许 Bot 从自己在这个游戏里的经历写经验卡（总开关是 Learning_Settings.game_lesson_enabled）。
    """
    if not isinstance(payload, Mapping):
        raise GameKnowledgeError("invalid_payload", "游戏信息必须是对象")
    fields: dict[str, Any] = {}
    if not partial or "id" in payload or "game_id" in payload:
        fields["game_id"] = validate_game_id(payload.get("id", payload.get("game_id")))
    if not partial or "name" in payload:
        name = clean_text(payload.get("name"), limit=TITLE_MAX)
        if not name and not partial:
            name = fields.get("game_id", "")
        if not name:
            raise GameKnowledgeError("game_name_required", "显示名不能为空")
        fields["name"] = name
    if not partial or "aliases" in payload:
        fields["aliases"] = clean_list(payload.get("aliases"), item_limit=KEYWORD_MAX * 2, max_items=20)
    if not partial or "description" in payload:
        fields["description"] = clean_text(payload.get("description"), limit=TEXT_MAX)
    if not partial or "enabled" in payload:
        fields["enabled"] = _flag(payload.get("enabled", True))
    if not partial or "lessons_enabled" in payload:
        fields["lessons_enabled"] = _flag(payload.get("lessons_enabled", True))
    return fields


def _trigger_parts(payload: Mapping[str, Any]) -> tuple[Any, Any, bool, bool]:
    """``trigger: {keywords, conditions}`` 或平铺的 ``keywords`` / ``conditions``。"""
    trigger = payload.get("trigger")
    trigger = trigger if isinstance(trigger, Mapping) else {}
    has_keywords = "keywords" in payload or "keywords" in trigger
    has_conditions = "conditions" in payload or "conditions" in trigger
    keywords = trigger.get("keywords", payload.get("keywords"))
    conditions = trigger.get("conditions", payload.get("conditions"))
    return keywords, conditions, has_keywords, has_conditions


def normalize_card_fields(payload: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """知识卡字段。``conditions`` 解析成对象列表；``partial`` 时只规整出现的字段（编辑用）。"""
    if not isinstance(payload, Mapping):
        raise GameKnowledgeError("invalid_payload", "知识卡必须是对象")
    fields: dict[str, Any] = {}

    def present(*names: str) -> bool:
        return not partial or any(name in payload for name in names)

    if present("title"):
        title = clean_text(payload.get("title"), limit=TITLE_MAX)
        if not title:
            raise GameKnowledgeError("card_title_required", "知识卡标题不能为空")
        fields["title"] = title
    if "key" in payload or "card_key" in payload:
        raw_key = payload.get("key", payload.get("card_key"))
        if raw_key not in (None, ""):
            fields["card_key"] = validate_item_key(raw_key)
    if present("stage"):
        fields["stage"] = clean_text(payload.get("stage"), limit=STAGE_MAX)
    if present("points", "tips"):
        fields["points"] = clean_lines(payload.get("points", payload.get("tips")))
    if present("pitfalls", "cautions"):
        fields["pitfalls"] = clean_lines(payload.get("pitfalls", payload.get("cautions")))
    keywords, conditions, has_keywords, has_conditions = _trigger_parts(payload)
    if not partial or has_keywords:
        fields["keywords"] = clean_list(keywords, item_limit=KEYWORD_MAX, max_items=KEYWORDS_MAX)
    if not partial or has_conditions:
        fields["conditions"] = [condition.to_dict() for condition in parse_conditions(conditions)]
    if present("source"):
        fields["source"] = clean_text(payload.get("source"), limit=TEXT_MAX // 4)
    if present("source_url"):
        fields["source_url"] = validate_url(payload.get("source_url"))
    if present("source_type"):
        fields["source_type"] = validate_source_type(payload.get("source_type"))
    if "status" in payload and payload.get("status") not in (None, ""):
        fields["status"] = validate_status(payload.get("status"))
    if present("note"):
        fields["note"] = clean_text(payload.get("note"), limit=TEXT_MAX // 4)
    if present("meta"):
        fields["meta"] = _meta(payload.get("meta"))
    if not partial and not fields.get("points") and not fields.get("pitfalls"):
        raise GameKnowledgeError("card_content_required", "知识卡至少要有一条要点或注意")
    return fields


def normalize_term_fields(payload: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """词条字段：术语、别名、解释、相关条目、出处、状态。"""
    if not isinstance(payload, Mapping):
        raise GameKnowledgeError("invalid_payload", "词条必须是对象")
    fields: dict[str, Any] = {}

    def present(*names: str) -> bool:
        return not partial or any(name in payload for name in names)

    if present("term"):
        term = clean_text(payload.get("term"), limit=KEYWORD_MAX * 2)
        if not term:
            raise GameKnowledgeError("term_required", "词条的术语不能为空")
        fields["term"] = term
    if "key" in payload or "term_key" in payload:
        raw_key = payload.get("key", payload.get("term_key"))
        if raw_key not in (None, ""):
            fields["term_key"] = validate_item_key(raw_key)
    if present("aliases"):
        fields["aliases"] = clean_list(payload.get("aliases"), item_limit=KEYWORD_MAX * 2, max_items=20)
    if present("explanation"):
        explanation = clean_text(payload.get("explanation"), limit=TEXT_MAX // 2)
        if not explanation and not partial:
            raise GameKnowledgeError("term_explanation_required", "词条的解释不能为空")
        fields["explanation"] = explanation
    if present("related"):
        fields["related"] = clean_list(payload.get("related"), item_limit=TITLE_MAX, max_items=20)
    if present("source"):
        fields["source"] = clean_text(payload.get("source"), limit=TEXT_MAX // 4)
    if present("source_url"):
        fields["source_url"] = validate_url(payload.get("source_url"))
    if present("source_type"):
        fields["source_type"] = validate_source_type(payload.get("source_type"))
    if "status" in payload and payload.get("status") not in (None, ""):
        fields["status"] = validate_status(payload.get("status"))
    if present("note"):
        fields["note"] = clean_text(payload.get("note"), limit=TEXT_MAX // 4)
    if present("meta"):
        fields["meta"] = _meta(payload.get("meta"))
    return fields


__all__ = [
    "CONDITION_OPS",
    "Condition",
    "EXPERIENCE_SOURCE_TYPE",
    "FAIL",
    "GameContext",
    "GameKnowledgeConflict",
    "GameKnowledgeError",
    "KeywordHaystack",
    "KeywordHit",
    "PASS",
    "SOURCE_TYPE_LABELS",
    "STATUSES",
    "STATUS_LABELS",
    "TRIGGER_RULE_TEXT",
    "TriggerResult",
    "UNKNOWN",
    "as_number",
    "auto_item_key",
    "card_trigger",
    "clean_lines",
    "clean_list",
    "clean_text",
    "evaluate_condition",
    "fold",
    "normalize_card_fields",
    "normalize_game_fields",
    "normalize_term_fields",
    "parse_condition",
    "parse_conditions",
    "parse_game_context",
    "validate_game_id",
    "validate_item_key",
    "validate_source_type",
    "validate_status",
    "validate_url",
]
