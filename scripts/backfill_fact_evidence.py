"""Scoped Fact 原话证据回挂：默认只读预演，显式 ``--apply --yes`` 才写入。

默认行为与旧版一致：用 SQLite 只读 URI（``mode=ro`` + ``PRAGMA query_only``）
打开数据库，只扫描 ``scoped_facts`` 中缺失 ``source_memory_id`` 的事实，并在
同一 Scope、事实创建时间前后 ``--days`` 天的 ``memories`` 中寻找原话候选，
输出一份供人工复核的候选报告，不修改任何行。

写入模式（``--apply --yes``）只处理 ``status=suggested`` 且有唯一最高分候选的
事实，并在同一个 ``BEGIN IMMEDIATE`` 事务里：

1. 重新核对事实的 Scope、原话与三元组未变，``source_memory_id`` 仍为空；
2. 重新运行同一套匹配，候选记忆仍必须同 Scope、已解析、未隔离，且唯一命中；
3. 只更新 ``source_memory_id``、``provenance.evidence_backfill``、``updated_at``，
   并把 ``revision`` 加一；
4. 提交前生成逐行回滚 SQL，提交成功后落到 ``<db>.fact-evidence-undo-*.sql``。

由于线上库约 3.6G，默认不做整库备份；需要时用 ``--backup`` 显式指定路径。

匹配规则（不引入第三方依赖）：

1. 对 ``source_quote`` 与 ``memories.content`` 做 NFKC、空白折叠与 casefold
   归一化；
2. 先找归一化后的精确子串命中，命中分数记为 1.0；
3. 没有精确命中时，用 ``difflib.SequenceMatcher`` 计算模糊相似度（0..1）；
4. 最高分低于 ``--threshold`` 记为 ``no_match``；最高分与次高分差值小于
   0.05 时记为 ``ambiguous``。这两种情况都不会给出可写入建议。

候选记忆只取同一 Scope、时间窗口内 ``resolution_state='resolved'`` 且未隔离
（``quarantine=0``）的行，避免给出后续写入必然失败的锚点。

用法::

    python scripts/backfill_fact_evidence.py --db wave_memory.db \\
        --bot-id yushu --session qq:group:123 --visibility group \\
        --days 7 --threshold 0.72 --out candidates.csv
"""

import argparse
import csv
import difflib
import json
import math
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_DAYS = 7.0
DEFAULT_THRESHOLD = 0.72
AMBIGUITY_MARGIN = 0.05
MIN_QUOTE_LENGTH = 2

STATUS_SUGGESTED = "suggested"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NO_MATCH = "no_match"
STATUS_MISSING_QUOTE = "missing_quote"
STATUS_MISSING_SCOPE = "missing_scope"
STATUS_MISSING_CREATED_AT = "missing_created_at"

SUMMARY_KEYS = (
    "facts_seen",
    "facts_without_source_id",
    STATUS_SUGGESTED,
    STATUS_AMBIGUOUS,
    STATUS_NO_MATCH,
    STATUS_MISSING_QUOTE,
    STATUS_MISSING_SCOPE,
    STATUS_MISSING_CREATED_AT,
)

CSV_FIELDS = (
    "fact_id",
    "source_quote",
    "candidate_memory_id",
    "score",
    "match_method",
    "ambiguous",
    "suggested",
    "status",
    "reason",
    "fact_text",
    "fact_created_at",
    "bot_id",
    "session_id",
    "visibility",
    "candidate_preview",
)

_FACT_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "fact_id"),
    "bot_id": ("bot_id", "scope_bot_id", "bot"),
    "session_id": ("session_id", "scope_session_id", "session"),
    "visibility": ("visibility", "scope_visibility"),
    "source_memory_id": ("source_memory_id", "source_memory", "source_id"),
    "source_quote": ("source_quote",),
    "provenance": ("provenance",),
    "subject": ("subject",),
    "predicate": ("predicate",),
    "object": ("object",),
    "content": ("content", "fact_text", "text"),
    "created_at": ("created_at", "observed_at", "updated_at"),
}

_MEMORY_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "memory_id"),
    "content": ("content", "text"),
    "timestamp": ("timestamp", "created_at"),
    "bot_id": ("bot_id", "scope_bot_id"),
    "session_id": ("session_id", "scope_session_id"),
    "visibility": ("visibility", "scope_visibility"),
    "resolution_state": ("resolution_state",),
    "quarantine": ("quarantine",),
}


