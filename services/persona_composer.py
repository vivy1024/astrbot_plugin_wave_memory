"""Self persona composer for layered prompt injection.

This module keeps bot self-persona orchestration out of ``main.py`` and out of
``MetaThinking``.  It builds small, ordered blocks:

1. stable self persona
2. active beliefs
3. selected lived experiences
4. healthy style examples
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from astrbot.api import logger

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - standalone repository tests
    from domain.scope import RuntimeScope

from .identity_safety import is_identity_contamination

_EXPERIENCE_STYLE_CONTAMINATION_RE = re.compile(r"(猫耳|猫耳朵|兽耳|尾巴|小爪子|飞机耳|本真君|小鱼干|灵鱼干|喵)")


class PersonaComposer:
    """Build layered self-persona context for the current bot."""

    def __init__(
        self,
        *,
        db: Any,
        belief_engine: Any = None,
        query_engine: Any = None,
        few_shot_service: Any = None,
        bot_profiles: dict[str, Any] | None = None,
        max_experiences: int = 3,
        max_experience_chars: int = 700,
    ):
        self.db = db
        self.belief_engine = belief_engine
        self.query_engine = query_engine
        self.few_shot_service = few_shot_service
        self.bot_profiles = bot_profiles or {}
        self.max_experiences = max(1, int(max_experiences or 3))
        self.max_experience_chars = max(120, int(max_experience_chars or 700))

    async def build_self_persona(
        self,
        *,
        bot_id: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        message: str,
        recent_context: list[str] | None,
        scope: RuntimeScope | None = None,
    ) -> dict[str, Any]:
        """Return persona/belief/experience/style blocks plus compact debug data.

        The composer is also a direct service entry, so it must not use caller
        supplied bot/group values to recover a scope.  It accepts only the
        RuntimeScope parsed at ingress.
        """
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return self._empty_payload(source="scope_required")
        bot_id = scope.bot_id
        group_id = scope.session.conversation_id
        profile = self._get_profile(bot_id)
        if profile is None:
            return self._empty_payload(source="bot_profile_scope_unresolved")
        bot_name = self._profile_value(profile, "name", None) or "当前 bot"
        bot_db_id = self._profile_value(profile, "db_id", None) or scope.bot_id
        aliases = self._profile_value(profile, "aliases", []) or []

        persona_block = self._build_persona_block(bot_name=bot_name, bot_db_id=bot_db_id, aliases=aliases)
        belief_block, belief_debug = self._build_belief_block(
            bot_db_id=bot_db_id,
            sender_id=sender_id,
            message=message,
            scope=scope,
        )
        experience_block, experience_ids = await self._build_experience_block(
            message=message,
            group_id=group_id,
            bot_db_id=bot_db_id,
            scope=scope,
        )
        # few_shot_examples only carry legacy bot_id and cannot prove the current
        # RuntimeScope.  Do not re-inject them through the persona path.
        style_block, style_ids = "", []

        return {
            "persona_block": persona_block,
            "belief_block": belief_block,
            "experience_block": experience_block,
            "style_block": style_block,
            "debug": {
                "persona_sources": ["bot_profile"],
                "belief_ids": belief_debug.get("belief_ids", []),
                "belief_source": belief_debug.get("source", ""),
                "experience_ids": experience_ids,
                "style_ids": style_ids,
            },
        }

    @staticmethod
    @staticmethod
    def _empty_payload(*, source: str) -> dict[str, Any]:
        return {
            "persona_block": "",
            "belief_block": "",
            "experience_block": "",
            "style_block": "",
            "debug": {
                "persona_sources": [],
                "belief_ids": [],
                "belief_source": source,
                "experience_ids": [],
                "style_ids": [],
            },
        }

    def _get_profile(self, bot_id: str) -> Any:
        if bot_id and bot_id in self.bot_profiles:
            return self.bot_profiles[bot_id]
        # Canonical RuntimeScope uses BotProfile.db_id while older registries may
        # still be keyed by QQ id.  Match the stable profile field, never infer a
        # bot from a display name or a default/first profile.
        for profile in self.bot_profiles.values():
            if self._profile_value(profile, "db_id", None) == bot_id:
                return profile
        return None

    @staticmethod
    def _profile_value(profile: Any, key: str, default: Any = None) -> Any:
        if profile is None:
            return default
        if isinstance(profile, dict):
            return profile.get(key, default)
        return getattr(profile, key, default)

    def _build_persona_block(self, *, bot_name: str, bot_db_id: str, aliases: list[str]) -> str:
        alias_text = "、".join(str(a) for a in aliases if a) or "无"
        return (
            "<self_persona>\n"
            f"当前自我身份：{bot_name}（db_id={bot_db_id}，别名：{alias_text}）。\n"
            "人格优先级最高：先保持稳定自我、事实判断和边界感，再参考记忆与经历。\n"
            "经历只提供素材，不能覆盖当前人格；信念提供长期立场，不能变成攻击性模板。\n"
            "遇到挑衅或辱骂时，可以冷处理、拒绝或简短设边界，但不要把攻击性当作默认风格。\n"
            "</self_persona>"
        )

    def _build_belief_block(
        self,
        *,
        bot_db_id: str,
        sender_id: str,
        message: str,
        scope: RuntimeScope | None,
    ) -> tuple[str, dict[str, Any]]:
        if not self.belief_engine:
            return "", {"source": "none", "belief_ids": []}
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            return "", {"source": "scope_rejected", "belief_ids": []}
        try:
            if hasattr(self.belief_engine, "bot_id"):
                self.belief_engine.bot_id = bot_db_id
            keywords = self._keywords(message)
            text = self.belief_engine.get_injection(scope=scope, sender_id=sender_id, keywords=keywords) or ""
            text = text.strip()
            if not text or is_identity_contamination(text):
                return "", {"source": "belief_engine", "belief_ids": []}
            return text, {"source": "belief_engine", "belief_ids": self._extract_ids(text)}
        except Exception as e:
            logger.debug(f"[PersonaComposer] belief block failed: {e}")
            return "", {"source": "belief_engine_error", "belief_ids": []}

    async def _build_experience_block(
        self,
        *,
        message: str,
        group_id: str,
        bot_db_id: str,
        scope: RuntimeScope | None,
    ) -> tuple[str, list[int]]:
        """注入当前 Bot 的书中第一人称经历。

        书中第一人称是 bot 级身份资产，不绑定聊天群。只要当前 RuntimeScope 的
        bot_id 已解析，就按 bot_id 读取 book_experience_episodes；group_id 仅作
        章节标签保留在表里，不参与注入过滤。
        """
        if not isinstance(scope, RuntimeScope) or not scope.bot_id:
            return "", []
        bot_key = str(bot_db_id or scope.bot_id or "").strip()
        if not bot_key:
            return "", []

        # 只读少量 bot 级书中经历；同连接同步读取，避免跨线程共享 sqlite connection。
        candidates = self._list_bot_book_experiences(bot_key, message)
        if not candidates:
            return "", []

        selected: list[dict[str, Any]] = []
        seen_content: set[str] = set()
        total_chars = 0
        for item in candidates:
            content = str(item.get("content") or "").strip()
            if not content or not self._is_safe_experience_text(content):
                continue
            key = content[:80]
            if key in seen_content:
                continue
            if total_chars + len(content) > self.max_experience_chars and selected:
                continue
            selected.append(item)
            seen_content.add(key)
            total_chars += len(content)
            if len(selected) >= self.max_experiences:
                break

        if not selected:
            return "", []

        lines = ["<self_experiences>", "以下是少量精选书中第一人称经历，只用于补细节，不得覆盖人格："]
        for item in selected:
            content = str(item.get("content") or "").strip()
            chapter = str(item.get("chapter") or item.get("group_id") or "").strip()
            prefix = f"[{chapter}] " if chapter else ""
            lines.append(f"- {prefix}{content[:240]}")
        lines.append("</self_experiences>")
        ids = [item.get("id") for item in selected if item.get("id") is not None]
        return "\n".join(lines), ids

    def _list_bot_book_experiences(self, bot_db_id: str, message: str) -> list[dict[str, Any]]:
        """按 bot_id 读取书中第一人称；不按群会话过滤。"""
        conn = getattr(self.db, "conn", None)
        if conn is None:
            return []
        limit = max(self.max_experiences * 12, 24)
        keywords = self._keywords(message)
        try:
            rows = conn.execute(
                """SELECT id, group_id, content, evidence_json
                     FROM book_experience_episodes
                    WHERE bot_id=?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?""",
                (bot_db_id, limit),
            ).fetchall()
        except Exception as exc:
            logger.debug(f"[PersonaComposer] book experience read failed: {exc}")
            return []

        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                content = str(row[2] or "").strip()
            except Exception:
                continue
            if not content:
                continue
            score = 0
            lowered = content.casefold()
            for token in keywords:
                if token.casefold() in lowered:
                    score += 1
            items.append({
                "id": row[0],
                "chapter": row[1],
                "group_id": row[1],
                "content": content,
                "score": score,
                "source": "book_experience_episodes",
            })
        items.sort(key=lambda item: (int(item.get("score") or 0), int(item.get("id") or 0)), reverse=True)
        return items

    @staticmethod
    def _is_safe_experience_text(text: str) -> bool:
        if is_identity_contamination(text):
            return False
        if _EXPERIENCE_STYLE_CONTAMINATION_RE.search(text or ""):
            return False
        return True

    @staticmethod
    def _keywords(message: str) -> list[str]:
        tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", message or "")
        return tokens[:5]

    @staticmethod
    def _extract_ids(text: str) -> list[int]:
        return [int(x) for x in re.findall(r"ID[:#]?(\d+)", text or "")[:10]]
