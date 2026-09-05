"""MemoryRepo — memories 与 memory_tags 的规范存储操作。"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import time
from typing import Any, Optional

import numpy as np

try:
    from ...domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - repository tests import engine as top-level
    from domain.scope import RuntimeScope

from .connection import ConnectionManager
from .migrations.memories_v2 import MEMORIES_V2_VERSION


class MemoryScopeError(ValueError):
    """拒绝把新记忆写入未解析或非群聊 Scope 的稳定错误。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message or code)


class MemoryRevisionConflict(ValueError):
    """Scoped memory mutation failed its in-transaction revision precondition."""

    def __init__(self, message: str = "memory revision precondition failed") -> None:
        self.code = "memory_revision_conflict"
        self.reason_code = self.code
        super().__init__(message)


_UNSET = object()


def _has_table_column(connection, table: str, column: str) -> bool:
    """Return whether an already-open SQLite connection exposes a column."""
    return any(str(row[1]) == column for row in connection.execute(f"PRAGMA table_info({table})").fetchall())


def _require_memory_scope(scope: RuntimeScope | None, group_id: str) -> RuntimeScope:
    if not isinstance(scope, RuntimeScope):
        raise MemoryScopeError("scope_required", "group/private RuntimeScope is required for memory writes")
    if scope.visibility not in {"group", "private"} or scope.session is None:
        raise MemoryScopeError(
            "memory_scope_visibility_unsupported",
            "memory writes only accept group/private RuntimeScope values",
        )
    if not isinstance(group_id, str) or group_id != scope.session.conversation_id:
        raise MemoryScopeError(
            "scope_session_mismatch",
            "group_id must equal the RuntimeScope canonical group id/conversation id",
        )
    return scope


def _require_group_scope(scope: RuntimeScope | None, group_id: str) -> RuntimeScope:
    scope = _require_memory_scope(scope, group_id)
    if scope.visibility != "group":
        raise MemoryScopeError(
            "memory_scope_visibility_unsupported",
            "this operation only accepts group RuntimeScope values",
        )
    return scope


