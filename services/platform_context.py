"""Platform and group context prefetching, mapping and registry."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from astrbot.api import logger


class PlatformContextManager:
    """Manages runtime OneBot group mappings and preheats display names for WebUI Scopes."""

    def __init__(self, context: Any, bot_registry: Mapping[str, Any]) -> None:
        self.context = context
        self.bot_registry = bot_registry
        self.group_names: dict[tuple[str, str], str] = {}

    def remember_group_name(self, bot_id: str, group_id: str, group_name: str | None) -> None:
        normalized_bot = str(bot_id or "").strip()
        normalized_group = str(group_id or "").strip()
        normalized_name = str(group_name or "").strip()
        if not normalized_bot or not normalized_group or not normalized_name:
            return
        self.group_names[(normalized_bot, normalized_group)] = normalized_name

    def get_group_name(self, bot_id: str, group_id: str) -> str | None:
        normalized_bot = str(bot_id or "").strip()
        normalized_group = str(group_id or "").strip()
        direct = self.group_names.get((normalized_bot, normalized_group))
        if direct:
            return direct
        names = {
            name
            for (cached_bot, cached_group), name in self.group_names.items()
            if cached_bot and cached_group == normalized_group and name
        }
        return next(iter(names)) if len(names) == 1 else None

    async def refresh_group_names_from_platforms(self) -> None:
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        platforms = get_insts() if callable(get_insts) else getattr(manager, "platform_insts", ())
        profiles_by_name = {
            str(profile.name or "").strip(): profile
            for profile in self.bot_registry.values()
            if str(getattr(profile, "name", "") or "").strip()
        }
        for platform in platforms or ():
            try:
                metadata = platform.meta()
                profile = profiles_by_name.get(str(getattr(metadata, "id", "") or "").strip())
                call_action = getattr(getattr(platform, "bot", None), "call_action", None)
                if profile is None or not callable(call_action):
                    continue
                groups = await asyncio.wait_for(call_action("get_group_list"), timeout=8.0)
                for item in groups or ():
                    if not isinstance(item, dict):
                        continue
                    self.remember_group_name(
                        getattr(profile, "db_id", str(profile)),
                        str(item.get("group_id") or ""),
                        item.get("group_name") or item.get("name"),
                    )
            except Exception as exc:
                logger.debug("[WaveMemory] group name prefetch skipped for platform: %s", exc)

    async def warm_group_names_when_platforms_ready(self) -> None:
        for _ in range(15):
            await self.refresh_group_names_from_platforms()
            if self.group_names:
                logger.info("[WaveMemory] group names ready: %s", len(self.group_names))
                return
            await asyncio.sleep(2.0)
        logger.warning("[WaveMemory] group name prefetch timed out; Scope options will fall back to group ids")
