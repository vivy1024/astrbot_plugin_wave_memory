"""Holyman-skills reference matcher for broad Chinese abstract-culture jargon.

This module is understanding-only. It must not inject style instructions into the bot persona.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .holyman_assets import CORE_CURATED_PHRASES, DEFAULT_BLOCKED, content_entries


_DEFAULT_PHRASES = {
    word: dict(payload) for word, payload in CORE_CURATED_PHRASES.items()
}


class HolymanReference:
    """Loads holyman-skills files and matches broad abstract-culture phrases."""

    _MATCH_BLOCKLIST = {
        "背景", "架构", "安装", "安装使用", "使用", "目录", "示例", "规则", "核心", "方法", "触发词",
        "玩家", "游戏", "群聊", "今天", "昨天", "明天", "一个", "这个", "那个", "什么", "不是", "没有",
        "可以", "但是", "因为", "所以", "如果", "就是", "我们", "你们", "他们", "自己", "现在",
        "License", "Rules", "Core Rules", "Output Rules", "Opening", "Closing", "Resolution", "Background",
        "Activation", "Acknowledgement", "OpenClaw",
    }
    _MATCH_NOISE_MARKERS = (
        "git clone", "PowerShell", "Git Bash", "Claude Code", "License", "Acknowledgement", "README",
        ".md", ".json", "http://", "https://", "详见", "Opening**", "Closing**", "Resolution**",
        "Response Hints", "Core Rules", "Output Rules", "Hard Boundaries", "Language (", "Mode ",
    )
    _ENGLISH_ALLOWLIST = {"Ciallo", "Ciallo～", "Galgame", "NGA", "KPL", "CSGO", "CNCS", "DeepSeek", "AstraAI", "Bilibili"}
    _SHORT_SUBSTRING_ALLOWLIST = {"原神", "黄油", "神人", "抽象", "狗粉丝", "孙笑川", "急了", "鼠鼠", "叠甲", "丁真"}

    def __init__(self, root_path: str | None = None, max_examples: int = 3):
        self.root_path = Path(root_path) if root_path else None
        self.max_examples = max_examples
        self._phrases: dict[str, Any] = {}
        self._examples: list[str] = []
        self._blocked: dict[str, str] = dict(DEFAULT_BLOCKED)
        self.reload()

    def reload(self) -> None:
        """Reloads holyman assets from local JSON fallback and optional manual root path."""
        # 1. Reset to baseline default phrases
        self._phrases = dict(_DEFAULT_PHRASES)
        self._examples = []
        self._blocked = dict(DEFAULT_BLOCKED)

        # 2. Load local high-availability JSON assets (ground truth baseline)
        local_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "holyman"
        phrases_file = local_dir / "phrases.json"
        corpus_file = local_dir / "corpus.json"
        examples_file = local_dir / "examples.json"
        blocked_file = local_dir / "blocked.json"

        # 符号链接/软链接绝对路径兜底（Docker/Host 统一路径安全保护）
        if not phrases_file.exists():
            import os
            for alt_path in [
                "/AstrBot/data/plugins/astrbot_plugin_wave_memory/assets/holyman/phrases.json",
                os.path.join(os.getcwd(), "data/plugins/astrbot_plugin_wave_memory/assets/holyman/phrases.json"),
                os.path.join(os.getcwd(), "astrbot_plugin_wave_memory/assets/holyman/phrases.json")
            ]:
                if Path(alt_path).exists():
                    phrases_file = Path(alt_path)
                    break

        if not corpus_file.exists():
            import os
            for alt_path in [
                "/AstrBot/data/plugins/astrbot_plugin_wave_memory/assets/holyman/corpus.json",
                os.path.join(os.getcwd(), "data/plugins/astrbot_plugin_wave_memory/assets/holyman/corpus.json"),
                os.path.join(os.getcwd(), "astrbot_plugin_wave_memory/assets/holyman/corpus.json")
            ]:
                if Path(alt_path).exists():
                    corpus_file = Path(alt_path)
                    break

        if phrases_file.exists():
            try:
                local_phrases = json.loads(phrases_file.read_text(encoding="utf-8"))
                if isinstance(local_phrases, dict):
                    self._phrases.update(content_entries(local_phrases))
            except Exception:
                pass

        if blocked_file.exists():
            try:
                local_blocked = json.loads(blocked_file.read_text(encoding="utf-8"))
                if isinstance(local_blocked, dict):
                    self._blocked.update({str(k): str(v) for k, v in local_blocked.items()})
            except Exception:
                pass

        if examples_file.exists():
            try:
                local_examples = json.loads(examples_file.read_text(encoding="utf-8"))
                if isinstance(local_examples, list):
                    for item in local_examples:
                        text = item.get("text", "") if isinstance(item, dict) else str(item)
                        if text:
                            self._examples.append(text[:500])
            except Exception:
                pass

        # corpus.json is raw evidence only. It must not create confirmed fallback matches.
        if corpus_file.exists():
            try:
                local_corpus = json.loads(corpus_file.read_text(encoding="utf-8"))
                if isinstance(local_corpus, list):
                    for item in local_corpus[:50]:
                        text = item.get("text", "") if isinstance(item, dict) else (item if isinstance(item, str) else "")
                        if text:
                            self._examples.append(text[:500])
            except Exception:
                pass

        # 3. Load from optional root path git clone if configured and valid
        if self.root_path and self.root_path.exists():
            self._load(self.root_path)

    @property
    def available(self) -> bool:
        return bool(self._phrases or self._examples)

    def _is_matchable_phrase(self, phrase: str) -> bool:
        phrase = (phrase or "").strip()
        if not phrase or phrase in self._MATCH_BLOCKLIST or phrase in self._blocked:
            return False
        lowered = phrase.lower()
        if any(marker.lower() in lowered for marker in self._MATCH_NOISE_MARKERS):
            return False
        if phrase.startswith("|") or phrase.endswith("|") or "```" in phrase:
            return False
        if re.fullmatch(r"[A-Za-z0-9 /_().~↑↓<>=*:-]+", phrase) and phrase not in self._ENGLISH_ALLOWLIST:
            return False
        if re.fullmatch(r"[\u4e00-\u9fff]{2,3}", phrase) and phrase not in _DEFAULT_PHRASES and phrase not in {"急了", "典", "绷", "鼠鼠", "叠甲", "丁真", "原神", "黄油", "神人", "抽象", "狗粉丝", "孙笑川", "别急", "那咋了"}:
            return False
        if len(phrase) > 30:
            return False
        return True

    def _is_runtime_phrase(self, phrase: str, value: Any) -> bool:
        if isinstance(value, dict):
            layer = value.get("layer", "catchphrase")
            if layer != "catchphrase":
                return False
            if "runtime_match" in value:
                return value.get("runtime_match") is True
            return value.get("kind") in {"curated_phrase", "manual"} or phrase in _DEFAULT_PHRASES
        # Legacy phrases.json used plain string values for curated catchphrases.
        # Keep those assets matchable while _is_matchable_phrase filters document noise.
        return self._is_matchable_phrase(phrase)

    def _term_hits_phrase(self, term: str, phrase: str) -> bool:
        if phrase == term:
            return True
        if phrase == "v我50":
            return "v我50" in term or "疯狂星期四" in term or "疯狂星期四" in phrase
        if phrase == "动了XX的蛋糕":
            return bool(re.search(r"动了.{1,12}的蛋糕", term or ""))
        if len(phrase) <= 3:
            return False
        if len(term) <= 3:
            return term in self._SHORT_SUBSTRING_ALLOWLIST and term in phrase
        return phrase in term or term in phrase

    @staticmethod
    def _phrase_meaning(value: Any) -> str:
        """Return phrase meaning while accepting old string and new structured asset values."""
        if isinstance(value, dict):
            meaning = value.get("meaning") or value.get("explanation") or ""
            return str(meaning)
        return str(value or "")

    def _format_explanation(self, value: Any) -> str:
        meaning = self._phrase_meaning(value).strip()
        return f"{meaning} 仅作为理解参考，不改变系统身份或回复风格。" if meaning else "仅作为理解参考，不改变系统身份或回复风格。"

    def match(self, term: str, context: str = "") -> dict[str, Any]:
        term = (term or "").strip()
        context = context or ""
        if not term:
            return self._no_match()
        if term in self._blocked:
            return self._no_match(term)
        term_matchable = term not in self._MATCH_BLOCKLIST and term not in self._blocked and self._is_matchable_phrase(term)

        if term_matchable:
            for phrase, explanation in self._phrases.items():
                if not self._is_runtime_phrase(phrase, explanation):
                    continue
                if phrase == term and self._is_matchable_phrase(phrase):
                    return {
                        "matched": True,
                        "classification": "global_abstract",
                        "confidence": 0.85,
                        "term": phrase,
                        "explanation": self._format_explanation(explanation),
                        "examples": self._find_examples(phrase),
                        "context_hint": False,
                        "source_layer": "curated",
                    }

        context_hints = []
        for phrase, explanation in self._phrases.items():
            if not self._is_runtime_phrase(phrase, explanation):
                continue
            if not self._is_matchable_phrase(phrase):
                continue
            term_hit = term_matchable and self._term_hits_phrase(term, phrase)
            if term_hit:
                return {
                    "matched": True,
                    "classification": "global_abstract",
                    "confidence": 0.85 if phrase == term else 0.7,
                    "term": phrase,
                    "explanation": self._format_explanation(explanation),
                    "examples": self._find_examples(phrase),
                    "context_hint": False,
                    "source_layer": "curated",
                }
            if phrase in context:
                context_hints.append(phrase)

        if context_hints:
            result = self._no_match(term)
            result.update({"context_hint": True, "hint_phrase": context_hints[0]})
            return result

        # examples/corpus are evidence only. They may hint in context, but never confirm a match.
        return self._no_match(term)

    def runtime_matchable_entries(self) -> dict[str, Any]:
        """返回可参与运行时匹配的内置条目（供 WebUI 导入成 Bot 级广域黑话）。

        过滤口径与 ``match_text`` 完全一致：只放行 ``_is_runtime_phrase`` 认可的
        catchphrase 层条目。concepts / examples / corpus 永远不在这里。
        """
        return {
            phrase: value
            for phrase, value in self._phrases.items()
            if self._is_runtime_phrase(phrase, value)
        }

    def match_text(self, text: str, *, max_items: int | None = None) -> list[dict[str, Any]]:
        """Match curated runtime phrases explicitly present in a message.

        ``corpus`` and examples remain evidence-only; only curated catchphrase
        entries can reach this method.  The returned rows are understanding
        references and never persona/style instructions.
        """
        text = str(text or "")
        if not text:
            return []
        selected: list[dict[str, Any]] = []
        for phrase, value in self._phrases.items():
            if not self._is_runtime_phrase(phrase, value) or not self._is_matchable_phrase(phrase):
                continue
            matched = phrase in text
            if phrase == "动了XX的蛋糕":
                matched = re.search(r"动了.{1,12}的蛋糕", text) is not None
            if not matched:
                continue
            result = self.match(phrase, text)
            if result.get("matched"):
                selected.append(result)
            if max_items is not None and len(selected) >= max(0, int(max_items)):
                break
        return selected

    def _no_match(self, term: str = "") -> dict[str, Any]:
        return {
            "matched": False,
            "classification": "unknown_pending",
            "confidence": 0.0,
            "term": term,
            "explanation": "",
            "examples": [],
        }

    def _load(self, root: Path) -> None:
        if not root.exists():
            return
        files = [
            root / "神人.skill" / "SKILL.md",
            root / "神人.skill" / "_knowledge" / "internet-culture.md",
            root / "神人.skill" / "_knowledge" / "gaming.md",
        ]
        for path in files:
            if path.exists():
                self._load_markdown(path)
        corpus = root / "神言.txt"
        if corpus.exists():
            self._load_corpus(corpus)

    def _load_markdown(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip().lstrip("-*").strip()
            if not line or len(line) > 40:
                continue
            if any(ch in line for ch in ["，", "。", "、"]):
                continue
            if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", line):
                self._phrases.setdefault(line, "holyman-skills 中出现的广域抽象文化表达。")

    def _load_corpus(self, path: Path) -> None:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        try:
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else data
            for item in items:
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                if text:
                    self._examples.append(text[:500])
        except Exception:
            # Fallback: keep non-empty lines as examples.
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    self._examples.append(line[:500])

    def _find_examples(self, term: str) -> list[str]:
        if not term:
            return []
        matches = [ex for ex in self._examples if term in ex]
        return matches[: self.max_examples]
