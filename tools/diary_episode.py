"""WaveMemory Bot 亲笔经历/心智日记工具 (Daily Diary & Reflection Tool).

由群内真实 Bot 亲笔撰写每日经历总结，并作为一等公民节点挂载进 Bot 经历时间线 (scoped_soul_timeline)。
职责单一明确：专注记录 Bot 的亲笔日记与经历骨架。
其他认知资产（事实、信念、黑话、社交好感）由 Bot 在对话/反思中自主调用各自专门工具完成。
"""

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
    T = TypeVar("T")
    class FunctionTool(Generic[T]): pass
    class ContextWrapper(Generic[T]): pass
    class AstrAgentContext: pass

try:
    from .scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message
    from ..services.identity_safety import is_identity_contamination
except ImportError:
    from tools.scope_boundary import require_group_runtime_scope, resolve_source_memory_id, scope_error_message
    from services.identity_safety import is_identity_contamination


@dataclass
class WaveMemoryRecordDiaryEpisodeTool(FunctionTool[AstrAgentContext]):
    """记录 Bot 今日经历日记与心智自省，成为 Bot 经历时间线的主干。"""

    name: str = "wave_memory_record_diary_episode"
    description: str = (
        "Bot 亲笔记录今日生活日记、经历复盘与心智自省，沉淀为经历片段并钉入 Bot 经历时间线主干。"
        "包含标题、正文心智思考与一句话摘要。如需记录事实、信念、黑话或群友印象，请使用各自的专门工具。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "diary_title": {
                "type": "string",
                "description": "今日日记标题，如：关于AI画图探讨与群聊深夜随笔",
            },
            "diary_content": {
                "type": "string",
                "description": "Bot 亲笔日记正文/心智思考（Markdown 格式，讲述自己今天经历了什么、看到了什么、有什么感悟）",
            },
            "episode_summary": {
                "type": "string",
                "description": "一句话经历摘要，如：和群友探讨AI生图与软件开发，调侃养鼠被咬常识",
            },
            "emotional_weight": {
                "type": "number",
                "description": "今日整体情绪/重要度权重（0.0 ~ 10.0，默认 5.0）",
            },
        },
        "required": ["diary_title", "diary_content", "episode_summary"],
    })
    db: Any = field(default=None, repr=False)
    write_gateway: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        scope, error = require_group_runtime_scope(ctx, "diary.record")
        if error:
            return scope_error_message("经历日记记录", error)
        if self.db is None or scope is None or scope.session is None:
            return "数据库或群 Scope 未初始化"

        diary_title = str(kwargs.get("diary_title") or "").strip()
        diary_content = str(kwargs.get("diary_content") or "").strip()
        episode_summary = str(kwargs.get("episode_summary") or "").strip()

        if not diary_title or not diary_content or not episode_summary:
            return "diary_title, diary_content 与 episode_summary 均为必填项"

        if is_identity_contamination(f"{diary_title} {diary_content} {episode_summary}"):
            return "经历日记被拒绝：检测到身份角色扮演污染"

        weight = max(0.0, min(10.0, float(kwargs.get("emotional_weight", 5.0) or 5.0)))
        now = time.time()

        conn = getattr(self.db, "conn", None)
        if conn is None:
            return "数据库连接不可用"

        # 防刷冷却：同群同 Bot 在 6 小时内避免生成多篇碎片化日记
        group_id = scope.session.conversation_id
        recent_row = conn.execute(
            """SELECT id, created_at, trigger_text FROM experience_episodes
               WHERE bot_id=? AND group_id=? AND episode_type='daily_diary'
               ORDER BY created_at DESC LIMIT 1""",
            (scope.bot_id, group_id),
        ).fetchone()
        if recent_row and (now - float(recent_row[1])) < 6 * 3600:
            diff_hours = round((now - float(recent_row[1])) / 3600.0, 1)
            return (
                f"今日群聊经历日记已存在（距今 {diff_hours} 小时，篇目 #{recent_row[0]}「{recent_row[2]}」）。"
                "为保持生命历程的凝聚度与深度，无需频繁重复撰写日记；具体事实或好感变化请使用对应专门工具。"
            )

        # 自动溯源当轮真实群聊记忆作为日记证据锚点
        auto_mid = resolve_source_memory_id(self.db, scope, quote=episode_summary)
        source_mids_json = json.dumps([auto_mid] if auto_mid else [], ensure_ascii=False)

        coordinator = getattr(self.write_gateway, "coordinator", None) or getattr(self.db, "coordinator", None)

        def persist_all(connection: Any) -> int:
            # 1. 写入 experience_episodes
            sql_episode = """
                INSERT INTO experience_episodes (
                    bot_id, group_id, user_id, episode_type, trigger_text,
                    bot_inner_thought, bot_action, bot_reply, user_reaction,
                    outcome, source_memory_ids, emotional_weight, created_at
                ) VALUES (?, ?, ?, 'daily_diary', ?, ?, 'wrote_daily_diary', ?, 'self_reflection', 'crystallized', ?, ?, ?)
            """
            cur = connection.execute(
                sql_episode,
                (
                    scope.bot_id,
                    scope.session.conversation_id,
                    (scope.subject_principal_id or "").rsplit(":", 1)[-1] or None,
                    diary_title,
                    episode_summary,
                    diary_content,
                    source_mids_json,
                    weight,
                    now,
                ),
            )
            episode_id = int(getattr(cur, "lastrowid", 0) or 0)

            # 2. 写入 scoped_soul_timeline (Bot 经历时间线主干)
            sql_timeline = """
                INSERT INTO scoped_soul_timeline (
                    bot_id, session_id, visibility, subject_principal_id,
                    event_summary, event_type, emotional_weight, occurred_at,
                    revision, evidence, created_at
                ) VALUES (?, ?, 'group', ?, ?, 'episode', ?, ?, 1, ?, ?)
            """
            evidence_json = json.dumps({
                "episode_id": episode_id,
                "title": diary_title,
                "source_memory_id": auto_mid,
            }, ensure_ascii=False)
            connection.execute(
                sql_timeline,
                (
                    scope.bot_id,
                    scope.session.id,
                    scope.subject_principal_id,
                    f"【日记】{diary_title}：{episode_summary}",
                    weight,
                    now,
                    evidence_json,
                    now,
                ),
            )

            return episode_id

        try:
            if coordinator is not None and callable(getattr(coordinator, "transaction_blocking", None)):
                episode_id = int(coordinator.transaction_blocking(persist_all))
            else:
                episode_id = int(persist_all(conn))
                if hasattr(conn, "commit"):
                    conn.commit()

            return (
                f"✅ **今日心智日记与经历已成功落盘 (Episode #{episode_id})**\n"
                f"- **标题**：{diary_title}\n"
                f"- **主线摘要**：{episode_summary}\n"
                f"- **时间线节点**：已作为经历锚点钉入 Bot 经历时间线 (`scoped_soul_timeline`)\n"
                f"- **说明**：此篇日记已成为你今日生命历程的正式记忆。如需记录具体事实、信念或人物好感，请配合使用专门工具。"
            )
        except Exception as exc:
            return f"日记记录失败: {exc}"


__all__ = ["WaveMemoryRecordDiaryEpisodeTool"]