@dataclass(frozen=True)
class FactSchema:
    id: str
    bot_id: str | None
    session_id: str | None
    visibility: str | None
    source_memory_id: str
    source_quote: str | None
    provenance: str | None
    subject: str | None
    predicate: str | None
    object: str | None
    content: str | None
    created_at: str | None


@dataclass(frozen=True)
class MemorySchema:
    id: str
    content: str
    timestamp: str
    bot_id: str
    session_id: str
    visibility: str
    resolution_state: str | None
    quarantine: str | None


@dataclass
class ApplyOutcome:
    """一次写入的结果；``planned`` 是预演建议数，``applied`` 才真正落库。"""

    db_path: str
    planned: int = 0
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    backup: str | None = None
    undo_log: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_payload(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for item in self.skipped:
            reason = str(item.get("reason") or "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "dry_run": False,
            "read_only": False,
            "database": self.db_path,
            "planned": self.planned,
            "applied": len(self.applied),
            "skipped": len(self.skipped),
            "skip_reasons": reasons,
            "backup": self.backup,
            "undo_log": self.undo_log,
            "error": self.error,
            "applied_facts": list(self.applied),
            "skipped_facts": list(self.skipped),
        }


@dataclass
class BackfillReport:
    """一次只读扫描的结果；``error`` 非空表示 schema/连接层面的失败。"""

    db_path: str
    params: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=lambda: {key: 0 for key in SUMMARY_KEYS})
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "dry_run": True,
            "read_only": True,
            "database": self.db_path,
            "params": dict(self.params),
            "summary": dict(self.summary),
            "error": self.error,
            "rows": list(self.rows),
        }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return []
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _pick_column(columns: Sequence[str], aliases: Sequence[str]) -> str | None:
    for name in aliases:
        if name in columns:
            return name
    return None


def _resolve_fact_schema(columns: Sequence[str]) -> FactSchema | None:
    id_column = _pick_column(columns, _FACT_COLUMN_ALIASES["id"])
    source_column = _pick_column(columns, _FACT_COLUMN_ALIASES["source_memory_id"])
    if id_column is None or source_column is None:
        return None
    return FactSchema(
        id=id_column,
        bot_id=_pick_column(columns, _FACT_COLUMN_ALIASES["bot_id"]),
        session_id=_pick_column(columns, _FACT_COLUMN_ALIASES["session_id"]),
        visibility=_pick_column(columns, _FACT_COLUMN_ALIASES["visibility"]),
        source_memory_id=source_column,
        source_quote=_pick_column(columns, _FACT_COLUMN_ALIASES["source_quote"]),
        provenance=_pick_column(columns, _FACT_COLUMN_ALIASES["provenance"]),
        subject=_pick_column(columns, _FACT_COLUMN_ALIASES["subject"]),
        predicate=_pick_column(columns, _FACT_COLUMN_ALIASES["predicate"]),
        object=_pick_column(columns, _FACT_COLUMN_ALIASES["object"]),
        content=_pick_column(columns, _FACT_COLUMN_ALIASES["content"]),
        created_at=_pick_column(columns, _FACT_COLUMN_ALIASES["created_at"]),
    )


def _resolve_memory_schema(columns: Sequence[str]) -> MemorySchema | None:
    names = {
        key: _pick_column(columns, aliases)
        for key, aliases in _MEMORY_COLUMN_ALIASES.items()
    }
    required = ("id", "content", "timestamp", "bot_id", "session_id", "visibility")
    if any(names[key] is None for key in required):
        return None
    return MemorySchema(
        id=str(names["id"]),
        content=str(names["content"]),
        timestamp=str(names["timestamp"]),
        bot_id=str(names["bot_id"]),
        session_id=str(names["session_id"]),
        visibility=str(names["visibility"]),
        resolution_state=names["resolution_state"],
        quarantine=names["quarantine"],
    )


