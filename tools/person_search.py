"""Wave Memory 人物检索工具 — 按 QQ 主键查询。

默认只查当前群；可选 all_groups 跨群按 QQ 检索（只读，不 fanout）。
名字只用于解析到 QQ；真正检索一律使用 QQ。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

try:
    from ..domain.scope import RuntimeScope
    from ..engine.db.connection import ConnectionManager
    from .person_identity import display_name_for_user, resolve_user_id
    from .scope_boundary import require_group_runtime_scope, scope_error_message
except ImportError:  # pragma: no cover - direct tools imports in isolated tests
    from domain.scope import RuntimeScope
    from engine.db.connection import ConnectionManager
    from tools.person_identity import display_name_for_user, resolve_user_id
    from tools.scope_boundary import require_group_runtime_scope, scope_error_message


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_search_scope(raw: Any) -> str:
    """Return current_group | all_groups."""
    text = str(raw or "current_group").strip().lower()
    if text in {"all", "all_groups", "cross_group", "global", "cross"}:
        return "all_groups"
    if text in {"1", "true", "yes", "on"}:
        return "all_groups"
    return "current_group"


@dataclass
class WaveMemoryPersonSearchTool(FunctionTool[AstrAgentContext]):
    """按 QQ 主键检索人物相关记忆（默认当前群，可选跨群）。"""

    name: str = "wave_memory_person_search"
    description: str = (
        "按人物搜索记忆。支持 QQ 号或昵称；昵称会先解析为 QQ，再按 QQ 精确查询。"
        "默认只查当前群；需要看此人在其它群的发言时设 scope=all_groups。"
        "query_type: recent=最近发言, about=被提及/关于此人, social=常互动对象, profile=人物画像, "
        "timeline=人物时间线完整摘要与详情。timeline 可用 query 搜索关键词或旧事实编号，"
        "event_id 精确查询时间线编号，offset 分页；所有查询仍受当前 Bot/人物/群范围限制。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "要查找的人物（昵称或 QQ 号）",
            },
            "query_type": {
                "type": "string",
                "enum": ["recent", "about", "social", "profile", "timeline"],
                "description": "查询类型：recent/about/social/profile/timeline（完整人物时间线）",
                "default": "recent",
            },
            "scope": {
                "type": "string",
                "enum": ["current_group", "all_groups"],
                "description": (
                    "检索范围：current_group=仅当前群（默认）；"
                    "all_groups=跨群按同一 QQ 检索（只读，结果带群号，按时间倒序）"
                ),
                "default": "current_group",
            },
            "cross_group": {
                "type": "boolean",
                "description": "兼容参数：true 等价于 scope=all_groups",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "返回数量，默认 8",
                "default": 8,
            },
            "query": {
                "type": "string",
                "description": "仅 timeline：搜索关键词或旧事实编号；空字符串查询全部",
                "default": "",
            },
            "event_id": {
                "type": "integer",
                "minimum": 1,
                "description": "仅 timeline：精确时间线事件编号（不是旧事实编号），不绕过作用域限制",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "仅 timeline：分页起点，继续查询时使用返回的 next_offset",
                "default": 0,
            },
        },
        "required": ["person"],
    })

    db: Any = field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        person = str(kwargs.get("person", "") or "").strip()
        query_type = str(kwargs.get("query_type", "recent") or "recent").strip().lower()
        search_scope = _parse_search_scope(kwargs.get("scope", "current_group"))
        if _as_bool(kwargs.get("cross_group"), False):
            search_scope = "all_groups"
        try:
            limit = max(1, min(int(kwargs.get("limit", 8)), 30))
        except (TypeError, ValueError):
            limit = 8

        if not person:
            return "请提供要查找的人物名称或 QQ 号"

        # Validate the group-only boundary before touching any data source.  This
        # keeps direct/malformed calls fail-closed even when the DB is unavailable.
        scope, error_code = require_group_runtime_scope(context, "memory.message.read")
        if error_code:
            return scope_error_message("人物检索", error_code)
        assert scope is not None

        if not self.db:
            return "记忆数据库未初始化"
        if query_type == "timeline":
            # Do not reopen/bootstrap storage on this read-only path. Production
            # connections support workers; legacy sqlite fixtures are thread-bound.
            try:
                connection = getattr(self.db, "conn", None)
                manager = getattr(connection, "_mgr", connection)
                arguments = {
                    "cross_group": search_scope == "all_groups",
                    "query": kwargs.get("query", ""),
                    "event_id": kwargs.get("event_id"),
                    "offset": kwargs.get("offset", 0),
                }
                if isinstance(manager, ConnectionManager):
                    return await asyncio.to_thread(
                        self._search_timeline, scope, person, limit, **arguments
                    )
                return self._search_timeline(scope, person, limit, **arguments)
            except Exception as exc:
                logger.warning(f"[WaveMemory] PersonSearch timeline failed: {exc}")
                return f"查询出错：{exc}"
        if getattr(self.db, "closed", False):
            try:
                self.db.reopen()
            except Exception:
                return "记忆数据库连接异常"

        try:
            qq_id = resolve_user_id(self.db, person, scope)
            if not qq_id:
                return f"没有在当前 Bot/群作用域找到人物「{person}」"

            display_name = display_name_for_user(self.db, qq_id, scope)
            cross = search_scope == "all_groups"
            if query_type == "profile":
                return self._format_profile(scope, qq_id, display_name, cross_group=cross)
            if query_type == "social":
                return self._format_social(
                    scope, qq_id, display_name, limit, cross_group=cross
                )
            if query_type == "about":
                return self._format_about(
                    scope, qq_id, display_name, limit, cross_group=cross
                )
            return self._format_recent(
                scope, qq_id, display_name, limit, cross_group=cross
            )
        except Exception as exc:
            logger.warning(f"[WaveMemory] PersonSearch failed: {exc}")
            return f"查询出错：{exc}"

    def _search_timeline(
        self,
        scope: RuntimeScope,
        person: str,
        limit: int,
        *,
        cross_group: bool,
        query: Any,
        event_id: Any,
        offset: Any,
    ) -> str:
        """Read, resolve identity and format the whole page on the same thread."""
        assert scope.session is not None
        if event_id is not None:
            if (
                isinstance(event_id, bool)
                or not str(event_id).strip().isdecimal()
                or int(event_id) < 1
            ):
                return "event_id 必须是正整数时间线编号"
            event_id = int(event_id)
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            return "offset 必须是非负整数"
        query = str(query or "").strip()

        repo = getattr(self.db, "person_timeline", None)
        if repo is None:
            return "人物时间线存储未初始化"
        qq_id = resolve_user_id(self.db, person, scope)
        if not qq_id:
            return f"没有在当前 Bot/群作用域找到人物「{person}」"
        display_name = display_name_for_user(self.db, qq_id, scope)
        page = repo.page_events(
            bot_id=scope.bot_id,
            user_id=qq_id,
            group_id=None if cross_group else scope.session.conversation_id,
            query=query,
            event_id=event_id,
            limit=limit,
            offset=offset,
            strict=True,
        )
        items = page["items"]
        total = page["total"]
        next_offset = offset + len(items) if items and offset + len(items) < total else None
        place = "跨群" if cross_group else "当前群"
        parts = [
            f"【{display_name}】{place}人物时间线",
            f"QQ: {qq_id}",
            f"total: {total}",
            f"offset: {offset}",
            f"next_offset: {next_offset if next_offset is not None else 'null'}",
        ]
        if not items:
            parts.append("未找到符合条件的人物时间线事件")
        for event in items:
            stamp = event["occurred_at"]
            formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(stamp)))
            parts.extend([
                f"id: {event['id']} | 时间: {formatted_time} (occurred_at: {stamp}) | [群 {event['group_id']}]",
                f"摘要: {event['summary']}",
                f"详情: {event['detail']}",
            ])
        return "\n".join(parts)

    def _scope_memory_filter(
        self,
        scope: RuntimeScope,
        *,
        cross_group: bool = False,
    ) -> tuple[str, tuple[Any, ...]]:
        assert scope.session is not None
        # Active rows only. Historical sessions may use 羽书:group:… while runtime
        # uses qq:group:…. Cross-group mode keys on QQ (sender_id) across groups.
        active = """
            COALESCE(quarantine, 0) = 0
            AND COALESCE(memory_type, 'message') NOT IN
                ('archived', 'evicted', 'deleted', 'noise')
            AND COALESCE(source, '') NOT IN ('noise', 'identity_quarantine')
        """
        bot_clause = """
            AND (
                    COALESCE(bot_id, '') = ?
                 OR COALESCE(bot_id, '') = ''
            )
        """
        if cross_group:
            return (
                f"""
                {active}
                {bot_clause}
                AND COALESCE(group_id, '') GLOB '[0-9]*'
                """,
                (scope.bot_id,),
            )
        return (
            f"""
            {active}
            AND COALESCE(group_id, '') = ?
            {bot_clause}
            AND (
                    COALESCE(session_id, '') = ?
                 OR session_id LIKE ?
                 OR COALESCE(session_id, '') = ''
            )
            """,
            (
                scope.session.conversation_id,
                scope.bot_id,
                scope.session.id,
                f"%:group:{scope.session.conversation_id}",
            ),
        )

    def _order_prefer_current_group(self, scope: RuntimeScope, *, cross_group: bool) -> str:
        # all_groups recent/about should be true multi-group recency; group tags
        # show origin. Preferring current group first would fill the entire limit
        # with only the active home group for talkative users.
        return "timestamp DESC"

    def _format_profile(
        self,
        scope: RuntimeScope,
        qq_id: str,
        display_name: str,
        *,
        cross_group: bool = False,
    ) -> str:
        assert scope.session is not None
        conn = self.db.conn
        where, params = self._scope_memory_filter(scope, cross_group=cross_group)
        sender_count = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE sender_id=? AND {where}",
            (qq_id, *params),
        ).fetchone()[0]
        mentioned_count = conn.execute(
            f"""SELECT COUNT(*) FROM memories
                WHERE {where}
                  AND (
                        content LIKE ?
                     OR content LIKE ?
                  )""",
            (*params, f"%{qq_id}%", f"%{display_name}%"),
        ).fetchone()[0]
        first_last = conn.execute(
            f"""SELECT MIN(timestamp), MAX(timestamp)
                  FROM memories
                 WHERE sender_id=? AND {where}""",
            (qq_id, *params),
        ).fetchone()
        group_rows: list[Any] = []
        if cross_group:
            group_rows = conn.execute(
                f"""SELECT group_id, COUNT(*) AS n
                      FROM memories
                     WHERE sender_id=? AND {where}
                     GROUP BY group_id
                     ORDER BY n DESC
                     LIMIT 12""",
                (qq_id, *params),
            ).fetchall()
        interaction = conn.execute(
            """SELECT interaction_count, nickname, last_seen
                 FROM user_profiles
                WHERE user_id=? AND group_id=? AND bot_id=?
                LIMIT 1""",
            (qq_id, scope.session.conversation_id, scope.bot_id),
        ).fetchone()
        reg_cols = {str(c[1]) for c in conn.execute("PRAGMA table_info(person_registry)").fetchall()} if "person_registry" in {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()} else set()
        if "aliases" in reg_cols:
            where_reg = "qq_id=?" if "qq_id" in reg_cols else "user_id=?"
            registry_row = conn.execute(
                f"""SELECT aliases, display_name
                     FROM person_registry
                    WHERE {where_reg}
                    LIMIT 1""",
                (qq_id,),
            ).fetchone()
        else:
            registry_row = None
        relation_line = ""
        soul = getattr(self.db, "soul_repository", None)
        if soul is not None:
            try:
                target_scope = RuntimeScope(
                    bot_id=scope.bot_id,
                    visibility="group",
                    session=scope.session,
                    subject_principal_id=f"{scope.session.platform_id}:user:{qq_id}",
                )
                state = soul.get_state(target_scope, limit=5, offset=0)
                relationship = (
                    state.get("relationship") if isinstance(state, dict) else None
                )
                if isinstance(relationship, dict) and relationship.get("affinity") is not None:
                    relation_line = (
                        f"正式关系: affinity={relationship.get('affinity')} "
                        f"state={relationship.get('state') or 'unknown'}"
                    )
            except Exception:
                relation_line = ""

        title = "跨群画像" if cross_group else "当前群画像"
        parts = [f"【{display_name}】的{title}", f"QQ: {qq_id}"]
        if registry_row and registry_row[0]:
            try:
                import json as _json
                aliases_list = _json.loads(registry_row[0]) if isinstance(registry_row[0], str) else registry_row[0]
                if isinstance(aliases_list, list) and aliases_list:
                    clean_aliases = [str(a).strip() for a in aliases_list if str(a).strip()]
                    if clean_aliases:
                        parts.append(f"历史昵称/别名: {'、'.join(clean_aliases)}")
            except Exception:
                pass
        if interaction:
            parts.append(f"当前群互动次数: {int(interaction[0] or 0)}")
            if interaction[1]:
                parts.append(f"档案昵称: {interaction[1]}")
        label = "跨群发言" if cross_group else "本群发言"
        parts.append(
            f"{label}: {int(sender_count or 0)} | 被提及/相关: {int(mentioned_count or 0)}"
        )
        if group_rows:
            dist = ", ".join(f"{gid}:{int(n)}" for gid, n in group_rows if gid)
            if dist:
                parts.append(f"分群发言: {dist}")
        if first_last and first_last[0]:
            first_ts = time.strftime("%Y-%m-%d", time.localtime(float(first_last[0])))
            last_ts = time.strftime(
                "%Y-%m-%d",
                time.localtime(float(first_last[1] or first_last[0])),
            )
            span = "跨群时间跨度" if cross_group else "本群时间跨度"
            parts.append(f"{span}: {first_ts} ~ {last_ts}")
        if relation_line:
            parts.append(relation_line)
        return "\n".join(parts)

    def _format_social(
        self,
        scope: RuntimeScope,
        qq_id: str,
        display_name: str,
        limit: int,
        *,
        cross_group: bool = False,
    ) -> str:
        where, params = self._scope_memory_filter(scope, cross_group=cross_group)
        rows = self.db.conn.execute(
            f"""SELECT sender_id, COALESCE(NULLIF(sender_name, ''), sender_id) AS name, COUNT(*) AS cnt
                  FROM memories
                 WHERE {where}
                   AND content LIKE ?
                   AND sender_id != ?
                   AND COALESCE(sender_id, '') != ''
                 GROUP BY sender_id, name
                 ORDER BY cnt DESC
                 LIMIT ?""",
            (*params, f"%{qq_id}%", qq_id, limit),
        ).fetchall()
        if not rows:
            rows = self.db.conn.execute(
                f"""SELECT sender_id, COALESCE(NULLIF(sender_name, ''), sender_id) AS name, COUNT(*) AS cnt
                      FROM memories
                     WHERE {where}
                       AND content LIKE ?
                       AND sender_id != ?
                       AND COALESCE(sender_id, '') != ''
                     GROUP BY sender_id, name
                     ORDER BY cnt DESC
                     LIMIT ?""",
                (*params, f"%{display_name}%", qq_id, limit),
            ).fetchall()
        place = "跨群" if cross_group else "当前群"
        if not rows:
            return f"未找到 {display_name} 在{place}的社交共现数据"
        parts = [f"【{display_name}】{place}相关互动对象"]
        for index, (other_id, name, count) in enumerate(rows, 1):
            shown = display_name_for_user(self.db, str(other_id), scope) or name
            parts.append(f"  {index}. {shown}（{other_id}） — 相关 {int(count)} 次")
        return "\n".join(parts)

    def _format_about(
        self,
        scope: RuntimeScope,
        qq_id: str,
        display_name: str,
        limit: int,
        *,
        cross_group: bool = False,
    ) -> str:
        where, params = self._scope_memory_filter(scope, cross_group=cross_group)
        order = self._order_prefer_current_group(scope, cross_group=cross_group)
        rows = self.db.conn.execute(
            f"""SELECT timestamp, sender_name, sender_id, content, group_id
                  FROM memories
                 WHERE {where}
                   AND sender_id != ?
                   AND (
                        content LIKE ?
                     OR content LIKE ?
                   )
                 ORDER BY {order}
                 LIMIT ?""",
            (*params, qq_id, f"%{qq_id}%", f"%{display_name}%", limit),
        ).fetchall()
        place = "跨群" if cross_group else "当前群"
        if not rows:
            return f"未找到关于 {display_name} 的{place}记忆"
        parts = [f"关于【{display_name}】的{place}记忆（{len(rows)} 条）"]
        for ts, sender_name, sender_id, content, group_id in rows:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))
            speaker = sender_name or sender_id or "unknown"
            gtag = f"[群 {group_id}] " if cross_group and group_id else ""
            parts.append(f"  [{stamp}] {gtag}{speaker}: {str(content or '')[:120]}")
        return "\n".join(parts)

    def _format_recent(
        self,
        scope: RuntimeScope,
        qq_id: str,
        display_name: str,
        limit: int,
        *,
        cross_group: bool = False,
    ) -> str:
        where, params = self._scope_memory_filter(scope, cross_group=cross_group)
        if cross_group:
            # Diversify groups: pure global ORDER BY timestamp fills limit with the
            # busiest home group only. Cap rows per group then re-sort by time.
            per_group = max(2, min(5, (limit + 1) // 2))
            rows = self.db.conn.execute(
                f"""
                WITH ranked AS (
                    SELECT timestamp, content, group_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY group_id
                               ORDER BY timestamp DESC
                           ) AS rn
                      FROM memories
                     WHERE sender_id=? AND {where}
                )
                SELECT timestamp, content, group_id
                  FROM ranked
                 WHERE rn <= ?
                 ORDER BY timestamp DESC
                 LIMIT ?
                """,
                (qq_id, *params, per_group, limit),
            ).fetchall()
        else:
            order = self._order_prefer_current_group(scope, cross_group=cross_group)
            rows = self.db.conn.execute(
                f"""SELECT timestamp, content, group_id
                      FROM memories
                     WHERE sender_id=? AND {where}
                     ORDER BY {order}
                     LIMIT ?""",
                (qq_id, *params, limit),
            ).fetchall()
        place = "跨群" if cross_group else "当前群"
        if not rows:
            return f"未找到 {display_name} 在{place}的最近发言"
        parts = [f"【{display_name}】{place}最近发言（{len(rows)} 条）"]
        for ts, content, group_id in rows:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))
            gtag = f"[群 {group_id}] " if cross_group and group_id else ""
            parts.append(f"  [{stamp}] {gtag}{str(content or '')[:120]}")
        return "\n".join(parts)


__all__ = ["WaveMemoryPersonSearchTool"]
