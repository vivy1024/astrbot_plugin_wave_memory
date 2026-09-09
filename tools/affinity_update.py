"""WaveMemory affinity update tool — records real relationship events."""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

try:  # 兼容插件包导入和仓库测试直接导入
    from ..domain.scope import RuntimeScope
    from .person_identity import display_name_for_user, resolve_user_id
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover - 由仓库测试直接导入 tools 使用
    from domain.scope import RuntimeScope
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


@dataclass
class WaveMemoryAffinityUpdateTool(FunctionTool[AstrAgentContext]):
    """让模型通过受约束的关系事件真实更新好感度。"""

    name: str = "wave_memory_affinity_update"
    description: str = (
        "记录一次真实的关系变化事件。只能增减某个关系维度，不能直接设置总分。"
        "必须提供 target_user、dimension、delta、event_type、reason。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user": {"type": "string", "description": "目标群友名字/别名/QQ号"},
            "dimension": {
                "type": "string",
                "enum": ["familiarity", "trust", "fun", "hostility", "depth"],
                "description": "要改变的关系维度",
            },
            "delta": {"type": "number", "description": "维度变化量，受系统约束，不能直接设置总分"},
            "event_type": {
                "type": "string",
                "enum": [
                    "direct_reply", "bot_praised", "bot_attacked", "correction",
                    "gift_or_feed", "confession", "joke", "deep_talk", "ignored_boundary", "manual_adjustment",
                ],
                "description": "关系事件类型",
            },
            "reason": {"type": "string", "description": "为什么这件事改变了关系，必须具体"},
        },
        "required": ["target_user", "dimension", "delta", "event_type", "reason"],
    })

    db: Any = field(default=None, repr=False)
    relationship_events: Any = field(default=None, repr=False)
    bot_db_ids: dict[str, str] = field(default_factory=dict, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db or not self.relationship_events:
            return "关系事件系统未初始化"
        if self.db.closed:
            try:
                self.db.reopen()
            except Exception:
                return "数据库连接已断开"

        runtime_scope, error_code = require_group_runtime_scope(ctx, "affinity.update")
        if error_code:
            return scope_error_message("关系更新", error_code)
        assert runtime_scope is not None

        target = (kwargs.get("target_user") or "").strip()
        dimension = kwargs.get("dimension") or ""
        event_type = kwargs.get("event_type") or ""
        reason = (kwargs.get("reason") or "").strip()
        try:
            delta = float(kwargs.get("delta", 0))
        except Exception:
            return "delta 必须是数字"

        user_id = self._resolve_user(target, runtime_scope)
        if not user_id:
            return f"没有在当前 Bot/群作用域找到目标用户「{target}」，无法更新好感度"
        target_scope = self._target_scope(runtime_scope, user_id)

        try:
            result = self.relationship_events.record_event(
                scope=target_scope,
                event_type=event_type,
                dimension=dimension,
                delta=delta,
                reason=reason,
            )
        except Exception as e:
            return f"关系事件记录失败：{e}"

        display = self._display_name(user_id, runtime_scope) or target or user_id
        return (
            f"已记录关系事件：{display} / {dimension} {result.applied_delta:+g}\n"
            f"当前好感度：{result.before_affection} → {result.after_affection}\n"
            f"原因：{reason}"
        )

    @staticmethod
    def _scope_subject_user_id(scope: RuntimeScope) -> str:
        if scope.session is None:
            return ""
        prefix = f"{scope.session.platform_id}:user:"
        principal = scope.subject_principal_id or ""
        return principal[len(prefix):] if principal.startswith(prefix) else ""

    @classmethod
    def _target_scope(cls, scope: RuntimeScope, user_id: str) -> RuntimeScope:
        if scope.visibility != "group" or scope.session is None:
            raise ValueError("relationship target requires a group RuntimeScope")
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("relationship target user_id is required")
        return RuntimeScope(
            bot_id=scope.bot_id,
            visibility="group",
            session=scope.session,
            subject_principal_id=f"{scope.session.platform_id}:user:{user_id}",
        )

    def _resolve_user(self, target: str, scope: RuntimeScope) -> str:
        """Resolve only within the active Bot + canonical group session."""
        return resolve_user_id(self.db, target, scope)

    def _display_name(self, user_id: str, scope: RuntimeScope) -> str:
        return display_name_for_user(self.db, user_id, scope)


@dataclass
class WaveMemoryAffinityTool(FunctionTool[AstrAgentContext]):
    """Read formal relationship state and scoped rankings for the active group only."""

    name: str = "wave_memory_affinity"
    description: str = (
        "查询当前 Bot 在当前 canonical 群会话内的正式关系。"
        "mode=single 查询某人；ranking 查询好感排行；blacklist 查询最低关系；"
        "active 查询互动最活跃群友。所有查询都严格限定在当前群，不读取跨群或跨 Bot 的旧总分。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user": {"type": "string", "description": "目标群友名字、别名或 QQ 号；single 模式留空表示当前发言者"},
            "user_id": {"type": "string", "description": "target_user 的兼容别名"},
            "mode": {
                "type": "string",
                "enum": ["single", "ranking", "blacklist", "active"],
                "default": "single",
                "description": "single=个人关系；ranking=好感排行；blacklist=低关系排行；active=互动活跃排行",
            },
            "limit": {"type": "integer", "default": 10, "description": "排行返回数量，1 到 50，默认 10"},
        },
        "required": [],
    })
    db: Any = field(default=None, repr=False)

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if not self.db or not getattr(self.db, "soul_repository", None):
            return "正式关系系统未初始化"
        runtime_scope, error_code = require_group_runtime_scope(ctx, "affinity.read")
        if error_code:
            return scope_error_message("关系查询", error_code)
        assert runtime_scope is not None
        mode = str(kwargs.get("mode") or "single").strip().lower()
        if mode not in {"single", "ranking", "blacklist", "active"}:
            return "mode 必须是 single、ranking、blacklist 或 active"
        try:
            limit = max(1, min(int(kwargs.get("limit", 10)), 50))
        except (TypeError, ValueError):
            return "limit 必须是 1 到 50 的整数"
        if mode != "single":
            return self._rank(mode, runtime_scope, limit)

        target = str(kwargs.get("target_user") or kwargs.get("user_id") or "").strip()
        user_id = self._resolve_user(target, runtime_scope) if target else self._current_user(runtime_scope)
        if not user_id:
            return f"没有在当前 Bot/群作用域找到目标用户「{target}」"
        target_scope = WaveMemoryAffinityUpdateTool._target_scope(runtime_scope, user_id)
        try:
            state = self.db.soul_repository.get_state(target_scope, limit=25, offset=0)
        except Exception as exc:
            return f"关系查询失败：{exc}"
        relationship = state.get("relationship") if isinstance(state, dict) else None
        if not isinstance(relationship, dict) or relationship.get("affinity") is None:
            return f"当前 Scope 尚无「{target or user_id}」的正式关系记录（状态未知）"
        dimensions = relationship.get("dimensions") or {}
        values = relationship.get("values") or {}
        display = self._people_by_id(runtime_scope).get(user_id, {}).get("display_name") or target or user_id
        lines = [
            f"关系对象：{display}（{user_id}）",
            f"好感度：{relationship.get('affinity')}（{relationship.get('state') or 'unknown'}）",
        ]
        for name in ("familiarity", "trust", "fun", "hostility", "depth"):
            item = values.get(name) if isinstance(values, dict) else None
            value = item.get("effective_value") if isinstance(item, dict) else dimensions.get(name, 0)
            lines.append(f"- {name}: {value}")
        # Durable evidence summary on formal row (if migrated); read-only.
        try:
            from services.relationship_evidence_display import format_evidence_summary_lines
        except ImportError:  # pragma: no cover
            from ..services.relationship_evidence_display import format_evidence_summary_lines
        lines.extend(
            format_evidence_summary_lines(
                relationship.get("evidence") if isinstance(relationship, dict) else None,
                max_items=2,
            )
        )
        lines.extend(self._legacy_audit_lines(target_scope))
        return "\n".join(lines)

    def _legacy_audit_lines(self, scope: RuntimeScope) -> list[str]:
        """Append historical audit summary; never affects affinity calculation."""
        repo = getattr(self.db, "soul_repository", None)
        if repo is None or not hasattr(repo, "list_legacy_relationship_audit_summary"):
            return []
        try:
            summary = repo.list_legacy_relationship_audit_summary(scope, recent_limit=3)
        except Exception:
            return []
        if not isinstance(summary, dict) or not summary.get("available"):
            return []
        total = int(summary.get("total") or 0)
        if total <= 0:
            return ["历史事件审计：0 条（仅统计，不改变好感度）"]
        by_type = summary.get("by_type") or []
        type_bits = []
        for item in by_type[:4]:
            if not isinstance(item, dict):
                continue
            type_bits.append(f"{item.get('event_type') or '?'}×{int(item.get('count') or 0)}")
        lines = [
            f"历史事件审计：{total} 条（只读侧写，不改变好感度）",
        ]
        if type_bits:
            lines.append("类型分布：" + "，".join(type_bits))
        recent = summary.get("recent") or []
        for item in recent[:3]:
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or "").strip() or "无原因"
            if len(reason) > 40:
                reason = reason[:40] + "…"
            delta = item.get("delta")
            try:
                delta_text = f"{float(delta):+g}"
            except (TypeError, ValueError):
                delta_text = str(delta or "")
            lines.append(
                f"- 近例 {item.get('event_type') or '?'}/{item.get('dimension') or '?'} "
                f"{delta_text} · {reason}"
            )
        return lines

    def _rank(self, mode: str, scope: RuntimeScope, limit: int) -> str:
        assert scope.session is not None
        people = self._people_by_id(scope)
        # ``list_relationships`` defaults to the subject attached to its Scope.
        # Rankings deliberately retain the exact Bot + group session but remove that
        # subject filter, otherwise an Agent could only rank the current speaker.
        group_scope = RuntimeScope(
            bot_id=scope.bot_id,
            visibility="group",
            session=scope.session,
        )
        try:
            relationships = self.db.soul_repository.list_relationships(group_scope)
        except Exception as exc:
            return f"关系排行查询失败：{exc}"

        def user_id_from_subject(subject: Any) -> str:
            prefix = f"{scope.session.platform_id}:user:"
            value = str(subject or "")
            return value[len(prefix):] if value.startswith(prefix) else ""

        relationship_by_user = {
            user_id: item
            for item in relationships
            if (user_id := user_id_from_subject(item.get("subject_principal_id")))
        }

        if mode == "active":
            ranked = sorted(
                people.values(),
                key=lambda item: (int(item.get("interaction_count") or 0), str(item.get("user_id") or "")),
                reverse=True,
            )[:limit]
            if not ranked:
                return "当前 Scope 没有可排行的人物画像"
            lines = [f"【当前群互动排行 TOP {len(ranked)}】"]
            for index, person in enumerate(ranked, 1):
                user_id = str(person["user_id"])
                relation = relationship_by_user.get(user_id)
                affinity = relation.get("affinity") if isinstance(relation, dict) else None
                affinity_text = "未激活" if affinity is None else f"{int(affinity):+d}"
                lines.append(f"{index}. {person['display_name']}（{user_id}） · 互动 {int(person.get('interaction_count') or 0)} · Affinity {affinity_text}")
            return "\n".join(lines)

        ranked = [item for item in relationships if item.get("affinity") is not None]
        if mode == "ranking":
            ranked = [item for item in ranked if int(item.get("affinity") or 0) > 0]
            ranked.sort(key=lambda item: (int(item.get("affinity") or 0), str(item.get("subject_principal_id") or "")), reverse=True)
            title = "当前群好感排行"
        else:
            ranked.sort(key=lambda item: (int(item.get("affinity") or 0), str(item.get("subject_principal_id") or "")))
            title = "当前群低关系排行"
        ranked = ranked[:limit]
        if not ranked:
            return f"当前 Scope 没有可展示的{title}数据"
        lines = [f"【{title} TOP {len(ranked)}】"]
        for index, relation in enumerate(ranked, 1):
            user_id = user_id_from_subject(relation.get("subject_principal_id"))
            person = people.get(user_id, {"display_name": user_id, "interaction_count": 0})
            affinity = int(relation.get("affinity") or 0)
            state = str(relation.get("state") or "unknown")
            lines.append(f"{index}. {person['display_name']}（{user_id}） · {affinity:+d} · {state} · 互动 {int(person.get('interaction_count') or 0)}")
        return "\n".join(lines)

    def _people_by_id(self, scope: RuntimeScope) -> dict[str, dict[str, Any]]:
        """Read identity helpers only inside the already verified Bot + group Scope."""
        assert scope.session is not None
        rows = self.db.conn.execute(
            """SELECT user_id, nickname, interaction_count
                 FROM user_profiles
                WHERE bot_id=? AND group_id=?""",
            (scope.bot_id, scope.session.conversation_id),
        ).fetchall()
        people: dict[str, dict[str, Any]] = {}
        for user_id, nickname, interaction_count in rows:
            if user_id is None:
                continue
            uid = str(user_id)
            people[uid] = {
                "user_id": uid,
                "display_name": display_name_for_user(self.db, uid, scope) or str(nickname or uid),
                "interaction_count": int(interaction_count or 0),
            }
        return people

    @staticmethod
    def _current_user(scope: RuntimeScope) -> str:
        if scope.session is None:
            return ""
        prefix = f"{scope.session.platform_id}:user:"
        principal = scope.subject_principal_id or ""
        return principal[len(prefix):] if principal.startswith(prefix) else ""

    def _resolve_user(self, target: str, scope: RuntimeScope) -> str:
        return resolve_user_id(self.db, target, scope)


__all__ = ["WaveMemoryAffinityTool", "WaveMemoryAffinityUpdateTool"]
