"""Bot 自己的游戏经历：Runtime API 里 ``kind="game_event"`` 观察的校验与入库载荷。

Cortico 把直播里的游戏事件（死亡、立下/完成目标、重要发现、对局胜负等）写进直播间会话。
它是 Bot 本人的经历，不是群友说的话：

- 入库为 ``sender_id="bot"``、``source="game_event"``，内容保留 ``[我的世界]`` 这类游戏前缀，
  与弹幕、Bot 说的话在同一会话里按时间交错，检索和每日日记都能引用；
- 不走 InboundMessagePipeline（黑话、事实提审、好感、纠错都不碰），也不走 Bot 回复流程
  （回复计数、自省、心情疲劳、事后记账）；
- 幂等：写入的 event_id 是 ``game_event:<调用方 event_id>``，MessageWriter 用
  ``bot:visibility:session:event_id`` 作写入网关的幂等键，同一事件重放只入库一次。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import replace
from typing import Any, Mapping

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # top-level import in isolated tests
    from domain.scope import RuntimeScope

GAME_EVENT_KIND = "game_event"
GAME_EVENT_SOURCE = "game_event"
# 内容没带游戏前缀时按游戏标识补上（未知游戏用标识本身）。
GAME_LABELS = {"minecraft": "我的世界", "sts": "杀戮尖塔", "oni": "缺氧"}
MAX_CONTENT_CHARS = 500
# game / event_type：小写标识（minecraft、sts；death、goal、discovery、milestone……）
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class GameEventError(ValueError):
    """不合法的 game_event；``code`` 原样写进回执的 error。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _timestamp(value: Any, now: float | None) -> float:
    fallback = time.time() if now is None else float(now)
    if isinstance(value, bool):
        return fallback
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return fallback
    return stamp if math.isfinite(stamp) and stamp > 0 else fallback


def game_event_payload(
    observation: Mapping[str, Any],
    runtime_scope: RuntimeScope,
    *,
    bot_name: str,
    now: float | None = None,
) -> dict[str, Any]:
    """校验一条 game_event 并返回 MessageWriter 的入队载荷；不合法时抛 :class:`GameEventError`。"""
    if runtime_scope.visibility != "group" or runtime_scope.session is None:
        raise GameEventError("game_event_requires_group_scope")
    event_id = str(observation.get("event_id") or "").strip()
    if not event_id:
        raise GameEventError("event_id_required")
    game = str(observation.get("game") or "").strip()
    if not _IDENT_RE.match(game):
        raise GameEventError("invalid_game")
    event_type = str(observation.get("event_type") or "").strip()
    if not _IDENT_RE.match(event_type):
        raise GameEventError("invalid_event_type")
    content = " ".join(str(observation.get("content") or "").split())
    if not content:
        raise GameEventError("empty_content")
    if not content.startswith("["):
        content = f"[{GAME_LABELS.get(game, game)}] {content}"
    return {
        # Bot 自己的经历：不带发言人。
        "scope": replace(runtime_scope, subject_principal_id=None),
        "group_id": runtime_scope.session.conversation_id,
        "sender_id": "bot",
        "sender_name": bot_name,
        "content": content[:MAX_CONTENT_CHARS],
        "timestamp": _timestamp(observation.get("timestamp"), now),
        "source": GAME_EVENT_SOURCE,
        "event_id": f"{GAME_EVENT_KIND}:{event_id}",
        "metadata": {"origin_kind": f"runtime:{GAME_EVENT_KIND}:{game}:{event_type}"},
    }


__all__ = [
    "GAME_EVENT_KIND",
    "GAME_EVENT_SOURCE",
    "GAME_LABELS",
    "GameEventError",
    "game_event_payload",
]
