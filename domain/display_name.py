"""发送者昵称 / 显示名清洗。

QQ 群名片偶尔带着 protobuf 残片进来，例如
``'\\n\\x11\\x12\\x0f猫猫副队长\\n\\t\\n\\x07$ÿĀ\\x11\\x10\\x10\\x00'``：真实昵称被
控制字符夹在中间，后面跟一段被当成文本解码的二进制。这里给所有写入口一个统一的清洗：

1. U+FFFD（替换字符）与孤立代理项直接删除，不作分隔。
2. 不含「硬控制字符」（C0 中除 ``\\t\\n\\r`` 以外的字符、DEL、C1）时视为普通文本：
   ``\\t\\n\\r`` 当空白处理，连续空白折叠为一个空格，首尾去空白。其余字符一律不动——
   emoji、日文、全角符号、颜文字、``[LM导入]`` 这类前缀都原样保留。
3. 含硬控制字符时视为「二进制残片」形态：按全部控制字符（含 ``\\t\\n\\r``）切段；
   紧贴控制字符一侧的「ASCII 符号 + Latin-1/扩展拉丁字母」混合尾巴（``$ÿĀ``、``[$ÿĀ``
   这类）当作解码残渣剥掉；再按「可读字符数」（ASCII 字母数字、CJK/假名/谚文等非拉丁
   扩展区的字母数字、U+2000 以上的符号/emoji）挑分最高的一段，同分取靠前的一段。
   没有任何可读段时返回空串，由调用方决定回退值。

普通昵称（不含控制字符）只会被折叠空白与去首尾空白，不会被截断。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# 删除而不分隔：替换字符与孤立代理项（正常 UTF-8 解码不会产生，JSON/外部输入可能带进来）。
_DROP_RE = re.compile("[�\ud800-\udfff]")
# 硬控制字符：C0（除 \t \n \r）、DEL、C1。
_HARD_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# 二进制形态下的分隔符：全部 C0/DEL/C1。
_ANY_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]+")
_SOFT_WS_RE = re.compile("[\t\n\r]")
_MULTI_WS_RE = re.compile(r"\s{2,}")

_LATIN_EXT_MIN = 0x80
_LATIN_EXT_MAX = 0x24F


def _fold_whitespace(text: str) -> str:
    text = _SOFT_WS_RE.sub(" ", text)
    return _MULTI_WS_RE.sub(" ", text).strip()


def _is_latin_ext(ch: str) -> bool:
    return _LATIN_EXT_MIN <= ord(ch) <= _LATIN_EXT_MAX


def _is_ascii_symbol(ch: str) -> bool:
    return ord(ch) < 0x80 and not ch.isalnum() and not ch.isspace()


def _is_residue_char(ch: str) -> bool:
    return _is_ascii_symbol(ch) or _is_latin_ext(ch)


def _is_readable(ch: str) -> bool:
    code = ord(ch)
    if ch.isalnum():
        return code < 0x80 or code > _LATIN_EXT_MAX
    return code >= 0x2000 and unicodedata.category(ch).startswith("S")


def _looks_like_residue(run: str) -> bool:
    """贴着控制字符的尾巴是否像解码残渣：符号与扩展拉丁字母混杂，或连续两个以上扩展拉丁字母。

    单个 ``é``（José\\x00）或纯 ASCII 符号（abc~\\x00）不算残渣。
    """
    latin = sum(1 for ch in run if _is_latin_ext(ch))
    symbols = sum(1 for ch in run if _is_ascii_symbol(ch))
    return latin >= 2 or (latin >= 1 and symbols >= 1)


def _trim_edge(segment: str, *, left: bool, right: bool) -> str:
    if right:
        end = len(segment)
        while end > 0 and _is_residue_char(segment[end - 1]):
            end -= 1
        if end < len(segment) and _looks_like_residue(segment[end:]):
            segment = segment[:end]
    if left:
        start = 0
        while start < len(segment) and _is_residue_char(segment[start]):
            start += 1
        if start and _looks_like_residue(segment[:start]):
            segment = segment[start:]
    return segment


def sanitize_display_name(value: Any) -> str:
    """清洗昵称/显示名；``None`` 或清洗后无可读内容时返回空串。"""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="replace")
    else:
        text = str(value)
    if not text:
        return ""
    text = _DROP_RE.sub("", text)
    if not _HARD_CONTROL_RE.search(text):
        return _fold_whitespace(text)

    parts = _ANY_CONTROL_RE.split(text)
    last = len(parts) - 1
    best = ""
    best_score = 0
    for index, part in enumerate(parts):
        if not part:
            continue
        segment = _trim_edge(part, left=index > 0, right=index < last)
        segment = _fold_whitespace(segment)
        score = sum(1 for ch in segment if _is_readable(ch))
        if score > best_score:
            best, best_score = segment, score
    return best


def sanitize_alias_list(values: Any) -> list[str]:
    """逐个清洗别名，丢弃空值并按首次出现去重。"""
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        name = sanitize_display_name(item)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


__all__ = ["sanitize_alias_list", "sanitize_display_name"]
