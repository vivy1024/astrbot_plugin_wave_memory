"""羽书在对话里觉得某位发言者像 Bot（或确认是真人）时，顺手标一笔。

没有任何关键词、正则或行为统计：谁是 Bot 由模型在对话中自己判断，这个工具只负责把判断存下来。
存下之后系统只做两件事：被标为 Bot 的人说话、没 @ 她时不主动接；被他 @ 时限制来回轮数，避免两个 Bot 互相刷屏。

边界：
- 只能改「真人 / 疑似 Bot」之间的判断；管理员标定的 ``verified_human`` / ``confirmed_bot`` 和本系统自己的
  Bot 账号不会被改写。
- 只在群里使用；不能标自己。
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
    from ..domain.scope import RuntimeScope
    from .person_identity import display_name_for_user, resolve_user_id
    from .scope_boundary import require_memory_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_memory_runtime_scope, scope_error_message

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

BOT_TAG_SOURCE = "bot_check"
_VERDICTS = {"bot": "suspected_bot", "human": "human"}


@dataclass
class WaveMemoryNoteBotTagTool(FunctionTool[AstrAgentContext]):
    """记下「这位发言者是不是机器人」的判断。"""

    name: str = "wave_memory_note_bot_tag"
    description: str = (
        "聊着聊着觉得某位发言者其实是机器人（或之前看走眼、其实是真人）时，顺手记一笔。"
        "必须提供 target_user、verdict、reason。只依据对话里看到的，不要凭名字猜。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user": {
                "type": "string",
                "description": "要标记的人：QQ 号、名字或别名",
            },
            "verdict": {
                "type": "string",
                "enum": ["bot", "human"],
                "description": "bot=觉得对方是机器人；human=之前标错了或确认是真人",
            },
            "reason": {
                "type": "string",
                "description": "一句话说明你是从哪里看出来的（比如「三次回得一字不差」「说话带着模型的口吻」）",
            },
        },
        "required": ["target_user", "verdict", "reason"],
    })

    db: Any = field(default=None, repr=False)
    # 工具比 BotDiscoveryService 先构造（启动顺序），所以持有「怎么拿到它」，调用时再取。
    discovery_provider: Any = field(default=None, repr=False)

    def _discovery(self) -> Any:
        provider = self.discovery_provider
        return provider() if callable(provider) else None

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        discovery = self._discovery()
        if self.db is None or discovery is None:
            return "Bot 标记功能未就绪"

        runtime_scope, error_code = require_memory_runtime_scope(ctx, "bot_tag.note")
        if error_code:
            return scope_error_message("Bot 标记", error_code)
        assert runtime_scope is not None

        target = str(kwargs.get("target_user") or "").strip()
        verdict = str(kwargs.get("verdict") or "").strip().lower()
        reason = str(kwargs.get("reason") or "").strip()
        if not target or not verdict or not reason:
            return "target_user、verdict 与 reason 均为必填项"
        if verdict not in _VERDICTS:
            return "verdict 必须是 bot 或 human"

        user_id = resolve_user_id(self.db, target, runtime_scope)
        if not user_id:
            return f"没有在当前群找到「{target}」，无法标记"

        current = discovery.get_tag(user_id)
        if current.get("source") == "system_self":
            return "这是本系统自己的 Bot 账号，不需要标记"
        # 管理员已经标定的结论不被对话里的判断改写
        if current.get("entity_kind") in {"verified_human", "confirmed_bot"}:
            label = "真人" if current["entity_kind"] == "verified_human" else "机器人"
            return f"管理员已确认「{target}」是{label}，不改"

        kind = _VERDICTS[verdict]
        if current.get("entity_kind") == kind:
            return f"「{target}」已经是这个判断了"

        display = display_name_for_user(self.db, user_id, runtime_scope) or target
        try:
            discovery.set_tag(
                user_id,
                entity_kind=kind,
                source=BOT_TAG_SOURCE,
                reason=reason[:120],
                display_name=display,
            )
        except Exception as exc:
            logger.warning("[WaveMemory] bot tag write failed: %r", exc)
            return "标记没写进去，稍后再试"
        return f"记下了：{display} {'像机器人，之后他没 @ 你就不主动接' if kind == 'suspected_bot' else '是真人'}"


__all__ = ["BOT_TAG_SOURCE", "WaveMemoryNoteBotTagTool"]