def _normalize(value: Any) -> str:
    """NFKC + 空白折叠 + casefold；与生产证据校验的归一化口径一致。"""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_quote(provenance: Mapping[str, Any], column_value: Any) -> str:
    """优先取 provenance.source_quote；兼容嵌套 candidate 与独立列。"""
    containers: list[Mapping[str, Any]] = [provenance]
    nested = provenance.get("candidate")
    if isinstance(nested, Mapping):
        containers.append(nested)
    for container in containers:
        quote = container.get("source_quote")
        if isinstance(quote, str) and quote.strip():
            return quote.strip()
    if isinstance(column_value, str) and column_value.strip():
        return column_value.strip()
    return ""


def _fact_text(row: Mapping[str, Any], schema: FactSchema) -> str:
    parts: list[str] = []
    for column in (schema.subject, schema.predicate, schema.object):
        if column is not None:
            value = _text(row.get(column))
            if value:
                parts.append(value)
    if parts:
        return " ".join(parts)
    return _text(row.get(schema.content)) if schema.content is not None else ""


def _as_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _missing_source_id(value: Any) -> bool:
    """NULL、空串、0、非数字都视为无法作为证据锚点。"""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return float(value) <= 0
    text = str(value).strip()
    if not text:
        return True
    try:
        return int(text) <= 0
    except (TypeError, ValueError):
        return True


