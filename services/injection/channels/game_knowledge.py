"""游戏知识通道（``game_knowledge``）：只在请求带 ``game`` 时工作（Cortico 直播的 ``/context/prepare``）。

- 按 ``game.id`` 选游戏库（显示名、别名也认），注入被触发的知识卡（最多 2 张）与提到的词条；
- 卡片分两种：所有 Bot 共用的攻略，和这个 Bot 自己从经历里写下的经验卡（只在它自己的请求里出现、排在前面）；
- 只注入「启用」的内容，草稿与归档不注入；游戏库停用时整段跳过；
- 措辞分开写：攻略写明不是她的亲身经历；经验写明是她自己经历过、自己总结的（具体经过走记忆通道）；
- 卡片要点多时，挑和这句话、命中的关键词相关的几条；
- 观测台：每条命中带 ``matched_by``（命中了哪些关键词、哪些条件），超出 2 张上限的卡记在 filtered 里。

请求不带 ``game`` 时返回 ``disabled``，编排器直接丢弃，注入结果、trace 与通道统计都和没有这个通道时一样。
通道配置：``max_items`` 为最多几张卡（上限 2），``top_k`` 为最多几个词条（默认 3，上限 5），``token_budget`` 自有预算。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

try:
    from ....domain.scope import validate_formal_command_scope
    from ...game_knowledge.service import (
        DEFAULT_INJECT_TERMS,
        MAX_INJECT_CARDS,
        MAX_INJECT_TERMS,
        focus_words,
        is_experience,
        render_card,
        render_injection,
        render_term,
    )
except ImportError:  # pragma: no cover - 顶层导入回退
    from domain.scope import validate_formal_command_scope
    from services.game_knowledge.service import (
        DEFAULT_INJECT_TERMS,
        MAX_INJECT_CARDS,
        MAX_INJECT_TERMS,
        focus_words,
        is_experience,
        render_card,
        render_injection,
        render_term,
    )
from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from .safety import is_channel_allowed_in_mode


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    options = _mapping(getattr(ctx, "channel_options", {})).get("game_knowledge")
    if isinstance(options, Mapping):
        return options
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("game_knowledge", {}))


def _preview(text: str, limit: int = 160) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _card_item(hit: Any, game_id: str, *, rendered: str) -> dict[str, Any]:
    card = hit.card
    experience = is_experience(card)
    return {
        "id": card["id"],
        "card_id": card["id"],
        "card_key": card["key"],
        "game_id": game_id,
        "title": card["title"],
        "stage": card.get("stage") or "",
        "status": card.get("status"),
        "source": "game_knowledge",
        "source_layer": "game_experience" if experience else "game_card",
        "experience": experience,
        "reference_only": not experience,
        "matched_by": hit.reason,
        "score": hit.score,
        "preview": _preview(rendered),
        "rendered_text": rendered,
        "dedupe_key": f"game_knowledge:{game_id}:card:{card['id']}",
    }


def _term_item(hit: Any, game_id: str, *, rendered: str) -> dict[str, Any]:
    term = hit.term
    return {
        "id": term["id"],
        "term_id": term["id"],
        "game_id": game_id,
        "word": term["term"],
        "meaning": _preview(term.get("explanation") or "", 80),
        "source": "game_knowledge",
        "source_layer": "game_term",
        "reference_only": True,
        "matched_by": hit.reason,
        "score": hit.score,
        "preview": _preview(rendered),
        "rendered_text": rendered,
        "dedupe_key": f"game_knowledge:{game_id}:term:{term['id']}",
    }


class GameKnowledgeChannel:
    """按请求里的 ``game`` 注入她自己的经验与攻略参考；不写任何数据。"""

    name = "game_knowledge"
    # build 里没有 await、全是同步读库（带缓存）：编排器放到工作线程跑，不占事件循环
    offload_to_thread = True

    def __init__(self, *, service: Any = None):
        self.service = service

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        game = getattr(ctx, "game", None)
        if game is None:
            return InjectionResult.disabled(self.name, reason="no_game_context：请求没有带 game，本通道不工作")
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"game_knowledge channel disabled in {mode} mode")
        cfg = _channel_cfg(ctx)
        if not _as_bool(cfg.get("enabled"), True):
            return InjectionResult.disabled(self.name, reason="game_knowledge channel disabled by config")
        if self.service is None:
            return InjectionResult.empty(self.name, reason="game knowledge service is unavailable")
        runtime_scope = getattr(ctx, "scope", None)
        decision = validate_formal_command_scope("game_knowledge.inject", runtime_scope)
        if not decision.allowed:
            return InjectionResult.empty(self.name, reason=decision.reason_code or "scope_rejected")
        if getattr(game, "error", ""):
            return InjectionResult.skipped(self.name, reason=f"invalid_game_context：{game.error}")

        max_cards = max(0, min(_as_int(cfg.get("max_items"), MAX_INJECT_CARDS), MAX_INJECT_CARDS))
        max_terms = max(0, min(_as_int(cfg.get("top_k"), DEFAULT_INJECT_TERMS), MAX_INJECT_TERMS))
        warnings = list(getattr(game, "warnings", ()) or ())
        message = str(getattr(ctx, "message", "") or "")
        try:
            match = self.service.match(
                game, message=message, bot_id=str(getattr(runtime_scope, "bot_id", "") or ""),
                max_cards=max_cards, max_terms=max_terms,
            )
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

        if match.reason == "game_library_not_found":
            result = InjectionResult.empty(
                self.name,
                latency_ms=self._latency_ms(started),
                reason=f"game_library_not_found：还没有游戏库 {game.id}（在管理台「游戏知识库」新建并导入知识）",
            )
            result.warnings.extend(warnings)
            return result
        if match.reason == "game_library_disabled":
            return InjectionResult.skipped(self.name, reason=f"game_library_disabled：游戏库 {match.game['id']} 已停用")

        game_id = str(match.game["id"]) if match.game else game.id
        focus = focus_words(message, match.cards)
        filtered: list[dict[str, Any]] = []
        cards = []
        for hit in match.cards:
            block = render_card(hit.card, focus=focus)
            if is_identity_contamination(block):
                filtered.append({**_card_item(hit, game_id, rendered=block), "filter_reason": "identity_contamination", "filter_channel": self.name})
                continue
            cards.append(hit)
        terms = []
        for hit in match.terms:
            line = render_term(hit.term)
            if is_identity_contamination(line):
                filtered.append({**_term_item(hit, game_id, rendered=line), "filter_reason": "identity_contamination", "filter_channel": self.name})
                continue
            terms.append(hit)
        for hit in match.overflow:
            block = render_card(hit.card, focus=focus)
            filtered.append({
                **_card_item(hit, game_id, rendered=block),
                "filter_reason": f"game_knowledge_max_cards：命中但超过本通道 {max_cards} 张上限",
                "filter_channel": self.name,
            })

        if not cards and not terms:
            result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason=match.reason or "no_safe_game_knowledge")
            result.filtered = filtered
            result.warnings.extend(warnings)
            return result

        text, card_blocks, term_lines = render_injection(match.game, cards, terms, focus=focus)
        items = [_card_item(hit, game_id, rendered=block) for hit, block in zip(cards, card_blocks)]
        items += [_term_item(hit, game_id, rendered=line) for hit, line in zip(terms, term_lines)]
        return InjectionResult.hit(self.name, text, items=items, filtered=filtered, warnings=warnings, latency_ms=self._latency_ms(started))

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = ["GameKnowledgeChannel"]
