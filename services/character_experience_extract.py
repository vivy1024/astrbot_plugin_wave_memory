"""角色经历：原文切场景、提示词与模型输出解析（纯函数，不碰数据库与模型客户端）。

从原著提取沿用 v5 一次性脚本（graphrag_novels/scripts/extract_bzz_memories.py）的思路：
按章节切分 → 找出角色出场的段落、前后各带几段上下文拼成场景 → 模型把每个场景写成 1–3 条第一人称记忆。
区别是角色名、别名、语气与设定全部来自参数，不写死任何角色；场景拼接修掉了旧脚本重复收录段落的问题。

按设定编写（原创角色）分两步：先按设定起草人生时间线（阶段 + 关键事件，并注明依据的设定段落），
管理员在工作台改好、确认后，再把每个关键事件展开成第一人称记忆（标为「编造」）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from ..domain.character_experience import EMOTIONS, normalize_content
    from .book_lore_workbench import split_chapters
except ImportError:  # 仓库测试把插件根目录当顶层包
    from domain.character_experience import EMOTIONS, normalize_content
    from services.book_lore_workbench import split_chapters

MIN_SCENE_CHARS = 100
MAX_SCENE_CHARS = 2000
# 相邻两次出场之间隔着不超过这么多段，算同一个场景（中间的段落一并带上）。
SCENE_GAP_PARAGRAPHS = 3
# 场景结尾在最后一次出场之后再带几段（交代结果）。
SCENE_TAIL_PARAGRAPHS = 2
MEMORY_MIN_CHARS = 8
MEMORY_MAX_CHARS = 400
SETTING_MAX_CHARS = 12000


@dataclass(frozen=True)
class Scene:
    chapter: int
    title: str
    index: int  # 本章里的第几个场景（从 0 起）
    text: str
    key: str

    def preview(self, limit: int = 120) -> str:
        compact = self.text.replace("\n", " ").strip()
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _clean_names(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def scene_key(chapter: int, text: str) -> str:
    """场景的稳定标识：章号 + 规范化正文指纹。原文加了新章节、重新切章也对得上。"""
    digest = hashlib.sha1(normalize_content(text).encode("utf-8")).hexdigest()[:16]
    return f"novel:ch{int(chapter)}:{digest}"


def event_key(stage_key: str, event: str) -> str:
    digest = hashlib.sha1(normalize_content(event).encode("utf-8")).hexdigest()[:12]
    return f"event:{stage_key}:{digest}"


def extract_scenes(
    body: str,
    names: Sequence[str],
    *,
    require: Sequence[str] | None = None,
    min_chars: int = MIN_SCENE_CHARS,
    gap_limit: int = SCENE_GAP_PARAGRAPHS,
    tail: int = SCENE_TAIL_PARAGRAPHS,
) -> list[str]:
    """一章正文里角色出场的场景块。

    ``names`` 里任一个出现在段落里算出场；拼好的场景至少 ``min_chars`` 字，且要包含 ``require``
    里的某个名字（默认要求出现全名，避免只被别名顺带提到的场景）。
    """
    names = _clean_names(names)
    required = _clean_names(require) or names
    if not names:
        return []
    paragraphs = [line.strip() for line in str(body or "").split("\n") if line.strip()]
    scenes: list[str] = []
    current: list[str] = []
    last_added = -1
    gap = 0

    def flush() -> None:
        if not current:
            return
        text = "\n".join(current)
        if len(text) >= min_chars and any(name in text for name in required):
            scenes.append(text)

    for index, paragraph in enumerate(paragraphs):
        if any(name in paragraph for name in names):
            if current and gap > gap_limit:
                flush()
                current = []
            elif current and gap > 0:
                # 两次出场之间隔得不远：中间没带上的段落补进来，场景保持连续
                current.extend(paragraphs[last_added + 1:index])
            current.append(paragraph)
            last_added = index
            gap = 0
        else:
            gap += 1
            if current and gap <= tail:
                current.append(paragraph)
                last_added = index
    flush()
    return scenes


def plan_scenes(
    text: str,
    *,
    name: str,
    aliases: Sequence[str] = (),
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    min_chars: int = MIN_SCENE_CHARS,
    require_full_name: bool = True,
) -> tuple[list[Scene], dict[str, int]]:
    """整本原文 → 角色出场的全部场景（按章节顺序）与统计。"""
    names = _clean_names([name, *aliases])
    chapters = split_chapters(text)
    in_range = [
        chapter for chapter in chapters
        if (chapter_from is None or chapter["number"] >= int(chapter_from))
        and (chapter_to is None or chapter["number"] <= int(chapter_to))
    ]
    scenes: list[Scene] = []
    with_character = 0
    for chapter in in_range:
        body = chapter["body"]
        if not any(n in body for n in names):
            continue
        blocks = extract_scenes(
            body, names,
            require=[name] if require_full_name and name else names,
            min_chars=min_chars,
        )
        if blocks:
            with_character += 1
        for index, block in enumerate(blocks):
            scenes.append(Scene(chapter["number"], chapter["title"], index, block, scene_key(chapter["number"], block)))
    stats = {
        "chapters_total": len(chapters),
        "chapters_in_range": len(in_range),
        "chapters_with_character": with_character,
        "scenes": len(scenes),
    }
    return scenes, stats


def clip_scene(text: str, max_chars: int = MAX_SCENE_CHARS) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "...(截断)"


# ─── 提示词 ────────────────────────────────────────────────────────────

_EMOTION_TEXT = "/".join(EMOTIONS)


def extract_prompts(*, name: str, aliases: Sequence[str], description: str, chapter_title: str, scene_text: str) -> tuple[str, str]:
    """从原著提取：一个场景 → 1–3 条第一人称记忆。角色名、别名、设定全部来自参数。"""
    alias_text = "、".join(_clean_names(aliases))
    system = (
        f"你是一个角色经历整理者。你的任务是把小说片段改写成「{name}」本人的第一人称记忆。\n\n"
        f"角色：{name}" + (f"（别名：{alias_text}）" if alias_text else "") + "\n"
        f"角色设定：{description.strip() or '（未提供，按片段里的表现把握语气）'}\n\n"
        "规则：\n"
        f"1. 用{name}的视角和语气重述这段经历，第一人称「我」；提到别人时用{name}平时对他们的称呼。\n"
        "2. 保留关键情节、人物、情感；不要编造片段里没有的事。\n"
        "3. 语气符合上面的角色设定。\n"
        "4. 输出 1-3 条记忆，每条 50-150 字。\n"
        f"5. 如果片段里{name}只是被提到、没有实际参与或感受，输出空数组 []。\n"
        f"6. emotion 从这些标签里选一个：{_EMOTION_TEXT}。\n"
        f"7. characters 写这段经历里与{name}有关的其他人物（原文名字）。\n"
        f"8. 如果片段里能明确看出{name}此时的时间（如「高一上学期」「第三年春天」），写在 time；看不出就写空字符串。\n\n"
        "严格输出 JSON 数组，不要其他内容：\n"
        '[{"memory": "第一人称记忆文本", "emotion": "情感标签", "characters": ["涉及的其他人物"], "time": ""}]\n\n'
        "如果无法提取有效记忆，输出：[]"
    )
    user = f"来源：{chapter_title}\n\n{scene_text}"
    return system, user


def split_setting_paragraphs(setting: str) -> list[str]:
    """设定文本按空行 / 换行分段（给模型编号引用）；过长的整体截断。"""
    text = str(setting or "").strip()[:SETTING_MAX_CHARS]
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if len(blocks) <= 1:
        blocks = [line.strip() for line in text.splitlines() if line.strip()]
    return blocks


def numbered_setting(paragraphs: Sequence[str]) -> str:
    return "\n".join(f"[{index}] {paragraph}" for index, paragraph in enumerate(paragraphs, 1))


def setting_excerpt(paragraphs: Sequence[str], basis: Iterable[int], *, limit: int = 1200) -> str:
    picked = [paragraphs[i - 1] for i in sorted(set(basis)) if 1 <= i <= len(paragraphs)]
    text = "\n".join(picked)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def timeline_prompts(*, name: str, paragraphs: Sequence[str]) -> tuple[str, str]:
    """按设定起草人生时间线：阶段 + 关键事件，每个阶段注明依据的设定段落编号。"""
    system = (
        f"你在为一个角色「{name}」整理她进入群聊之前的人生经历。下面给出她的设定，按段编号。\n"
        "请按时间顺序起草她的人生时间线，分成 3-8 个阶段（如童年、求学、某次变故之后……），每个阶段写：\n"
        "- title：阶段名（10 字以内）\n"
        "- story_time：她自己的时间（如「七岁到十二岁」「第三年春」），看不出就写大致年龄段\n"
        "- description：这一阶段的概况（60 字以内）\n"
        "- key_events：2-5 个关键事件，每个一句话，写成 {\"text\": \"事件\", \"basis\": [依据的设定段落编号]}\n"
        "- basis：这个阶段依据的设定段落编号数组；设定里没写、合理推演的填 []\n\n"
        "要求：不要和设定矛盾；不要写她进群之后的事；设定没写的细节可以合理推演，但要少而克制。\n"
        '只输出 JSON，不要其他内容：{"stages": [{"title": "", "story_time": "", "description": "", '
        '"key_events": [{"text": "", "basis": [1]}], "basis": [1]}]}'
    )
    user = f"角色：{name}\n设定：\n{numbered_setting(paragraphs)}"
    return system, user


def expand_prompts(
    *,
    name: str,
    setting: str,
    stage_title: str,
    stage_time: str,
    stage_description: str,
    event: str,
) -> tuple[str, str]:
    """把时间线上的一个关键事件展开成 1–3 条第一人称记忆。"""
    system = (
        f"你是「{name}」本人，正在回忆自己进群之前的一段经历。\n"
        f"写 1-3 条第一人称记忆，每条 50-150 字：像日记里的回忆，有具体的场景、细节和当时的感受；语气符合设定。\n"
        "不要和设定矛盾，不要写进群之后的事；设定没写的细节可以合理想象，但不要引入会改变人设的大事件。\n"
        f"emotion 从这些标签里选一个：{_EMOTION_TEXT}；characters 写涉及的其他人物（没有就空数组）。\n"
        "严格输出 JSON 数组，不要其他内容：\n"
        '[{"memory": "第一人称记忆文本", "emotion": "情感标签", "characters": []}]'
    )
    user = (
        f"设定（节选）：\n{setting.strip()}\n\n"
        f"所处阶段：{stage_title}" + (f"（{stage_time}）" if stage_time else "") + "\n"
        + (f"阶段概况：{stage_description}\n" if stage_description else "")
        + f"要回忆的事：{event}"
    )
    return system, user


# ─── 输出解析 ──────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*|\s*```$")


def parse_json_payload(raw: Any) -> Any | None:
    """模型输出 → JSON。容忍 ```json 包裹与前后多余的文字；解析不了返回 None（调用方按失败处理、续跑时重试）。"""
    if isinstance(raw, (list, dict)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (TypeError, ValueError):
                continue
    return None


def parse_memory_list(raw: Any) -> list[dict[str, Any]] | None:
    """提取 / 展开的输出 → [{memory, emotion, characters, time}]；空数组合法，解析不了返回 None。"""
    value = parse_json_payload(raw)
    if isinstance(value, Mapping):
        value = value.get("memories") if isinstance(value.get("memories"), list) else None
    if not isinstance(value, list):
        return None
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        memory = str(item.get("memory") or item.get("content") or "").strip()
        if len(memory) < MEMORY_MIN_CHARS:
            continue
        characters = item.get("characters")
        if not isinstance(characters, list):
            characters = [characters] if isinstance(characters, str) and characters.strip() else []
        out.append({
            "memory": memory[:MEMORY_MAX_CHARS],
            "emotion": str(item.get("emotion") or "").strip()[:10],
            "characters": [str(c).strip()[:40] for c in characters if str(c or "").strip()][:10],
            "time": str(item.get("time") or "").strip()[:40],
        })
    return out[:3]


def parse_timeline(raw: Any, *, paragraph_count: int) -> list[dict[str, Any]] | None:
    """起草时间线的输出 → 阶段列表；依据编号超出设定段数的丢掉。"""
    value = parse_json_payload(raw)
    if isinstance(value, Mapping):
        value = value.get("stages")
    if not isinstance(value, list):
        return None

    def basis_of(item: Any) -> list[int]:
        raw_basis = item.get("basis") if isinstance(item, Mapping) else None
        numbers: list[int] = []
        for entry in raw_basis if isinstance(raw_basis, list) else []:
            try:
                number = int(entry)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= paragraph_count and number not in numbers:
                numbers.append(number)
        return sorted(numbers)

    stages: list[dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()[:40]
        if not title:
            continue
        events: list[dict[str, Any]] = []
        for event in item.get("key_events") or []:
            if isinstance(event, Mapping):
                text = str(event.get("text") or event.get("event") or "").strip()
                basis = basis_of(event)
            else:
                text, basis = str(event or "").strip(), []
            if text:
                events.append({"text": text[:300], "basis": basis})
        stages.append({
            "title": title,
            "story_time": str(item.get("story_time") or "").strip()[:60],
            "description": str(item.get("description") or "").strip()[:500],
            "key_events": events[:8],
            "basis": basis_of(item),
        })
    return stages


__all__ = [
    "MAX_SCENE_CHARS",
    "MIN_SCENE_CHARS",
    "Scene",
    "clip_scene",
    "event_key",
    "expand_prompts",
    "extract_prompts",
    "extract_scenes",
    "numbered_setting",
    "parse_json_payload",
    "parse_memory_list",
    "parse_timeline",
    "plan_scenes",
    "scene_key",
    "setting_excerpt",
    "split_setting_paragraphs",
    "timeline_prompts",
]
