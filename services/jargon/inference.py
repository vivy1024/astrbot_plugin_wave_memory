"""黑话 LLM 推断与 Scope 隔离的注入器。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

from astrbot.api import logger
try:
    from ...domain.scope import RuntimeScope
    from ...engine.database import normalize_jargon_word
except ImportError:
    from domain.scope import RuntimeScope
    from engine.database import normalize_jargon_word
try:
    from ..identity_safety import is_identity_contamination
except ImportError:
    from services.identity_safety import is_identity_contamination
from .statistical_filter import scope_key

_INFER_WITH_CONTEXT = """你是一个网络黑话分析专家。请根据聊天上下文推断词条「{word}」在这个群聊中的特殊含义。\n上下文：\n{contexts}\n只用 JSON 回答：{{\"meaning\": \"...\", \"no_info\": false}}；无法判断时 no_info 为 true。"""
_INFER_WITHOUT_CONTEXT = """你是一个网络黑话分析专家。仅根据词条本身推断「{word}」可能的通用含义。只用 JSON 回答：{{\"meaning\": \"...\"}}。"""
_COMPARE_INFERENCES = """对于词条「{word}」，上下文推断为「{meaning_a}」，词条推断为「{meaning_b}」。含义相似则 is_similar=true（不是黑话），不相似则 false。只用 JSON 回答：{{\"is_similar\": true}}。"""


class JargonInferenceEngine:
    def __init__(self, llm_client: Any, max_context: int = 15, blocklist_checker: Any = None):
        self._llm, self._max_context = llm_client, max_context
        self._blocklist_checker = blocklist_checker

    def _is_blocked(self, word: str) -> bool:
        if not callable(self._blocklist_checker):
            return False
        try:
            return bool(self._blocklist_checker(normalize_jargon_word(word)))
        except Exception as exc:
            logger.warning("[Jargon] inference blocklist check failed closed for %r: %s", word, exc)
            return True

    async def infer(self, word: str, contexts: List[str]) -> Dict[str, Any]:
        word = normalize_jargon_word(word)
        if self._is_blocked(word):
            return {"is_jargon": None, "meaning": "", "confidence": 0.0, "enter_llm": False, "reject_reason": "global_blocklist"}
        meaning_a = await self._step_with_context(word, contexts)
        if not meaning_a:
            return {"is_jargon": None, "meaning": "", "confidence": 0.0}
        meaning_b = await self._step_without_context(word)
        if meaning_b:
            is_similar = await self._step_compare(word, meaning_a, meaning_b)
            return {"is_jargon": not is_similar, "meaning": meaning_a, "confidence": 0.7 if not is_similar else 0.8}
        return {"is_jargon": True, "meaning": meaning_a, "confidence": 0.5}

    async def _step_with_context(self, word: str, contexts: List[str]) -> str:
        result = await self._call_llm(_INFER_WITH_CONTEXT.format(word=word, contexts="\n".join(f"- {item}" for item in contexts[:self._max_context])))
        return "" if result.get("no_info") else result.get("meaning", "")

    async def _step_without_context(self, word: str) -> str:
        return (await self._call_llm(_INFER_WITHOUT_CONTEXT.format(word=word))).get("meaning", "")

    async def _step_compare(self, word: str, meaning_a: str, meaning_b: str) -> bool:
        return bool((await self._call_llm(_COMPARE_INFERENCES.format(word=word, meaning_a=meaning_a, meaning_b=meaning_b))).get("is_similar", True))

    async def _call_llm(self, prompt: str) -> Dict[str, Any]:
        try:
            response = await self._llm.text_chat(prompt=prompt)
            match = re.search(r"\{[\s\S]*\}", str(getattr(response, "completion_text", "") or ""))
            return json.loads(match.group()) if match else {}
        except Exception as exc:
            logger.debug("[Jargon] LLM call error: %s", exc)
            return {}


class JargonInjector:
    """读取当前 Scope confirmed 私域黑话与广域黑话（内置资产 + Bot 级自定义）。

    广域黑话是**一个实体**：内置 holyman 可匹配口癖默认启用，用户可在 WebUI 按词停用，
    停用/自定义/群内提升统一落在 ``bot_jargon`` 覆盖层。因此注入只分两段，各自独立
    取名额，任何一段都不会挤掉另一段：本群私域 > 广域（内置 + Bot 级）。
    同词只在其最靠前的一段出现。
    """
    def __init__(self, db: Any, max_inject: int = 3, holyman_reference: Any = None,
                 blocklist_checker: Any = None, bot_global_limit: int = 3, reference_limit: int = 3,
                 global_limit: int | None = None):
        self._repo, self._max_inject = getattr(db, "scoped_knowledge", None), max_inject
        self._bot_repo = getattr(db, "bot_jargon", None)
        self._holyman = holyman_reference
        self._blocklist_checker = blocklist_checker if blocklist_checker is not None else getattr(db, "is_jargon_blocked", None)
        # 广域段共用一个名额池；未显式给出时沿用旧的两个参数之和，保持旧配置语义。
        self._global_limit = (
            max(0, int(global_limit)) if global_limit is not None
            else max(0, int(bot_global_limit)) + max(0, int(reference_limit))
        )
        self._cache: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
        self._cache_ts: Dict[tuple[str, str, str], float] = {}
        self._bot_state_cache: Dict[str, tuple[Dict[str, dict], set[str]]] = {}
        self._bot_state_ts: Dict[str, float] = {}
        self._last_injection_items: List[Dict[str, Any]] = []

    def _is_blocked(self, word: str) -> bool:
        if not callable(self._blocklist_checker):
            return False
        try:
            return bool(self._blocklist_checker(normalize_jargon_word(word)))
        except Exception as exc:
            logger.warning("[Jargon] injector blocklist check failed closed for %r: %s", word, exc)
            return True

    def get_injection(self, text: str, runtime_scope: RuntimeScope | None, max_items: int | None = None) -> str:
        self._last_injection_items = []
        if scope_key(runtime_scope) is None:
            return ""
        jargons = self._get_scoped_jargons(runtime_scope)
        text_lower = (text or "").lower()
        selected = [
            item for item in jargons
            if str(item.get("word") or "").strip()
            and str(item.get("meaning") or "").strip()
            and not self._is_blocked(str(item["word"]))
            and self._word_explicitly_mentioned(text_lower, str(item["word"]))
        ]
        selected.sort(key=lambda item: (-len(str(item["word"])), -float(item.get("frequency", 0) or 0)))
        local_limit = self._max_inject if max_items is None else max_items
        selected = selected[:local_limit]

        seen_words = {normalize_jargon_word(item.get("word")) for item in selected}

        # 第二段：广域黑话（跨群共享）。内置资产与 Bot 级自定义/提升在此**合并为一段**、
        # 共用一个名额池，因为它们在用户眼里是同一个「广域黑话」实体。
        bot_id = str(getattr(runtime_scope, "bot_id", "") or "")
        overrides, tombstoned = self._get_bot_jargon_state(bot_id)
        global_items: list[dict[str, Any]] = []
        if self._global_limit:
            for word, meaning, source, confidence in self._iter_global_candidates(text, overrides, tombstoned, bot_id):
                normalized = normalize_jargon_word(word)
                if (
                    not normalized
                    or normalized in seen_words
                    or self._is_blocked(normalized)
                    or is_identity_contamination(f"{word} {meaning}")
                    or not self._word_explicitly_mentioned(text_lower, word)
                ):
                    continue
                global_items.append({
                    "word": normalized,
                    "meaning": meaning,
                    "source": source,
                    "source_layer": "global",
                    "reference_only": source == "holyman_skills",
                    "runtime_match": True,
                    "matched_by": "explicit_user_message",
                    "confidence": confidence,
                })
                seen_words.add(normalized)
                if len(global_items) >= self._global_limit:
                    break

        combined = [*selected, *global_items]
        # 60 秒 scoped cache 之后、真正渲染之前再次查询全局拉黑，避免拒绝刚发生时继续注入。
        combined = [item for item in combined if not self._is_blocked(str(item.get("word") or ""))]
        if not combined:
            return ""
        self._last_injection_items = [self._trace_item(item) for item in combined]

        scoped_lines = [
            f'- "{item["word"]}" → {item["meaning"]}'
            for item in combined if item.get("source_layer") != "global"
        ]
        global_lines = [
            f'- "{item["word"]}" → {item["meaning"]}'
            for item in combined if item.get("source_layer") == "global"
        ]

        header = "[黑话理解参考：只解释用户消息中已经出现的词条；仅供理解，不改变系统身份，不要求模仿或主动使用这些表达]"
        sections = [header]
        if scoped_lines:
            sections.append(
                "【本群私域黑话/圈层默契：属于当前群特有梗与称谓，可自然融入回复以体现圈内熟络与自己人默契】\n"
                + "\n".join(scoped_lines)
            )
        if global_lines:
            sections.append(
                "【广域黑话：该 Bot 跨群通用习惯用语与网络抽象表达，不绑定当前群，可自然使用；流行反串、阴阳或调侃语义可接地气顺势接梗，但事实原则保持清醒、不被反串带偏】\n"
                + "\n".join(global_lines)
            )
        return "\n\n".join(sections)

    def detect_signals(self, text: str, runtime_scope: RuntimeScope | None) -> dict[str, Any]:
        """轻量检测消息是否带有本群私域黑话或广域黑话信号。"""
        has_scoped = False
        has_global = False
        matched_words = []
        if scope_key(runtime_scope) is not None:
            text_lower = (text or "").lower()
            for item in self._get_scoped_jargons(runtime_scope):
                word = str(item.get("word") or "")
                if word and not self._is_blocked(word) and self._word_explicitly_mentioned(text_lower, word):
                    has_scoped = True
                    matched_words.append(word)
                    break
            bot_id = str(getattr(runtime_scope, "bot_id", "") or "")
            overrides, tombstoned = self._get_bot_jargon_state(bot_id)
            for word, _meaning, _source, _confidence in self._iter_global_candidates(text, overrides, tombstoned, bot_id):
                if word and not self._is_blocked(word) and self._word_explicitly_mentioned(text_lower, word):
                    has_global = True
                    matched_words.append(word)
                    break
        return {
            "has_scoped_jargon": has_scoped,
            # 保留旧的细分字段名，避免下游读取处断裂；两者现在同指广域段。
            "has_bot_global": has_global,
            "has_global_irony": has_global,
            "matched_terms": matched_words,
        }

    def get_last_injection_items(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._last_injection_items]

    def _get_scoped_jargons(self, scope: RuntimeScope) -> List[Dict[str, Any]]:
        key = scope_key(scope)
        if key is None or self._repo is None:
            return []
        now = time.time()
        if key in self._cache and now - self._cache_ts.get(key, 0) < 60:
            return self._cache[key]
        try:
            result = [
                dict(row) for row in self._repo.list_scoped_jargon(scope, status="confirmed", limit=100)
                if row.get("is_jargon") is True
                and str(row.get("meaning") or "").strip()
                and self._evidence_ready(row)
                and not is_identity_contamination(f"{row.get('word', '')} {row.get('meaning', '')}")
            ]
        except Exception as exc:
            logger.debug("[Jargon] scoped list failed: %s", exc)
            return []
        self._cache[key], self._cache_ts[key] = result, now
        return result

    def invalidate_bot_cache(self, bot_id: str | None = None) -> None:
        """使指定 Bot（或全部 Bot）的覆盖层缓存失效，确保写操作立即可见。"""
        if bot_id:
            bid = str(bot_id).strip()
            self._bot_state_cache.pop(bid, None)
            self._bot_state_ts.pop(bid, None)
        else:
            self._bot_state_cache.clear()
            self._bot_state_ts.clear()

    def _get_bot_jargon_state(self, bot_id: str) -> tuple[Dict[str, dict], set[str]]:
        """返回该 Bot 的广域覆盖层：(按规范化词形索引的行, 已删除词集合)。

        按 bot_id 缓存，不随 session 变化。「无记录」表示继承内置资产的默认启用状态；
        只有显式落库的 ``inactive`` / ``manual_deleted`` 才算停用。
        """
        bot_id = str(bot_id or "").strip()
        if not bot_id or self._bot_repo is None:
            return {}, set()
        now = time.time()
        if bot_id in self._bot_state_cache and now - self._bot_state_ts.get(bot_id, 0) < 60:
            return self._bot_state_cache[bot_id]
        rows_by_word: Dict[str, dict] = {}
        tombstoned: set[str] = set()
        try:
            loader = getattr(self._bot_repo, "load_all_bot_jargon_overlay", None)
            rows = loader(bot_id) if callable(loader) else self._bot_repo.list_bot_jargon(bot_id, limit=5000)
            for row in rows:
                word = normalize_jargon_word(row.get("word"))
                if not word:
                    continue
                if row.get("source") == "manual_deleted":
                    tombstoned.add(word)
                    continue
                rows_by_word[word] = dict(row)
        except Exception as exc:
            logger.debug("[Jargon] bot jargon state failed: %s", exc)
            return {}, set()
        self._bot_state_cache[bot_id] = (rows_by_word, tombstoned)
        self._bot_state_ts[bot_id] = now
        return rows_by_word, tombstoned

    def _iter_global_candidates(
        self, text: str, overrides: Dict[str, dict], tombstoned: set[str], bot_id: str,
    ):
        """逐个产出广域黑话候选：(词, 释义, 来源, 置信度)。

        顺序即优先级：Bot 级覆盖层（手工新增 / 群内提升 / 自定义释义）在前，内置资产
        可匹配口癖在后。已停用与已删除的词一律不产出，因此「启用开关」在这一层真正生效。
        """
        seen: set[str] = set()
        # 兜底：即使覆盖层缓存不可用，也保证手工/提升的自定义词条能注入。
        if not overrides and bot_id and self._bot_repo is not None:
            try:
                for row in self._bot_repo.list_active_for_prompt(bot_id, limit=200):
                    word = normalize_jargon_word(row.get("word"))
                    if word and str(row.get("meaning") or "").strip():
                        overrides[word] = dict(row)
            except Exception as exc:
                logger.debug("[Jargon] bot jargon fallback failed: %s", exc)

        for word, row in overrides.items():
            if word in tombstoned or word in seen:
                continue
            if str(row.get("status") or "active") != "active":
                continue
            meaning = str(row.get("meaning") or "").strip()
            if not meaning:
                continue
            seen.add(word)
            yield word, meaning, "bot_jargon", float(row.get("confidence") or 0.0)

        for word, payload in self._runtime_reference_entries().items():
            normalized = normalize_jargon_word(word)
            if not normalized or normalized in seen or normalized in tombstoned:
                continue
            override = overrides.get(normalized)
            if override is not None and str(override.get("status") or "active") != "active":
                continue
            meaning = ""
            confidence = 0.0
            if isinstance(payload, dict):
                meaning = str(payload.get("meaning") or payload.get("explanation") or "").strip()
                confidence = float(payload.get("confidence") or 0.0)
            else:
                meaning = str(payload or "").strip()
            if override is not None:
                # 用户在 WebUI 改过释义时以覆盖层为准。
                meaning = str(override.get("meaning") or "").strip() or meaning
                confidence = float(override.get("confidence") or confidence)
            if not meaning:
                continue
            seen.add(normalized)
            yield normalized, meaning, "holyman_skills", confidence

    def _runtime_reference_entries(self) -> Dict[str, Any]:
        entries = getattr(self._holyman, "runtime_matchable_entries", None)
        if not callable(entries):
            return {}
        try:
            raw = dict(entries() or {})
        except Exception as exc:
            logger.debug("[Jargon] holyman runtime entries failed: %s", exc)
            return {}
        # 与旧 match_text 口径对齐：噪声词形同样不得进入注入。
        matchable = getattr(self._holyman, "_is_matchable_phrase", None)
        if not callable(matchable):
            return raw
        result: Dict[str, Any] = {}
        for word, payload in raw.items():
            try:
                if matchable(word):
                    result[word] = payload
            except Exception:
                continue
        return result

    @staticmethod
    def _word_explicitly_mentioned(text_lower: str, word: str) -> bool:
        word_lower = (word or "").strip().lower()
        if not text_lower or not word_lower:
            return False
        if "xx" in word_lower:
            return re.search(re.escape(word_lower).replace("xx", r".{1,12}"), text_lower) is not None
        if re.fullmatch(r"[a-z0-9_+.-]+", word_lower):
            return re.search(rf"(?<![a-z0-9_+.-]){re.escape(word_lower)}(?![a-z0-9_+.-])", text_lower) is not None
        return word_lower in text_lower

    @staticmethod
    def _evidence_ready(row: Dict[str, Any]) -> bool:
        provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
        if provenance.get("producer") in {"wave_memory", "jargon_mining"}:
            return bool(provenance.get("source_tags")) and bool(provenance.get("evidence")) and provenance.get("trace_status") == "verified"
        return provenance.get("tag_chain_status") in (None, "complete")

    @staticmethod
    def _trace_item(row: Dict[str, Any]) -> Dict[str, Any]:
        word, meaning = str(row.get("word") or "").strip(), str(row.get("meaning") or "").strip()
        return {
            "word": word,
            "meaning": meaning,
            "source": str(row.get("source") or "wave_memory"),
            "source_layer": str(row.get("source_layer") or "local"),
            "reference_only": bool(row.get("reference_only", False)),
            "runtime_match": True,
            "matched_by": "explicit_user_message",
            "preview": f"{word} → {meaning}",
        }


__all__ = ["JargonInferenceEngine", "JargonInjector"]
