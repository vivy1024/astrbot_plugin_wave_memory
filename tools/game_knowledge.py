"""WaveMemory 游戏知识查询工具：查某个游戏的攻略常识、机制、名词，以及自己玩出来的经验。

只读游戏知识库里「启用」的知识卡与词条，不读写任何会话数据。作用域用来找「当前在玩的游戏」和「是谁在查」：
省略 ``game`` 时，先看这个会话最近的直播请求带的游戏，再看这个 Bot 最近在别处玩的（30 分钟内），
都没有就请模型填写；共用的攻略之外，只查得到这个 Bot 自己的经验卡。想查以前玩过的具体经过，用记忆搜索。
"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

try:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover
    from typing import Generic, TypeVar
    _T = TypeVar("_T")
    class FunctionTool(Generic[_T]): pass
    class ContextWrapper(Generic[_T]): pass
    class AstrAgentContext: pass

try:
    from ..domain.scope import validate_formal_command_scope
    from ..services.game_knowledge.service import TOOL_MAX_CHARS, render_search
    from .scope_boundary import extract_event_runtime_scope
except ImportError:  # pragma: no cover
    from domain.scope import validate_formal_command_scope
    from services.game_knowledge.service import TOOL_MAX_CHARS, render_search
    from tools.scope_boundary import extract_event_runtime_scope


def _enabled_games(service: Any) -> str:
    try:
        games = [game for game in service.list_games() if game.get("enabled")]
    except Exception:
        return ""
    return "、".join(f"{game['name']}（{game['id']}）" for game in games[:12])


@dataclass
class WaveMemoryGameKnowledgeTool(FunctionTool[AstrAgentContext]):
    """在游戏知识库里查攻略卡与词条，总长有上限。"""

    name: str = "wave_memory_game_knowledge"
    description: str = (
        "查游戏攻略、机制常识，以及你自己以前玩出来的经验（按游戏分库）。想知道某个游戏里某件事怎么做、"
        "某个机制怎么回事、某个名词是什么意思时调用，返回匹配的卡片（要点、注意、出处）与词条，总长约 1200 字。"
        "标了「你自己的经验」的是你亲身经历过、自己总结的，其余是攻略参考、不是你的亲身经历。"
        "想查以前玩过的具体经过、和谁一起玩过，用记忆搜索 wave_memory_search。查不到时不要自行编造攻略。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "game": {
                "type": "string",
                "description": "游戏标识或名称，如 minecraft、我的世界、oni、缺氧；正在直播玩的游戏可以省略",
            },
            "query": {
                "type": "string",
                "description": "要查的事或名词，如「第一夜怎么过」「苦力怕」「制氧」；写关键词，不要整段复制聊天记录",
            },
        },
        "required": ["query"],
    })

    service: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "请提供要查的内容（query），例如「第一夜怎么过」「苦力怕」。"
        service = self.service
        if service is None:
            return "游戏知识库暂时不可用。不要自行编造攻略。"
        scope = extract_event_runtime_scope(ctx)
        if scope is not None:
            decision = validate_formal_command_scope("game_knowledge.read", scope)
            if not decision.allowed:
                scope = None
        game_ref = str(kwargs.get("game") or "").strip()
        via = ""
        if not game_ref and scope is not None:
            active = service.active_game(scope)
            if active is not None:
                game_ref, via = active
        if not game_ref:
            available = _enabled_games(service)
            hint = f"现有游戏库：{available}。" if available else "还没有任何游戏库。"
            return f"没法确定要查哪个游戏：请在参数 game 里写上游戏标识或名称后再查。{hint}"
        try:
            result = service.search(game_ref, query, bot_id=str(getattr(scope, "bot_id", "") or "") if scope is not None else "")
        except Exception as exc:
            return f"游戏知识库查询失败：{type(exc).__name__}。不要自行编造攻略。"
        if result.reason == "game_library_not_found":
            available = _enabled_games(service)
            return f"没有「{game_ref}」的游戏库。{('现有：' + available + '。') if available else ''}不要自行编造攻略。"
        if result.reason == "game_library_disabled":
            return f"「{result.game.get('name')}」的游戏库已停用，查不到攻略。不要自行编造攻略。"
        if not result.cards and not result.terms:
            name = result.game.get("name") if result.game else game_ref
            return (
                f"在「{name}」的游戏库里没有找到和「{query[:30]}」相关的已启用攻略或词条。"
                "可以换个说法再查；查不到就直说不确定，不要自行编造攻略。"
            )
        marker = f"｜当前在玩（{'这个会话' if via == 'session' else '最近的直播'}）" if via else ""
        text = render_search(result, max_chars=TOOL_MAX_CHARS - len(marker))
        return text.replace("]", marker + "]", 1) if marker else text


__all__ = ["WaveMemoryGameKnowledgeTool"]
