"""关系注入的辅助：从 Bot 的经历时间线里挑出与某个人相关的「共同交往锚点」。

只做读侧筛选，不写库、不看好感分数。Bot 自己的群日记/经历（没有主体、也没有结构化参与者）
与这个人无关，不再原样塞进关系注入；昵称碰巧出现在日记里也不算与此人相关。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_ANCHOR_WORDS = {
    "bot_helped_user": "你帮过对方",
    "user_helped_bot": "对方帮过你",
    "bot_promised_user": "你答应过对方",
    "boundary_hit": "对方越过界",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _bigrams(text: str) -> set[str]:
    chars = re.sub(r"\s+", "", str(text or ""))
    return {chars[i:i + 2] for i in range(max(0, len(chars) - 1))}


def _similar(left: str, right: str, threshold: float = 0.5) -> bool:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return left == right
    return len(a & b) / max(1, min(len(a), len(b))) >= threshold


def shared_experience_lines(
    soul_timeline_items: Sequence[Any] | None,
    *,
    subject_principal_id: str,
    display_names: Sequence[str] = (),
    limit: int = 2,
    snippet_chars: int = 48,
) -> list[str]:
    """只认主体或结构化 participant_principal_ids；昵称不作为人物归属证据。

    参与者可直接在条目上，或在 evidence 的结构化记录中。保留 display_names 参数兼容旧调用。
    """
    if not subject_principal_id:
        return []
    out: list[str] = []
    for item in soul_timeline_items or ():
        if not isinstance(item, Mapping) or len(out) >= limit:
            continue
        summary = " ".join(str(item.get("event_summary") or "").split())
        for code, words in _ANCHOR_WORDS.items():
            summary = summary.replace(f"（{code}）", f"（{words}）")
        if not summary:
            continue
        subject = str(item.get("subject_principal_id") or "")
        evidence = item.get("evidence")
        records = [item]
        if isinstance(evidence, Mapping):
            records.append(evidence)
        elif isinstance(evidence, (list, tuple)):
            records.extend(record for record in evidence if isinstance(record, Mapping))
        participant = any(
            isinstance(record.get("participant_principal_ids"), (list, tuple))
            and subject_principal_id in record["participant_principal_ids"]
            for record in records
        )
        if subject != subject_principal_id and not participant:
            continue
        text = _clip(summary, snippet_chars)
        if not any(_similar(text, other) for other in out):
            out.append(text)
    return out


__all__ = ["shared_experience_lines"]
