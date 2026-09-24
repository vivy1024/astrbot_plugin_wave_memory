"""黑话统计预筛：所有运行时状态均以 RuntimeScope 三元组隔离。"""

from __future__ import annotations

try:
    from ...domain import bot_identity
except ImportError:  # top-level import in isolated tests
    from domain import bot_identity

import re
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Set

try:
    from ...domain.scope import RuntimeScope
except ImportError:
    from domain.scope import RuntimeScope
from astrbot.api import logger

try:
    import jieba
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

_STOPWORDS: Set[str] = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她", "吗", "什么", "啊", "呢", "吧", "哦", "嗯", "哈", "呀", "啦", "那", "还", "能", "把", "让", "被", "从", "对", "但", "可以", "这个", "那个", "就是", "没", "来", "出", "想", "做", "里"}
_COMMON_WORDS: Set[str] = {"哈哈", "哈哈哈", "好的", "可以", "谢谢", "不是", "知道", "怎么", "为什么", "因为", "所以", "但是", "然后", "如果", "已经", "可能", "应该", "需要", "觉得", "感觉", "喜欢"}


def scope_key(scope: RuntimeScope | None) -> tuple[str, str, str] | None:
    """返回唯一正式 Jargon 键；非已解析群 Scope 一律拒绝。"""
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        return None
    return (scope.bot_id, scope.session.id, scope.visibility)


class JargonStatisticalFilter:
    """词频统计器；不允许 group_id-only 的内存状态。"""

    def __init__(self, context_keep: int = 10, window_days: int = 7, jieba_threshold: int = 100,
                 weight_idf: float = 0.4, weight_burst: float = 0.3, weight_concentration: float = 0.3,
                 candidate_router: Callable[..., Dict[str, Any]] | None = None):
        self._context_keep, self._window_days, self._jieba_threshold = context_keep, window_days, jieba_threshold
        self._weight_idf, self._weight_burst, self._weight_concentration = weight_idf, weight_burst, weight_concentration
        self._candidate_router = candidate_router
        self._group_freq: Dict[tuple[str, str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._first_seen: Dict[tuple[str, str, str], Dict[str, float]] = defaultdict(dict)
        self._user_freq: Dict[tuple[str, str, str], Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
        self._contexts: Dict[tuple[str, str, str], Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    def feed(self, text: str, scope: RuntimeScope | None, sender_id: str = "", timestamp: float | None = None) -> None:
        key = scope_key(scope)
        if key is None or not _HAS_JIEBA or self._is_bot_sender(sender_id):
            return
        now = timestamp or time.time()
        source_context = {
            "content": text[:300],
            "timestamp": now,
            "sender_id": sender_id or "",
            "bot_id": scope.bot_id if scope else "",
            "session_id": scope.session.id if scope and scope.session else "",
            "visibility": scope.visibility if scope else "",
        }
        group_id = scope.session.conversation_id if scope and scope.session else ""
        for word in self._tokenize(text):
            if self._candidate_router is not None:
                route = self._candidate_router(word, group_id, source_context, [text])
                if not route.get("enter_llm", False):
                    continue
            self._group_freq[key][word] += 1
            self._first_seen[key].setdefault(word, now)
            if sender_id:
                self._user_freq[key][word].add(sender_id)
            contexts = self._contexts[key][word]
            if len(contexts) < self._context_keep:
                contexts.append(dict(source_context))

    def get_candidates(self, scope: RuntimeScope | None, min_freq: int = 5, top_k: int = 20) -> List[Dict[str, Any]]:
        key = scope_key(scope)
        if key is None or not _HAS_JIEBA:
            return []
        now, window_ago = time.time(), time.time() - self._window_days * 86400
        freq, total_scopes = self._group_freq.get(key, {}), max(len(self._group_freq), 1)
        candidates: List[Dict[str, Any]] = []
        for word, count in freq.items():
            first_seen = self._first_seen[key].get(word, now)
            if count < min_freq or first_seen < window_ago or self._is_standard_word(word):
                continue
            import math
            scopes_with_word = sum(1 for values in self._group_freq.values() if word in values)
            idf = math.log(total_scopes / max(scopes_with_word, 1) + 1)
            burst = count / max((now - first_seen) / 86400, 0.1)
            concentration = 1.0 / max(len(self._user_freq[key].get(word, set())), 1)
            contexts = self._contexts[key].get(word, [])[:self._context_keep]
            candidates.append({"word": word, "frequency": count,
                "score": round(self._weight_idf * idf + self._weight_burst * min(burst / 10, 1.0) + self._weight_concentration * concentration, 3),
                "contexts": [ctx.get("content", "") for ctx in contexts], "source_contexts": contexts})
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[:top_k]

    def _tokenize(self, text: str) -> List[str]:
        text = re.sub(r'https?://\S+|@\S+|\[.*?\]', '', text or '')
        result = []
        for word in jieba.cut(text):
            word = word.strip()
            if len(word) >= 2 and word not in _STOPWORDS and word not in _COMMON_WORDS and not re.match(r'^[\d\s\W]+$', word) and not self._is_vocal_noise(word):
                result.append(word)
        return result

    @staticmethod
    def _is_bot_sender(sender_id: str) -> bool:
        sid = str(sender_id or "")
        return sid in bot_identity.bot_sender_ids() or sid.endswith("_archived") or sid.startswith("bot_")

    @staticmethod
    def _is_vocal_noise(word: str) -> bool:
        word = (word or "").strip()
        if re.fullmatch(r"[呜嗷啊哈呵嗯喵汪]+", word) and len(set(word)) <= 4 and len(word) >= 3:
            return True
        chars = [char for char in word if char.strip()]
        return bool(len(word) >= 4 and chars and max(chars.count(char) for char in set(chars)) / len(chars) >= .75)

    def _is_standard_word(self, word: str) -> bool:
        return bool(_HAS_JIEBA and getattr(jieba.dt, 'FREQ', {}).get(word, 0) > self._jieba_threshold)


__all__ = ["JargonStatisticalFilter", "scope_key"]
