"""游戏知识库：按游戏分库的攻略常识，所有 Bot 共用；直播时经 ``game_knowledge`` 通道与查询工具使用。"""

from .service import (
    ACTIVE_GAME_TTL_SECONDS,
    GameKnowledgeService,
    GameMatch,
    GameSearch,
    render_injection,
    render_search,
)

__all__ = [
    "ACTIVE_GAME_TTL_SECONDS",
    "GameKnowledgeService",
    "GameMatch",
    "GameSearch",
    "render_injection",
    "render_search",
]
