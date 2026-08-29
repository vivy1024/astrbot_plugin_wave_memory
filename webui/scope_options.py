"""生产 WebUI 的真实 Scope options 与显式请求 Scope 组合器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable

try:
    from quart import has_request_context, request
except ImportError:  # pragma: no cover - 旧单测可能注入不完整 fake Quart
    try:
        from quart import request
    except ImportError:
        request = None

    def has_request_context() -> bool:
        return request is not None

try:
    from domain.scope import RuntimeScope, SessionRef
except ImportError:  # pragma: no cover - AstrBot 包导入路径
    from ..domain.scope import RuntimeScope, SessionRef


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _table_columns(conn: Any, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _session_from_id(session_id: Any) -> SessionRef | None:
    if not isinstance(session_id, str):
        return None
    parts = session_id.split(":", 2)
    if len(parts) != 3:
        return None
    platform_id, kind, conversation_id = parts
    try:
        return SessionRef(
            id=session_id,
            platform_id=platform_id,
            kind=kind,
            conversation_id=conversation_id,
        )
    except Exception:
        return None


class RuntimeScopeOptionsSource:
    """从 Bot registry、canonical DB 与通道配置投影真实可选 Scope。"""

    def __init__(
        self,
        *,
        db: Any,
        bot_registry: Mapping[str, Any] | None,
        channel_config: Any = None,
        group_name_resolver: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._db = db
        self._bot_registry = dict(bot_registry or {})
        self._channel_config = channel_config
        self._group_name_resolver = group_name_resolver

    def _bots(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key, profile in self._bot_registry.items():
            # Registry keys/QQ ids are not canonical identities; require the stable db_id.
            db_id = str(_value(profile, "db_id", None) or "").strip()
            if not db_id:
                continue
            aliases = _value(profile, "aliases", ()) or ()
            if isinstance(aliases, str):
                aliases = [part.strip() for part in aliases.split(",") if part.strip()]
            items.append(
                {
                    "db_id": db_id,
                    "name": str(_value(profile, "name", db_id) or db_id),
                    "qq_id": str(_value(profile, "qq_id", "") or ""),
                    "aliases": list(aliases),
                    "status": "active",
                }
            )
        return sorted(items, key=lambda item: item["db_id"])

    def _registered_bot_ids(self) -> set[str]:
        return {item["db_id"] for item in self._bots()}

    def _group_name(self, bot_id: Any, group_id: Any) -> str | None:
        resolver = self._group_name_resolver
        if not callable(resolver):
            return None
        try:
            name = resolver(str(bot_id or "").strip(), str(group_id or "").strip())
        except Exception:
            return None
        normalized = str(name or "").strip()
        return normalized or None

    @staticmethod
    def _session_label(session: SessionRef, group_name: str | None) -> str:
        conversation_id = session.conversation_id
        if session.kind != "group":
            return conversation_id
        normalized_name = str(group_name or "").strip()
        return f"{normalized_name}（{conversation_id}）" if normalized_name and normalized_name != conversation_id else conversation_id

    @staticmethod
    def _add_session(
        target: dict[tuple[str, str], dict[str, Any]],
        *,
        bot_id: Any,
        session: SessionRef | None,
        source: str,
        count: Any,
        registered_bots: set[str],
        capabilities: Mapping[str, int] | None = None,
        group_name: str | None = None,
    ) -> None:
        normalized_bot = str(bot_id or "").strip()
        if not normalized_bot or normalized_bot not in registered_bots or session is None:
            return
        key = (normalized_bot, session.id)
        amount = max(0, int(count or 0))
        normalized_group_name = str(group_name or "").strip() or None
        current = target.get(key)
        if current is None:
            current = target[key] = {
                "id": session.id,
                "bot_id": normalized_bot,
                "platform_id": session.platform_id,
                "kind": session.kind,
                "conversation_id": session.conversation_id,
                "group_name": normalized_group_name,
                "label": RuntimeScopeOptionsSource._session_label(session, normalized_group_name),
                "source": source,
                "sources": [source],
                "count": amount,
                "capabilities": {},
            }
        else:
            if normalized_group_name and not current.get("group_name"):
                current["group_name"] = normalized_group_name
                current["label"] = RuntimeScopeOptionsSource._session_label(session, normalized_group_name)
            current["count"] += amount
            sources = set(current["sources"])
            sources.add(source)
            current["sources"] = sorted(sources)
            current["source"] = ",".join(current["sources"])
        for name, value in (capabilities or {}).items():
            current["capabilities"][name] = current["capabilities"].get(name, 0) + max(0, int(value or 0))

    @staticmethod
    def _metadata_scope(value: Any) -> tuple[str, SessionRef] | None:
        try:
            metadata = json.loads(value or "{}") if isinstance(value, str) else value
            scope = metadata.get("runtime_scope") if isinstance(metadata, Mapping) else None
            if scope is None and isinstance(metadata, Mapping) and "bot_id" in metadata and "session" in metadata:
                scope = metadata
            if not isinstance(scope, Mapping):
                return None
            bot_id = str(scope.get("bot_id") or "").strip()
            session_data = scope.get("session")
            if isinstance(session_data, Mapping):
                session = SessionRef.from_dict(session_data)
            else:
                session = _session_from_id(session_data)
            return (bot_id, session) if bot_id and session is not None else None
        except Exception:
            return None

    @staticmethod
    def _is_chat_group_id(group_id: str) -> bool:
        value = str(group_id or "").strip()
        if not value:
            return False
        lowered = value.casefold()
        if lowered.startswith(("arc", "oni", "lore", "legacy_private", "private:")):
            return False
        if ":" in value or "/" in value:
            return False
        return True

    @staticmethod
    def _annotate_session_aliases(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in sessions:
            if item.get("kind") != "group":
                item.setdefault("alias_group", None)
                item.setdefault("is_primary_alias", True)
                item.setdefault("alias_session_ids", [item["id"]])
                continue
            key = (str(item.get("bot_id") or ""), str(item.get("kind") or ""), str(item.get("conversation_id") or ""))
            grouped.setdefault(key, []).append(item)
        for members in grouped.values():
            ranked = sorted(
                members,
                key=lambda item: (
                    -int(item.get("count") or 0),
                    len(str(item.get("platform_id") or "")),
                    str(item.get("id") or ""),
                ),
            )
            primary = ranked[0]
            alias_ids = [str(item["id"]) for item in ranked]
            for item in members:
                item["alias_group"] = f"{primary['bot_id']}:{primary['kind']}:{primary['conversation_id']}"
                item["is_primary_alias"] = item["id"] == primary["id"]
                item["alias_session_ids"] = alias_ids
                if not item["is_primary_alias"]:
                    item["label"] = f"{item.get('label') or item['conversation_id']} · {item['platform_id']}残留"
        return sessions

    def _legacy_groups(self, conn: Any) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        def add(bot_id: Any, group_id: Any, source: str, count: Any) -> None:
            bot, group = str(bot_id or "").strip(), str(group_id or "").strip()
            if not bot or not group or not RuntimeScopeOptionsSource._is_chat_group_id(group):
                return
            key = (bot, group)
            item = groups.setdefault(key, {"bot_id": bot, "group_id": group, "label": group, "source": source, "count": 0})
            item["count"] += max(0, int(count or 0))
            sources = set(str(item["source"]).split(","))
            sources.add(source)
            item["source"] = ",".join(sorted(sources))
        for table, source in (("user_profiles", "profiles"), ("relationship_events", "relationships")):
            columns = _table_columns(conn, table)
            if {"bot_id", "group_id"}.issubset(columns):
                for bot_id, group_id, count in conn.execute(
                    f"SELECT bot_id,group_id,COUNT(*) FROM {table} WHERE bot_id IS NOT NULL AND group_id IS NOT NULL AND group_id!='' GROUP BY bot_id,group_id"
                ).fetchall():
                    add(bot_id, group_id, source, count)
        return sorted(groups.values(), key=lambda item: (item["bot_id"], item["group_id"]))

    def _sessions(self, conn: Any) -> list[dict[str, Any]]:
        sessions: dict[tuple[str, str], dict[str, Any]] = {}
        registered_bots = self._registered_bot_ids()
        memory_columns = _table_columns(conn, "memories")
        if {"bot_id", "session_id"}.issubset(memory_columns):
            where = "bot_id IS NOT NULL AND session_id IS NOT NULL"
            if "resolution_state" in memory_columns:
                where += " AND resolution_state='resolved'"
            for bot_id, session_id, count in conn.execute(
                f"SELECT bot_id,session_id,COUNT(*) FROM memories WHERE {where} GROUP BY bot_id,session_id"
            ).fetchall():
                session = _session_from_id(session_id)
                self._add_session(
                    sessions,
                    bot_id=bot_id,
                    session=session,
                    source="memories",
                    count=count,
                    registered_bots=registered_bots,
                    group_name=self._group_name(bot_id, session.conversation_id if session is not None else ""),
                )

        trace_columns = _table_columns(conn, "injection_traces")
        if "metadata_json" in trace_columns:
            for (metadata_json,) in conn.execute("SELECT metadata_json FROM injection_traces").fetchall():
                parsed = self._metadata_scope(metadata_json)
                if parsed is None:
                    continue
                bot_id, session = parsed
                self._add_session(
                    sessions,
                    bot_id=bot_id,
                    session=session,
                    source="traces",
                    count=1,
                    registered_bots=registered_bots,
                    group_name=self._group_name(bot_id, session.conversation_id),
                )

        # Formal scoped tables enrich an already established canonical session;
        # they never manufacture one from a legacy group id.
        formal_tables = (
            "scoped_facts", "scoped_beliefs", "scoped_jargon", "scoped_tags",
            "scoped_soul_revisions", "scoped_soul_mood", "scoped_soul_concerns",
            "scoped_soul_timeline", "scoped_soul_relationships", "scoped_soul_relationship_events",
        )
        for table in formal_tables:
            columns = _table_columns(conn, table)
            if {"bot_id", "session_id"}.issubset(columns):
                for bot_id, session_id, count in conn.execute(
                    f"SELECT bot_id,session_id,COUNT(*) FROM {table} WHERE bot_id IS NOT NULL AND session_id IS NOT NULL GROUP BY bot_id,session_id"
                ).fetchall():
                    session = _session_from_id(session_id)
                    if (str(bot_id or "").strip(), session.id if session is not None else "") not in sessions:
                        continue
                    self._add_session(
                        sessions,
                        bot_id=bot_id,
                        session=session,
                        source=table,
                        count=count,
                        registered_bots=registered_bots,
                        capabilities={table: count},
                        group_name=self._group_name(bot_id, session.conversation_id),
                    )
        for table, scope_column in (("scoped_few_shot_examples", "runtime_scope_json"),):
            if scope_column not in _table_columns(conn, table):
                continue
            grouped: dict[tuple[str, str], int] = {}
            for (scope_json,) in conn.execute(f"SELECT {scope_column} FROM {table}").fetchall():
                parsed = self._metadata_scope(json.dumps({"runtime_scope": json.loads(scope_json or "{}")}))
                if parsed is not None:
                    bot_id, session = parsed
                    grouped[(bot_id, session.id)] = grouped.get((bot_id, session.id), 0) + 1
            for (bot_id, session_id), count in grouped.items():
                session = _session_from_id(session_id)
                if (str(bot_id or "").strip(), session.id if session is not None else "") in sessions:
                    self._add_session(
                        sessions,
                        bot_id=bot_id,
                        session=session,
                        source=table,
                        count=count,
                        registered_bots=registered_bots,
                        capabilities={table: count},
                        group_name=self._group_name(bot_id, session.conversation_id),
                    )
        return self._annotate_session_aliases(sorted(sessions.values(), key=lambda item: (item["bot_id"], item["id"])))

    def _channels(self, conn: Any) -> list[dict[str, Any]]:
        channels: dict[str, dict[str, Any]] = {}
        configured = getattr(self._channel_config, "channels", None)
        if isinstance(configured, Mapping):
            for name, config in configured.items():
                channel_id = str(name)
                channels[channel_id] = {
                    "id": channel_id,
                    "enabled": bool(_value(config, "enabled", True)),
                    "source": "runtime-config",
                }
        columns = _table_columns(conn, "injection_trace_channels")
        if "channel" in columns:
            for name, count in conn.execute(
                "SELECT channel,COUNT(*) FROM injection_trace_channels "
                "WHERE channel IS NOT NULL AND channel!='' GROUP BY channel"
            ).fetchall():
                channel_id = str(name)
                item = channels.setdefault(
                    channel_id,
                    {"id": channel_id, "enabled": True, "source": "traces"},
                )
                item["trace_count"] = int(count or 0)
                if item["source"] != "traces":
                    item["source"] = "runtime-config,traces"
        return sorted(channels.values(), key=lambda item: item["id"])

    def get_scope_options(self) -> dict[str, Any]:
        conn = getattr(self._db, "conn", None)
        if conn is None:
            raise RuntimeError("canonical database connection is unavailable")
        sessions = self._sessions(conn)
        established = {
            (str(item.get("bot_id") or ""), str(item.get("conversation_id") or ""))
            for item in sessions
            if item.get("kind") == "group"
        }
        legacy_groups = []
        for item in self._legacy_groups(conn):
            key = (str(item.get("bot_id") or ""), str(item.get("group_id") or ""))
            if key in established:
                continue
            legacy_groups.append({**item, "binding_status": "unresolved"})
        return {
            "bots": self._bots(),
            "sessions": sessions,
            "legacy_groups": legacy_groups,
            "channels": self._channels(conn),
            "source": {
                "providers": [
                    "bot_registry", "resolved_memories", "trace_runtime_scope",
                    "formal_scoped_tables", "legacy_profiles", "legacy_relationships", "channel_registry",
                ]
            },
        }


class ExplicitRequestScopeProvider:
    """只接受请求显式携带的规范 Scope，不猜测默认 Bot 或会话。"""

    def __init__(self, *, bot_registry: Mapping[str, Any] | None) -> None:
        self._bot_ids = {
            str(_value(profile, "db_id", None) or "").strip()
            for key, profile in dict(bot_registry or {}).items()
            if str(_value(profile, "db_id", key) or "").strip()
        }

    def get_request_scope(self) -> RuntimeScope | None:
        if not has_request_context():
            return None
        bot_id = str(request.args.get("bot_id") or request.headers.get("X-Wave-Bot-Id") or "").strip()
        visibility = str(
            request.args.get("visibility") or request.headers.get("X-Wave-Visibility") or ""
        ).strip()
        session_id = str(
            request.args.get("session_id") or request.headers.get("X-Wave-Session-Id") or ""
        ).strip()
        subject = str(
            request.args.get("subject_principal_id")
            or request.headers.get("X-Wave-Subject-Principal-Id")
            or ""
        ).strip()
        if not bot_id or bot_id not in self._bot_ids or not visibility:
            return None
        session = _session_from_id(session_id) if session_id else None
        try:
            return RuntimeScope(
                bot_id=bot_id,
                visibility=visibility,
                session=session,
                subject_principal_id=subject or None,
            )
        except Exception:
            return None


__all__ = ["ExplicitRequestScopeProvider", "RuntimeScopeOptionsSource"]
