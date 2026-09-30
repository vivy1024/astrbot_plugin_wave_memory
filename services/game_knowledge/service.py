"""游戏知识库服务：直播注入的匹配与渲染、工具查询、管理台读写、导入导出、当前在玩的游戏。

一个会玩游戏的人分几层知识，WaveMemory 管其中三层：

- **攻略常识**：按游戏分库，所有 Bot 共用（如缺氧的教程整理）；
- **自己玩出来的经验**：每个 Bot 从自己的游戏经历里写下的经验卡（``services/game_knowledge/lessons.py``），
  同一个库里 ``bot_id`` 不为空的卡，只在这个 Bot 的请求里出现；
- **游玩经历**：Cortico 把游戏事件写回记忆（经验就是从这里想出来的）。存档状态留在 Cortico。

直播时 Cortico 在 ``/context/prepare`` 带上 ``game``，``game_knowledge`` 通道按 ``game.id`` 选库，注入命中的卡
（合计最多 2 张，她自己的经验排在前面）与相关词条。匹配只看卡片自己的触发条件（关键词 + 数值条件，
规则见 ``domain.game_knowledge.card_trigger``），不调用模型、不做向量检索；工具查询用与记忆相同的
中文两字切词全文检索（``engine/db/fts_cjk.py``）。草稿与归档的内容不注入、工具也查不到，只在管理台可见。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    from ...domain.game_knowledge import (
        EXPERIENCE_SOURCE_TYPE,
        GameContext,
        GameKnowledgeError,
        KeywordHaystack,
        KeywordHit,
        TRIGGER_RULE_TEXT,
        auto_item_key,
        card_trigger,
        fold,
        normalize_card_fields,
        normalize_game_fields,
        normalize_term_fields,
        parse_conditions,
        parse_game_context,
        validate_game_id,
        validate_status,
    )
    from ...engine.db import fts_cjk
    from . import importers
except ImportError:  # pragma: no cover - 仓库测试以插件根目录为顶层
    from domain.game_knowledge import (
        EXPERIENCE_SOURCE_TYPE,
        GameContext,
        GameKnowledgeError,
        KeywordHaystack,
        KeywordHit,
        TRIGGER_RULE_TEXT,
        auto_item_key,
        card_trigger,
        fold,
        normalize_card_fields,
        normalize_game_fields,
        normalize_term_fields,
        parse_conditions,
        parse_game_context,
        validate_game_id,
        validate_status,
    )
    from engine.db import fts_cjk
    from services.game_knowledge import importers

logger = logging.getLogger("astrbot")

MAX_INJECT_CARDS = 2
DEFAULT_INJECT_TERMS = 3
MAX_INJECT_TERMS = 5
CARD_POINTS_LIMIT = 5
CARD_PITFALLS_LIMIT = 3
ITEM_CHARS = 90
TERM_CHARS = 90
TOOL_MAX_CHARS = 1200
TOOL_MAX_CARDS = 3
TOOL_MAX_TERMS = 5
ACTIVE_GAME_TTL_SECONDS = 30 * 60
_ACTIVE_MAX_ENTRIES = 512
# 排序权重：当前消息里说到的 > 场景关键词 / 状态值；纯条件卡（如血量低）按条件数计，排在单个场景词前面。
_SOURCE_WEIGHT = {"message": 3.0, "keywords": 2.5, "state": 2.5}
_PURE_CONDITION_WEIGHT = 2.8
_REFINING_CONDITION_WEIGHT = 1.0
# 没满足的「参考」条件：照样触发，但排在对得上的卡后面
_SOFT_FAIL_PENALTY = 1.5
# 她自己吃过的亏比攻略先想起来
_EXPERIENCE_BONUS = 0.5

INJECTION_HEADER = (
    "[游戏攻略参考 · {name}：以下是这个游戏的通用攻略常识，不是你的亲身经历，也不一定适合眼前的局面；"
    "用不用、怎么说由你结合实况自己判断]"
)
EXPERIENCE_HEADER = (
    "[你自己玩出来的经验 · {name}：这些是你以前玩的时候亲身经历过、自己总结的；眼前用不用得上由你判断]"
)
TOOL_FOOTER = "（以上是攻略参考，不是你的亲身经历；想查自己以前玩过的事，用记忆搜索 wave_memory_search）"
TOOL_FOOTER_WITH_EXPERIENCE = (
    "（标了「你自己的经验」的是你亲身经历过、自己总结的；其余是攻略参考，不是你的亲身经历。"
    "想查以前玩过的具体经过，用记忆搜索 wave_memory_search）"
)
_QUERY_STOPWORDS = frozenset({
    "怎么", "怎么办", "如何", "什么", "为什么", "怎样", "可以", "应该", "需要", "这个", "那个", "一下", "我们",
    "你们", "他们", "现在", "还是", "就是", "没有", "不是", "知道", "办法", "攻略", "教程", "一个", "有没有",
    "能不能", "是不是", "么办", "为什", "么样", "游戏", "问题", "请问", "告诉",
})


# ─────────────────────────── 结果对象 ───────────────────────────


@dataclass(frozen=True)
class CardHit:
    card: Mapping[str, Any]
    score: float
    reason: str
    keyword_hits: tuple[KeywordHit, ...] = ()
    conditions_passed: int = 0


@dataclass(frozen=True)
class TermHit:
    term: Mapping[str, Any]
    score: float
    reason: str
    matched: str


@dataclass
class GameMatch:
    requested_id: str
    game: Mapping[str, Any] | None
    cards: list[CardHit] = field(default_factory=list)
    overflow: list[CardHit] = field(default_factory=list)
    terms: list[TermHit] = field(default_factory=list)
    # 没有可注入内容的原因码（空串 = 有内容）
    reason: str = ""
    blocked: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GameSearch:
    game: Mapping[str, Any] | None
    query: str
    cards: list[Mapping[str, Any]] = field(default_factory=list)
    terms: list[Mapping[str, Any]] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class _IndexedCard:
    card: Mapping[str, Any]
    keywords: tuple[str, ...]
    conditions: tuple[Any, ...]


@dataclass(frozen=True)
class _GameIndex:
    revision: tuple[Any, ...]
    game: Mapping[str, Any]
    cards: tuple[_IndexedCard, ...]
    terms: tuple[tuple[Mapping[str, Any], tuple[str, ...]], ...]

    def card(self, card_id: int) -> Mapping[str, Any] | None:
        for entry in self.cards:
            if entry.card["id"] == card_id:
                return entry.card
        return None


# ─────────────────────────── 渲染 ───────────────────────────


def _clip(text: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().rstrip("。；;，,、 ")
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def is_experience(card: Mapping[str, Any]) -> bool:
    return bool(card.get("bot_id")) and card.get("source_type") == EXPERIENCE_SOURCE_TYPE


def pick_items(items: Sequence[Any], limit: int, focus: Sequence[str] = ()) -> list[str]:
    """条目多于 ``limit`` 时，先留下提到 ``focus`` 里的词最多的几条（保持原顺序）；都没提到就取前几条。"""
    texts = [str(item) for item in items if str(item or "").strip()]
    if len(texts) <= limit or not focus:
        return texts[:limit]
    words = [fold(word) for word in focus if len(fold(word)) >= 2]
    scored = [(sum(1 for word in words if word in fold(text)), index) for index, text in enumerate(texts)]
    if not any(score for score, _ in scored):
        return texts[:limit]
    keep = sorted(sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:limit], key=lambda pair: pair[1])
    return [texts[index] for _, index in keep]


def _experience_note(card: Mapping[str, Any]) -> str:
    meta = card.get("meta") or {}
    times = int(meta.get("times") or 1)
    last = meta.get("last_at")
    when = ""
    if last:
        stamp = datetime.fromtimestamp(float(last))
        when = f"，最近 {stamp.month}月{stamp.day}日"
    return f"{times} 次经历{when}"


def render_card(card: Mapping[str, Any], *, with_source: bool = False, focus: Sequence[str] = ()) -> str:
    """一张卡的注入文本：标题行、要点、注意（条目多时挑和 ``focus`` 相关的几条），查询结果里再加出处。

    她自己的经验卡：标题后注明经历过几次，要点写作「做法」、注意写作「经过」。
    """
    experience = is_experience(card)
    stage = str(card.get("stage") or "").strip()
    suffix = f"（{_experience_note(card)}）" if experience else ""
    lines = [f"【{card.get('title')}{' · ' + stage if stage else ''}】{suffix}"]
    points = [_clip(item, ITEM_CHARS) for item in pick_items(card.get("points") or [], CARD_POINTS_LIMIT, focus)]
    pitfalls = [_clip(item, ITEM_CHARS) for item in pick_items(card.get("pitfalls") or [], CARD_PITFALLS_LIMIT, focus)]
    if points := [p for p in points if p]:
        lines.append(("做法：" if experience else "要点：") + "；".join(points))
    if pitfalls := [p for p in pitfalls if p]:
        lines.append(("经过：" if experience else "注意：") + "；".join(pitfalls))
    if with_source:
        if experience:
            lines.append("出处：你自己的经历")
        elif card.get("source"):
            lines.append("出处：" + _clip(card.get("source"), 120))
    return "\n".join(lines)


def render_term(term: Mapping[str, Any]) -> str:
    aliases = [str(a) for a in (term.get("aliases") or [])[:2] if a]
    label = f"{term.get('term')}（{'、'.join(aliases)}）" if aliases else str(term.get("term"))
    return f"- {label}：{_clip(term.get('explanation'), TERM_CHARS)}"


def render_injection(
    game: Mapping[str, Any], cards: Sequence[CardHit], terms: Sequence[TermHit], *, focus: Sequence[str] = (),
) -> tuple[str, list[str], list[str]]:
    """注入块：她自己的经验（有的话）在前、攻略参考在后，各带一行说明；最后是词条。

    返回 (全文, 每张卡的文本（与 ``cards`` 同序）, 每个词条的行)。
    """
    name = game.get("name") or game.get("id")
    card_blocks = [render_card(hit.card, focus=focus) for hit in cards]
    term_lines = [render_term(hit.term) for hit in terms]
    own = [block for hit, block in zip(cards, card_blocks) if is_experience(hit.card)]
    reference = [block for hit, block in zip(cards, card_blocks) if not is_experience(hit.card)]
    parts: list[str] = []
    if own:
        parts += [EXPERIENCE_HEADER.format(name=name), *own]
    if reference or (term_lines and not own):
        parts += [INJECTION_HEADER.format(name=name), *reference]
    if term_lines:
        parts.append("词条：\n" + "\n".join(term_lines))
    return "\n".join(parts), card_blocks, term_lines


def render_search(result: GameSearch, *, max_chars: int = TOOL_MAX_CHARS) -> str:
    """工具返回：卡片（带出处）+ 词条，总长不超过 ``max_chars``。"""
    game = result.game or {}
    has_experience = any(is_experience(card) for card in result.cards)
    label = "游戏知识" if has_experience else "游戏攻略参考"
    header = f"[{label} · {game.get('name') or game.get('id')}｜查「{_clip(result.query, 30)}」]"
    footer = TOOL_FOOTER_WITH_EXPERIENCE if has_experience else TOOL_FOOTER
    budget = max(200, int(max_chars)) - len(header) - len(footer) - 2
    focus = query_words(result.query)
    body: list[str] = []
    used = 0
    for card in result.cards:
        block = render_card(card, with_source=True, focus=focus)
        if is_experience(card):
            block = "（你自己的经验）" + block
        if used + len(block) + 1 > budget:
            room = budget - used - 1
            if room > 80 and not body:
                body.append(block[: room - 1] + "…")
                used = budget
            break
        body.append(block)
        used += len(block) + 1
    term_lines = [render_term(term) for term in result.terms]
    if term_lines and used + 4 < budget:
        kept: list[str] = []
        for line in term_lines:
            if used + len(line) + 5 > budget:
                break
            kept.append(line)
            used += len(line) + 1
        if kept:
            body.append("词条：\n" + "\n".join(kept))
    return "\n".join([header, *body, footer])


# ─────────────────────────── 查询词 ───────────────────────────


def query_words(query: str, *, limit: int = 8) -> list[str]:
    """查询 → 检索词：jieba 可用时分词，否则按连续中文 / 英文数字切；长串拆成两字片段（与 fts5 通道一致）。"""
    text = str(query or "")
    try:
        import jieba  # type: ignore

        try:
            jieba.setLogLevel(40)
        except Exception:  # pragma: no cover
            pass
        candidates = [str(w).strip() for w in jieba.cut(text)]
    except Exception:  # pragma: no cover - 没有 jieba
        candidates = re.findall(r"[A-Za-z0-9]{2,}|[一-鿿]{2,}", text)
    words: list[str] = []
    for word in candidates:
        if len(word) < 2 or word in _QUERY_STOPWORDS or not re.search(r"[A-Za-z0-9一-鿿]", word):
            continue
        pieces = fts_cjk.tokenize(word)
        expanded = [p for p in pieces if p not in _QUERY_STOPWORDS] if len(pieces) > 3 else [word]
        for item in expanded:
            if item not in words:
                words.append(item)
    return words[: max(1, limit) * 3]


# ─────────────────────────── 服务 ───────────────────────────


class GameKnowledgeService:
    def __init__(self, repo: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.repo = repo
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: dict[str, _GameIndex] = {}
        self._active_sessions: dict[tuple[str, str], tuple[str, float]] = {}
        self._active_bots: dict[str, tuple[str, float]] = {}
        self._legacy_scans: dict[str, dict[str, Any]] = {}
        # 每个游戏库已经记下的状态键（出现新键时才写库）
        self._known_state_keys: dict[str, set[str]] = {}

    # ─────────────── 游戏库 ───────────────

    def list_games(self) -> list[dict[str, Any]]:
        return self.repo.list_games()

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        return self.repo.get_game(game_id)

    def create_game(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        game = self.repo.create_game(payload)
        self._invalidate(game["id"])
        return game

    def update_game(self, game_id: str, payload: Mapping[str, Any], *, expected_version: int | None) -> dict[str, Any]:
        game = self.repo.update_game(game_id, payload, expected_version=expected_version)
        self._invalidate(game["id"])
        return game

    def resolve_game_id(self, identifier: Any) -> str | None:
        """游戏标识优先；认不出时按显示名与别名找（「我的世界」「MC」→ minecraft）。"""
        text = fold(identifier)
        if not text:
            return None
        try:
            game_id = validate_game_id(identifier)
        except GameKnowledgeError:
            game_id = ""
        if game_id and self.repo.get_game(game_id) is not None:
            return game_id
        for game in self.repo.list_games():
            names = {fold(game["id"]), fold(game["name"]), *(fold(alias) for alias in game.get("aliases") or [])}
            if text in names:
                return str(game["id"])
        return None

    # ─────────────── 知识卡 / 词条 ───────────────

    def list_items(self, kind: str, game_id: str, **filters: Any) -> dict[str, Any]:
        self.repo.require_game(game_id)
        return self.repo.list_items(kind, game_id, **filters)

    def get_item(self, kind: str, game_id: str, item_id: int) -> dict[str, Any] | None:
        return self.repo.get_item(kind, item_id, game_id=validate_game_id(game_id))

    def create_item(self, kind: str, game_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        gid = validate_game_id(game_id)
        item = self.repo.create_item(kind, gid, {"source_type": "manual", **dict(payload)})
        self._invalidate(gid)
        return item

    def update_item(self, kind: str, game_id: str, item_id: int, payload: Mapping[str, Any], *, expected_version: int | None) -> dict[str, Any]:
        gid = validate_game_id(game_id)
        item = self.repo.update_item(kind, item_id, payload, expected_version=expected_version, game_id=gid)
        self._invalidate(gid)
        return item

    def set_status(self, kind: str, game_id: str, item_ids: Iterable[Any], status: str, *, versions: Mapping[Any, Any] | None = None) -> dict[str, Any]:
        gid = validate_game_id(game_id)
        self.repo.require_game(gid)
        result = self.repo.set_status(kind, gid, item_ids, status, versions=versions)
        self._invalidate(gid)
        return result

    def stage_facets(self, game_id: str) -> list[dict[str, Any]]:
        return self.repo.stage_facets(game_id)

    def _note_state_keys(self, game: GameContext, *, now: float) -> None:
        """直播请求里出现了新的状态键：记到游戏库里（她写经验卡的条件时只能用这些键）。"""
        if not game.state:
            return
        game_id = self.resolve_game_id(game.id)
        if game_id is None:
            return
        with self._lock:
            known = self._known_state_keys.get(game_id)
        if known is None:
            stored = self.repo.get_game(game_id) or {}
            known = set((stored.get("state_keys") or {}).keys())
            with self._lock:
                self._known_state_keys[game_id] = known
        fresh = {key: value for key, value in game.state.items() if key not in known}
        if not fresh:
            return
        try:
            self.repo.note_state_keys(game_id, fresh, now=now)
        except Exception as exc:  # pragma: no cover - 只影响经验卡能用的条件
            logger.debug("[WaveMemory] 记录游戏状态键失败 %s: %s", game_id, exc)
            return
        with self._lock:
            known.update(fresh)

    # ─────────────── 缓存 ───────────────

    def _invalidate(self, game_id: str | None = None) -> None:
        with self._lock:
            if game_id is None:
                self._cache.clear()
            else:
                self._cache.pop(game_id, None)

    def _build_index(self, game_id: str, *, statuses: Sequence[str] = ("active",)) -> _GameIndex | None:
        revision = self.repo.revision(game_id)
        game = self.repo.get_game(game_id)
        if game is None:
            return None
        # 游戏自己的名字 / 别名当触发词会命中每一句提到这个游戏的话，匹配时不用
        excluded = {fold(game["id"]), fold(game["name"]), *(fold(a) for a in game.get("aliases") or [])}
        cards: list[_IndexedCard] = []
        for card in self.repo.all_items("card", game_id, statuses=list(statuses)):
            try:
                conditions = tuple(parse_conditions(card.get("conditions")))
            except GameKnowledgeError as exc:
                logger.warning("[WaveMemory] 游戏知识卡 %s 的条件无法解析，本轮不参与匹配: %s", card.get("id"), exc)
                continue
            keywords = tuple(k for k in card.get("keywords") or [] if fold(k) and fold(k) not in excluded)
            cards.append(_IndexedCard(card=card, keywords=keywords, conditions=conditions))
        terms = tuple(
            (term, tuple(name for name in [term["term"], *(term.get("aliases") or [])] if fold(name) and fold(name) not in excluded))
            for term in self.repo.all_items("term", game_id, statuses=list(statuses))
        )
        return _GameIndex(revision=revision, game=game, cards=tuple(cards), terms=terms)

    def _index(self, game_id: str) -> _GameIndex | None:
        """启用中的卡与词条（解析好的条件）；库有任何改动（含外部直接写库）时按数据指纹重建。"""
        revision = self.repo.revision(game_id)
        with self._lock:
            cached = self._cache.get(game_id)
            if cached is not None and cached.revision == revision:
                return cached
        index = self._build_index(game_id)
        if index is not None:
            with self._lock:
                self._cache[game_id] = index
        return index

    # ─────────────── 注入匹配 ───────────────

    @staticmethod
    def _card_score(trigger: Any, card: Mapping[str, Any] | None = None) -> float:
        if trigger.keyword_hits:
            score = sum(_SOURCE_WEIGHT.get(hit.source, 1.0) + min(len(fold(hit.keyword)), 8) * 0.05 for hit in trigger.keyword_hits[:3])
            score += _REFINING_CONDITION_WEIGHT * (trigger.passed_conditions)
            score -= _SOFT_FAIL_PENALTY * trigger.soft_failed
        else:
            # 纯条件卡（如「血量低先撤」）由实况状态直接触发，排在单个场景关键词前面
            score = _PURE_CONDITION_WEIGHT * trigger.passed_conditions
        if card is not None and is_experience(card):
            score += _EXPERIENCE_BONUS
        return round(score, 3)

    @staticmethod
    def _visible(card: Mapping[str, Any], bot_id: str) -> bool:
        """共用的攻略谁都能看到；经验卡只属于写下它的 Bot。"""
        owner = str(card.get("bot_id") or "")
        return not owner or owner == str(bot_id or "")

    def match(
        self,
        game: GameContext,
        *,
        message: str = "",
        bot_id: str = "",
        max_cards: int = MAX_INJECT_CARDS,
        max_terms: int = DEFAULT_INJECT_TERMS,
        include_drafts: bool = False,
        explain: bool = False,
    ) -> GameMatch:
        """按 ``game.id`` 选库，返回命中的卡（最多 2 张，其余记为 overflow）与消息里提到的词条。

        ``bot_id``：谁在玩——共用的攻略之外，只看这个 Bot 自己的经验卡。
        """
        requested = str(getattr(game, "id", "") or "")
        game_id = self.resolve_game_id(requested)
        if game_id is None:
            return GameMatch(requested_id=requested, game=None, reason="game_library_not_found")
        index = self._build_index(game_id, statuses=("active", "draft")) if include_drafts else self._index(game_id)
        if index is None:
            return GameMatch(requested_id=requested, game=None, reason="game_library_not_found")
        if not index.game.get("enabled"):
            return GameMatch(requested_id=requested, game=index.game, reason="game_library_disabled")
        state = game.folded_state()
        haystack = KeywordHaystack(message=message, keywords=game.keywords, state=game.state)
        hits: list[CardHit] = []
        blocked: list[dict[str, Any]] = []
        for entry in index.cards:
            if not self._visible(entry.card, bot_id):
                continue
            trigger = card_trigger(entry.keywords, entry.conditions, haystack, state)
            if trigger.triggered:
                hits.append(CardHit(entry.card, self._card_score(trigger, entry.card), trigger.reason, trigger.keyword_hits, trigger.passed_conditions))
            elif explain and entry.keywords and trigger.reason.startswith("条件不满足"):
                words = [hit.describe() for hit in (haystack.find(k) for k in entry.keywords) if hit is not None]
                if words:
                    blocked.append({"card": entry.card, "reason": f"关键词 {'、'.join(words[:3])} 命中，但{trigger.reason}"})
        hits.sort(key=lambda hit: (-hit.score, hit.card["id"]))
        limit = max(0, min(int(max_cards), MAX_INJECT_CARDS))
        chosen, overflow = hits[:limit], hits[limit:]
        terms = self._match_terms(index, haystack, max(0, min(int(max_terms), MAX_INJECT_TERMS)))
        reason = "" if (chosen or terms) else ("nothing_to_match" if haystack.empty() else "no_card_or_term_matched")
        return GameMatch(requested_id=requested, game=index.game, cards=chosen, overflow=overflow, terms=terms, reason=reason, blocked=blocked)

    @staticmethod
    def _match_terms(index: _GameIndex, haystack: KeywordHaystack, limit: int) -> list[TermHit]:
        if limit <= 0:
            return []
        found: list[TermHit] = []
        for term, names in index.terms:
            for name in names:
                hit = haystack.find(name)
                if hit is None:
                    continue
                score = _SOURCE_WEIGHT.get(hit.source, 1.0) + min(len(fold(name)), 8) * 0.05
                found.append(TermHit(term, round(score, 3), f"词条「{name}」命中（{hit.detail}）", name))
                break
        found.sort(key=lambda hit: (-hit.score, hit.term["id"]))
        return found[:limit]

    def trial_match(
        self,
        game_id: str,
        *,
        message: str = "",
        keywords: Any = None,
        state: Any = None,
        include_drafts: bool = False,
        bot_id: str = "",
    ) -> dict[str, Any]:
        """管理台「试一下」：给一句消息 + 状态 + 关键词，看会注入哪些卡、为什么（可把草稿也算上）。

        ``bot_id`` 给了就连这个 Bot 自己的经验卡一起算（和直播时她的请求一样）。
        """
        gid = validate_game_id(game_id)
        self.repo.require_game(gid)
        context = parse_game_context({"id": gid, "state": state if isinstance(state, Mapping) else {}, "keywords": keywords})
        assert context is not None
        match = self.match(context, message=message, bot_id=bot_id, include_drafts=include_drafts, explain=True)
        text = ""
        if match.game is not None and (match.cards or match.terms):
            text = render_injection(match.game, match.cards, match.terms, focus=focus_words(message, match.cards))[0]
        return {
            "game_id": gid,
            "bot_id": bot_id,
            "include_drafts": include_drafts,
            "state": dict(context.state),
            "keywords": list(context.keywords),
            "warnings": list(context.warnings),
            "reason": match.reason,
            "cards": [_hit_payload(hit) for hit in match.cards],
            "overflow": [_hit_payload(hit) for hit in match.overflow],
            "terms": [{"id": hit.term["id"], "term": hit.term["term"], "status": hit.term["status"], "reason": hit.reason} for hit in match.terms],
            "blocked": [{"id": item["card"]["id"], "title": item["card"]["title"], "reason": item["reason"]} for item in match.blocked[:10]],
            "text": text,
            "rule": TRIGGER_RULE_TEXT,
        }

    # ─────────────── 工具查询 ───────────────

    def search(
        self, game_ref: Any, query: str, *, bot_id: str = "", max_cards: int = TOOL_MAX_CARDS, max_terms: int = TOOL_MAX_TERMS,
    ) -> GameSearch:
        """在一个游戏库里查已启用的卡与词条：中文两字切词全文检索 + 触发词 / 标题 / 术语直接命中。

        共用的攻略之外，只查 ``bot_id`` 自己的经验卡。
        """
        text = str(query or "").strip()
        game_id = self.resolve_game_id(game_ref)
        if game_id is None:
            return GameSearch(game=None, query=text, reason="game_library_not_found")
        index = self._index(game_id)
        if index is None:
            return GameSearch(game=None, query=text, reason="game_library_not_found")
        if not index.game.get("enabled"):
            return GameSearch(game=index.game, query=text, reason="game_library_disabled")
        folded = fold(text)
        expr = fts_cjk.match_expr(query_words(text))
        card_scores: dict[int, float] = {}
        term_scores: dict[int, float] = {}
        for position, (card_id, _rank) in enumerate(self.repo.search_ids("card", game_id, expr, limit=20)):
            card_scores[card_id] = card_scores.get(card_id, 0.0) + 3.0 / (1 + position)
        for position, (term_id, _rank) in enumerate(self.repo.search_ids("term", game_id, expr, limit=20)):
            term_scores[term_id] = term_scores.get(term_id, 0.0) + 3.0 / (1 + position)
        haystack = KeywordHaystack(message=text)
        visible = [entry for entry in index.cards if self._visible(entry.card, bot_id)]
        for entry in visible:
            card_id = int(entry.card["id"])
            hits = [k for k in entry.keywords if haystack.find(k) is not None]
            if hits:
                card_scores[card_id] = card_scores.get(card_id, 0.0) + 2.0 * min(len(hits), 3)
            title = fold(entry.card.get("title"))
            if folded and (folded == title or (len(folded) >= 2 and folded in title)):
                card_scores[card_id] = card_scores.get(card_id, 0.0) + 3.0
        for term, names in index.terms:
            term_id = int(term["id"])
            for name in names:
                folded_name = fold(name)
                if folded == folded_name:
                    term_scores[term_id] = term_scores.get(term_id, 0.0) + 6.0
                    break
                if len(folded_name) >= 2 and folded_name in folded or (len(folded) >= 2 and folded in folded_name):
                    term_scores[term_id] = term_scores.get(term_id, 0.0) + 4.0
                    break
        cards_by_id = {int(entry.card["id"]): entry.card for entry in visible}
        terms_by_id = {int(term["id"]): term for term, _ in index.terms}
        cards = [cards_by_id[cid] for cid, _ in sorted(card_scores.items(), key=lambda kv: (-kv[1], kv[0])) if cid in cards_by_id]
        terms = [terms_by_id[tid] for tid, _ in sorted(term_scores.items(), key=lambda kv: (-kv[1], kv[0])) if tid in terms_by_id]
        result = GameSearch(game=index.game, query=text, cards=cards[: max(0, max_cards)], terms=terms[: max(0, max_terms)])
        if not result.cards and not result.terms:
            result.reason = "nothing_found"
        return result

    # ─────────────── 当前在玩的游戏 ───────────────

    def note_active_game(self, scope: Any, game: GameContext | None, *, now: float | None = None) -> None:
        """记下某个 Bot / 会话正在玩的游戏（来自 ``/context/prepare`` 的 ``game``），工具省略游戏时用。"""
        if game is None or not game.id or game.error:
            return
        bot_id = str(getattr(scope, "bot_id", "") or "")
        if not bot_id:
            return
        session = getattr(scope, "session", None)
        session_id = str(getattr(session, "id", "") or "")
        stamp = float(now if now is not None else self._clock())
        with self._lock:
            self._active_sessions[(bot_id, session_id)] = (game.id, stamp)
            self._active_bots[bot_id] = (game.id, stamp)
            if len(self._active_sessions) > _ACTIVE_MAX_ENTRIES:
                cutoff = stamp - ACTIVE_GAME_TTL_SECONDS
                self._active_sessions = {k: v for k, v in self._active_sessions.items() if v[1] >= cutoff}
        self._note_state_keys(game, now=stamp)

    def active_game(self, scope: Any, *, now: float | None = None) -> tuple[str, str] | None:
        """(游戏标识, 来源 session|bot)：先看同一会话，再看这个 Bot 最近在别处玩的；超过 30 分钟不算。"""
        bot_id = str(getattr(scope, "bot_id", "") or "")
        if not bot_id:
            return None
        session = getattr(scope, "session", None)
        session_id = str(getattr(session, "id", "") or "")
        cutoff = float(now if now is not None else self._clock()) - ACTIVE_GAME_TTL_SECONDS
        with self._lock:
            entry = self._active_sessions.get((bot_id, session_id))
            if entry is not None and entry[1] >= cutoff:
                return entry[0], "session"
            entry = self._active_bots.get(bot_id)
            if entry is not None and entry[1] >= cutoff:
                return entry[0], "bot"
        return None

    # ─────────────── 导入 / 导出 ───────────────

    def _prepare(
        self,
        game: Mapping[str, Any],
        package: importers.ImportPackage,
        *,
        status_mode: str,
        default_source_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
        """规整导入条目；返回 (卡, 词条, 错误, 去掉的「游戏名当触发词」数)。"""
        mode = str(status_mode or "auto").strip().lower()
        if mode not in {"auto", "draft", "active"}:
            raise GameKnowledgeError("invalid_status_mode", "导入状态只能是 auto（按文件，没写的为草稿）、draft 或 active")
        forced = package.force_status or (mode if mode != "auto" else None)
        excluded = {fold(game["id"]), fold(game["name"]), *(fold(a) for a in game.get("aliases") or [])}
        cards: list[dict[str, Any]] = []
        terms: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        dropped_keywords = 0
        default_source_type = default_source_type or ("import" if package.format == "generic" else "tutorial")
        for index, raw in enumerate(package.cards):
            try:
                fields = normalize_card_fields({"source_type": default_source_type, **raw})
            except GameKnowledgeError as exc:
                errors.append({"kind": "card", "index": index, "label": str(raw.get("title") or raw.get("key") or "")[:40], "code": exc.code, "message": str(exc)})
                continue
            kept = [k for k in fields["keywords"] if fold(k) not in excluded]
            dropped_keywords += len(fields["keywords"]) - len(kept)
            fields["keywords"] = kept
            fields["status"] = validate_status(forced or fields.get("status"), default="draft")
            fields["card_key"] = fields.get("card_key") or auto_item_key("c", fields["title"])
            cards.append(fields)
        for index, raw in enumerate(package.terms):
            try:
                fields = normalize_term_fields({"source_type": default_source_type, **raw})
            except GameKnowledgeError as exc:
                errors.append({"kind": "term", "index": index, "label": str(raw.get("term") or raw.get("key") or "")[:40], "code": exc.code, "message": str(exc)})
                continue
            fields["status"] = validate_status(forced or fields.get("status"), default="draft")
            fields["term_key"] = fields.get("term_key") or auto_item_key("t", fields["term"])
            terms.append(fields)
        return cards, terms, errors, dropped_keywords

    def _package_warnings(self, game: Mapping[str, Any], package: importers.ImportPackage, dropped: int) -> list[str]:
        warnings = list(package.warnings)
        file_game = str(package.game.get("id") or "").strip().lower()
        if file_game and file_game != game["id"]:
            warnings.append(f"文件里写的游戏是 {file_game}，将导入到当前游戏库 {game['id']}")
        if dropped:
            warnings.append(f"去掉了 {dropped} 个与游戏名 / 别名相同的触发词（它们会命中每一句提到这个游戏的话）")
        return warnings

    def preview_import(self, game_id: str, data: Any, *, fmt: str = "auto", status_mode: str = "auto") -> dict[str, Any]:
        """只解析不写入：识别格式、统计新增 / 已有 / 不合法，列出前几条样例。"""
        game = self.repo.require_game(game_id)
        package = importers.to_package(data, fmt)
        cards, terms, errors, dropped = self._prepare(game, package, status_mode=status_mode)
        return self._preview_payload(game, package, cards, terms, errors, dropped)

    def _preview_payload(self, game: Mapping[str, Any], package: importers.ImportPackage, cards: list[dict[str, Any]], terms: list[dict[str, Any]], errors: list[dict[str, Any]], dropped: int) -> dict[str, Any]:
        def summarize(kind: str, items: list[dict[str, Any]], key_field: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            existing = 0
            by_status: dict[str, int] = {}
            samples: list[dict[str, Any]] = []
            for item in items:
                exists = self.repo.get_item_by_key(kind, game["id"], item[key_field]) is not None
                existing += exists
                by_status[item["status"]] = by_status.get(item["status"], 0) + 1
                if len(samples) < 12:
                    sample = {"key": item[key_field], "status": item["status"], "exists": exists}
                    if kind == "card":
                        sample.update({
                            "title": item["title"],
                            "stage": item.get("stage") or "",
                            "keywords": item.get("keywords") or [],
                            "conditions": [c.text() for c in parse_conditions(item.get("conditions"))],
                            "points": len(item.get("points") or []),
                            "pitfalls": len(item.get("pitfalls") or []),
                            "source": item.get("source") or "",
                        })
                    else:
                        sample.update({"term": item["term"], "aliases": item.get("aliases") or [], "explanation": item.get("explanation") or ""})
                    samples.append(sample)
            invalid = sum(1 for error in errors if error["kind"] == kind)
            return {"total": len(items) + invalid, "new": len(items) - existing, "existing": existing, "invalid": invalid, "by_status": by_status}, samples

        card_summary, card_samples = summarize("card", cards, "card_key")
        term_summary, term_samples = summarize("term", terms, "term_key")
        return {
            "format": package.format,
            "format_label": package.format_label,
            "game_id": game["id"],
            "file_game": dict(package.game),
            "force_status": package.force_status,
            "default_status_mode": "draft" if package.format == "generic" and not package.force_status else "auto",
            "warnings": self._package_warnings(game, package, dropped),
            "cards": card_summary,
            "terms": term_summary,
            "errors": errors[:30],
            "samples": {"cards": card_samples, "terms": term_samples},
        }

    def _import_package(
        self,
        game: Mapping[str, Any],
        package: importers.ImportPackage,
        *,
        status_mode: str,
        on_existing: str,
        default_source_type: str | None = None,
    ) -> dict[str, Any]:
        cards, terms, errors, dropped = self._prepare(game, package, status_mode=status_mode, default_source_type=default_source_type)
        result = self.repo.import_items(game["id"], cards=cards, terms=terms, on_existing=on_existing)
        self._invalidate(game["id"])
        result.pop("created_ids", None)
        return {
            **result,
            "format": package.format,
            "format_label": package.format_label,
            "status_mode": package.force_status or status_mode,
            "invalid": {"cards": sum(1 for e in errors if e["kind"] == "card"), "terms": sum(1 for e in errors if e["kind"] == "term")},
            "errors": errors[:30],
            "warnings": self._package_warnings(game, package, dropped),
        }

    def import_data(self, game_id: str, data: Any, *, fmt: str = "auto", status_mode: str = "auto", on_existing: str = "skip") -> dict[str, Any]:
        """导入到已有的游戏库；已有同键条目按 ``on_existing``（skip / update / merge）处理，状态不变。"""
        game = self.repo.require_game(game_id)
        package = importers.to_package(data, fmt)
        return self._import_package(game, package, status_mode=status_mode, on_existing=on_existing)

    def export_game(self, game_id: str, *, statuses: Sequence[str] | None = None) -> dict[str, Any]:
        """导出共用的攻略卡与词条（各 Bot 自己的经验卡不导出）。"""
        game = self.repo.require_game(game_id)
        wanted = [validate_status(s) for s in statuses or [] if s] or None
        cards = self.repo.all_items("card", game["id"], statuses=wanted, bot_id="")
        terms = self.repo.all_items("term", game["id"], statuses=wanted)
        return importers.export_document(game, cards, terms, exported_at=self._clock())

    # ─────────────── 记忆表里的旧知识点 ───────────────

    def legacy_sources(self) -> list[dict[str, Any]]:
        """可迁移的旧来源；记忆表扫描比较慢（source 列没有索引），只在管理员点检查 / 迁移时才扫。"""
        out: list[dict[str, Any]] = []
        for key, spec in importers.LEGACY_MEMORY_SOURCES.items():
            game_id = str(spec.game["id"])
            game = self.repo.get_game(game_id)
            migrated = self.repo.list_items("card", game_id, source_type="memory_migration", limit=1)["total"] if game else 0
            with self._lock:
                scan = dict(self._legacy_scans.get(key) or {})
            out.append({
                "source": key,
                "game_id": game_id,
                "game_name": spec.game.get("name"),
                "game_exists": game is not None,
                "description": spec.description,
                "scanned": bool(scan),
                "rows": scan.get("rows"),
                "scanned_at": scan.get("at"),
                "migrated": migrated,
            })
        return out

    def _legacy_spec(self, source: str) -> Any:
        spec = importers.LEGACY_MEMORY_SOURCES.get(str(source or ""))
        if spec is None:
            raise GameKnowledgeError("legacy_source_not_found", f"不认识的旧来源 {source!r}")
        return spec

    def scan_legacy(self, source: str) -> dict[str, Any]:
        self._legacy_spec(source)
        rows = self.repo.legacy_memory_rows(source)
        with self._lock:
            self._legacy_scans[source] = {"rows": len(rows), "at": self._clock()}
        parsed = sum(1 for row in rows if importers.parse_oni_lore_content(row["content"]) is not None) if source == "oni_lore" else None
        return {"source": source, "rows": len(rows), "parsed": parsed}

    def migrate_legacy(self, source: str) -> dict[str, Any]:
        """把记忆表里某个旧来源的行迁移成游戏库草稿；原记忆行不删不改，重复执行只补新行。

        没有这些行时直接返回（不新建游戏库）。
        """
        spec = self._legacy_spec(source)
        rows = self.repo.legacy_memory_rows(source)
        with self._lock:
            self._legacy_scans[source] = {"rows": len(rows), "at": self._clock()}
        base = {"source": source, "rows": len(rows), "game_id": spec.game["id"]}
        if not rows:
            return {**base, "skipped": True, "reason": "no_rows", "created_game": False,
                    "cards": {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}}
        game_id = validate_game_id(spec.game["id"])
        game = self.repo.get_game(game_id)
        created_game = game is None
        if game is None:
            game = self.repo.create_game(normalize_game_fields(dict(spec.game)))
        episode_sources: dict[int, str] = {}
        for card in self.repo.all_items("card", game_id, bot_id=""):
            meta = card.get("meta") or {}
            ep = meta.get("ep")
            if isinstance(ep, int) and card.get("source_url") and not meta.get("legacy_source"):
                episode_sources.setdefault(ep, str(card["source_url"]))
        package, stats = importers.oni_lore_package(rows, episode_sources=episode_sources)
        result = self._import_package(game, package, status_mode="draft", on_existing="skip")
        return {**base, **stats, **result, "skipped": False, "created_game": created_game}


def focus_words(message: str, cards: Sequence[CardHit] = ()) -> list[str]:
    """挑要点用的词：消息里的词加上命中的关键词。"""
    words = query_words(message)
    for hit in cards:
        for keyword_hit in hit.keyword_hits:
            if keyword_hit.keyword not in words:
                words.append(keyword_hit.keyword)
    return words


def _hit_payload(hit: CardHit) -> dict[str, Any]:
    card = hit.card
    return {
        "id": card["id"],
        "key": card["key"],
        "title": card["title"],
        "stage": card.get("stage") or "",
        "status": card.get("status"),
        "bot_id": card.get("bot_id") or "",
        "experience": is_experience(card),
        "score": hit.score,
        "reason": hit.reason,
    }


__all__ = [
    "ACTIVE_GAME_TTL_SECONDS",
    "CardHit",
    "DEFAULT_INJECT_TERMS",
    "EXPERIENCE_HEADER",
    "GameKnowledgeService",
    "GameMatch",
    "GameSearch",
    "INJECTION_HEADER",
    "MAX_INJECT_CARDS",
    "MAX_INJECT_TERMS",
    "TOOL_FOOTER",
    "TOOL_MAX_CHARS",
    "TermHit",
    "focus_words",
    "is_experience",
    "pick_items",
    "query_words",
    "render_card",
    "render_injection",
    "render_search",
    "render_term",
]
