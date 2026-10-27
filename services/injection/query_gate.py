"""短查询免检索门控。

背景：像「你跑不过我」「如何评价？」「这个视频」这类短句本身不需要检索，但向量
与 FTS5 仍会为它们召回一批语义相近的旧发言。这些结果与当前意图无关，却占据注入
预算，并让模型更容易把别人的历史当成对话者说过的话。

判定只做词法层面的检查，不引入模型，因此可以放在注入热路径上。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 兜底阈值：仅用于极短且无任何实体线索的残渣（默认 2 字）。
# 真实注入样本显示 4-5 字消息里大量是有检索目标的（「我什么星座」「查询好感度」
# 「有没有我的」），所以不能按长度粗暴拦截，只保留最低限度的兜底。
DEFAULT_MIN_QUERY_CHARS = 2

# 无检索目标的功能性短句：应答、附和、催促或指代当前消息里的图片/视频。
# 只有整句完全等于这些词才拦截，避免误伤「你玩原神吗」这类含实体的短问句。
_GENERIC_SHORT_QUERIES = frozenset({
    "如何评价", "怎么评价", "评价一下", "评价下", "怎么看", "怎么样", "咋样",
    "是吗", "对吗", "行吗", "好吗", "可以吗", "在吗", "还在吗",
    "为什么", "为啥", "咋回事", "然后呢", "所以呢", "接着说",
    "确实", "好的", "收到", "知道了", "明白", "懂了", "算了", "随便",
    "谢谢", "多谢", "不用了", "没事", "哦", "嗯", "啊", "呃",
    "这个", "那个", "这图", "这视频", "这个图", "这个视频", "上面那个",
    "听到没有", "看看", "去搜一搜",
})

# 指向具体历史的检索意图词：出现这些词说明用户真的在要求回忆。
_RECALL_INTENT = re.compile(
    r"(记得|还记得|忘了|之前|以前|上次|那次|当初|昨天|昨晚|前天|上周|"
    r"这几天|前几天|说过|提过|聊过|讲过|答应|承诺|欠我|历史|记录)"
)

# 专有名词线索：拉丁字母串、数字串或 @ 提及，通常是可检索的实体。
_ENTITY_HINT = re.compile(r"[A-Za-z]{3,}|\d{3,}|@\S+")

# 去噪：标点、空白与表情不计入有效长度。
_NOISE = re.compile(
    r"[\s，。、；；：:！!？?~～…·—\-_,.;'\"“”‘’（）()\[\]【】《》<>|/\\+=*&^%$#@]+"
)


# 平台占位符：图片、表情、语音、视频、@、引用块与 CQ 码本身不携带可检索文字。
# 纯占位消息（例如只发了一张图）按旧规则会规范化成「图片」二字，命中全库图片行。
_PLACEHOLDER = re.compile(
    r"\[(?:图片|动画表情|表情(?:[:：]\s*\d+)?|语音|视频|文件|At[:：]\s*\d+|"
    r"引用消息|转发消息)\]"
    r"|\[CQ:[^\]]*\]"
)
# 合并记忆里每行可能带「名字: 」前缀（含 sender/名字(QQ) 两种写法）。
_SPEAKER_PREFIX = re.compile(r"(?m)^[^\n:：\[\]]{1,40}[:：]\s*")
# 去抖合并会把多条写在同一行：「伊芙: [图片] 伊芙: [图片]」。紧挨占位符的
# 「名字:」只是发言人标签，不是正文。
_SPEAKER_BEFORE_PLACEHOLDER = re.compile(r"[^\s:：\[\]]{1,40}[:：]\s*(?=\x00)")


def strip_placeholders(text: Any) -> str:
    """去掉平台占位符与发言人标签，返回剩余的真实文字（已 strip）。"""
    value = _PLACEHOLDER.sub("\x00", str(text or ""))
    value = _SPEAKER_BEFORE_PLACEHOLDER.sub("", value)
    value = value.replace("\x00", " ")
    value = _SPEAKER_PREFIX.sub("", value)
    return value.strip()


def is_placeholder_only(text: Any) -> bool:
    """内容去掉占位符、发言人前缀与标点后为空：只有图片/表情等，没有文字。"""
    raw = str(text or "")
    # 必须真的出现过占位符，避免把「注意：」这类普通短句误判为纯占位。
    if not raw.strip() or not _PLACEHOLDER.search(raw):
        return False
    return not normalize_query(strip_placeholders(raw))


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_query(message: str) -> str:
    """去掉标点与空白，返回用于长度判定的有效文本。"""
    return _NOISE.sub("", str(message or ""))


def gate_config(ctx: Any) -> Mapping[str, Any]:
    config = getattr(ctx, "config", {})
    config = config if isinstance(config, Mapping) else {}
    section = config.get("query_gate", {})
    return section if isinstance(section, Mapping) else {}


def should_skip_retrieval(ctx: Any) -> tuple[bool, str]:
    """判断当前消息是否应跳过语义/全文召回。

    返回 ``(是否跳过, 原因)``；原因会写进 trace，使门控可观测而非隐式行为。
    """
    cfg = gate_config(ctx)
    if not _as_bool(cfg.get("enabled"), True):
        return False, ""

    raw = str(getattr(ctx, "message", "") or "")
    normalized = normalize_query(raw)
    if not normalized:
        return True, "empty_query_after_normalization"

    # 只有图片/表情/@ 等占位时没有检索目标，否则会用「图片」二字召回全库图片行。
    if is_placeholder_only(raw):
        return True, "placeholder_only_query"

    # 明确要求回忆时永不跳过，即使句子很短（例如「还记得吗」）。
    if _RECALL_INTENT.search(raw):
        return False, ""

    min_chars = max(0, _as_int(cfg.get("min_query_chars"), DEFAULT_MIN_QUERY_CHARS))

    if normalized in _GENERIC_SHORT_QUERIES:
        return True, "generic_short_query"

    if len(normalized) >= min_chars:
        return False, ""

    # 短句里若含专有名词/ID/@提及，仍然值得检索。
    if _ENTITY_HINT.search(raw):
        return False, ""

    return True, "short_query_below_threshold"


__all__ = [
    "DEFAULT_MIN_QUERY_CHARS",
    "gate_config",
    "is_placeholder_only",
    "normalize_query",
    "should_skip_retrieval",
    "strip_placeholders",
]
