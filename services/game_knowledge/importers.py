"""游戏知识库的导入格式：WaveMemory 通用 JSON，以及现有资产的一次性适配。

- **通用格式**（``format = "wavememory.game_knowledge"``，见 ``docs/game-knowledge.md``）：管理台导入 / 导出都用它。
  仓库里不带任何预设攻略：游戏自己的经验由 Bot 从经历里写（``lessons.py``），复杂的游戏再导入教程。
- **现有资产适配**：缺氧教程的 ``oni_tutorial_cards.json``（策略卡）与 ``oni_timeline.json``（B 站《缺氧从新开始》
  时间线），以及线上记忆表里 ``source='oni_lore'`` 的字幕知识点。这些适配只做格式转换（集数 → 条目键、周期提示 →
  「参考」的 ``cycle`` 条件、bvid → 出处），不进入注入与匹配逻辑；注入与匹配对所有游戏一视同仁。

适配器只产出「原始条目」字典，字段规整与校验统一走 :mod:`domain.game_knowledge`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

try:
    from ...domain.game_knowledge import GameKnowledgeError, clean_text, fold
except ImportError:  # pragma: no cover - 仓库测试以插件根目录为顶层
    from domain.game_knowledge import GameKnowledgeError, clean_text, fold

FORMAT_ID = "wavememory.game_knowledge"
FORMAT_VERSION = 1
FORMAT_LABELS = {
    "generic": "WaveMemory 通用格式",
    "oni_tutorial_cards": "缺氧教程策略卡（oni_tutorial_cards.json）",
    "oni_timeline": "缺氧教程时间线（oni_timeline.json）",
    "legacy_memory": "记忆表旧知识点",
}
IMPORT_MAX_ITEMS = 2000


@dataclass
class ImportPackage:
    """一次导入的内容：原始条目（未规整）+ 文件里带的游戏信息 + 警告。"""

    format: str
    game: dict[str, Any] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)
    terms: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 文件 ``pack.force_status`` 指定的强制状态（"draft" / "active"）；None 时按条目自带状态，没有就是草稿。
    force_status: str | None = None
    pack: dict[str, Any] | None = None

    @property
    def format_label(self) -> str:
        return FORMAT_LABELS.get(self.format, self.format)


# ─────────────────────────── 关键词建议（通用） ───────────────────────────

_BRACKET_RE = re.compile(r"[【「]([^】」\n]{1,24})[】」]")
_PAREN_RE = re.compile(r"[（(]([^）)]{1,24})[）)]")
_SEGMENT_SPLIT_RE = re.compile(r"[，,、：:；;。！!？?\s（）()【】「」《》“”\"'·•/|]+|以及|还是|与|和|及|或")
# 教程标题里常见、但不能当触发词的套话；按最长优先去掉
_FLUFF = (
    "详细注意事项", "注意事项", "详细建造步骤", "建造步骤", "详细开发步骤", "开发步骤", "的通用解法", "通用解法",
    "包教包会", "简单易懂", "新手无压力", "大指南", "指南", "教程", "的实际运用", "实际运用", "的运用", "的获取",
    "的使用", "的摆放", "的选择", "入门级", "入门", "新手", "详细", "简单", "方法", "技巧", "问题", "如何", "怎么",
    "游戏",
)
_FLUFF_RE = re.compile("|".join(re.escape(item) for item in sorted(_FLUFF, key=len, reverse=True)) + r"|第[一二三四五六七八九十0-9]+道坎")
KEYWORD_STOPWORDS = frozenset({
    "游戏", "攻略", "教程", "新手", "玩家", "时候", "时间", "东西", "地方", "方法", "问题", "办法", "情况", "左右",
    "模块", "系统", "空间", "位置", "部分", "一下", "可以", "需要", "注意", "建议", "开始", "进行", "使用",
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "如何", "现在", "之后", "以后", "之前", "以前", "所有",
    "初期", "前期", "中期", "后期", "阶段", "步骤", "详细", "简单", "基础", "设计", "建造", "制作",
    "入门", "注意事项", "指南", "包教包会", "无所谓", "易懂", "基地", "地图", "区域", "建设", "方式", "设置",
    "检查", "调整", "实际", "精准", "地点", "经典", "内地", "楼层", "运用", "转型", "本体", "自动", "全家",
    "上方", "下方", "旁边", "附近", "周围", "整个", "一定", "直接", "利用", "字形",
})
# jieba 常把没见过的词标成人名 / 地名（nr / ns），只取可靠的名词类
_JIEBA_FLAGS = frozenset({"n", "nz", "vn", "eng"})
_PRONOUN_RE = re.compile(r"[我你他她它咱]|自己")
SEGMENT_MAX_CHARS = 5


def _jieba_nouns(text: str) -> list[str]:
    """jieba 可用时抽名词（AstrBot 自带 jieba）；没有 jieba 返回空列表，只用规则切分。"""
    try:
        import jieba  # type: ignore
        import jieba.posseg as pseg  # type: ignore
    except Exception:  # pragma: no cover - 环境没有 jieba
        return []
    try:
        jieba.setLogLevel(40)
    except Exception:  # pragma: no cover
        pass
    words: list[str] = []
    try:
        for token in pseg.cut(text or ""):
            word, flag = str(token.word).strip(), str(token.flag)
            if flag in _JIEBA_FLAGS and len(word) >= 2:
                words.append(word)
    except Exception:  # pragma: no cover - jieba 词典加载失败
        return []
    return words


def _usable_keyword(word: str, *, explicit: bool = False) -> bool:
    text = word.strip()
    if not text or text in KEYWORD_STOPWORDS or _FLUFF_RE.fullmatch(text) or _PRONOUN_RE.search(text):
        return False
    if re.fullmatch(r"[\d\s.%℃+-]+", text):
        return False
    length = len(text)
    if re.fullmatch(r"[A-Za-z0-9 _.+-]+", text):
        # 两个字母的缩写（如 ST、AT）只在显式给出时收：从正文里抽出来的太容易误命中英文单词
        return (2 if explicit else 3) <= length <= 24
    return 2 <= length <= 10


def title_segments(title: str) -> list[str]:
    """按标点与连接词切标题，去掉「详细注意事项」「入门第一道坎」这类套话；太长、带人称的片段不要。"""
    segments: list[str] = []
    for raw in _SEGMENT_SPLIT_RE.split(title or ""):
        segment = _FLUFF_RE.sub("", raw).strip("的了 ")
        if not segment or _PRONOUN_RE.search(segment):
            continue
        if not re.fullmatch(r"[A-Za-z0-9 _.+-]+", segment) and len(segment) > SEGMENT_MAX_CHARS:
            continue
        segments.append(segment)
    return segments


def suggest_keywords(title: str, texts: Sequence[str] = (), *, extra: Sequence[str] = (), limit: int = 10) -> list[str]:
    """给没有触发词的条目建议关键词（导入后可在管理台修改）。

    顺序：显式给出的（如集数标记）→ 【】「」里的术语 → 标题切分 → 标题与前两段正文里的名词（jieba 可用时）。
    """
    candidates: list[str] = [str(item) for item in extra if item]
    explicit = {fold(item) for item in candidates}
    blobs = [title, *texts]
    for blob in blobs:
        for term in _BRACKET_RE.findall(blob or ""):
            base = _PAREN_RE.sub("", term).strip()
            candidates.append(base or term)
            candidates.extend(inner for inner in _PAREN_RE.findall(term) if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{1,23}", inner))
    candidates.extend(title_segments(title))
    candidates.extend(re.findall(r"\b[A-Z][A-Za-z0-9]{1,15}\b", title or ""))
    candidates.extend(_jieba_nouns(title))
    for blob in texts[:2]:
        candidates.extend(_jieba_nouns(blob))
    words: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        word = clean_text(candidate, limit=24)
        marker = fold(word)
        if not marker or marker in seen or not _usable_keyword(word, explicit=marker in explicit):
            continue
        seen.add(marker)
        words.append(word)
        if len(words) >= limit:
            break
    return words


def flag_shared_sources(cards: list[dict[str, Any]], *, meta_key: str = "bvid") -> None:
    """同一个出处（如 bvid）标在好几集上，多半是资产错配：在备注里提示核对，不改出处本身。"""
    owners: dict[str, list[str]] = {}
    for card in cards:
        value = str((card.get("meta") or {}).get(meta_key) or "")
        if value:
            owners.setdefault(value, []).append(str(card.get("title") or card.get("key")).split(" ")[0])
    for card in cards:
        value = str((card.get("meta") or {}).get(meta_key) or "")
        shared = owners.get(value, [])
        if value and len(shared) > 1:
            hint = f"资产里 {value} 同时标在 {'、'.join(shared)} 上，出处链接可能错配，建议核对"
            card["note"] = "；".join(part for part in (card.get("note"), hint) if part)


def drop_common_keywords(cards: list[dict[str, Any]], *, protected: Iterable[str] = (), ratio: float = 0.3, minimum: int = 3) -> None:
    """一次导入里出现在太多卡上的建议词（如「小人」「氧气」若到处都有）区分不了卡片，去掉。"""
    if not cards:
        return
    keep = {fold(word) for word in protected}
    frequency: dict[str, int] = {}
    for card in cards:
        for word in {fold(w) for w in card.get("keywords") or []}:
            frequency[word] = frequency.get(word, 0) + 1
    limit = max(minimum, int(len(cards) * ratio))
    for card in cards:
        card["keywords"] = [w for w in card.get("keywords") or [] if fold(w) in keep or frequency.get(fold(w), 0) <= limit]


# ─────────────────────────── 通用格式 ───────────────────────────


def detect_format(data: Any) -> str:
    """识别导入文件格式：通用格式 / 缺氧策略卡 / 缺氧时间线；认不出时抛错。"""
    if isinstance(data, Mapping):
        if str(data.get("format") or "") == FORMAT_ID or isinstance(data.get("cards"), list) or isinstance(data.get("terms"), list):
            return "generic"
        episodes = data.get("episodes")
        if isinstance(episodes, list) and episodes and all(isinstance(e, Mapping) and "ep" in e for e in episodes[:5]):
            return "oni_timeline"
    if isinstance(data, list) and data and all(isinstance(item, Mapping) for item in data[:5]):
        sample = data[0]
        if "ep" in sample and "approx_cycle_hint" in sample and ("prefer" in sample or "trigger" in sample):
            return "oni_tutorial_cards"
    raise GameKnowledgeError(
        "unknown_import_format",
        "认不出这个文件的格式：支持 WaveMemory 通用格式（format=wavememory.game_knowledge），"
        "以及缺氧教程的 oni_tutorial_cards.json、oni_timeline.json",
    )


def adapt_generic(data: Mapping[str, Any]) -> ImportPackage:
    warnings: list[str] = []
    version = data.get("format_version", FORMAT_VERSION)
    if str(data.get("format") or FORMAT_ID) != FORMAT_ID:
        warnings.append(f"format 字段是 {data.get('format')!r}，按通用格式读取")
    try:
        if int(version) > FORMAT_VERSION:
            warnings.append(f"文件格式版本 {version} 比当前支持的 {FORMAT_VERSION} 新，未知字段会被忽略")
    except (TypeError, ValueError):
        warnings.append(f"format_version 不是数字：{version!r}")
    game = data.get("game") if isinstance(data.get("game"), Mapping) else {}
    cards = [dict(item) for item in data.get("cards") or [] if isinstance(item, Mapping)]
    terms = [dict(item) for item in data.get("terms") or [] if isinstance(item, Mapping)]
    skipped = sum(1 for item in [*(data.get("cards") or []), *(data.get("terms") or [])] if not isinstance(item, Mapping))
    if skipped:
        warnings.append(f"{skipped} 个条目不是对象，已跳过")
    pack = data.get("pack") if isinstance(data.get("pack"), Mapping) else None
    force = str((pack or {}).get("force_status") or "").strip().lower() or None
    return ImportPackage(
        format="generic",
        game=dict(game),
        cards=cards,
        terms=terms,
        warnings=warnings,
        force_status=force if force in {"draft", "active"} else None,
        pack=dict(pack) if pack else None,
    )


def to_package(data: Any, fmt: str = "auto") -> ImportPackage:
    """按格式（``auto`` 时自动识别）把导入数据转成 :class:`ImportPackage`。"""
    name = detect_format(data) if not fmt or fmt == "auto" else fmt
    if name == "generic":
        if not isinstance(data, Mapping):
            raise GameKnowledgeError("invalid_import_file", "通用格式的文件必须是 JSON 对象")
        package = adapt_generic(data)
    elif name == "oni_tutorial_cards":
        package = adapt_oni_tutorial_cards(data)
    elif name == "oni_timeline":
        package = adapt_oni_timeline(data)
    else:
        raise GameKnowledgeError("unknown_import_format", f"不支持的导入格式：{fmt}")
    if len(package.cards) + len(package.terms) > IMPORT_MAX_ITEMS:
        raise GameKnowledgeError("import_too_large", f"一次最多导入 {IMPORT_MAX_ITEMS} 个条目，请分批")
    return package


def export_document(game: Mapping[str, Any], cards: Sequence[Mapping[str, Any]], terms: Sequence[Mapping[str, Any]], *, exported_at: float) -> dict[str, Any]:
    """导出为通用格式；再导入时同键条目会被识别为同一条。"""
    return {
        "format": FORMAT_ID,
        "format_version": FORMAT_VERSION,
        "exported_at": exported_at,
        "game": {
            "id": game.get("id"),
            "name": game.get("name"),
            "aliases": list(game.get("aliases") or []),
            "description": game.get("description") or "",
        },
        "cards": [
            {
                "key": card.get("key"),
                "title": card.get("title"),
                "stage": card.get("stage") or "",
                "points": list(card.get("points") or []),
                "pitfalls": list(card.get("pitfalls") or []),
                "trigger": {
                    "keywords": list(card.get("keywords") or []),
                    "conditions": [dict(item) for item in card.get("conditions") or []],
                },
                "source": card.get("source") or "",
                "source_url": card.get("source_url") or "",
                "source_type": card.get("source_type") or "manual",
                "status": card.get("status") or "draft",
                "version": card.get("version"),
                "note": card.get("note") or "",
                "meta": dict(card.get("meta") or {}),
            }
            for card in cards
        ],
        "terms": [
            {
                "key": term.get("key"),
                "term": term.get("term"),
                "aliases": list(term.get("aliases") or []),
                "explanation": term.get("explanation") or "",
                "related": list(term.get("related") or []),
                "source": term.get("source") or "",
                "source_url": term.get("source_url") or "",
                "source_type": term.get("source_type") or "manual",
                "status": term.get("status") or "draft",
                "version": term.get("version"),
                "note": term.get("note") or "",
                "meta": dict(term.get("meta") or {}),
            }
            for term in terms
        ],
    }


# ─────────────────────────── 缺氧教程资产（一次性适配） ───────────────────────────

ONI_GAME = {
    "id": "oni",
    "name": "缺氧",
    "aliases": ["Oxygen Not Included", "ONI"],
    "description": "缺氧（Oxygen Not Included）。教程资产来自 B 站系列《缺氧从新开始》。",
}
ONI_SERIES = "《缺氧从新开始》"
# 策略卡的 phase 与时间线的 phase_label 一一对应；stage 代码见 oni_lore 知识点前缀
ONI_PHASE_LABELS = {"foundation": "筑基开局", "industry": "工业转型与自动化", "late": "中后期能源与工业", "space": "补天飞天与裂口毕业"}
ONI_STAGE_LABELS = {"early": "筑基开局", "mid": "工业转型与自动化", "late": "中后期能源与工业", "space": "补天飞天与裂口毕业"}
# 时间线里每个 stage 的周期提示（oni_lore 行只带 stage 代码，按同一套划分补 cycle 条件）
ONI_STAGE_CYCLES = {"early": "0-30", "mid": "30-100", "late": "100-200", "space": "200+"}
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cycle_conditions(hint: Any) -> list[str]:
    """周期提示 → 「参考」的 ``cycle`` 条件："0-30" → 参考 cycle 0..30；"200+" → 参考 cycle >= 200；认不出返回空。

    教程每集的周期只是大致进度：周期对不上时卡片照样能被问到，只是排在对得上的后面（不挡）。
    """
    text = str(hint or "").strip()
    match = re.fullmatch(r"(\d+)\s*[-~–—]\s*(\d+)", text)
    if match and int(match[1]) <= int(match[2]):
        return [f"参考 cycle {int(match[1])}..{int(match[2])}"]
    match = re.fullmatch(r"(\d+)\s*\+", text)
    if match:
        return [f"参考 cycle >= {int(match[1])}"]
    return []


def _strs(value: Any) -> list[str]:
    return [str(item).strip() for item in value or [] if isinstance(item, (str, int, float)) and str(item).strip()]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        marker = fold(item)
        if marker and marker not in seen:
            seen.add(marker)
            out.append(item)
    return out


def _episode_markers(ep: int) -> list[str]:
    return [f"EP{ep:02d}", f"第{ep}集"]


def adapt_oni_tutorial_cards(data: Any) -> ImportPackage:
    """``oni_tutorial_cards.json``：每集一张策略卡 → 一张知识卡（键 ``oni_ep01``…，与时间线同键）。"""
    if not isinstance(data, list):
        raise GameKnowledgeError("invalid_import_file", "oni_tutorial_cards.json 应该是卡片数组")
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    protected: list[str] = []
    for raw in data:
        if not isinstance(raw, Mapping):
            continue
        ep = _as_int(raw.get("ep"))
        if ep is None:
            warnings.append(f"卡片 {raw.get('id')!r} 没有集数，已跳过")
            continue
        title = str(raw.get("title") or "").strip() or f"EP{ep:02d}"
        plain_title = re.sub(r"^EP\d+\s*", "", title)
        urls = _strs(raw.get("source_urls"))
        bvid_match = _BVID_RE.search(" ".join(urls))
        bvid = bvid_match.group(0) if bvid_match else ""
        phase = str(raw.get("phase") or "")
        stage = ONI_PHASE_LABELS.get(phase) or ONI_STAGE_LABELS.get(str(raw.get("stage") or "")) or str(raw.get("stage") or "")
        prefer = _dedupe(_strs(raw.get("prefer")))
        markers = _episode_markers(ep)
        protected.extend(markers)
        short_triggers = [t for t in _strs(raw.get("trigger")) if len(t) <= 12 and t not in {stage}]
        conditions = cycle_conditions(raw.get("approx_cycle_hint"))
        if not conditions and raw.get("approx_cycle_hint"):
            warnings.append(f"EP{ep:02d} 的周期提示 {raw.get('approx_cycle_hint')!r} 认不出，没有加 cycle 条件")
        stability = str(raw.get("stability") or "")
        cards.append({
            "key": f"oni_ep{ep:02d}",
            "title": f"EP{ep:02d} {plain_title}",
            "stage": stage,
            "points": prefer,
            "pitfalls": _dedupe(_strs(raw.get("avoid"))),
            "keywords": suggest_keywords(plain_title, prefer[:4], extra=[*markers, *short_triggers]),
            "conditions": conditions,
            "source": f"B站教程{ONI_SERIES}EP{ep:02d}「{plain_title}」" + (f"（{bvid}）" if bvid else ""),
            "source_url": urls[0] if urls else "",
            "source_type": "tutorial",
            "status": "active",
            "note": "" if stability in {"", "current"} else f"资产标记 stability={stability}，建议复核",
            "meta": {
                "asset": "oni_tutorial_cards",
                "asset_id": raw.get("id"),
                "ep": ep,
                "bvid": bvid,
                "phase": phase,
                "risk": raw.get("risk"),
                "mode_bias": raw.get("mode_bias"),
                "stability": stability,
                "last_reviewed": raw.get("last_reviewed"),
                "approx_cycle_hint": raw.get("approx_cycle_hint"),
            },
        })
    drop_common_keywords(cards, protected=protected)
    flag_shared_sources(cards)
    return ImportPackage(format="oni_tutorial_cards", game=dict(ONI_GAME), cards=cards, warnings=warnings)


def adapt_oni_timeline(data: Any) -> ImportPackage:
    """``oni_timeline.json``：每集 → 一张知识卡（与策略卡同键 ``oni_ep01``…，可用「合并」补全）。

    要点顺序：阶段目标 → 分步做法 → 其余建议；注意：典型痛点在前，其后是要避免的做法。
    ``extras`` 只有知识点编号、没有正文，不导入（记一条警告）。
    """
    if not isinstance(data, Mapping) or not isinstance(data.get("episodes"), list):
        raise GameKnowledgeError("invalid_import_file", "oni_timeline.json 应该是带 episodes 数组的对象")
    source_name = str(data.get("source") or "").strip()
    series = ONI_SERIES if source_name in {"", "从新开始"} else f"《{source_name}》"
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    protected: list[str] = []
    for raw in data["episodes"]:
        if not isinstance(raw, Mapping):
            continue
        ep = _as_int(raw.get("ep"))
        if ep is None:
            warnings.append("有一集没有集数，已跳过")
            continue
        plain_title = str(raw.get("title") or "").strip() or f"第{ep}集"
        steps = [step for step in raw.get("steps") or [] if isinstance(step, Mapping)]
        step_titles = _strs(step.get("title") for step in steps)
        goals = _strs(raw.get("goals"))
        points = _dedupe([*goals, *step_titles, *(p for step in steps for p in _strs(step.get("prefer"))), *_strs(raw.get("prefer"))])
        pain = str(raw.get("pain") or "").strip()
        pitfalls = _dedupe([*( [f"典型痛点：{pain}"] if pain else []), *_strs(raw.get("avoid")), *(a for step in steps for a in _strs(step.get("avoid")))])
        if not points and not pitfalls:
            warnings.append(f"EP{ep:02d} 没有任何要点或注意，已跳过")
            continue
        bvid = str(raw.get("bvid") or "").strip()
        stage = str(raw.get("phase_label") or "").strip() or ONI_PHASE_LABELS.get(str(raw.get("phase") or ""), "") or ONI_STAGE_LABELS.get(str(raw.get("stage") or ""), "")
        conditions = cycle_conditions(raw.get("approx_cycle_hint"))
        if not conditions and raw.get("approx_cycle_hint"):
            warnings.append(f"EP{ep:02d} 的周期提示 {raw.get('approx_cycle_hint')!r} 认不出，没有加 cycle 条件")
        markers = _episode_markers(ep)
        protected.extend(markers)
        notes = []
        if raw.get("no_transcript_points") or raw.get("missing_source"):
            notes.append("本集字幕知识点偏少，要点以阶段目标与分步做法为准")
        prerequisites = [n for n in (_as_int(v) for v in raw.get("prerequisites") or []) if n is not None]
        cards.append({
            "key": f"oni_ep{ep:02d}",
            "title": f"EP{ep:02d} {plain_title}",
            "stage": stage,
            "points": points,
            "pitfalls": pitfalls,
            "keywords": suggest_keywords(plain_title, goals, extra=[*markers, *(t for s in step_titles for t in _BRACKET_RE.findall(s))]),
            "conditions": conditions,
            "source": f"B站教程{series}EP{ep:02d}「{plain_title}」" + (f"（{bvid}）" if bvid else ""),
            "source_url": str(raw.get("source_url") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")),
            "source_type": "tutorial",
            "status": "active",
            "note": "；".join(notes),
            "meta": {
                "asset": "oni_timeline",
                "ep": ep,
                "bvid": bvid,
                "series_title": raw.get("series_title"),
                "phase": raw.get("phase"),
                "approx_cycle_hint": raw.get("approx_cycle_hint"),
                "prerequisites": prerequisites,
                "knowledge_count": raw.get("knowledge_count"),
                "knowledge_point_ids": _dedupe(_strs(raw.get("knowledge_point_ids")))[:40],
            },
        })
    extras = [item for item in data.get("extras") or [] if isinstance(item, Mapping)]
    if extras:
        ids = sum(len(item.get("knowledge_point_ids") or []) for item in extras)
        warnings.append(f"extras（{'、'.join(str(item.get('title') or item.get('id')) for item in extras)}）只有 {ids} 个知识点编号、没有正文，未导入")
    drop_common_keywords(cards, protected=protected)
    flag_shared_sources(cards)
    return ImportPackage(format="oni_timeline", game=dict(ONI_GAME), cards=cards, warnings=warnings)


# ─────────────────────────── 记忆表里的旧知识点（oni_lore） ───────────────────────────


@dataclass(frozen=True)
class LegacyMemorySource:
    source: str
    game: Mapping[str, Any]
    description: str


LEGACY_MEMORY_SOURCES: dict[str, LegacyMemorySource] = {
    "oni_lore": LegacyMemorySource(
        source="oni_lore",
        game=ONI_GAME,
        description="7 月从《缺氧从新开始》字幕导入记忆表的缺氧知识点：没有归属任何 Bot，按作用域规则基本召回不到",
    ),
}

_ONI_LORE_PREFIX_RE = re.compile(
    r"^\s*\[\s*EP\s*(?P<ep>\d{1,3})\s*[·・•]\s*(?P<episode>.+?)\s*[·・•]\s*#\s*(?P<index>\d{1,4})\s*/\s*(?P<total>\d{1,4})"
    r"\s*[·・•]\s*(?P<stage>[A-Za-z_]+)\s*\]\s*(?P<body>.*)$",
    re.S,
)
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])")
POINT_CHARS = 150


def parse_oni_lore_content(content: str) -> dict[str, Any] | None:
    """解析「[EP06·挖向原油区，野外炼钢详细注意事项 · #18/30 · mid] 水的相变温度：……」；认不出前缀返回 None。"""
    match = _ONI_LORE_PREFIX_RE.match(content or "")
    if not match:
        return None
    body = match["body"].strip()
    title, text = "", body
    colon = re.search(r"[：:]", body[:32])
    if colon and colon.start() >= 2:
        title, text = body[: colon.start()].strip(), body[colon.end():].strip() or body
    return {
        "ep": int(match["ep"]),
        "episode_title": match["episode"].strip(),
        "index": int(match["index"]),
        "total": int(match["total"]),
        "stage_code": match["stage"].strip().lower(),
        "title": title,
        "text": text,
    }


def split_points(text: str, *, limit: int = POINT_CHARS, max_points: int = 8) -> list[str]:
    """长段落按句切成若干条要点（每条不超过 ``limit`` 字），不丢内容。"""
    sentences = [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()]
    points: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:
            if current:
                points.append(current)
                current = ""
            points.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) > limit:
            points.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        points.append(current)
    return points[:max_points]


