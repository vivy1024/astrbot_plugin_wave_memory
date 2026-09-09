"""WaveMemory social anchor tool — records mutual debts, promises, and boundary hits."""

from __future__ import annotations

import json
import time
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
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - repository tests run without AstrBot
    import logging

    logger = logging.getLogger(__name__)


@dataclass
class WaveMemoryNoteSocialAnchorTool(FunctionTool[AstrAgentContext]):
    """记录与群友之间的关键人情事实、现实承诺或越界备忘。"""

    name: str = "wave_memory_note_social_anchor"
    description: str = (
        "记录与群友之间发生的关键人情借还、现实承诺或底线越界事实，形成长久的人际纽带备忘。"
        "必须提供 target_user, anchor_type, summary。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user": {
                "type": "string",
                "description": "交互对象的 QQ 号、名字或别名",
            },
            "anchor_type": {
                "type": "string",
                "enum": ["bot_helped_user", "user_helped_bot", "bot_promised_user", "boundary_hit"],
                "description": "人情类型：bot_helped_user(我帮了他)/user_helped_bot(他帮了我)/bot_promised_user(我答应了某事)/boundary_hit(触犯底线)",
            },
            "summary": {
                "type": "string",
                "description": "一句话客观描述这笔人情或承诺（如：通宵帮他排查了毕设代码死锁、答应明天提醒他早起等）",
            },
            "is_active_concern": {
                "type": "boolean",
                "description": "是否需要作为当前活跃关切挂在心上，以便下次互动时主动跟进进展",
            },
        },
        "required": ["target_user", "anchor_type", "summary"],
    })

    db: Any = field(default=None, repr=False)
    concern_tracker: Any = field(default=None, repr=False)
    repository: Any = field(default=None, repr=False)
    write_gateway: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db:
            return "数据库未初始化"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        runtime_scope, error_code = require_group_runtime_scope(ctx, "social_anchor.note")
        if error_code:
            return scope_error_message("人情备忘", error_code)
        assert runtime_scope is not None

        target = str(kwargs.get("target_user") or "").strip()
        anchor_type = str(kwargs.get("anchor_type") or "").strip()
        summary = str(kwargs.get("summary") or "").strip()
        is_active_concern = bool(kwargs.get("is_active_concern", False))

        if not target or not anchor_type or not summary:
            return "target_user、anchor_type 与 summary 均为必填项"

        user_id = resolve_user_id(self.db, target, runtime_scope)
        if not user_id:
            return f"没有在当前群找到目标用户「{target}」，无法记录人情备忘"

        display = display_name_for_user(self.db, user_id, runtime_scope) or target
        target_scope = RuntimeScope(
            bot_id=runtime_scope.bot_id,
            visibility="group",
            session=runtime_scope.session,
            subject_principal_id=f"{runtime_scope.session.platform_id}:user:{user_id}",
        )
        now = time.time()

        # 1. 如果标为活跃关切，经正式写入链挂入灵魂关切投影
        if is_active_concern:
            concern_topic = f"{display}：{summary}".replace("\n", " ").strip()[:80]
            gateway = self.write_gateway or getattr(self.db, "write_gateway", None)
            if gateway is None or not callable(getattr(gateway, "transition_concern", None)):
                logger.warning(
                    "[WaveMemory] social anchor concern skipped: concern_writer_unavailable"
                )
            else:
                try:
                    await gateway.transition_concern(
                        scope=runtime_scope,
                        action="note",
                        topic=concern_topic,
                        intensity=0.75,
                        concern_type="social_anchor",
                        evidence=[{"kind": "social_anchor", "summary": summary[:80]}],
                        actor="social_anchor_tool",
                    )
                    tracker = self.concern_tracker
                    if tracker is not None and hasattr(tracker, "invalidate"):
                        tracker.invalidate(runtime_scope)
                except Exception as concern_error:
                    # 关切挂接失败不得回滚已成立的锚点，但必须可见，不能静默吞掉。
                    logger.warning(
                        f"[WaveMemory] social anchor concern failed: {concern_error}"
                    )

        # 2. 写入 scoped_soul_timeline 作为社交人情锚点
        repo = self.repository or getattr(self.db, "soul_repository", None)
        if repo is not None and hasattr(repo, "add_timeline_event"):
            try:
                repo.add_timeline_event(
                    target_scope,
                    event_summary=f"人情备忘（{anchor_type}）：{summary}",
                    emotional_weight=0.7 if is_active_concern else 0.5,
                    timestamp=now,
                    event_type=f"social_anchor.{anchor_type}",
                    evidence=[{
                        "kind": "social_anchor",
                        "anchor_type": anchor_type,
                        "summary": summary,
                        "is_active_concern": is_active_concern,
                    }],
                )
            except Exception:
                pass

        # 3. 写入 user_profiles metadata 中的 social_anchors 列表
        try:
            row = self.db.conn.execute(
                "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                (user_id, runtime_scope.session.conversation_id, runtime_scope.bot_id),
            ).fetchone()
            metadata: dict[str, Any] = {}
            if row and row[0]:
                try:
                    loaded = json.loads(row[0])
                    if isinstance(loaded, dict):
                        metadata = loaded
                except Exception:
                    metadata = {}
            anchors = metadata.get("social_anchors")
            if not isinstance(anchors, list):
                anchors = []
            anchors.append({
                "anchor_type": anchor_type,
                "summary": summary,
                "is_active_concern": is_active_concern,
                "created_at": now,
            })
            metadata["social_anchors"] = anchors[-10:]
            self.db.conn.execute(
                """INSERT INTO user_profiles (user_id, group_id, bot_id, metadata, interaction_count, last_seen)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(user_id, group_id, bot_id) DO UPDATE SET
                   metadata=excluded.metadata, last_seen=excluded.last_seen""",
                (user_id, runtime_scope.session.conversation_id, runtime_scope.bot_id, json.dumps(metadata, ensure_ascii=False), now),
            )
            self.db.conn.commit()
        except Exception as e:
            return f"人情备忘保存失败: {e}"

        concern_msg = "（已挂载为活跃关切，下次互动时将主动跟进）" if is_active_concern else ""
        return f"已记录与「{display}」的人情备忘：[{anchor_type}] {summary}{concern_msg}"
