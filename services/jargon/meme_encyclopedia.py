"""神人包梗百科：理解（词典）、查询（工具）与用梗提示共用的只读知识库。

神人包本质是浓缩的梗百科，这里只把它当知识用，不当人格用：

- **词条**：``CORE_CURATED_PHRASES`` 的精选梗 + ``assets/holyman/meme_encyclopedia.json``
  的人工整理扩充词条（来自 ``_knowledge`` / ``_quotes``，不含 ``_persona`` 风格指令）；
- **段落**：``_knowledge/*.md`` 与 ``_quotes/*.md`` 的原文段落，只供查询工具检索；
- **语料**：``神言.txt`` 原始复制粘贴文案，**只能**经 ``wave_memory_meme_lookup`` 查到，
  任何注入通道都不读它。

梗不按内容类别拦截：真实人物梗、辱骂、低俗、灾难段子等照常收录与返回，只在后面加简短
标注让模型知道分寸（见 :mod:`services.jargon.meme_safety`）。唯一的剔除是身份污染守卫，
而且只剔除命中的那一句。用梗提示（主动建议用的梗）另有更窄的挑选范围。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from ..identity_safety import is_identity_contamination
except ImportError:  # pragma: no cover - 顶层导入回退
    from services.identity_safety import is_identity_contamination

from .holyman_assets import CORE_CURATED_PHRASES, content_entries, is_valid_phrase_entry
from .meme_safety import SENSITIVITY_LABELS, sensitivity_note, sensitivity_tags, strip_identity_contamination

logger = logging.getLogger("astrbot")

ENCYCLOPEDIA_FILE = "meme_encyclopedia.json"
_SKILL_DIR = "神人.skill"
# 查询工具检索的原文段落来源；_persona/ 是风格指令，刻意不在这里。
SEGMENT_SOURCES: tuple[str, ...] = (
    "_knowledge/gaming.md",
    "_knowledge/internet-culture.md",
    "_quotes/iconic.md",
    "_quotes/internal.md",
)
CORPUS_SOURCE = "神言.txt"

SAFETY_LEVELS = frozenset({"safe_reference", "understand_only"})
CATEGORIES: dict[str, str] = {
    "gaming": "游戏",
    "internet-culture": "网络文化",
    "copypasta": "复制粘贴文案",
    "catchphrase": "口头禅",
    "abstract-rhetoric": "抽象修辞",
    "acg": "二次元",
}
UNDERSTAND_ONLY_SUFFIX = "（仅供理解，不建议主动使用）"
LOOKUP_MAX_CHARS = 600
LOOKUP_MAX_ENTRIES = 2
LOOKUP_MAX_SNIPPETS = 2
_SNIPPET_WIDTH = 110
_MAX_QUERY_CHARS = 40
# is_valid_phrase_entry 里按“内容”屏蔽的原因；梗百科词条不受它约束（梗不做内容限制）。
_CONTENT_BLOCK_REASONS = frozenset({"toxic_or_sensitive"})

# 用梗提示的“严肃场合”判断：对方在认真说事、情绪低落或谈到伤病时不提示梗。
_SERIOUS_MARKERS: tuple[str, ...] = (
    "难过", "想哭", "哭了", "崩溃", "抑郁", "焦虑", "想死", "不想活", "活不下去", "轻生",
    "住院", "生病", "病了", "医院", "手术", "去世", "走了", "葬礼", "节哀", "分手", "失恋",
    "出事", "车祸", "救救", "求助", "说正经的", "认真的", "严肃点", "别开玩笑", "不是开玩笑",
)


def _default_assets_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "assets" / "holyman"


def normalize_word(word: Any) -> str:
    """与 ``engine.database.normalize_jargon_word`` 一致：NFKC + casefold + trim。"""
    return unicodedata.normalize("NFKC", str(word or "")).casefold().strip()


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and str(item).strip())


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


@dataclass(frozen=True)
class MemeEntry:
    """一条梗词条（精选或扩充）。"""

    word: str
    meaning: str
    aliases: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    category: str = ""
    safety_level: str = "safe_reference"
    sensitivity: tuple[str, ...] = ()
    source: str = ""
    layer: str = "encyclopedia"
    runtime_match: bool = False
    short_match_ok: bool = False
    match_regex: str = ""
    match_context: tuple[str, ...] = ()
    hint_topics: tuple[str, ...] = ()
    hint_ok: bool = False
    entry_id: str = ""

    @property
    def forms(self) -> tuple[str, ...]:
        return (self.word, *self.aliases)

    @property
    def usable(self) -> bool:
        """可以被建议主动去用：``safe_reference`` 且没有冒犯/敏感标注。"""
        return self.safety_level == "safe_reference" and not self.sensitivity

    def display_meaning(self) -> str:
        note = sensitivity_note(self.sensitivity)
        if note:
            return f"{self.meaning}{note}"
        if self.safety_level != "safe_reference":
            return f"{self.meaning}{UNDERSTAND_ONLY_SUFFIX}"
        return self.meaning

    def runtime_payload(self) -> dict[str, Any]:
        """并入 HolymanReference 的运行时条目（只带释义与匹配规则，不带例句）。"""
        return {
            "meaning": self.display_meaning(),
            "category": self.category,
            "source": "holyman_encyclopedia",
            "kind": "encyclopedia",
            "confidence": 0.85,
            "safety_level": self.safety_level,
            "sensitivity": list(self.sensitivity),
            "layer": "catchphrase",
            "reference_only": True,
            "runtime_match": True,
            "match_regex": self.match_regex,
            "match_context": list(self.match_context),
            "entry_id": self.entry_id,
        }

    def rule_matches(self, text: str) -> bool:
        return phrase_rule_matches(self.match_regex, self.match_context, text)


def phrase_rule_matches(match_regex: str, match_context: tuple[str, ...] | list[str], text: str) -> bool:
    """词条的附加匹配规则：正则（在原文上找，忽略大小写）与上下文词（至少出现一个）。"""
    raw = str(text or "")
    if match_regex:
        try:
            if re.search(match_regex, raw, re.IGNORECASE) is None:
                return False
        except re.error:
            return True
    if match_context:
        lowered = raw.lower()
        if not any(str(word).lower() in lowered for word in match_context):
            return False
    return True


@dataclass
class EncyclopediaLoad:
    entries: list[MemeEntry] = field(default_factory=list)
    supplements: dict[str, MemeEntry] = field(default_factory=dict)
    rejected: list[dict[str, str]] = field(default_factory=list)
    identity_removed: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _strip_texts(texts: tuple[str, ...]) -> tuple[tuple[str, ...], int]:
    kept: list[str] = []
    removed = 0
    for text in texts:
        cleaned, count = strip_identity_contamination(text)
        removed += count
        if cleaned.strip():
            kept.append(cleaned)
    return tuple(kept), removed


def parse_encyclopedia_entry(raw: Any) -> tuple[MemeEntry | None, str, int]:
    """校验一条扩充词条；返回 ``(entry, "ok", 剔除的身份污染句数)`` 或 ``(None, 原因, 0)``。

    不按内容拒绝。身份污染只剔除命中的句子（释义、别名、例句各自处理），词条本身保留。
    """
    if not isinstance(raw, dict):
        return None, "not_an_object", 0
    word = str(raw.get("word") or "").strip()
    supplement = raw.get("supplement") is True
    if not word or len(word) > 30:
        return None, "invalid_word", 0
    meaning = _compact(raw.get("meaning") or "")
    if not supplement and (len(meaning) < 8 or len(meaning) > 300):
        return None, "invalid_meaning", 0
    safety_level = str(raw.get("safety_level") or "safe_reference")
    if safety_level not in SAFETY_LEVELS:
        return None, "invalid_safety_level", 0
    category = str(raw.get("category") or "")
    if category not in CATEGORIES:
        return None, "invalid_category", 0
    sensitivity = _str_list(raw.get("sensitivity"))
    if any(tag not in SENSITIVITY_LABELS for tag in sensitivity):
        return None, "invalid_sensitivity", 0
    examples = _str_list(raw.get("examples"))
    if any(len(example) > 200 for example in examples):
        return None, "example_too_long", 0
    source = str(raw.get("source") or "")
    if not source.startswith((f"{_SKILL_DIR}/_knowledge/", f"{_SKILL_DIR}/_quotes/")):
        return None, "source_not_knowledge_or_quotes", 0
    match_regex = str(raw.get("match_regex") or "")
    if match_regex:
        try:
            re.compile(match_regex)
        except re.error:
            return None, "invalid_match_regex", 0

    meaning, meaning_removed = strip_identity_contamination(meaning)
    if not supplement and len(meaning) < 8:
        return None, "meaning_only_identity_contamination", 0
    aliases = tuple(alias for alias in _str_list(raw.get("aliases")) if not is_identity_contamination(alias))
    alias_removed = len(_str_list(raw.get("aliases"))) - len(aliases)
    examples, example_removed = _strip_texts(examples)
    removed = meaning_removed + alias_removed + example_removed
    if is_identity_contamination(word):
        return None, "word_identity_contamination", 0

    entry = MemeEntry(
        word=word,
        meaning=meaning,
        aliases=aliases,
        examples=examples,
        category=category,
        safety_level=safety_level,
        sensitivity=sensitivity,
        source=source,
        layer="supplement" if supplement else "encyclopedia",
        runtime_match=raw.get("runtime_match") is True and not supplement,
        short_match_ok=raw.get("short_match_ok") is True,
        match_regex=match_regex,
        match_context=_str_list(raw.get("match_context")),
        hint_topics=_str_list(raw.get("hint_topics")),
        hint_ok=raw.get("hint_ok") is True and not supplement,
        entry_id=str(raw.get("id") or ""),
    )
    if supplement:
        if word not in CORE_CURATED_PHRASES:
            return None, "supplement_requires_core_phrase", 0
        return entry, "ok", removed
    if word in CORE_CURATED_PHRASES:
        return None, "duplicate_core_phrase_use_supplement", 0
    if entry.hint_ok and (not entry.usable or not entry.hint_topics):
        return None, "hint_requires_usable_entry_and_topics", 0
    if entry.runtime_match:
        ok, reason = is_valid_phrase_entry(word, {"meaning": meaning, "kind": "curated_phrase"})
        # clean_word 会剥掉开头数字（「8u」「749局」），纯字母词会被当成实体名：这两类要人工确认
        # 特异性（short_match_ok）；按内容屏蔽的原因不适用于梗百科。噪声、残句等质量问题仍拒绝。
        exempt = reason in _CONTENT_BLOCK_REASONS or (
            entry.short_match_ok and len(word) >= 2 and reason in {"entity_only", "empty_or_short"}
        )
        if not ok and not exempt:
            return None, f"runtime_word_{reason}", 0
    return entry, "ok", removed


def load_encyclopedia(path: str | Path | None = None) -> EncyclopediaLoad:
    """读取扩充词条文件；文件缺失或损坏时返回空结果（不影响精选词条）。"""
    target = Path(path) if path else _default_assets_dir() / ENCYCLOPEDIA_FILE
    result = EncyclopediaLoad()
    if not target.exists():
        return result
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[WaveMemory] 梗百科词条文件读取失败 %s: %s", target, exc)
        return result
    if not isinstance(payload, dict):
        return result
    result.meta = dict(payload.get("_meta") or {}) if isinstance(payload.get("_meta"), dict) else {}
    seen: set[str] = set()
    for raw in payload.get("entries") or []:
        entry, reason, removed = parse_encyclopedia_entry(raw)
        word = str(raw.get("word") or "") if isinstance(raw, dict) else ""
        if entry is None:
            result.rejected.append({"word": word, "reason": reason})
            continue
        if removed:
            result.identity_removed.append({"word": word, "sentences": removed})
        if entry.layer == "supplement":
            result.supplements[entry.word] = entry
            continue
        if entry.word in seen:
            result.rejected.append({"word": word, "reason": "duplicate_word"})
            continue
        seen.add(entry.word)
        result.entries.append(entry)
    if result.rejected:
        logger.warning("[WaveMemory] 梗百科有 %d 条词条格式不对，未载入: %s", len(result.rejected), result.rejected[:5])
    return result


@dataclass(frozen=True)
class SourceSegment:
    text: str
    source: str
    title: str = ""


@dataclass(frozen=True)
class CorpusItem:
    item_id: str
    text: str


def _strip_frontmatter(lines: list[str]) -> list[str]:
    if lines and lines[0].strip() == "---":
        close = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if close is not None:
            return lines[close + 1:]
    return lines


def _clean_segment_line(line: str) -> str:
    text = line.strip()
    text = re.sub(r"^\d+\.\s*", "", text)
    text = text.lstrip(">").strip()
    text = re.sub(r"^[-*]\s+", "", text)
    text = text.replace("**", "")
    return text.strip()


def parse_segments(text: str, source: str) -> list[SourceSegment]:
    """把 markdown 切成段落：空行、代码围栏与标题都是段落边界。"""
    segments: list[SourceSegment] = []
    title = ""
    buffer: list[str] = []

    def flush() -> None:
        body = _compact(" ".join(part for part in buffer if part))
        buffer.clear()
        if len(body) >= 2:
            segments.append(SourceSegment(text=body, source=source, title=title))

    for raw in _strip_frontmatter(str(text or "").splitlines()):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped == "---" or not stripped:
            flush()
            continue
        if stripped.startswith("#"):
            flush()
            title = stripped.lstrip("#").strip()
            continue
        if stripped.startswith("//"):
            continue
        cleaned = _clean_segment_line(stripped)
        if cleaned:
            buffer.append(cleaned)
    flush()
    return segments


def _excerpt(text: str, needle: str, width: int = _SNIPPET_WIDTH) -> str:
    compact = _compact(text)
    if len(compact) <= width:
        return compact
    index = compact.lower().find(needle.lower()) if needle else -1
    if index < 0:
        return compact[: width - 1] + "…"
    start = max(0, index - width // 3)
    end = min(len(compact), start + width)
    start = max(0, end - width)
    snippet = compact[start:end]
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(compact) else "")


def _contains(text: str, needle: str) -> bool:
    """不区分大小写、不计空白差异的包含判断（原文里常夹换行和空格）。"""
    hay = _compact(text).lower()
    target = _compact(needle).lower()
    if not target:
        return False
    return target in hay or target.replace(" ", "") in hay.replace(" ", "")


def _source_label(source: str) -> str:
    if source == CORPUS_SOURCE:
        return "神言语料"
    if "/_quotes/" in source or source.startswith("_quotes/"):
        return "神人包语录"
    if "/_knowledge/" in source or source.startswith("_knowledge/"):
        return "神人包知识"
    return "词条例句"


def _pattern_match(form: str, text: str) -> bool:
    """支持「动了XX的蛋糕」这类带占位符的词形。"""
    if "XX" not in form:
        return False
    pattern = re.escape(form).replace("XX", ".{1,12}")
    return re.search(pattern, text) is not None


@dataclass
class MemeLookupResult:
    query: str
    entries: list[MemeEntry] = field(default_factory=list)
    snippets: list[dict[str, Any]] = field(default_factory=list)
    # 因身份污染守卫剔除的句数（唯一的剔除规则）
    identity_removed: int = 0

    @property
    def found(self) -> bool:
        return bool(self.entries or self.snippets)

    def render(self, max_chars: int = LOOKUP_MAX_CHARS) -> str:
        query = self.query
        if not self.found:
            if self.identity_removed:
                return _clip(
                    f"查到与「{query}」相关的内容，但只剩改写 Bot 身份的句子，已按身份安全规则略去。"
                    "不要自行编造释义。",
                    max_chars,
                )
            return _clip(f"梗百科里没有查到「{query}」。不要自行编造释义，可以直接问对方是什么意思。", max_chars)

        header = f"【梗百科】「{query}」"
        footer_parts = []
        if self.identity_removed:
            footer_parts.append(f"（已按身份安全规则略去 {self.identity_removed} 句改写 Bot 身份的内容）")
        if self.entries:
            footer_parts.append("以上供理解；想接梗可以用，不是必须用，标注“理解即可”“仅供理解”的别主动用。")
        else:
            footer_parts.append("词典里没有这个词条，以上只是相关文案：释义请结合上下文理解，不要自行编造。")
        footer = "".join(footer_parts)

        body: list[str] = []
        for entry in self.entries:
            label = CATEGORIES.get(entry.category, entry.category or "梗")
            body.append(f"· {entry.word}（{label}）：{entry.display_meaning()}")
        if self.snippets:
            body.append("例句/相关文案：")
            for index, snippet in enumerate(self.snippets, start=1):
                note = sensitivity_note(snippet.get("tags") or ())
                label = snippet["label"] + (f"；{note[1:-1]}" if note else "")
                body.append(f"{index}.「{snippet['text']}」（{label}）")

        # 头尾固定，正文按顺序装入剩余篇幅；装不下的行截断，剩余不足一行就停。
        available = max_chars - len(header) - len(footer) - 2
        kept: list[str] = []
        for line in body:
            joined = len("\n".join(kept))
            room = available - joined - (1 if kept else 0)
            if room < 12:
                break
            if len(line) > room:
                line = line[: room - 1] + "…"
            kept.append(line)
        if kept and kept[-1] == "例句/相关文案：":
            kept.pop()
        return _clip("\n".join([header, *kept, footer]), max_chars)


def _clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


class MemeKnowledgeBase:
    """梗百科只读知识库。文件在首次使用时读取并缓存。"""

    def __init__(self, assets_dir: str | Path | None = None) -> None:
        self.assets_dir = Path(assets_dir) if assets_dir else _default_assets_dir()
        self._lock = threading.Lock()
        self._encyclopedia: EncyclopediaLoad | None = None
        self._curated: list[MemeEntry] | None = None
        self._segments: list[SourceSegment] | None = None
        self._corpus: list[CorpusItem] | None = None

    # ── 数据加载 ──────────────────────────────────────────────

    def encyclopedia(self) -> EncyclopediaLoad:
        with self._lock:
            if self._encyclopedia is None:
                self._encyclopedia = load_encyclopedia(self.assets_dir / ENCYCLOPEDIA_FILE)
            return self._encyclopedia

    def encyclopedia_entries(self) -> list[MemeEntry]:
        return list(self.encyclopedia().entries)

    def curated_entries(self) -> list[MemeEntry]:
        supplements = self.encyclopedia().supplements
        with self._lock:
            if self._curated is None:
                self._curated = self._load_curated(supplements)
            return list(self._curated)

    def _load_curated(self, supplements: dict[str, MemeEntry]) -> list[MemeEntry]:
        phrases: dict[str, Any] = {word: dict(payload) for word, payload in CORE_CURATED_PHRASES.items()}
        phrases_file = self.assets_dir / "phrases.json"
        if phrases_file.exists():
            try:
                loaded = json.loads(phrases_file.read_text(encoding="utf-8"))
                for word, payload in content_entries(loaded if isinstance(loaded, dict) else {}).items():
                    if word in phrases or not isinstance(payload, dict):
                        continue
                    if payload.get("layer", "catchphrase") != "catchphrase" or payload.get("runtime_match") is not True:
                        continue
                    phrases[word] = payload
            except Exception as exc:
                logger.debug("[WaveMemory] phrases.json 读取失败: %s", exc)
        entries = []
        for word, payload in phrases.items():
            meaning = _compact(payload.get("meaning") or payload.get("explanation") or "")
            if not meaning:
                continue
            meaning, _ = strip_identity_contamination(meaning)
            if not meaning:
                continue
            # 精选词条的例句与别名由扩充文件里的 supplement 条目补充；释义始终以精选为准。
            extra = supplements.get(word)
            entries.append(MemeEntry(
                word=word,
                meaning=meaning,
                aliases=extra.aliases if extra else (),
                examples=extra.examples if extra else (),
                category=str(payload.get("category") or ""),
                safety_level="safe_reference",
                sensitivity=extra.sensitivity if extra else (),
                source=(extra.source if extra else str(payload.get("source") or "curated/core")),
                layer="curated",
                runtime_match=True,
            ))
        return entries

    def entries(self) -> list[MemeEntry]:
        return [*self.curated_entries(), *self.encyclopedia_entries()]

    def segments(self) -> list[SourceSegment]:
        with self._lock:
            if self._segments is None:
                segments: list[SourceSegment] = []
                root = self.assets_dir / "raw" / _SKILL_DIR
                for rel in SEGMENT_SOURCES:
                    path = root / rel
                    if not path.exists():
                        continue
                    try:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    segments.extend(parse_segments(text, f"{_SKILL_DIR}/{rel}"))
                self._segments = segments
            return list(self._segments)

    def corpus(self) -> list[CorpusItem]:
        with self._lock:
            if self._corpus is None:
                self._corpus = self._load_corpus()
            return list(self._corpus)

    def _load_corpus(self) -> list[CorpusItem]:
        raw_file = self.assets_dir / "raw" / CORPUS_SOURCE
        items: list[CorpusItem] = []
        if raw_file.exists():
            try:
                payload = json.loads(raw_file.read_text(encoding="utf-8"))
                rows = payload.get("items", []) if isinstance(payload, dict) else payload
                for index, row in enumerate(rows if isinstance(rows, list) else [], start=1):
                    text = row.get("text", "") if isinstance(row, dict) else str(row or "")
                    item_id = str(row.get("id", index)) if isinstance(row, dict) else str(index)
                    if str(text).strip():
                        items.append(CorpusItem(item_id=item_id, text=str(text).strip()))
                return items
            except Exception as exc:
                logger.debug("[WaveMemory] 神言.txt 读取失败，改读 corpus.json: %s", exc)
        corpus_file = self.assets_dir / "corpus.json"
        if corpus_file.exists():
            try:
                rows = json.loads(corpus_file.read_text(encoding="utf-8"))
                for index, row in enumerate(rows if isinstance(rows, list) else [], start=1):
                    text = row.get("text", "") if isinstance(row, dict) else str(row or "")
                    if str(text).strip():
                        items.append(CorpusItem(item_id=str(index), text=str(text).strip()))
            except Exception as exc:
                logger.debug("[WaveMemory] corpus.json 读取失败: %s", exc)
        return items

    # ── 运行时匹配（黑话理解参考） ─────────────────────────────

    def runtime_phrases(self) -> dict[str, dict[str, Any]]:
        return {entry.word: entry.runtime_payload() for entry in self.encyclopedia_entries() if entry.runtime_match}

    def short_match_words(self) -> set[str]:
        return {entry.word for entry in self.encyclopedia_entries() if entry.runtime_match and entry.short_match_ok}

    # ── 查询工具 ──────────────────────────────────────────────

    def lookup(self, query: str) -> MemeLookupResult:
        normalized = self._normalize_query(query)
        result = MemeLookupResult(query=normalized)
        if not normalized:
            return result

        scored: list[tuple[float, int, MemeEntry]] = []
        for order, entry in enumerate(self.entries()):
            score = self._entry_score(entry, normalized)
            if score > 0:
                scored.append((score, -order, entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        result.entries = [entry for _, _, entry in scored[:LOOKUP_MAX_ENTRIES]]

        needles = [normalized]
        for entry in result.entries:
            for form in entry.forms:
                if len(form) >= 2 and form not in needles and "XX" not in form:
                    needles.append(form)

        def already_shown(text: str) -> bool:
            # 例句本身摘自语录/语料：同一段原文不重复给出。
            compact = _compact(text)
            return any(head and head in compact for head in (self._snippet_head(s["text"]) for s in result.snippets))

        def add_snippet(text: str, source: str, needle: str, *, tags: tuple[str, ...] = ()) -> None:
            cleaned, removed = strip_identity_contamination(text)
            result.identity_removed += removed
            if not cleaned.strip():
                return
            excerpt = _excerpt(cleaned, needle)
            merged = tuple(dict.fromkeys([*tags, *sensitivity_tags(cleaned)]))
            result.snippets.append({"text": excerpt, "source": source, "label": _source_label(source), "tags": merged})

        # 1) 词条自带例句
        for entry in result.entries:
            for example in entry.examples[:1]:
                if len(result.snippets) < LOOKUP_MAX_SNIPPETS:
                    add_snippet(example, entry.source, entry.word, tags=entry.sensitivity)

        # 2) 神人包知识与语录原文段落；3) 神言语料。原文未经整理，自动加内容标注。
        for pool in (
            [(segment.text, segment.source) for segment in self.segments()],
            [(item.text, CORPUS_SOURCE) for item in self.corpus()],
        ):
            for text, source in pool:
                if len(result.snippets) >= LOOKUP_MAX_SNIPPETS:
                    break
                needle = next((n for n in needles if _contains(text, n)), "")
                if not needle or already_shown(text):
                    continue
                add_snippet(text, source, needle)
        return result

    @staticmethod
    def _snippet_head(snippet: str) -> str:
        """片段开头一小段（去掉省略号），用来判断是不是同一段原文。"""
        body = _compact(snippet).strip("…").strip("\"“”「」『』 ")
        head = re.split(r"……|…", body, maxsplit=1)[0].strip("\"“”「」『』 ")
        return head[:14] if len(head) >= 8 else ""

    @staticmethod
    def _normalize_query(query: Any) -> str:
        text = _compact(str(query or ""))
        stripped = text.strip(" \"'“”‘’「」『』《》【】[]()（）")
        return (stripped or text)[:_MAX_QUERY_CHARS]

    @staticmethod
    def _entry_score(entry: MemeEntry, query: str) -> float:
        best = 0.0
        lowered_query = query.lower()
        for form in entry.forms:
            lowered = form.lower()
            if not lowered:
                continue
            if lowered == lowered_query:
                best = max(best, 100.0)
            elif _pattern_match(form, query):
                best = max(best, 85.0)
            elif len(lowered) >= 2 and lowered in lowered_query:
                best = max(best, 70.0 + min(len(lowered), 20))
            elif len(lowered_query) >= 2 and lowered_query in lowered:
                best = max(best, 50.0 + 20.0 * len(lowered_query) / max(len(lowered), 1))
        return best

    # ── 用梗提示 ──────────────────────────────────────────────

    def hint_candidates(self, message: str, *, max_items: int = 2) -> list[MemeEntry]:
        """话题命中时返回至多 ``max_items`` 个适合主动用的梗。

        这是“建议用”的范围，比理解范围窄：只挑 ``safe_reference``、没有冒犯/敏感标注、
        人工标了 ``hint_ok`` 的词条；对方在认真说事或情绪低落时一律不提示。
        """
        text = str(message or "")
        lowered = text.lower()
        limit = max(0, min(int(max_items), 2))
        if not lowered.strip() or limit == 0 or is_serious_context(text):
            return []
        scored: list[tuple[int, int, MemeEntry]] = []
        for order, entry in enumerate(self.encyclopedia_entries()):
            if not entry.hint_ok or not entry.usable:
                continue
            # 群友已经说出口的梗交给黑话理解参考解释，这里只提示“话题相关、还没人用的梗”。
            if any(form and form.lower() in lowered for form in entry.forms):
                continue
            hits = [topic for topic in entry.hint_topics if topic.lower() in lowered]
            if not hits:
                continue
            scored.append((max(len(topic) for topic in hits), -order, entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in scored[:limit]]


def is_serious_context(message: str) -> bool:
    """对方在认真说事、情绪低落、谈伤病或内容本身敏感时，不主动建议玩梗。"""
    text = str(message or "")
    if any(marker in text for marker in _SERIOUS_MARKERS):
        return True
    if is_identity_contamination(text):
        return True
    return bool(sensitivity_tags(text))


_default_kb: MemeKnowledgeBase | None = None
_default_lock = threading.Lock()


def get_meme_knowledge_base() -> MemeKnowledgeBase:
    global _default_kb
    with _default_lock:
        if _default_kb is None:
            _default_kb = MemeKnowledgeBase()
        return _default_kb


def reset_meme_knowledge_base() -> None:
    """测试用：丢弃缓存的默认知识库。"""
    global _default_kb
    with _default_lock:
        _default_kb = None


__all__ = [
    "CATEGORIES",
    "LOOKUP_MAX_CHARS",
    "MemeEntry",
    "MemeKnowledgeBase",
    "MemeLookupResult",
    "SEGMENT_SOURCES",
    "get_meme_knowledge_base",
    "is_serious_context",
    "load_encyclopedia",
    "normalize_word",
    "parse_encyclopedia_entry",
    "parse_segments",
    "phrase_rule_matches",
    "reset_meme_knowledge_base",
]