def _fact_id_value(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _memory_candidates(
    conn: sqlite3.Connection,
    schema: MemorySchema,
    *,
    bot_id: str,
    session_id: str,
    visibility: str,
    created_at: float,
    days: float,
) -> list[tuple[int, str]]:
    conditions = [
        f'"{schema.bot_id}" = ?',
        f'"{schema.session_id}" = ?',
        f'"{schema.visibility}" = ?',
        f'"{schema.timestamp}" >= ?',
        f'"{schema.timestamp}" <= ?',
        f'"{schema.content}" IS NOT NULL',
        f'TRIM(CAST("{schema.content}" AS TEXT)) != \'\'',
    ]
    params: list[Any] = [
        bot_id,
        session_id,
        visibility,
        created_at - days * 86400.0,
        created_at + days * 86400.0,
    ]
    # 与生产证据校验保持一致：只有已解析且未隔离的记忆才能成为锚点。
    if schema.resolution_state is not None:
        conditions.append(f'"{schema.resolution_state}" = ?')
        params.append("resolved")
    if schema.quarantine is not None:
        conditions.append(f'COALESCE("{schema.quarantine}", 0) = 0')

    rows = conn.execute(
        f'SELECT "{schema.id}", "{schema.content}" FROM "memories" '
        f'WHERE {" AND ".join(conditions)} ORDER BY "{schema.timestamp}", "{schema.id}"',
        params,
    ).fetchall()
    candidates: list[tuple[int, str]] = []
    for memory_id, content in rows:
        try:
            parsed_id = int(memory_id)
        except (TypeError, ValueError):
            continue
        text = str(content or "")
        if parsed_id > 0 and text.strip():
            candidates.append((parsed_id, text))
    return candidates


def _match_candidates(
    quote: str,
    candidates: Sequence[tuple[int, str]],
    threshold: float,
) -> tuple[str, list[tuple[int, float, str, str]], str, str]:
    """返回 (status, [(memory_id, score, method, content)], method, reason)。"""
    normalized_quote = _normalize(quote)
    if not normalized_quote:
        return STATUS_NO_MATCH, [], "none", "no_quote"
    exact: list[tuple[int, float, str, str]] = []
    for memory_id, content in candidates:
        if normalized_quote in _normalize(content):
            exact.append((memory_id, 1.0, "exact", content))

    # 精确命中已经拿满分，但仍有必要看看是否存在几乎同分的改写候选：
    # 两条极相似的消息同样无法确定唯一来源。此时只计算可能落在
    # 0.05 歧义带内的模糊分数，避免为无关候选做无谓的 SequenceMatcher。
    fuzzy_floor = 1.0 - AMBIGUITY_MARGIN if exact else threshold
    scored: list[tuple[int, float, str, str]] = list(exact)
    quote_length = len(normalized_quote)
    for memory_id, content in candidates:
        normalized_content = _normalize(content)
        if not normalized_content or normalized_quote in normalized_content:
            continue
        # 长度上界是 ratio() 的必要条件；先剪枝，避免对明显不可能过线的
        # 长/短消息调用 SequenceMatcher。
        length_bound = (
            2.0 * min(quote_length, len(normalized_content))
            / (quote_length + len(normalized_content))
        )
        if length_bound < fuzzy_floor:
            continue
        score = difflib.SequenceMatcher(
            None, normalized_quote, normalized_content, autojunk=False
        ).ratio()
        if exact and score < fuzzy_floor:
            continue
        scored.append((memory_id, score, "fuzzy", content))

    scored.sort(key=lambda item: (-item[1], item[0]))
    if not scored:
        return STATUS_NO_MATCH, [], "none", "no_candidate_in_window"

    top = scored[0]
    if top[1] < threshold:
        return STATUS_NO_MATCH, [top], "fuzzy", "below_threshold"

    if len(scored) > 1 and top[1] - scored[1][1] < AMBIGUITY_MARGIN:
        tied = [item for item in scored if top[1] - item[1] < AMBIGUITY_MARGIN]
        reason = (
            "multiple_exact_matches"
            if all(item[2] == "exact" for item in tied)
            else "scores_within_margin"
        )
        return STATUS_AMBIGUOUS, tied, "fuzzy", reason

    return STATUS_SUGGESTED, [top], "exact" if top[2] == "exact" else "fuzzy", ""


def _base_fields(
    *,
    fact_id: Any,
    quote: str,
    fact_text: str,
    created_at: float | None,
    bot_id: str,
    session_id: str,
    visibility: str,
) -> dict[str, Any]:
    return {
        "fact_id": fact_id,
        "source_quote": quote,
        "fact_text": fact_text,
        "fact_created_at": created_at,
        "bot_id": bot_id,
        "session_id": session_id,
        "visibility": visibility,
    }


def _row(
    base: Mapping[str, Any],
    *,
    candidate_memory_id: int | None,
    score: float | None,
    method: str,
    ambiguous: bool,
    status: str,
    suggested: bool,
    candidate_preview: str,
    reason: str,
) -> dict[str, Any]:
    return {
        **base,
        "candidate_memory_id": candidate_memory_id,
        "score": round(score, 6) if score is not None else None,
        "match_method": method,
        "ambiguous": ambiguous,
        "suggested": suggested,
        "status": status,
        "reason": reason,
        "candidate_preview": candidate_preview,
    }


def _emit_result(
    report: BackfillReport,
    base: Mapping[str, Any],
    *,
    status: str,
    method: str,
    reason: str,
    candidates: Sequence[tuple[int, float, str, str]],
) -> None:
    report.summary[status] += 1
    suggested = status == STATUS_SUGGESTED
    ambiguous = status == STATUS_AMBIGUOUS
    if not candidates:
        report.rows.append(
            _row(
                base,
                candidate_memory_id=None,
                score=None,
                method=method,
                ambiguous=False,
                status=status,
                suggested=False,
                candidate_preview="",
                reason=reason,
            )
        )
        return
    for memory_id, score, candidate_method, content in candidates:
        report.rows.append(
            _row(
                base,
                candidate_memory_id=memory_id,
                score=score,
                method=candidate_method,
                ambiguous=ambiguous,
                status=status,
                suggested=suggested,
                candidate_preview=_text(content)[:160],
                reason=reason,
            )
        )


def _matches_filters(
    row: Mapping[str, Any],
    schema: FactSchema,
    *,
    bot_id: str | None,
    session_id: str | None,
    visibility: str | None,
) -> bool:
    checks = (
        (bot_id, schema.bot_id),
        (session_id, schema.session_id),
        (visibility, schema.visibility),
    )
    for expected, column in checks:
        if expected is not None and column is not None:
            if _text(row.get(column)) != expected:
                return False
    return True


def _evaluate_fact_row(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    fact_schema: FactSchema,
    memory_schema: MemorySchema,
    *,
    days: float,
    threshold: float,
) -> tuple[dict[str, Any], str, list[tuple[int, float, str, str]], str, str]:
    """按当前库内容评估一条事实；预演与写入共用这一套判定。"""
    fact_id = _fact_id_value(row.get(fact_schema.id))
    provenance = (
        _json_object(row.get(fact_schema.provenance))
        if fact_schema.provenance is not None
        else {}
    )
    quote = _source_quote(
        provenance,
        row.get(fact_schema.source_quote) if fact_schema.source_quote is not None else None,
    )
    fact_text = _fact_text(row, fact_schema)
    scope_bot = _text(row.get(fact_schema.bot_id)) if fact_schema.bot_id else ""
    scope_session = (
        _text(row.get(fact_schema.session_id)) if fact_schema.session_id else ""
    )
    scope_visibility = (
        _text(row.get(fact_schema.visibility)) if fact_schema.visibility else ""
    )
    created_at = (
        _as_timestamp(row.get(fact_schema.created_at))
        if fact_schema.created_at is not None
        else None
    )
    base = _base_fields(
        fact_id=fact_id,
        quote=quote,
        fact_text=fact_text,
        created_at=created_at,
        bot_id=scope_bot,
        session_id=scope_session,
        visibility=scope_visibility,
    )

    if not (scope_bot and scope_session and scope_visibility):
        return base, STATUS_MISSING_SCOPE, [], "none", "fact_scope_columns_empty"
    if len(_normalize(quote)) < MIN_QUOTE_LENGTH:
        return base, STATUS_MISSING_QUOTE, [], "none", "source_quote_missing_or_too_short"
    if created_at is None:
        return base, STATUS_MISSING_CREATED_AT, [], "none", "fact_created_at_missing_or_invalid"

    candidates = _memory_candidates(
        conn,
        memory_schema,
        bot_id=scope_bot,
        session_id=scope_session,
        visibility=scope_visibility,
        created_at=created_at,
        days=days,
    )
    status, matched, method, reason = _match_candidates(quote, candidates, threshold)
    return base, status, matched, method, reason


def _scan(
    conn: sqlite3.Connection,
    report: BackfillReport,
    *,
    bot_id: str | None,
    session_id: str | None,
    visibility: str | None,
    days: float,
    threshold: float,
) -> BackfillReport:
    fact_columns = _table_columns(conn, "scoped_facts")
    if not fact_columns:
        report.error = "scoped_facts table not found"
        return report

    fact_schema = _resolve_fact_schema(fact_columns)
    if fact_schema is None:
        report.error = (
            "scoped_facts has no source_memory_id column (or no id column); "
            "schema migration is required before an evidence backfill can be planned"
        )
        return report

    filter_columns = (
        ("--bot-id", bot_id, fact_schema.bot_id, "bot"),
        ("--session", session_id, fact_schema.session_id, "session"),
        ("--visibility", visibility, fact_schema.visibility, "visibility"),
    )
    for argument, expected, column, label in filter_columns:
        if expected is not None and column is None:
            report.error = f"scoped_facts has no {label} scope column for {argument}"
            return report

    memory_columns = _table_columns(conn, "memories")
    if not memory_columns:
        report.error = "memories table not found"
        return report
    memory_schema = _resolve_memory_schema(memory_columns)
    if memory_schema is None:
        report.error = (
            "memories lacks id/content/timestamp or canonical scope columns; "
            "refusing to match across unverifiable scopes"
        )
        return report

    ordered_columns = list(fact_columns)
    fact_rows = conn.execute(
        f'SELECT * FROM "scoped_facts" ORDER BY "{fact_schema.id}"'
    ).fetchall()

    for raw_row in fact_rows:
        row = dict(zip(ordered_columns, raw_row))
        if not _matches_filters(
            row,
            fact_schema,
            bot_id=bot_id,
            session_id=session_id,
            visibility=visibility,
        ):
            continue
        report.summary["facts_seen"] += 1

        if not _missing_source_id(row.get(fact_schema.source_memory_id)):
            continue
        report.summary["facts_without_source_id"] += 1

        base, status, matched, method, reason = _evaluate_fact_row(
            conn,
            row,
            fact_schema,
            memory_schema,
            days=days,
            threshold=threshold,
        )
        _emit_result(
            report,
            base,
            status=status,
            method=method,
            reason=reason,
            candidates=matched,
        )

    report.summary["candidate_rows"] = len(report.rows)
    return report


def run_backfill(
    db_path: str | Path,
    *,
    bot_id: str | None = None,
    session_id: str | None = None,
    visibility: str | None = None,
    days: float = DEFAULT_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
) -> BackfillReport:
    """只读扫描并返回候选报告；没有写入参数，也没有写入分支。"""
    try:
        days_value = float(days)
        threshold_value = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("days and threshold must be numbers") from exc
    if not math.isfinite(days_value) or days_value < 0:
        raise ValueError("days must be a finite non-negative number")
    if not math.isfinite(threshold_value) or not 0.0 <= threshold_value <= 1.0:
        raise ValueError("threshold must be between 0 and 1")

    path = Path(db_path)
    report = BackfillReport(
        db_path=str(path),
        params={
            "bot_id": bot_id,
            "session_id": session_id,
            "visibility": visibility,
            "days": days_value,
            "threshold": threshold_value,
        },
    )
    if not path.is_file():
        report.error = f"database not found: {path}"
        return report

    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        report.error = f"unable to open database read-only: {exc}"
        return report

    try:
        return _scan(
            conn,
            report,
            bot_id=bot_id,
            session_id=session_id,
            visibility=visibility,
            days=days_value,
            threshold=threshold_value,
        )
    except sqlite3.Error as exc:
        report.error = f"sqlite error: {exc}"
        return report
    finally:
        conn.close()


def _backup_database(db_path: Path, target: Path) -> Path:
    """SQLite 在线备份；目标已存在时拒绝覆盖。"""
    if target.exists():
        raise FileExistsError(f"backup target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _strict_json_object(value: Any) -> dict[str, Any]:
    """写入路径不能把无法解析的 provenance 悄悄重置成 {}。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("provenance is not a JSON object")
    return dict(parsed)


def _skip(outcome: ApplyOutcome, fact_id: Any, reason: str) -> None:
    outcome.skipped.append({"fact_id": fact_id, "reason": reason})


def apply_backfill(
    db_path: str | Path,
    *,
    bot_id: str | None = None,
    session_id: str | None = None,
    visibility: str | None = None,
    days: float = DEFAULT_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
    backup_path: str | Path | None = None,
) -> ApplyOutcome:
    """预演后在同一事务内重新校验并写入唯一命中的 source_memory_id。"""
    path = Path(db_path)
    outcome = ApplyOutcome(db_path=str(path))
    plan = run_backfill(
        path,
        bot_id=bot_id,
        session_id=session_id,
        visibility=visibility,
        days=days,
        threshold=threshold,
    )
    if not plan.ok:
        outcome.error = plan.error
        return outcome

    suggestions: dict[int, dict[str, Any]] = {}
    for row in plan.rows:
        if not row.get("suggested"):
            continue
        try:
            fact_id = int(row["fact_id"])
            memory_id = int(row["candidate_memory_id"])
        except (TypeError, ValueError):
            _skip(outcome, row.get("fact_id"), "invalid_planned_id")
            continue
        if fact_id <= 0 or memory_id <= 0:
            _skip(outcome, fact_id, "invalid_planned_id")
            continue
        suggestions[fact_id] = {**row, "candidate_memory_id": memory_id}
    outcome.planned = len(suggestions)
    if not suggestions:
        return outcome

    if backup_path is not None:
        try:
            outcome.backup = str(_backup_database(path, Path(backup_path)))
        except (OSError, sqlite3.Error) as exc:
            outcome.error = f"backup failed: {exc}"
            return outcome

    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error as exc:
        outcome.error = f"unable to open database for writing: {exc}"
        return outcome

    stamp = time.strftime("%Y%m%d-%H%M%S")
    undo_path = path.with_name(f"{path.name}.fact-evidence-undo-{stamp}.sql")
    pending_undo = undo_path.with_name(undo_path.name + ".pending")
    undo_lines = [
        f"-- fact evidence backfill undo, generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"-- database: {path}",
        "BEGIN IMMEDIATE;",
    ]

    try:
        fact_columns = _table_columns(conn, "scoped_facts")
        fact_schema = _resolve_fact_schema(fact_columns)
        memory_schema = _resolve_memory_schema(_table_columns(conn, "memories"))
        if fact_schema is None or memory_schema is None:
            outcome.error = "schema changed since planning; refusing to write"
            return outcome
        if "revision" not in fact_columns:
            outcome.error = "scoped_facts has no revision column; refusing to write"
            return outcome

        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        for fact_id in sorted(suggestions):
            expected = suggestions[fact_id]
            raw_row = conn.execute(
                f'SELECT * FROM "scoped_facts" WHERE "{fact_schema.id}"=?',
                (fact_id,),
            ).fetchone()
            if raw_row is None:
                _skip(outcome, fact_id, "fact_missing")
                continue
            row = dict(zip(fact_columns, raw_row))

            if not _matches_filters(
                row,
                fact_schema,
                bot_id=expected.get("bot_id"),
                session_id=expected.get("session_id"),
                visibility=expected.get("visibility"),
            ):
                _skip(outcome, fact_id, "scope_changed")
                continue
            if not _missing_source_id(row.get(fact_schema.source_memory_id)):
                _skip(outcome, fact_id, "already_linked")
                continue

            base, status, matched, method, _reason = _evaluate_fact_row(
                conn,
                row,
                fact_schema,
                memory_schema,
                days=days,
                threshold=threshold,
            )
            if (
                base["source_quote"] != expected.get("source_quote")
                or base["fact_text"] != expected.get("fact_text")
                or _text(base["bot_id"]) != expected.get("bot_id")
                or _text(base["session_id"]) != expected.get("session_id")
                or _text(base["visibility"]) != expected.get("visibility")
            ):
                _skip(outcome, fact_id, "fact_changed")
                continue
            if status != STATUS_SUGGESTED or not matched:
                _skip(outcome, fact_id, f"no_longer_{status}")
                continue
            memory_id, score, candidate_method, _content = matched[0]
            if memory_id != expected["candidate_memory_id"]:
                _skip(outcome, fact_id, "candidate_changed")
                continue

            try:
                provenance = (
                    _strict_json_object(row.get(fact_schema.provenance))
                    if fact_schema.provenance is not None
                    else {}
                )
            except (TypeError, ValueError):
                _skip(outcome, fact_id, "provenance_unparsable")
                continue
            previous_source = row.get(fact_schema.source_memory_id)
            previous_revision = int(row.get("revision") or 0)
            previous_updated = row.get("updated_at")
            previous_provenance = (
                row.get(fact_schema.provenance) if fact_schema.provenance is not None else None
            )
            provenance["evidence_backfill"] = {
                "schema": "fact-evidence-backfill/v1",
                "actor": "scripts.backfill_fact_evidence",
                "applied_at": now,
                "memory_id": memory_id,
                "match_method": candidate_method,
                "score": round(float(score), 6),
            }
            encoded = json.dumps(
                provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )

            set_parts = ['"source_memory_id"=?', '"revision"="revision"+1']
            params: list[Any] = [memory_id]
            if "updated_at" in fact_columns:
                set_parts.append('"updated_at"=?')
                params.append(now)
            if fact_schema.provenance is not None:
                set_parts.append('"provenance"=?')
                params.append(encoded)
            params.extend([fact_id, previous_revision])
            cursor = conn.execute(
                f'UPDATE "scoped_facts" SET {", ".join(set_parts)} '
                f'WHERE "{fact_schema.id}"=? AND "revision"=? '
                'AND ("source_memory_id" IS NULL OR "source_memory_id"=0)',
                params,
            )
            if cursor.rowcount != 1:
                _skip(outcome, fact_id, "concurrent_change")
                continue

            new_revision = previous_revision + 1
            undo_assignments = [
                f'"source_memory_id"={_sql_literal(previous_source)}',
                f'"revision"={_sql_literal(previous_revision)}',
            ]
            if "updated_at" in fact_columns:
                undo_assignments.append(f'"updated_at"={_sql_literal(previous_updated)}')
            if fact_schema.provenance is not None:
                undo_assignments.append(f'"provenance"={_sql_literal(previous_provenance)}')
            undo_lines.append(
                f'UPDATE "scoped_facts" SET {", ".join(undo_assignments)} '
                f'WHERE "{fact_schema.id}"={_sql_literal(fact_id)} '
                f"AND \"revision\"={_sql_literal(new_revision)};"
            )
            outcome.applied.append(
                {
                    "fact_id": fact_id,
                    "memory_id": memory_id,
                    "match_method": candidate_method,
                    "score": round(float(score), 6),
                    "previous_revision": previous_revision,
                    "new_revision": new_revision,
                }
            )

        if not outcome.applied:
            conn.rollback()
            return outcome

        undo_lines.append("COMMIT;")
        undo_lines.append("")
        pending_undo.write_text("\n".join(undo_lines), encoding="utf-8")
        conn.commit()
        try:
            pending_undo.replace(undo_path)
            outcome.undo_log = str(undo_path)
        except OSError as exc:
            outcome.undo_log = str(pending_undo)
            outcome.error = f"database committed, but undo log rename failed: {exc}"
        return outcome
    except (OSError, sqlite3.Error, ValueError) as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        outcome.error = f"apply failed: {exc}"
        if pending_undo.exists():
            try:
                pending_undo.unlink()
            except OSError:
                pass
        return outcome
    finally:
        conn.close()


def _csv_value(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if key == "score":
        return f"{float(value):.6f}"
    if key == "fact_created_at":
        return f"{float(value):.3f}"
    return str(value)


def write_report(report: BackfillReport, out_path: str | Path) -> Path:
    """把报告写到 .csv 或 .json；只写报告文件，不碰数据库。"""
    target = Path(out_path)
    suffix = target.suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise ValueError("--out must end with .csv or .json")
    target.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".json":
        payload = {"generated_at": time.time(), **report.to_payload()}
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in report.rows:
            writer.writerow({key: _csv_value(row, key) for key in CSV_FIELDS})
    return target


def _summary_line(report: BackfillReport) -> str:
    summary = report.summary
    return (
        f"scanned={summary['facts_seen']} "
        f"missing_source_id={summary['facts_without_source_id']} "
        f"suggested={summary[STATUS_SUGGESTED]} "
        f"ambiguous={summary[STATUS_AMBIGUOUS]} "
        f"no_match={summary[STATUS_NO_MATCH]} "
        f"missing_quote={summary[STATUS_MISSING_QUOTE]} "
        f"missing_scope={summary[STATUS_MISSING_SCOPE]} "
        f"missing_created_at={summary[STATUS_MISSING_CREATED_AT]} "
        f"rows={len(report.rows)}"
    )


def _apply_line(outcome: ApplyOutcome) -> str:
    if outcome.error:
        return f"apply error={outcome.error} planned={outcome.planned} applied={len(outcome.applied)}"
    return (
        f"planned={outcome.planned} applied={len(outcome.applied)} "
        f"skipped={len(outcome.skipped)} undo_log={outcome.undo_log or '-'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scoped fact evidence matcher. Default is read-only; "
            "writing source_memory_id requires --apply --yes."
        )
    )
    parser.add_argument("--db", required=True, type=Path, help="wave_memory.db 路径（只读打开）")
    parser.add_argument("--bot-id", default=None, help="只扫描指定 Bot")
    parser.add_argument("--session", default=None, help="只扫描指定 session_id")
    parser.add_argument("--visibility", default=None, help="只扫描指定 visibility")
    parser.add_argument(
        "--days",
        type=float,
        default=DEFAULT_DAYS,
        help=f"事实创建时间前后多少天（默认 {DEFAULT_DAYS:g}）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"模糊匹配最低分（默认 {DEFAULT_THRESHOLD:g}）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="候选报告输出路径（.csv 或 .json）；省略时 JSON 打印到 stdout",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写入唯一命中的 source_memory_id（必须同时给 --yes）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认写入；没有它时 --apply 会被拒绝",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="写入前用 SQLite 在线备份到该路径（可选；线上库约 3.6G）",
    )
    args = parser.parse_args(argv)

    if args.out is not None and args.out.suffix.lower() not in {".csv", ".json"}:
        parser.error("--out must end with .csv or .json")
    if args.apply and not args.yes:
        parser.error("--apply requires --yes")
    if args.yes and not args.apply:
        parser.error("--yes is only valid with --apply")

    if args.apply:
        try:
            outcome = apply_backfill(
                args.db,
                bot_id=args.bot_id,
                session_id=args.session,
                visibility=args.visibility,
                days=args.days,
                threshold=args.threshold,
                backup_path=args.backup,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(outcome.to_payload(), ensure_ascii=False, indent=2))
        print(_apply_line(outcome), file=sys.stderr)
        return 0 if outcome.ok else 1

    try:
        report = run_backfill(
            args.db,
            bot_id=args.bot_id,
            session_id=args.session,
            visibility=args.visibility,
            days=args.days,
            threshold=args.threshold,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if report.error:
        print(f"error: {report.error}", file=sys.stderr)
        return 2

    if args.out is not None:
        try:
            target = write_report(report, args.out)
        except OSError as exc:
            print(f"error: unable to write report: {exc}", file=sys.stderr)
            return 1
        print(f"{_summary_line(report)} out={target}")
    else:
        payload = {"generated_at": time.time(), **report.to_payload()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(_summary_line(report), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