def _require_mapping(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping when provided")
    return dict(value)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _column_expression(columns: set[str], name: str, fallback: str) -> str:
    return name if name in columns else fallback


def _legacy_unscoped_predicate(columns: set[str], *, table_alias: str = "") -> str:
    """Match only rows with no formal Scope fields, never partial scope rows."""
    prefix = f"{table_alias}." if table_alias else ""
    fields = ("bot_id", "session_id", "visibility")
    available = [field for field in fields if field in columns]
    if not available:
        return "1=1"
    return " AND ".join(f"COALESCE({prefix}{field}, '')=''" for field in available)


def _read_active_memory_predicates(
    columns: set[str],
    *,
    table_alias: str = "",
) -> list[str]:
    """Read-path activity filter: keep searchable rows even without formal Scope.

    WaveMemory retrieval is multi-channel (vector / FTS / tags). Scope is a
    *ranking preference*, not a hard gate: quarantine and deleted/noise stay out,
    but partial or missing bot/session rows remain searchable.
    """
    prefix = f"{table_alias}." if table_alias else ""
    predicates: list[str] = []
    if "memory_type" in columns:
        predicates.append(
            f"COALESCE({prefix}memory_type, 'message') NOT IN "
            f"('archived', 'evicted', 'deleted', 'noise')"
        )
    if "source" in columns:
        predicates.append(f"COALESCE({prefix}source, '') != 'noise'")
    if "quarantine" in columns:
        predicates.append(f"COALESCE({prefix}quarantine, 0)=0")
    # Do NOT require resolution_state='resolved' — formalize markers and legacy
    # empties must remain recallable.
    return predicates


def _memory_type_predicate(
    columns: set[str],
    *,
    table_alias: str = "",
    legacy_compat: bool = False,
) -> str:
    if "memory_type" not in columns:
        return "1=1"
    prefix = f"{table_alias}." if table_alias else ""
    excluded = "'deleted', 'noise'" if legacy_compat else "'archived', 'evicted', 'deleted'"
    return f"COALESCE({prefix}memory_type, 'message') NOT IN ({excluded})"


def _active_memory_predicates(
    columns: set[str],
    *,
    table_alias: str = "",
    legacy_compat: bool = False,
    include_memory_type: bool = True,
) -> list[str]:
    prefix = f"{table_alias}." if table_alias else ""
    predicates: list[str] = []
    if include_memory_type:
        predicates.append(_memory_type_predicate(
            columns,
            table_alias=table_alias,
            legacy_compat=legacy_compat,
        ))
    if "source" in columns:
        predicates.append(f"COALESCE({prefix}source, '') != 'noise'")
    if "resolution_state" in columns:
        predicates.append(f"COALESCE({prefix}resolution_state, '') IN ('', 'resolved')")
    if "quarantine" in columns:
        predicates.append(f"COALESCE({prefix}quarantine, 0)=0")
    return predicates


class MemoryRepo:
    """记忆数据仓库；向量唯一真相为 ``memories.vector``。"""

    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        self._create_tables()

    def _create_tables(self):
        self.cm.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                sender_id TEXT,
                sender_name TEXT,
                content TEXT NOT NULL,
                vector BLOB,
                timestamp REAL NOT NULL,
                importance REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL,
                memory_type TEXT DEFAULT 'message',
                source TEXT DEFAULT 'live',
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_tags (
                memory_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                position INTEGER DEFAULT 0,
                relevance REAL DEFAULT 1.0,
                PRIMARY KEY (memory_id, tag_id),
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_memories_group ON memories(group_id);
            CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
            CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_id);
        """)
        self.cm.commit()

    def _memories_columns(self) -> set[str]:
        return {
            row[1]
            for row in self.cm.execute_read("PRAGMA table_info(memories)").fetchall()
        }

    def add_memory(
        self,
        group_id: str,
        content: str,
        vector: Optional[np.ndarray] = None,
        sender_id: str = "",
        sender_name: str = "",
        timestamp: Optional[float] = None,
        importance: float = 1.0,
        source: str = "live",
        *,
        scope: RuntimeScope | None = None,
        provenance: Mapping[str, Any] | None = None,
        origin_metadata: Mapping[str, Any] | None = None,
        quarantine: bool = False,
    ) -> int:
        """写入一条 v2 记忆；新写入必须携带已解析的群聊 Scope。

        ``group_id`` 保留为 legacy 外形，但仅作为对 RuntimeScope canonical
        conversation ID 的断言，不能再独立决定新记录的归属。
        """
        scope = _require_memory_scope(scope, group_id)
        if scope.visibility == "private":
            required_v2 = {"bot_id", "session_id", "visibility", "resolution_state", "quarantine", "version"}
            missing = required_v2 - self._memories_columns()
            if missing:
                raise MemoryScopeError(
                    "memory_schema_v2_required",
                    "private memory writes require the complete v2 schema",
                )
        metadata = _require_mapping(provenance, "provenance")
        origin = _require_mapping(origin_metadata, "origin_metadata")
        ts = timestamp or time.time()
        vec_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        scope_payload = {
            "bot_id": scope.bot_id,
            "session_id": scope.session.id,
            "visibility": scope.visibility,
            "group_id": scope.session.conversation_id,
        }
        origin_payload = {
            "kind": "wave_memory_origin",
            "version": MEMORIES_V2_VERSION,
            "scope": scope_payload,
            "content": content,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "timestamp": ts,
            "source": source,
            "metadata": origin,
        }
        origin_fingerprint = hashlib.sha256(
            _canonical_json(origin_payload).encode("utf-8")
        ).hexdigest()
        provenance_payload = {
            "kind": "wave_memory_provenance",
            "version": MEMORIES_V2_VERSION,
            "fingerprint_algorithm": "sha256",
            "origin_fingerprint": origin_fingerprint,
            "scope": scope_payload,
            "metadata": metadata,
        }
        cur = self.cm.execute_write(
            """INSERT INTO memories (
                    group_id, sender_id, sender_name, content, vector, timestamp, importance, source,
                    bot_id, session_id, visibility, origin_fingerprint, provenance, version,
                    quarantine, resolution_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                group_id, sender_id, sender_name, content, vec_blob, ts, importance, source,
                scope.bot_id, scope.session.id, scope.visibility, origin_fingerprint,
                _canonical_json(provenance_payload), MEMORIES_V2_VERSION, int(bool(quarantine)),
                "resolved",
            ),
        )
        self.cm.commit()
        return cur.lastrowid

    def get_memory_by_id(self, memory_id: int) -> Optional[dict]:
        row = self.cm.execute_read(
            "SELECT id, group_id, sender_id, sender_name, content, vector, timestamp, importance, access_count FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "group_id": row[1], "sender_id": row[2],
            "sender_name": row[3], "content": row[4],
            "vector": np.frombuffer(row[5], dtype=np.float32) if row[5] else None,
            "timestamp": row[6], "importance": row[7], "access_count": row[8],
        }

    def get_all_memory_vectors(self, group_id: Optional[str] = None) -> list:
        quarantine_filter = (
            " AND COALESCE(quarantine, 0)=0"
            if "quarantine" in self._memories_columns()
            else ""
        )
        if group_id:
            rows = self.cm.execute_read(
                "SELECT id, vector FROM memories WHERE group_id=? AND vector IS NOT NULL "
                "AND memory_type = 'message'" + quarantine_filter,
                (group_id,),
            ).fetchall()
        else:
            rows = self.cm.execute_read(
                "SELECT id, vector FROM memories WHERE vector IS NOT NULL "
                "AND memory_type = 'message'" + quarantine_filter
            ).fetchall()
        return [(r[0], np.frombuffer(r[1], dtype=np.float32)) for r in rows]

    def get_memories_by_ids(
        self,
        ids: list,
        *,
        scope: RuntimeScope | None = None,
        allow_unscoped: bool = False,
        allow_cross_group_recall: bool = False,
        shared_grant_memory_ids: list[int] | tuple[int, ...] | None = None,
    ) -> list:
        """Read hot IDs through the formal Scope boundary plus legacy group fallback.

        A scoped request receives exact modern rows and fully unscoped legacy rows
        from the same canonical group only. A no-Scope caller must opt in
        explicitly, and then receives legacy rows only; formal scoped rows never
        leak through that compatibility path.

        ``shared_grant_memory_ids`` is a narrow allow-list for *read* expansion:
        those IDs may be returned even when their owner group differs, but only
        if they are formal resolved group rows. This is not physical fanout and
        does not authorize touch/write.
        """
        if not ids:
            return []
        columns = self._memories_columns()
        if not {"id", "group_id", "content"} <= columns:
            return []
        try:
            normalized_ids = []
            for value in ids:
                try:
                    ival = int(value)
                    # 严格限制在合法 SQLite 64 位有符号整型范围内，杜绝 Python int too large 溢出崩溃
                    if 0 < ival <= 9223372036854775807:
                        normalized_ids.append(ival)
                except (TypeError, ValueError):
                    continue
        except Exception:
            return []
        if not normalized_ids:
            return []
        grant_ids: list[int] = []
        if shared_grant_memory_ids:
            seen_g: set[int] = set()
            for raw in shared_grant_memory_ids:
                try:
                    gid = int(raw)
                except (TypeError, ValueError):
                    continue
                if 0 < gid <= 9223372036854775807 and gid not in seen_g:
                    seen_g.add(gid)
                    grant_ids.append(gid)
                if len(grant_ids) >= 5000:
                    break
        grant_id_set = set(grant_ids)
        placeholders = ",".join("?" * len(normalized_ids))
        selected = {
            "sender_id": _column_expression(columns, "sender_id", "''"),
            "sender_name": _column_expression(columns, "sender_name", "''"),
            "timestamp": _column_expression(columns, "timestamp", "0"),
            "importance": _column_expression(columns, "importance", "1.0"),
            "access_count": _column_expression(columns, "access_count", "0"),
            "source": _column_expression(columns, "source", "''"),
            "memory_type": _column_expression(columns, "memory_type", "'message'"),
            "bot_id": _column_expression(columns, "bot_id", "''"),
            "session_id": _column_expression(columns, "session_id", "''"),
            "visibility": _column_expression(columns, "visibility", "''"),
            "resolution_state": _column_expression(columns, "resolution_state", "''"),
            "quarantine": _column_expression(columns, "quarantine", "0"),
            "origin_fingerprint": _column_expression(columns, "origin_fingerprint", "''"),
            "provenance": _column_expression(columns, "provenance", "''"),
        }
        # Every formal group row is owner-scoped; fully unscoped legacy rows are
        # retained as a separate compatibility lane.  Private is never a legacy
        # fallback and always uses its exact owner tuple below.
        where = [f"id IN ({placeholders})"]
        parameters: list[Any] = list(normalized_ids)
        private_active = _memory_type_predicate(columns)

        supplied_scope = scope
        if supplied_scope is not None:
            try:
                scope = _require_memory_scope(
                    supplied_scope,
                    supplied_scope.session.conversation_id
                    if isinstance(supplied_scope, RuntimeScope) and supplied_scope.session else "",
                )
            except Exception:
                # A supplied but unsupported RuntimeScope (including bot_private)
                # must never fall through to an unscoped compatibility read.
                return []

        if isinstance(scope, RuntimeScope) and scope.session is not None and scope.visibility == "private":
            # Private reads are an exact v2 lane. Never widen them with grants,
            # cross-group recall, unscoped compatibility, or missing-column fallbacks.
            required_private = {"bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
            if required_private - columns or allow_cross_group_recall or grant_ids:
                return []
            where.extend([
                "bot_id=? AND session_id=? AND visibility='private'",
                "COALESCE(group_id, '')=?",
                "resolution_state='resolved'",
                "COALESCE(quarantine, 0)=0",
                private_active,
            ])
            parameters.extend([
                scope.bot_id,
                scope.session.id,
                scope.session.conversation_id,
            ])
        elif isinstance(scope, RuntimeScope) and scope.session is not None:
            # Group reads allow only the exact formal owner plus fully-unscoped
            # legacy rows in the current group.  Cross-group recall may expand to
            # other canonical group owners, never private/partial/unresolved rows.
            formal_lane = "0=1"
            if {"bot_id", "session_id", "visibility", "resolution_state", "quarantine"} <= columns:
                canonical_group_session = (
                    "instr(session_id, ':group:') > 0 "
                    "AND substr(session_id, instr(session_id, ':group:') + 7) = group_id"
                )
                formal_lane = " AND ".join([
                    "COALESCE(bot_id, '')<>''",
                    "COALESCE(session_id, '')<>''",
                    "visibility='group'",
                    "resolution_state='resolved'",
                    canonical_group_session,
                    *_read_active_memory_predicates(columns),
                ])
            legacy_lane = " AND ".join([
                _legacy_unscoped_predicate(columns),
                "COALESCE(group_id, '') NOT LIKE 'private:%'",
                *_active_memory_predicates(columns, legacy_compat=True),
            ])
            if allow_cross_group_recall:
                where.append(f"(({formal_lane}) OR ({legacy_lane}))")
            else:
                local_formal = f"(({formal_lane}) AND bot_id=? AND session_id=? AND group_id=?)"
                local_legacy = f"(({legacy_lane}) AND group_id=?)"
                local_lane = f"({local_formal} OR {local_legacy})"
                parameters.extend([
                    scope.bot_id,
                    scope.session.id,
                    scope.session.conversation_id,
                    scope.session.conversation_id,
                ])
                if grant_ids:
                    g_placeholders = ",".join("?" * len(grant_ids))
                    grant_lane = f"(({formal_lane}) AND id IN ({g_placeholders}))"
                    where.append(f"({local_lane} OR {grant_lane})")
                    parameters.extend(grant_ids)
                else:
                    where.append(local_lane)
        elif allow_unscoped or scope is None:
            # No-Scope compatibility must not reveal either formal or legacy private rows.
            where.extend(_read_active_memory_predicates(columns))
            if "visibility" in columns:
                where.append("COALESCE(visibility, '') != 'private'")
            where.append("COALESCE(group_id, '') NOT LIKE 'private:%'")
        else:
            return []

        rows = self.cm.execute_read(
            f"""SELECT id, group_id, {selected['sender_id']} AS sender_id,
                       {selected['sender_name']} AS sender_name, content,
                       {selected['timestamp']} AS timestamp, {selected['importance']} AS importance,
                       {selected['access_count']} AS access_count, {selected['source']} AS source,
                       {selected['memory_type']} AS memory_type, {selected['bot_id']} AS bot_id,
                       {selected['session_id']} AS session_id, {selected['visibility']} AS visibility,
                       {selected['resolution_state']} AS resolution_state,
                       {selected['quarantine']} AS quarantine,
                       {selected['origin_fingerprint']} AS origin_fingerprint,
                       {selected['provenance']} AS provenance
                  FROM memories WHERE {' AND '.join(where)}""",
            parameters,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            lane = "legacy" if not any(str(value or "").strip() for value in r[10:13]) else "catalog"
            provenance_raw = r[16]
            provenance: dict[str, Any] = {}
            if isinstance(provenance_raw, str) and provenance_raw.strip():
                try:
                    loaded = json.loads(provenance_raw)
                    if isinstance(loaded, dict):
                        provenance = loaded
                except Exception:
                    provenance = {}
            elif isinstance(provenance_raw, dict):
                provenance = provenance_raw
            item = {
                "id": r[0], "group_id": r[1], "sender_id": r[2], "sender_name": r[3],
                "content": r[4], "timestamp": r[5], "importance": r[6],
                "access_count": r[7], "source": r[8], "memory_type": r[9],
                "bot_id": r[10], "session_id": r[11], "visibility": r[12],
                "resolution_state": r[13], "quarantine": r[14],
                "origin_fingerprint": r[15], "provenance": provenance,
                "_tag_lane": lane,
            }
            if str(provenance.get("projection_kind") or "") == "fanout_duplicate":
                item["_fanout_duplicate"] = True
                item["fanout_family_id"] = provenance.get("fanout_family_id")
            if int(r[0]) in grant_id_set and str(r[1] or "") != (
                scope.session.conversation_id if isinstance(scope, RuntimeScope) and scope.session else ""
            ):
                item["_shared_grant"] = True
            result.append(item)
        return result

    def find_recent_duplicate_memory(
        self,
        *,
        scope: RuntimeScope,
        normalized_content: str,
        since_ts: float,
    ) -> int | None:
        """仅在完整 v2 Scope 内查询近期重复记忆，legacy NULL 永不命中。"""
        if not isinstance(scope, RuntimeScope):
            raise MemoryScopeError(
                "scope_required",
                "group RuntimeScope is required for scoped duplicate lookup",
            )
        scope = _require_memory_scope(scope, scope.session.conversation_id if scope.session else "")
        if not isinstance(normalized_content, str) or not normalized_content:
            return None
        required_v2 = {"bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
        if required_v2 - self._memories_columns():
            return None
        row = self.cm.execute_read(
            """SELECT id FROM memories
               WHERE group_id=?
                 AND bot_id=?
                 AND session_id=?
                 AND visibility=?
                 AND resolution_state='resolved'
                 AND COALESCE(quarantine, 0)=0
                 AND content=?
                 AND timestamp>=?
                 AND memory_type='message'
               ORDER BY timestamp DESC, id DESC
               LIMIT 1""",
            (
                scope.session.conversation_id,
                scope.bot_id,
                scope.session.id,
                scope.visibility,
                normalized_content,
                since_ts,
            ),
        ).fetchone()
        return int(row[0]) if row else None

    def touch_memories(self, ids: list, importance_boost: float = 0.01):
        """标记记忆被访问 + 微量提升 importance。"""
        now = time.time()
        for mid in ids:
            self.cm.execute_write(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ?, importance = MIN(3.0, importance + ?) WHERE id = ?",
                (now, importance_boost, mid),
            )
        self.cm.commit()

    def get_memory_count(self, group_id: Optional[str] = None) -> int:
        if group_id:
            return self.cm.execute_read(
                "SELECT COUNT(*) FROM memories WHERE group_id=?", (group_id,)
            ).fetchone()[0]
        return self.cm.execute_read("SELECT COUNT(*) FROM memories").fetchone()[0]

    def link_memory_tags(self, memory_id: int, tag_ids: list):
        for pos, tid in enumerate(tag_ids, 1):
            self.cm.execute_write(
                "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position) VALUES (?, ?, ?)",
                (memory_id, tid, pos),
            )
        self.cm.commit()

    def get_memory_vectors(self, memory_ids: list) -> dict:
        """从规范列批量获取记忆向量，返回 ``{memory_id: np.ndarray}``。"""
        if not memory_ids:
            return {}
        placeholders = ",".join("?" * len(memory_ids))
        rows = self.cm.execute_read(
            f"SELECT id, vector FROM memories WHERE id IN ({placeholders}) AND vector IS NOT NULL",
            memory_ids,
        ).fetchall()
        result = {}
        for row in rows:
            try:
                vector = np.frombuffer(row[1], dtype=np.float32)
                if len(vector) > 0:
                    result[row[0]] = vector
            except Exception:
                continue
        return result

    @staticmethod
    def update_scoped_memory(
        connection,
        *,
        scope: RuntimeScope,
        memory_id: int,
        expected_revision: int,
        content: Any = _UNSET,
        importance: Any = _UNSET,
        vector: Any = _UNSET,
    ) -> dict[str, int]:
        """Update one resolved memory under exact Scope/revision without committing.

        Content changes invalidate the canonical vector unless a replacement vector is
        supplied in the same transaction. The caller owns commit/rollback and outbox.
        """
        scope = _require_memory_scope(
            scope,
            scope.session.conversation_id if isinstance(scope, RuntimeScope) and scope.session else "",
        )
        if content is _UNSET and importance is _UNSET and vector is _UNSET:
            raise ValueError("at least one mutable memory field is required")
        updates: list[str] = []
        parameters: list[Any] = []
        if content is not _UNSET:
            updates.append("content=?")
            parameters.append(str(content or ""))
            if vector is _UNSET:
                updates.append("vector=NULL")
        if importance is not _UNSET:
            try:
                normalized_importance = float(importance)
            except (TypeError, ValueError) as exc:
                raise ValueError("importance must be numeric") from exc
            if not 0.0 <= normalized_importance <= 3.0:
                raise ValueError("importance must be between 0 and 3")
            updates.append("importance=?")
            parameters.append(normalized_importance)
        if vector is not _UNSET:
            if vector is None:
                vector_blob = None
            elif isinstance(vector, np.ndarray):
                vector_blob = vector.astype(np.float32).tobytes()
            else:
                vector_blob = np.asarray(vector, dtype=np.float32).reshape(-1).tobytes()
            updates.append("vector=?")
            parameters.append(vector_blob)
        updates.append("version=version+1")
        assert scope.session is not None
        quarantine_filter = " AND COALESCE(quarantine, 0)=0" if _has_table_column(connection, "memories", "quarantine") else ""
        cursor = connection.execute(
            f"UPDATE memories SET {', '.join(updates)} "
            "WHERE id=? AND version=? AND bot_id=? AND session_id=? AND visibility=? "
            "AND group_id=? AND resolution_state='resolved'" + quarantine_filter,
            (
                *parameters,
                int(memory_id),
                int(expected_revision),
                scope.bot_id,
                scope.session.id,
                scope.visibility,
                scope.session.conversation_id,
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            raise MemoryRevisionConflict()
        return {
            "memory_id": int(memory_id),
            "previous_revision": int(expected_revision),
            "revision": int(expected_revision) + 1,
        }

    @staticmethod
    def delete_scoped_memories(
        connection,
        *,
        scope: RuntimeScope,
        expected_revisions: Mapping[int, int],
    ) -> tuple[dict[str, int], ...]:
        """Delete a scoped batch atomically after validating every expected revision.

        No row is changed until all targets pass the same Scope/revision predicate.
        The caller owns the surrounding transaction and committed outbox events.
        """
        scope = _require_memory_scope(
            scope,
            scope.session.conversation_id if isinstance(scope, RuntimeScope) and scope.session else "",
        )
        targets = {
            int(memory_id): int(revision)
            for memory_id, revision in expected_revisions.items()
        }
        if not targets:
            raise ValueError("at least one memory target is required")
        ids = tuple(targets)
        placeholders = ",".join("?" for _ in ids)
        assert scope.session is not None
        quarantine_filter = " AND COALESCE(quarantine, 0)=0" if _has_table_column(connection, "memories", "quarantine") else ""
        rows = connection.execute(
            f"SELECT id, version FROM memories WHERE id IN ({placeholders}) "
            "AND bot_id=? AND session_id=? AND visibility=? AND group_id=? "
            "AND resolution_state='resolved'" + quarantine_filter,
            (
                *ids,
                scope.bot_id,
                scope.session.id,
                scope.visibility,
                scope.session.conversation_id,
            ),
        ).fetchall()
        actual = {int(row[0]): int(row[1]) for row in rows}
        if len(actual) != len(targets) or any(actual.get(mid) != revision for mid, revision in targets.items()):
            raise MemoryRevisionConflict()

        table_names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "scoped_memory_tags" in table_names:
            connection.execute(
                f"DELETE FROM scoped_memory_tags WHERE memory_id IN ({placeholders})", ids
            )
        if "memory_tags" in table_names:
            connection.execute(
                f"DELETE FROM memory_tags WHERE memory_id IN ({placeholders})", ids
            )
        if "facts" in table_names:
            fact_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(facts)").fetchall()
            }
            if "source_memory_id" in fact_columns:
                connection.execute(
                    f"DELETE FROM facts WHERE source_memory_id IN ({placeholders})", ids
                )
        cursor = connection.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        if int(cursor.rowcount or 0) != len(ids):
            raise MemoryRevisionConflict()
        return tuple(
            {
                "memory_id": memory_id,
                "previous_revision": targets[memory_id],
                "revision": targets[memory_id] + 1,
            }
            for memory_id in ids
        )

    def delete_memory(self, memory_id: int) -> bool:
        existing = self.cm.execute_read("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            return False
        self.cm.execute_write("DELETE FROM memory_tags WHERE memory_id=?", (memory_id,))
        self.cm.execute_write("DELETE FROM memories WHERE id=?", (memory_id,))
        self.cm.commit()
        self.cm._sync_index_delete([memory_id])
        return True

    def delete_memories(self, ids: list) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        self.cm.execute_write(f"DELETE FROM memory_tags WHERE memory_id IN ({placeholders})", ids)
        cursor = self.cm.execute_write(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        self.cm.commit()
        self.cm._sync_index_delete(ids)
        return cursor.rowcount

    def update_memory(self, memory_id: int, content: str = None, importance: float = None) -> bool:
        updates = []
        params = []
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if importance is not None:
            updates.append("importance = ?")
            params.append(importance)
        if not updates:
            return False
        params.append(memory_id)
        self.cm.execute_write(f"UPDATE memories SET {', '.join(updates)} WHERE id=?", params)
        self.cm.commit()
        return True

    def update_memory_vector(self, memory_id: int, vector: np.ndarray):
        self.cm.execute_write(
            "UPDATE memories SET vector=? WHERE id=?",
            (vector.tobytes(), memory_id),
        )
        self.cm.commit()

    def get_memories_without_tags(self, limit: int = 100) -> list:
        rows = self.cm.execute_read(
            """SELECT id FROM memories
               WHERE id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)
               AND LENGTH(content) >= 10
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_memories_without_vector(self, limit: int = 100) -> list:
        rows = self.cm.execute_read(
            "SELECT id FROM memories WHERE vector IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_cooccurrence_data(self) -> list:
        rows = self.cm.execute_read("""
            SELECT a.tag_id, b.tag_id, COUNT(*) as cnt
            FROM memory_tags a
            JOIN memory_tags b ON a.memory_id = b.memory_id AND a.tag_id < b.tag_id
            GROUP BY a.tag_id, b.tag_id
        """).fetchall()
        return rows
