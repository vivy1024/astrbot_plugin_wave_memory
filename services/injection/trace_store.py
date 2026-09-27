"""注入 trace 持久化。

只保存可审计摘要和限长预览，不保存 provider 凭证或无限全文。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, Iterable

from .channel_base import InjectionResult

try:
    from ...domain.scope import RuntimeScope, ScopeCodec
except ImportError:  # pragma: no cover - direct services imports in isolated tests
    from domain.scope import RuntimeScope, ScopeCodec

_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|credential|provider)", re.I)
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")
# items[].id 指向 memories.id 的通道（memory 语义召回、fts5 全文召回）
MEMORY_ITEM_CHANNELS: tuple[str, ...] = ("memory", "fts5")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()


def runtime_scope_metadata(scope: Any) -> dict[str, Any]:
    """Encode only a complete RuntimeScope for trace persistence."""
    if not isinstance(scope, RuntimeScope):
        return {}
    return {"runtime_scope": ScopeCodec.to_dict(scope)}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, val in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = _redact(val)
        return result
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[redacted]", value)
    return value


class InjectionTraceStore:
    """SQLite-backed per-request injection trace store."""

    def __init__(
        self,
        conn,
        max_preview_chars: int = 1200,
        *,
        retention_days: int | float | None = 14,
        max_rows: int | None = 5000,
        cleanup_on_record: bool = False,
        payload_retention_days: int | float | None = None,
        now_provider: Callable[[], float] | None = None,
    ):
        self.conn = conn
        self.max_preview_chars = max(20, int(max_preview_chars))
        self.retention_seconds = None if retention_days is None else max(0.0, float(retention_days) * 86400)
        self.max_rows = None if max_rows is None else max(0, int(max_rows))
        # 完整请求载荷（单条约 50 KB）只留最近几天，更早的 trace 保留通道明细与摘要
        self.payload_retention_seconds = (
            None if payload_retention_days is None else max(0.0, float(payload_retention_days) * 86400)
        )
        self.cleanup_on_record = bool(cleanup_on_record)
        self._now_provider = now_provider or time.time

    def ensure_schema(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS injection_traces (
                trace_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                mode TEXT NOT NULL,
                group_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                bot_id TEXT,
                bot_profile_id TEXT,
                message_hash TEXT,
                message_preview TEXT,
                final_preview TEXT,
                total_tokens INTEGER DEFAULT 0,
                total_chars INTEGER DEFAULT 0,
                total_latency_ms REAL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT,
                metadata_json TEXT,
                payload_json TEXT
            )"""
        )
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(injection_traces)").fetchall()}
        if "payload_json" not in columns:
            self.conn.execute("ALTER TABLE injection_traces ADD COLUMN payload_json TEXT")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS injection_trace_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                tokens INTEGER DEFAULT 0,
                chars INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0,
                score REAL,
                item_count INTEGER DEFAULT 0,
                filtered_count INTEGER DEFAULT 0,
                preview TEXT,
                details TEXT,
                FOREIGN KEY(trace_id) REFERENCES injection_traces(trace_id) ON DELETE CASCADE
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_injection_traces_ts ON injection_traces(timestamp)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_injection_traces_status ON injection_traces(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_injection_trace_channels_trace ON injection_trace_channels(trace_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_injection_trace_channels_channel ON injection_trace_channels(channel)")
        self.conn.commit()

    def _preview(self, value: Any) -> str:
        text = _SECRET_VALUE_RE.sub("[redacted]", str(value or ""))
        return text[: self.max_preview_chars]

    def _bounded_redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, val in value.items():
                if _SECRET_KEY_RE.search(str(key)):
                    result[str(key)] = "[redacted]"
                else:
                    result[str(key)] = self._bounded_redact(val)
            return result
        if isinstance(value, list):
            return [self._bounded_redact(v) for v in value]
        if isinstance(value, str):
            return self._preview(value)
        return value

    @staticmethod
    def _channel_dict(result: InjectionResult | dict[str, Any]) -> dict[str, Any]:
        if isinstance(result, InjectionResult):
            return result.__dict__.copy()
        return dict(result or {})

    def record(self, trace: dict[str, Any], channels: Iterable[InjectionResult | dict[str, Any]]) -> str:
        trace = dict(trace or {})
        trace_id = str(trace.get("trace_id") or f"trace-{int(time.time() * 1000)}")
        ts = float(trace.get("timestamp") or trace.get("ts") or time.time())
        message = str(trace.get("message") or "")
        final_text = str(trace.get("final_text") or trace.get("final_injection") or "")
        metadata_json = json.dumps(self._bounded_redact(trace.get("metadata") or {}), ensure_ascii=False, sort_keys=True)
        channel_items = [self._channel_dict(result) for result in (channels or [])]
        payload_json = json.dumps(
            _redact({"trace": trace, "channels": channel_items}),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

        self.conn.execute(
            """INSERT OR REPLACE INTO injection_traces
               (trace_id, timestamp, mode, group_id, sender_id, sender_name, bot_id, bot_profile_id,
                message_hash, message_preview, final_preview, total_tokens, total_chars,
                total_latency_ms, status, error, metadata_json, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trace_id,
                ts,
                str(trace.get("mode") or "full"),
                trace.get("group_id"),
                trace.get("sender_id"),
                trace.get("sender_name"),
                trace.get("bot_id"),
                trace.get("bot_profile_id"),
                _hash_text(message),
                self._preview(message),
                self._preview(final_text),
                int(_num(trace.get("total_tokens"))),
                int(_num(trace.get("total_chars")) or len(final_text)),
                _num(trace.get("total_latency_ms")),
                str(trace.get("status") or "ok"),
                self._preview(trace.get("error") or ""),
                metadata_json,
                payload_json,
            ),
        )
        self.conn.execute("DELETE FROM injection_trace_channels WHERE trace_id = ?", (trace_id,))

        for item in channel_items:
            text = str(item.get("text") or "")
            details = {
                "items": self._bounded_redact(item.get("items") or []),
                "filtered": self._bounded_redact(item.get("filtered") or []),
                "warnings": self._bounded_redact(item.get("warnings") or []),
                "error": self._bounded_redact(item.get("error") or ""),
            }
            try:
                details_json = json.dumps(details, ensure_ascii=False, sort_keys=True)
            except TypeError:
                details_json = json.dumps(
                    {
                        "items": [],
                        "filtered": [],
                        "warnings": details.get("warnings") or [],
                        "error": details.get("error") or "channel_details_not_json_serializable",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            self.conn.execute(
                """INSERT INTO injection_trace_channels
                   (trace_id, channel, status, tokens, chars, latency_ms, score,
                    item_count, filtered_count, preview, details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trace_id,
                    str(item.get("channel") or "unknown"),
                    str(item.get("status") or "empty"),
                    int(_num(item.get("tokens"))),
                    int(_num(item.get("chars")) or len(text)),
                    _num(item.get("latency_ms")),
                    item.get("score") if isinstance(item.get("score"), (int, float)) and not isinstance(item.get("score"), bool) else None,
                    len(item.get("items") or []),
                    len(item.get("filtered") or []),
                    self._preview(text),
                    details_json,
                ),
            )
        self.conn.commit()
        if self.cleanup_on_record:
            self.cleanup(
                now=self._now_provider(),
                retention_seconds=self.retention_seconds,
                max_rows=self.max_rows,
                payload_retention_seconds=self.payload_retention_seconds,
            )
        return trace_id

    def safe_record(self, trace: dict[str, Any], channels: Iterable[InjectionResult | dict[str, Any]]) -> bool:
        try:
            self.record(trace, channels)
            return True
        except Exception:
            return False

    def get(self, trace_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT trace_id, timestamp, mode, group_id, sender_id, sender_name, bot_id,
                      bot_profile_id, message_hash, message_preview, final_preview,
                      total_tokens, total_chars, total_latency_ms, status, error, metadata_json,
                      payload_json
               FROM injection_traces WHERE trace_id = ?""",
            (trace_id,),
        ).fetchone()
        if not row:
            return None
        channels = self._load_channels(trace_id)
        return {
            "trace_id": row[0],
            "timestamp": row[1],
            "mode": row[2],
            "group_id": row[3],
            "sender_id": row[4],
            "sender_name": row[5],
            "bot_id": row[6],
            "bot_profile_id": row[7],
            "message_hash": row[8],
            "message_preview": row[9],
            "final_preview": row[10],
            "total_tokens": row[11],
            "total_chars": row[12],
            "total_latency_ms": row[13],
            "status": row[14],
            "error": row[15],
            "metadata_json": row[16] or "{}",
            "payload_json": row[17] or "",
            "channels": channels,
        }

    def get_for_scope(self, trace_id: str, scope: RuntimeScope) -> dict[str, Any] | None:
        """Return a trace only when its persisted canonical RuntimeScope matches exactly.

        Legacy traces did not persist a self-describing scope envelope.  They are
        intentionally unreadable through formal tool paths instead of rebuilding
        a scope from their old ``group_id``/``bot_id`` columns.
        """
        if not isinstance(scope, RuntimeScope):
            return None
        trace = self.get(trace_id)
        if trace is None:
            return None
        try:
            metadata = json.loads(trace.get("metadata_json") or "{}")
            encoded_scope = metadata.get("runtime_scope") if isinstance(metadata, dict) else None
            stored_scope = ScopeCodec.from_dict(encoded_scope)
        except Exception:
            return None
        return trace if isinstance(stored_scope, RuntimeScope) and stored_scope == scope else None

    def _load_channels(self, trace_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT channel, status, tokens, chars, latency_ms, score, item_count,
                      filtered_count, preview, details
               FROM injection_trace_channels WHERE trace_id = ? ORDER BY id ASC""",
            (trace_id,),
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "channel": row[0],
                "status": row[1],
                "tokens": row[2],
                "chars": row[3],
                "latency_ms": row[4],
                "score": row[5],
                "item_count": row[6],
                "filtered_count": row[7],
                "preview": row[8],
                "details": row[9] or "{}",
            })
        return result

    @staticmethod
    def _session_group_id(session_id: Any) -> str | None:
        """从 ``<平台前缀>:group:<群号>`` 解析群号；私聊或格式不符返回 None。"""
        parts = str(session_id or "").split(":", 2)
        if len(parts) != 3 or parts[1] != "group":
            return None
        return parts[2].strip() or None

    @staticmethod
    def _query_filter(
        *,
        from_ts: float,
        to_ts: float,
        group_id: str | None = None,
        sender_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        status: str | None = None,
        has_error: bool | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        config_revision: str | None = None,
        source: str | None = None,
    ) -> tuple[str, list[Any]]:
        conditions = ["timestamp >= ?", "timestamp <= ?"]
        params: list[Any] = [float(from_ts), float(to_ts)]
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope in {"group", "group_chat"}:
            conditions.append("COALESCE(group_id, '') != ''")
        elif normalized_scope in {"private", "private_chat", "direct"}:
            conditions.append("COALESCE(group_id, '') = ''")
        for column, value in (("group_id", group_id), ("sender_id", sender_id)):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        if bot_id:
            # WebUI 传的是 BotProfile.db_id（如 "yushu"）；AstrBot 路径的旧 trace 在 bot_id 列存的是
            # QQ 号、bot_profile_id 列才是 db_id，Cortico 路径两列都是 db_id。两列任一命中即可。
            conditions.append("(bot_profile_id = ? OR bot_id = ?)")
            params.extend([bot_id, bot_id])
        if status:
            if status == "timeout":
                conditions.append(
                    "("
                    "status = ? OR instr(COALESCE(error, ''), ':timeout') > 0 OR "
                    "EXISTS (SELECT 1 FROM injection_trace_channels tc "
                    "WHERE tc.trace_id = injection_traces.trace_id AND tc.status = 'timeout')"
                    ")"
                )
                params.append(status)
            else:
                conditions.append("status = ?")
                params.append(status)
        if session_id:
            session_expr = "json_extract(metadata_json, '$.runtime_scope.session.id')"
            legacy_group = InjectionTraceStore._session_group_id(session_id)
            if legacy_group:
                # 旧 trace 没有持久化 runtime_scope，只能按群号列回退；带 canonical session 的
                # 新 trace 以 metadata 为准，避免同群号的其它平台会话串进来。
                conditions.append(f"({session_expr} = ? OR ({session_expr} IS NULL AND group_id = ?))")
                params.extend([session_id, legacy_group])
            else:
                conditions.append(f"{session_expr} = ?")
                params.append(session_id)
        if config_revision:
            conditions.append("json_extract(metadata_json, '$.config_revision') = ?")
            params.append(config_revision)
        if source:
            # v6 之前的 trace 没有 source 字段，全部来自 AstrBot 消息钩子。
            conditions.append("COALESCE(json_extract(metadata_json, '$.source'), 'astrbot') = ?")
            params.append(source)
        channel_error_exists = (
            "EXISTS (SELECT 1 FROM injection_trace_channels ec "
            "WHERE ec.trace_id = injection_traces.trace_id "
            "AND (ec.status IN ('error', 'timeout') OR COALESCE(json_extract(ec.details, '$.error'), '') != ''))"
        )
        if has_error is True:
            conditions.append(f"(COALESCE(error, '') != '' OR {channel_error_exists})")
        elif has_error is False:
            conditions.append(f"(COALESCE(error, '') = '' AND NOT {channel_error_exists})")
        if channel:
            conditions.append(
                "("
                "EXISTS (SELECT 1 FROM injection_trace_channels c "
                "WHERE c.trace_id = injection_traces.trace_id AND c.channel = ?) "
                "OR instr(COALESCE(error, ''), ?) > 0 "
                "OR instr(COALESCE(payload_json, ''), ?) > 0"
                ")"
            )
            params.extend([channel, f"{channel}:", f"\"channel\": \"{channel}\""])
        return " AND ".join(conditions), params

    def query(
        self,
        *,
        from_ts: float,
        to_ts: float,
        group_id: str | None = None,
        sender_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        status: str | None = None,
        has_error: bool | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        config_revision: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._query_filter(
            from_ts=from_ts, to_ts=to_ts, group_id=group_id, sender_id=sender_id,
            bot_id=bot_id, channel=channel, status=status, has_error=has_error, scope=scope,
            session_id=session_id, config_revision=config_revision, source=source,
        )
        rows = self.conn.execute(
            f"""SELECT trace_id, timestamp, mode, group_id, sender_id, sender_name, bot_id,
                       bot_profile_id, message_preview, final_preview, total_tokens,
                       total_chars, total_latency_ms, status, error, metadata_json
                FROM injection_traces WHERE {where}
                ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            params + [max(1, int(limit)), max(0, int(offset))],
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row[15] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            runtime_scope = metadata.get("runtime_scope") if isinstance(metadata, dict) else None
            session = runtime_scope.get("session") if isinstance(runtime_scope, dict) else None
            result.append({
                "trace_id": row[0], "timestamp": row[1], "mode": row[2], "group_id": row[3],
                "sender_id": row[4], "sender_name": row[5], "bot_id": row[6],
                "bot_profile_id": row[7], "message_preview": row[8], "final_text_preview": row[9],
                "total_tokens": row[10], "total_chars": row[11], "latency_ms": row[12],
                "status": row[13], "error": row[14],
                "has_error": bool(row[14]),
                "session": session if isinstance(session, dict) else None,
                "session_id": session.get("id") if isinstance(session, dict) else None,
                "config_revision": metadata.get("config_revision") if isinstance(metadata, dict) else None,
                "source": (metadata.get("source") if isinstance(metadata, dict) else None) or "astrbot",
            })
        return result

    def find_traces_for_memory(
        self,
        memory_id: int,
        *,
        bot_id: str,
        limit: int = 10,
        channels: Iterable[str] = MEMORY_ITEM_CHANNELS,
    ) -> list[dict[str, Any]]:
        """返回最近把 ``memory_id`` 作为命中条目注入的 trace（按时间倒序，每个 trace 一行）。

        只看 ``details.items``（真正注入的条目），不看 ``filtered``。details 由 :meth:`record`
        以 ``json.dumps(sort_keys=True)`` 默认分隔符写入，形如 ``{"group_id": "1", "id": 123, "preview": …}``，
        先用 ``instr`` 粗筛再用 ``json_each`` 精确匹配，避免 123 误中 1234。
        """
        memory_id = int(memory_id)
        bot_id = str(bot_id or "").strip()
        channel_list = [str(name) for name in channels if str(name or "").strip()]
        if not bot_id or not channel_list:
            return []
        limit = max(1, min(int(limit), 100))
        placeholders = ",".join("?" * len(channel_list))
        rows = self.conn.execute(
            f"""SELECT t.trace_id, t.timestamp, t.group_id, t.sender_id, t.sender_name,
                       t.bot_id, t.bot_profile_id, t.message_preview, t.final_preview,
                       t.status, t.metadata_json, c.channel, c.details
                  FROM injection_trace_channels c
                  JOIN injection_traces t ON t.trace_id = c.trace_id
                 WHERE c.channel IN ({placeholders})
                   AND (instr(c.details, ?) > 0 OR instr(c.details, ?) > 0)
                   AND EXISTS (
                       SELECT 1 FROM json_each(c.details, '$.items') j
                        WHERE json_extract(j.value, '$.id') = ?
                   )
                   AND (t.bot_profile_id = ? OR t.bot_id = ?)
                 ORDER BY t.timestamp DESC, c.id ASC
                 LIMIT ?""",
            [
                *channel_list,
                f'"id": {memory_id},',
                f'"id": {memory_id}}}',
                memory_id,
                bot_id,
                bot_id,
                # 同一 trace 可能在多个通道里命中同一条记忆，多取一些再按 trace 合并
                limit * len(channel_list),
            ],
        ).fetchall()
        result: list[dict[str, Any]] = []
        by_trace: dict[str, dict[str, Any]] = {}
        for row in rows:
            trace_id = str(row[0])
            try:
                details = json.loads(row[12] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            items = details.get("items") if isinstance(details, dict) else None
            hit: dict[str, Any] | None = None
            rank = 0
            for index, item in enumerate(items if isinstance(items, list) else []):
                if isinstance(item, dict) and item.get("id") == memory_id:
                    hit, rank = item, index + 1
                    break
            if hit is None:
                continue
            channel_hit = {
                "channel": row[11],
                "score": hit.get("score"),
                "similarity": hit.get("similarity"),
                "rank": rank,
                "item_count": len(items or []),
                "source": hit.get("source"),
            }
            entry = by_trace.get(trace_id)
            if entry is not None:
                entry["channels"].append(channel_hit)
                continue
            if len(result) >= limit:
                continue
            try:
                metadata = json.loads(row[10] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            runtime_scope = metadata.get("runtime_scope") if isinstance(metadata, dict) else None
            session = runtime_scope.get("session") if isinstance(runtime_scope, dict) else None
            entry = {
                "trace_id": trace_id,
                "timestamp": row[1],
                "group_id": row[2],
                "sender_id": row[3],
                "sender_name": row[4],
                "bot_id": row[5],
                "bot_profile_id": row[6],
                "message_preview": row[7],
                "final_text_preview": row[8],
                "status": row[9],
                "session_id": session.get("id") if isinstance(session, dict) else None,
                "source": (metadata.get("source") if isinstance(metadata, dict) else None) or "astrbot",
                "channel": row[11],
                "score": hit.get("score"),
                "channels": [channel_hit],
            }
            by_trace[trace_id] = entry
            result.append(entry)
        return result

    def count(self, **filters: Any) -> int:
        """返回与 ``query`` 完全相同筛选条件的精确总数。"""
        allowed = {
            key: value for key, value in filters.items()
            if key in {"from_ts", "to_ts", "group_id", "sender_id", "bot_id", "channel", "status", "has_error", "scope", "session_id", "config_revision", "source"}
        }
        where, params = self._query_filter(**allowed)
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM injection_traces WHERE {where}", params
        ).fetchone()[0])

    def cleanup(
        self,
        *,
        now: float | None = None,
        retention_seconds: float | None = 14 * 86400,
        max_rows: int | None = None,
        payload_retention_seconds: float | None = None,
    ) -> int:
        if payload_retention_seconds is not None and payload_retention_seconds >= 0:
            payload_cutoff = float(now if now is not None else time.time()) - float(payload_retention_seconds)
            self.conn.execute(
                "UPDATE injection_traces SET payload_json=NULL WHERE timestamp < ? AND payload_json IS NOT NULL",
                (payload_cutoff,),
            )
            # 必须无条件提交：没改到行时 sqlite3 也已隐式开启事务并拿着写锁，不提交会一直锁到
            # 下一次有人在这条连接上提交（线上表现为每次注入后写入协调器都拿不到锁）。
            self.conn.commit()
        delete_ids: list[str] = []
        if retention_seconds is not None and retention_seconds >= 0:
            cutoff = float(now if now is not None else time.time()) - float(retention_seconds)
            delete_ids.extend(
                r[0] for r in self.conn.execute("SELECT trace_id FROM injection_traces WHERE timestamp < ?", (cutoff,)).fetchall()
            )
        if max_rows is not None and max_rows >= 0:
            extra_rows = self.conn.execute(
                """SELECT trace_id FROM injection_traces
                   WHERE trace_id NOT IN ({})
                   ORDER BY timestamp DESC LIMIT -1 OFFSET ?""".format(
                    ",".join("?" * len(delete_ids)) if delete_ids else "''"
                ),
                (delete_ids + [int(max_rows)]) if delete_ids else [int(max_rows)],
            ).fetchall()
            delete_ids.extend([r[0] for r in extra_rows])
        if not delete_ids:
            return 0
        delete_ids = list(dict.fromkeys(delete_ids))
        placeholders = ",".join("?" * len(delete_ids))
        self.conn.execute(f"DELETE FROM injection_trace_channels WHERE trace_id IN ({placeholders})", delete_ids)
        cur = self.conn.execute(f"DELETE FROM injection_traces WHERE trace_id IN ({placeholders})", delete_ids)
        self.conn.commit()
        return int(getattr(cur, "rowcount", 0) or 0)
