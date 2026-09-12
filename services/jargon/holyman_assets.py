"""Layered Holyman jargon assets.

Holyman-skills is a reference corpus, not a flat activatable phrase list.
Only curated phrases should participate in confirmed runtime matching.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOLYMAN_SOURCE = "holyman_skills"

def make_runtime_catchphrase(meaning: str, *, category: str = "catchphrase", confidence: float = 0.92) -> dict[str, Any]:
    return {
        "meaning": meaning,
        "category": category,
        "source": "curated/core",
        "kind": "curated_phrase",
        "confidence": confidence,
        "safety_level": "safe_reference",
        "layer": "catchphrase",
        "reference_only": True,
        "runtime_match": True,
    }


CORE_CURATED_PHRASES: dict[str, dict[str, Any]] = {
    "v我50": make_runtime_catchphrase("长篇铺垫或煽情叙述后突然索要 50 元，常关联疯狂星期四，用来制造荒诞转折。", category="catchphrase", confidence=0.98),
    "叠甲": make_runtime_catchphrase("提前声明立场、限制讨论范围或自我免责，以避免被攻击或误解。", category="internet-culture", confidence=0.96),
    "不是哥们": make_runtime_catchphrase("面对离谱、荒谬或难以接受的内容时使用的吐槽起手式，语气偏惊讶和无语。", confidence=0.95),
    "差不多得了": make_runtime_catchphrase("用于制止过度复读、争论、玩梗或情绪输出，意思是提醒对方适可而止。", confidence=0.95),
    "疯狂星期四": make_runtime_catchphrase("肯德基星期四促销梗，常出现在长篇故事结尾并转向借钱或求 V 的荒诞文案。", category="copypasta", confidence=0.96),
    "你说得对，但是": make_runtime_catchphrase("常见反串或复制粘贴起手式，表面认可对方，随后突然切入夸张传教或长文。", category="copypasta", confidence=0.9),
    "动了XX的蛋糕": make_runtime_catchphrase("把失败或冲突荒诞地归因于触碰了某个群体利益，用于反串阴谋化解释。", category="abstract-rhetoric", confidence=0.86),
    "别急": make_runtime_catchphrase("常用于让对方不要急于反应或破防，也可作为轻度调侃式安抚。", confidence=0.9),
    "那咋了": make_runtime_catchphrase("用冷处理方式回应指责或质疑，表达不在乎、反问或摆烂态度。", confidence=0.9),
    "又幻想了": make_runtime_catchphrase("用于调侃过度脑补、自我代入或不现实的想象。", confidence=0.9),
}

DEFAULT_BLOCKED: dict[str, str] = {
    "你好。": "plain_sentence",
    "对不起，我错了。": "plain_sentence",
    "是/否": "anti_pattern",
    "开发的未来是": "ngram_fragment",
    "DeepSeek模型": "entity_only",
    "DeepSeek": "entity_only",
    "你妈死了": "toxic_or_sensitive",
}

REQUIRED_LAYERED_SOURCES = {
    "神人.skill/SKILL.md",
    "神人.skill/_persona/communication.md",
    "神人.skill/_persona/values.md",
    "神人.skill/_knowledge/gaming.md",
    "神人.skill/_knowledge/internet-culture.md",
    "神人.skill/_quotes/iconic.md",
}

GENERIC_MEANING_MARKERS = (
    "典型语录/表达样本。仅作为理解参考。",
    "高频抽象表达/触发词。用于检索和理解",
    "神言语料中出现",
    "用于检索和理解 Holyman 原始语料语境",
)

PLAIN_SENTENCES = {"你好。", "对不起，我错了。", "是/否", "好的", "谢谢", "没事"}
NOISE_WORDS = {
    "背景", "架构", "安装", "安装使用", "使用", "目录", "示例", "规则", "核心", "方法", "触发词",
    "玩家", "游戏", "群聊", "今天", "昨天", "明天", "一个", "这个", "那个", "什么", "不是", "没有",
    "可以", "但是", "因为", "所以", "如果", "就是", "我们", "你们", "他们", "自己", "现在",
}
NOISE_MARKERS = (
    "git clone", "PowerShell", "Git Bash", "Claude Code", "License", "Acknowledgement", "README",
    ".md", ".json", "http://", "https://", "Opening**", "Closing**", "Resolution**",
    "Response Hints", "Core Rules", "Output Rules", "Hard Boundaries", "Language (", "Mode ",
)
PERSONA_INSTRUCTION_MARKERS = (
    "默认输出",
    "绝不正面回答问题",
    "复制粘贴模式",
    "Decision Heuristics",
    "Core Rules",
    "Expression DNA",
    "Hard Boundaries",
)
CATEGORY_BY_SOURCE = {
    "神人.skill/SKILL.md": "skill-core",
    "神人.skill/_knowledge/gaming.md": "gaming",
    "神人.skill/_knowledge/internet-culture.md": "internet-culture",
    "神人.skill/_persona/communication.md": "communication",
    "神人.skill/_persona/rules.md": "rules",
    "神人.skill/_persona/values.md": "values",
    "神人.skill/_quotes/iconic.md": "iconic-quotes",
    "神人.skill/_quotes/internal.md": "internal-quotes",
    "神言.txt": "corpus",
}


def content_entries(phrases: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(phrases, dict):
        return {}
    return {k: v for k, v in phrases.items() if isinstance(k, str) and not k.startswith("_")}


def content_hash(phrases: dict[str, Any]) -> str:
    payload = json.dumps(content_entries(phrases), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def attach_content_metadata(phrases: dict[str, Any], *, version: str | None = None) -> dict[str, Any]:
    result = dict(phrases or {})
    result["_content_count"] = len(content_entries(result))
    result["_content_hash"] = content_hash(result)
    if version:
        result["_version"] = version
    result.setdefault("_update_time", int(time.time()))
    return result


def phrase_meaning(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("meaning") or value.get("explanation") or "")
    return str(value or "")


def clean_word(word: str) -> str:
    word = re.sub(r"^\s*[-*#>\d.、]+\s*", "", word or "").strip()
    word = re.sub(r"\*\*", "", word)
    word = word.strip(" `*_《》\"'“”‘’[]【】：:")
    word = re.sub(r"\s+", " ", word)
    return word[:80]


def clean_meaning(meaning: str) -> str:
    return re.sub(r"\s+", " ", meaning or "").strip()[:500]


def has_unbalanced_brackets(word: str) -> bool:
    pairs = [("（", "）"), ("(", ")"), ("[", "]"), ("【", "】")]
    return any((word or "").count(left) != (word or "").count(right) for left, right in pairs)


def is_generic_meaning(meaning: str) -> bool:
    return any(marker in (meaning or "") for marker in GENERIC_MEANING_MARKERS)


def is_entity_only(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{5,}", word or ""))


def is_plain_sentence(word: str) -> bool:
    if word in PLAIN_SENTENCES:
        return True
    return bool(re.search(r"[。！？!?]$", word or "") and len(word or "") <= 12)


def is_valid_phrase_entry(word: str, value: Any, blocked: dict[str, str] | None = None) -> tuple[bool, str]:
    blocked = blocked or DEFAULT_BLOCKED
    word = clean_word(word)
    meaning = phrase_meaning(value)
    kind = value.get("kind") if isinstance(value, dict) else "legacy"
    if not word or len(word) < 2:
        return False, "empty_or_short"
    if word in blocked:
        return False, blocked[word]
    if word in NOISE_WORDS:
        return False, "noise_word"
    if any(marker.lower() in word.lower() for marker in NOISE_MARKERS):
        return False, "noise_marker"
    if kind == "corpus_frequency":
        return False, "corpus_frequency"
    if has_unbalanced_brackets(word):
        return False, "truncated"
    if is_plain_sentence(word):
        return False, "plain_sentence"
    if is_entity_only(word):
        return False, "entity_only"
    if is_generic_meaning(meaning):
        return False, "generic_meaning"
    if re.fullmatch(r"[A-Za-z0-9 /_().~↑↓<>=*:-]+", word):
        return False, "entity_only"
    if len(word) > 30 and word not in CORE_CURATED_PHRASES:
        return False, "too_long"
    if not clean_meaning(meaning):
        return False, "empty_meaning"
    return True, "ok"


def make_phrase(word: str, meaning: str, *, category: str, source: str, kind: str = "curated_phrase", confidence: float = 0.72) -> dict[str, Any]:
    return {
        "meaning": clean_meaning(meaning),
        "category": category,
        "source": source,
        "kind": kind,
        "confidence": confidence,
        "safety_level": "safe_reference",
        "layer": "catchphrase",
        "reference_only": True,
        "runtime_match": kind in {"curated_phrase", "manual", "legacy"},
    }


def add_phrase(phrases: dict[str, Any], word: str, meaning: str, *, category: str, source: str, kind: str = "curated_phrase", confidence: float = 0.72, blocked: dict[str, str] | None = None) -> None:
    word = clean_word(word)
    value = make_phrase(word, meaning, category=category, source=source, kind=kind, confidence=confidence)
    ok, _ = is_valid_phrase_entry(word, value, blocked)
    if ok and word not in phrases:
        phrases[word] = value


def parse_curated_phrases(fetched: dict[str, str], existing_phrases: dict[str, Any] | None = None) -> dict[str, Any]:
    blocked = dict(DEFAULT_BLOCKED)
    phrases: dict[str, Any] = {word: dict(value) for word, value in CORE_CURATED_PHRASES.items()}

    for word, value in content_entries(existing_phrases or {}).items():
        normalized = clean_word(word)
        if normalized in phrases:
            continue
        source = value.get("source") if isinstance(value, dict) else "legacy"
        kind = value.get("kind") if isinstance(value, dict) else "legacy"
        # Old generated assets wrongly promoted Holyman skill rules and knowledge concepts
        # into activatable phrases. Keep only true legacy/manual entries here; fresh
        # Holyman markdown remains reference-only via concepts/examples/candidates.
        if isinstance(source, str) and source.startswith("神人.skill/"):
            continue
        if kind in {"bold_term", "colon_term", "corpus_frequency", "quote_term", "corpus_quote"}:
            continue
        ok, _ = is_valid_phrase_entry(normalized, value, blocked)
        if ok:
            meaning = phrase_meaning(value)
            category = value.get("category") if isinstance(value, dict) else "legacy"
            add_phrase(phrases, normalized, meaning, category=category or "legacy", source=source or "legacy", kind=kind or "legacy", blocked=blocked)

    for word, value in CORE_CURATED_PHRASES.items():
        phrases[word] = dict(value)
    return phrases


def parse_concepts(fetched: dict[str, str]) -> list[dict[str, Any]]:
    """抽取只读文化概念。

    Holyman 各类文档的标题层级并不统一：SKILL/_persona 用 `### `，而
    `_knowledge/*.md` 用 `## `。只认 `### ` 会让知识文档整段解析为 0 条
    （manifest 仍记 ok），因此这里同时接受 `## ` 与 `### `。
    """
    concepts: list[dict[str, Any]] = []
    for source, text in (fetched or {}).items():
        if source == "README.md" or source.startswith("神人.skill/_quotes/") or source == "神言.txt":
            continue
        lines = (text or "").splitlines()
        for idx, raw in enumerate(lines):
            line = raw.strip()
            marker = _heading_marker(line)
            if marker is None:
                continue
            title = clean_word(line[marker:])
            if not title or title in NOISE_WORDS:
                continue
            summary_parts = []
            for follow in lines[idx + 1: idx + 5]:
                follow = follow.strip().lstrip("-*").strip()
                if not follow or follow.startswith("#"):
                    break
                summary_parts.append(follow)
            summary = clean_meaning(" ".join(summary_parts) or f"Holyman-skills 中关于“{title}”的文化概念。")
            concepts.append({
                "id": f"{source}.{len(concepts) + 1}",
                "title": title,
                "summary": summary,
                "source": source,
                "tags": [CATEGORY_BY_SOURCE.get(source, "unknown")],
                "confidence": 0.78,
                "source_category": CATEGORY_BY_SOURCE.get(source, "unknown"),
                "layer": "concept",
                "reference_only": True,
                "runtime_match": False,
            })
    return concepts


def _heading_marker(line: str) -> int | None:
    """返回标题标记长度（`## `=3、`### `=4、`#### `=5），非标题返回 None。

    `# ` 一级标题是文档名/大章节，不作为概念标题；过深层级同理不取。
    """
    if not line.startswith("#"):
        return None
    hashes = len(line) - len(line.lstrip("#"))
    if hashes < 2 or hashes > 3:
        return None
    if len(line) <= hashes or line[hashes] != " ":
        return None
    return hashes + 1


def parse_examples(fetched: dict[str, str], phrases: dict[str, Any]) -> list[dict[str, Any]]:
    """抽取只读语录/声音样本。

    语录在各类文档里的载体并不统一：`iconic.md` 用 `> ` 引用，`communication.md`
    用 `- ` 列表，而 `internal.md` 把语录包在 ``` 代码围栏里。只认前两种会让
    `internal.md` 74 行只抽出 1 条，因此这里补上代码围栏内的引用识别。
    """
    examples: list[dict[str, Any]] = []
    terms = list(content_entries(phrases).keys())
    for source, text in (fetched or {}).items():
        if not (source.startswith("神人.skill/_quotes/") or source.endswith("communication.md")):
            continue
        category = CATEGORY_BY_SOURCE.get(source, "unknown")
        in_fence = False
        for raw in (text or "").splitlines():
            line = raw.strip()
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if line.startswith(">"):
                line = line.lstrip(">").strip()
            elif in_fence:
                # 代码围栏内是纯语录：去掉包裹引号即可，不做长度门槛。
                line = line.strip().strip('"“”\'')
            elif line.startswith(("- ", "* ")) and len(line) > 20:
                line = line[2:].strip()
            else:
                continue
            line = line.strip()
            if not line:
                continue
            linked = [term for term in terms if term and term in line][:5]
            examples.append({
                "text": line,
                "linked_terms": linked,
                "category": category,
                "source": source,
                "safe_for_prompt": len(line) <= 80,
                "layer": "quotes_knowledge",
                "reference_only": True,
                "runtime_match": False,
            })
    return examples[:400]


def parse_corpus(corpus_data: str) -> list[dict[str, Any]]:
    texts: list[str] = []
    try:
        parsed = json.loads(corpus_data or "[]")
        items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
        if isinstance(items, list):
            for item in items:
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                if text.strip():
                    texts.append(text.strip())
        elif isinstance(parsed, str) and parsed.strip():
            texts.append(parsed.strip())
    except Exception:
        texts = [line.strip() for line in (corpus_data or "").splitlines() if line.strip()]
    return [
        {
            "id": f"holyman-corpus-{idx:04d}",
            "text": text,
            "source": "神言.txt",
            "length": len(text),
            "tags": ["corpus"],
            "risk_flags": ["reference_only"] + (["long_text"] if len(text) > 120 else []),
            "safe_for_prompt": False,
            "layer": "corpus",
            "reference_only": True,
            "runtime_match": False,
        }
        for idx, text in enumerate(texts, start=1)
    ]


def generate_candidates(corpus: list[Any], phrases: dict[str, Any], blocked: dict[str, str] | None = None) -> list[dict[str, Any]]:
    blocked = blocked or DEFAULT_BLOCKED
    phrase_words = set(content_entries(phrases).keys())
    counter: dict[str, int] = {}
    for item in corpus:
        text = item.get("text", "") if isinstance(item, dict) else str(item)
        for quoted in re.findall(r"[\"“「『]([^\"”」』]{2,24})[\"”」』]", text):
            word = clean_word(quoted)
            if word not in phrase_words and is_valid_phrase_entry(word, {"meaning": "候选", "kind": "candidate"}, blocked)[0]:
                counter[word] = counter.get(word, 0) + 1
        for word in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,12}", text):
            word = clean_word(word)
            if word not in phrase_words and is_valid_phrase_entry(word, {"meaning": "候选", "kind": "candidate"}, blocked)[0]:
                counter[word] = counter.get(word, 0) + 1
    return [
        {
            "id": f"holyman-candidate-{idx:04d}",
            "word": word,
            "reason": "corpus_candidate",
            "count": count,
            "source": "神言.txt",
            "status": "pending_review",
            "reject_reason": "",
            "layer": "candidate",
            "reference_only": True,
            "runtime_match": False,
        }
        for idx, (word, count) in enumerate(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:300], start=1)
    ]


def build_manifest(fetched: dict[str, str], *, remote_version: str = "") -> dict[str, Any]:
    files = []
    for path, content in sorted((fetched or {}).items()):
        data = (content or "").encode("utf-8")
        files.append({
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "parse_status": "ok" if content is not None else "missing",
        })
    return {
        "source": "https://github.com/ykdeso/holyman-skills",
        "remote_version": remote_version,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def _declared_corpus_count(raw_sources: dict[str, str]) -> int | None:
    for name in ("README.md", "神人.skill/_meta/sources.md"):
        text = raw_sources.get(name, "") if isinstance(raw_sources, dict) else ""
        match = re.search(r"神言\.txt[（(]\s*(\d+)\s*条", text or "")
        if match:
            return int(match.group(1))
    return None


def _source_corpus_items_count(raw_sources: dict[str, str]) -> int | None:
    raw = raw_sources.get("神言.txt", "") if isinstance(raw_sources, dict) else ""
    if not raw:
        return None
    parsed = json.loads(raw)
    items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return None
    return sum(1 for item in items if ((item.get("text", "") if isinstance(item, dict) else str(item)).strip()))


def quality_report(assets: dict[str, Any]) -> dict[str, Any]:
    phrases = content_entries(assets.get("phrases") or {})
    blocked = assets.get("blocked") or DEFAULT_BLOCKED
    raw_sources = assets.get("raw_sources") or {}
    declared_corpus_count = _declared_corpus_count(raw_sources)
    try:
        source_corpus_items_count = _source_corpus_items_count(raw_sources)
    except Exception:
        source_corpus_items_count = None
    parsed_corpus_count = len(assets.get("corpus") or [])
    raw_parsed_mismatch = source_corpus_items_count is not None and source_corpus_items_count != parsed_corpus_count
    declared_source_mismatch = declared_corpus_count is not None and source_corpus_items_count is not None and declared_corpus_count != source_corpus_items_count
    errors = {
        "corpus_frequency_in_phrases": 0,
        "generic_meaning_in_phrases": 0,
        "unbalanced_bracket_in_phrases": 0,
        "plain_sentence_in_phrases": 0,
        "blocked_phrase_in_phrases": 0,
        "entity_only_in_phrases": 0,
        "missing_core_terms": 0,
        "missing_layered_sources": 0,
        "persona_instruction_in_phrases": 0,
        "raw_parsed_corpus_mismatch": 1 if raw_parsed_mismatch else 0,
    }
    for word, value in phrases.items():
        meaning = phrase_meaning(value)
        kind = value.get("kind") if isinstance(value, dict) else "legacy"
        haystack = f"{word} {meaning}"
        if any(marker in haystack for marker in PERSONA_INSTRUCTION_MARKERS):
            errors["persona_instruction_in_phrases"] += 1
        if kind == "corpus_frequency":
            errors["corpus_frequency_in_phrases"] += 1
        if is_generic_meaning(meaning):
            errors["generic_meaning_in_phrases"] += 1
        if has_unbalanced_brackets(word):
            errors["unbalanced_bracket_in_phrases"] += 1
        if is_plain_sentence(word):
            errors["plain_sentence_in_phrases"] += 1
        if word in blocked:
            errors["blocked_phrase_in_phrases"] += 1
        if is_entity_only(word):
            errors["entity_only_in_phrases"] += 1
    for word, value in CORE_CURATED_PHRASES.items():
        if word not in phrases or is_generic_meaning(phrase_meaning(phrases.get(word))) or len(phrase_meaning(phrases.get(word))) < 12:
            errors["missing_core_terms"] += 1
    manifest = assets.get("manifest") or {}
    manifest_files = manifest.get("files") if isinstance(manifest, dict) else []
    if manifest_files:
        ok_paths = {
            item.get("path")
            for item in manifest_files
            if isinstance(item, dict) and item.get("parse_status") == "ok"
        }
        errors["missing_layered_sources"] = len(REQUIRED_LAYERED_SOURCES - ok_paths)
    status = "ready" if phrases and all(count == 0 for count in errors.values()) else "blocked"
    if raw_parsed_mismatch:
        corpus_count_note = f"Raw 神言.txt contains {source_corpus_items_count} non-empty items, but corpus.json parsed {parsed_corpus_count}."
    elif declared_source_mismatch:
        corpus_count_note = f"README declares {declared_corpus_count} items; current raw JSON contains {source_corpus_items_count} non-empty items and all were parsed."
    elif source_corpus_items_count is not None:
        corpus_count_note = f"Raw JSON contains {source_corpus_items_count} non-empty items and all were parsed."
    else:
        corpus_count_note = "Corpus source count unavailable; using parsed corpus count only."
    return {
        "status": status,
        "phrases_count": len(phrases),
        "concepts_count": len(assets.get("concepts") or []),
        "examples_count": len(assets.get("examples") or []),
        "corpus_count": parsed_corpus_count,
        "candidates_count": len(assets.get("candidates") or []),
        "blocked_count": len(blocked),
        "declared_corpus_count": declared_corpus_count,
        "source_corpus_items_count": source_corpus_items_count,
        "parsed_corpus_count": parsed_corpus_count,
        "corpus_count_mismatch": bool(raw_parsed_mismatch or declared_source_mismatch),
        "corpus_count_note": corpus_count_note,
        "errors": errors,
        "generated_at": int(time.time()),
    }


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