def oni_lore_package(rows: Sequence[Mapping[str, Any]], *, episode_sources: Mapping[int, str] | None = None) -> tuple[ImportPackage, dict[str, int]]:
    """记忆表 ``oni_lore`` 行 → 缺氧库的草稿知识卡（键 ``oni_lore_m<记忆 id>``，重复迁移会跳过）。

    解析出的集数、标题、序号、阶段写进标题 / 阶段 / 出处 / meta；阶段按时间线的周期划分补 ``cycle`` 条件。
    前缀认不出的行也导入（标题取正文开头，备注里写明），完全重复的行（同集同序号同正文）只导入一次。
    """
    sources = dict(episode_sources or {})
    cards: list[dict[str, Any]] = []
    stats = {"rows": len(rows), "parsed": 0, "parse_failed": 0, "duplicates": 0, "empty": 0}
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        memory_id = int(row.get("id") or 0)
        content = str(row.get("content") or "").strip()
        if not content:
            stats["empty"] += 1
            continue
        parsed = parse_oni_lore_content(content)
        note = f"从记忆表 oni_lore 迁移（原记忆 #{memory_id}），字幕语音转写，错字较多，校对后再启用"
        if parsed is None:
            stats["parse_failed"] += 1
            marker = ("raw", fold(content))
            if marker in seen:
                stats["duplicates"] += 1
                continue
            seen.add(marker)
            title = clean_text(content, limit=24)
            cards.append({
                "key": f"oni_lore_m{memory_id}",
                "title": title,
                "stage": "",
                "points": split_points(content),
                "pitfalls": [],
                "keywords": suggest_keywords(title, [], extra=_BRACKET_RE.findall(content), limit=6),
                "conditions": [],
                "source": f"记忆表 oni_lore（原记忆 #{memory_id}）",
                "source_type": "memory_migration",
                "status": "draft",
                "note": note + "；前缀没解析出集数",
                "meta": {"memory_id": memory_id, "legacy_source": "oni_lore", "parse_failed": True},
            })
            continue
        stats["parsed"] += 1
        marker = ("parsed", parsed["ep"], parsed["index"], fold(parsed["text"]))
        if marker in seen:
            stats["duplicates"] += 1
            continue
        seen.add(marker)
        ep = parsed["ep"]
        title = parsed["title"] or clean_text(parsed["text"], limit=24)
        stage_code = parsed["stage_code"]
        cards.append({
            "key": f"oni_lore_m{memory_id}",
            "title": title,
            "stage": ONI_STAGE_LABELS.get(stage_code, stage_code),
            "points": split_points(parsed["text"]),
            "pitfalls": [],
            # 只从知识点标题取词：正文是语音转写，jieba 容易从错字里抽出无关名词
            "keywords": suggest_keywords(title, [], extra=_BRACKET_RE.findall(parsed["text"]), limit=6),
            "conditions": cycle_conditions(ONI_STAGE_CYCLES.get(stage_code)),
            "source": f"B站教程{ONI_SERIES}EP{ep:02d}「{parsed['episode_title']}」字幕知识点 #{parsed['index']}/{parsed['total']}",
            "source_url": sources.get(ep, ""),
            "source_type": "memory_migration",
            "status": "draft",
            "note": note,
            "meta": {
                "memory_id": memory_id,
                "legacy_source": "oni_lore",
                "ep": ep,
                "index": parsed["index"],
                "total": parsed["total"],
                "stage_code": stage_code,
            },
        })
    return ImportPackage(format="legacy_memory", game=dict(ONI_GAME), cards=cards), stats


__all__ = [
    "FORMAT_ID",
    "FORMAT_LABELS",
    "FORMAT_VERSION",
    "ImportPackage",
    "LEGACY_MEMORY_SOURCES",
    "ONI_GAME",
    "adapt_generic",
    "adapt_oni_timeline",
    "adapt_oni_tutorial_cards",
    "cycle_conditions",
    "detect_format",
    "drop_common_keywords",
    "export_document",
    "flag_shared_sources",
    "oni_lore_package",
    "parse_oni_lore_content",
    "split_points",
    "suggest_keywords",
    "title_segments",
    "to_package",
]
