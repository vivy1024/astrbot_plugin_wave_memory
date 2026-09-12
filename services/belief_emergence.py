"""Scoped experience-episode belief emergence.

Legacy relationship-event rows still lack canonical RuntimeScope, so they stay
auditable only. Formal candidates come from bot-lived episodes inside an exact
group Scope, and remain pending until evidence-v1 activation.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from astrbot.api import logger

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - focused tests import services directly
    from domain.scope import RuntimeScope

from .experience_episodes import ExperienceEpisodeService
from .identity_safety import is_identity_contamination
from .belief_confidence import POLICY_VERSION


class BeliefEmergenceService:
    """Create pending scoped beliefs from lived episodes, never from unscoped legacy events."""

    def __init__(self, db: Any, llm_client: Any = None, bot_id: str = ""):
        self.db = db
        self.llm = llm_client
        self.bot_id = bot_id
        self.legacy_scope_skip_total = 0

    async def emerge_recent(
        self,
        days: int = 14,
        limit: int = 3,
        scope: RuntimeScope | None = None,
    ) -> list[dict]:
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            self.legacy_scope_skip_total += 1
            if self.legacy_scope_skip_total == 1:
                logger.warning(
                    "[BeliefEmergence] skipped: relationship-event rows lack canonical RuntimeScope"
                )
            return []
        if str(scope.bot_id) != str(self.bot_id or scope.bot_id):
            return []
        upsert = getattr(self.db, "upsert_scoped_belief", None)
        if not callable(upsert):
            return []
        conn = getattr(self.db, "conn", None)
        if conn is None:
            return []
        since = time.time() - max(1, int(days)) * 86400
        episodes = ExperienceEpisodeService(conn).recent_episodes(
            bot_id=scope.bot_id,
            group_id=scope.session.conversation_id,
            limit=max(20, int(limit) * 8),
            since=since,
        )
        created: list[dict] = []
        for episode in episodes:
            if len(created) >= max(1, int(limit)):
                break
            content = self._content_from_episode(episode)
            if not content:
                continue
            memory_ids: list[int] = []
            for value in episode.get("source_memory_ids") or []:
                if isinstance(value, bool):
                    continue
                try:
                    memory_id = int(value)
                except (TypeError, ValueError):
                    continue
                if memory_id > 0 and memory_id not in memory_ids:
                    memory_ids.append(memory_id)
            if not memory_ids:
                continue
            belief_key = "episode-v1:" + hashlib.sha256(
                f"{scope.bot_id}\0{scope.session.id}\0{content.casefold()}".encode("utf-8")
            ).hexdigest()[:32]
            # 不写死 activation_eligible=False：涌现出的经历本身就是一类证据，但要真的
            # 够格激活必须能被 is_episode_backed 验证（同 Scope 内至少两条健康记忆）。
            # 这里只如实记录证据，是否够格由 belief_engine 判定，避免把资格判断复制两份。
            provenance = {
                "producer": "belief_emergence",
                "confidence_policy_version": POLICY_VERSION,
                "episode_id": episode.get("id"),
                "episode_type": episode.get("episode_type"),
                "source_memory_ids": memory_ids,
                "evidence": {"memory_ids": memory_ids, "episode_id": episode.get("id")},
                "activation_eligible": len(memory_ids) >= 2,
                "tag_chain_status": "complete" if len(memory_ids) >= 2 else "empty",
            }
            belief_id = upsert(
                scope,
                belief_key=belief_key,
                content=content,
                belief_type="self_identity" if str(episode.get("episode_type") or "") in {"bot_reply", "self_reflect"} else "world_view",
                strength=0.0,
                status="pending",
                source_memory_id=memory_ids[0],
                provenance=provenance,
            )
            created.append({"id": belief_id, "content": content, "status": "pending", "belief_key": belief_key})
        return created

    @staticmethod
    def _content_from_episode(episode: dict[str, Any]) -> str:
        outcome = str(episode.get("outcome") or "").strip()
        thought = str(episode.get("bot_inner_thought") or "").strip()
        reply = str(episode.get("bot_reply") or "").strip()
        reaction = str(episode.get("user_reaction") or "").strip()
        trigger = str(episode.get("trigger_text") or "").strip()
        parts = [part for part in (thought, outcome, reply, reaction, trigger) if part]
        content = "；".join(parts)[:180].strip()
        if len(content) < 8 or is_identity_contamination(content):
            return ""
        return content
