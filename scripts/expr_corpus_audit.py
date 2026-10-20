"""离线审计表达候选在群语料里的真实频次与说话人数。

背景：``reply_expression_candidates.occurrence_count`` / ``speaker_count`` 是
采集链路上的「事后记账」计数，只统计被模型挑中的来源消息，因此会严重低估一句
口头禅在群里的真实流行度。本脚本提供一把只读、可复现的离线标尺：

* 从 ``reply_expression_candidates`` 读某个 ``(bot_id, session_id, visibility)``
  scope 的全部候选；
* 从 ``memories`` 读同 scope 的全部消息，排除 Bot、noise/archived/evicted/deleted、
  quarantine=1 和空内容；
* 用 NFKC 归一化后做长度敏感的子串计数，并同时给出两侧边界严格口径；
* 按 ``(sender_id, 归一化内容, round(timestamp, 1))`` 去重，避免 @ 提及造成的
  重复行重复计数。

计数单位是「去重后的消息条数」：一个候选在同一句话里出现多次只算一次，这正好
对应候选表里按 ``source_memory_id`` 记账的 occurrence 语义。说话人数是这些命中
消息里去重后的 ``sender_id`` 数量；空 sender 仍计入出现次数，但不冒充一个说话人。

两种口径始终一起计算并输出：``*_loose`` 是归一化后子串出现，``*_strict`` 要求
匹配两侧是串首尾、空白或标点。不带后缀的字段跟随 ``--strict`` 选择的「主口径」
（默认 loose），因此直接看 ``real_occurrence_count`` 也不会在严格模式下读错列。

数据库始终以 SQLite URI ``mode=ro`` 打开，并额外设置 ``PRAGMA query_only=ON``；
本脚本不会写入数据库。

用法::

    python scripts/expr_corpus_audit.py \
        --db /path/to/wave_memory.db \
        --bot-id <bot_db_id> \
        --session <platform>:group:<conversation_id> \
        --out /tmp/expr_audit.json

    python scripts/expr_corpus_audit.py ... --out expr_audit.csv --strict
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Iterator, Sequence

EXCLUDED_MEMORY_TYPES = ("noise", "archived", "evicted", "deleted")
CANDIDATE_TABLE = "reply_expression_candidates"
MEMORY_TABLE = "memories"

CSV_FIELDS = (
    "id",
    "status",
    "content",
    "candidate_occurrence_count",
    "candidate_speaker_count",
    "real_occurrence_count",
    "real_speaker_count",
    "real_occurrence_count_loose",
    "real_speaker_count_loose",
    "real_occurrence_count_strict",
    "real_speaker_count_strict",
    "occurrence_ge_3",
    "speaker_ge_2",
    "both_ge",
    "qualified",
    "occurrence_ge_3_loose",
    "speaker_ge_2_loose",
    "both_ge_loose",
    "qualified_loose",
    "occurrence_ge_3_strict",
    "speaker_ge_2_strict",
    "both_ge_strict",
    "qualified_strict",
)


class AuditError(RuntimeError):
    """可读的审计失败原因（缺表、缺列、输出格式不支持等）。"""


# ─── 只读连接 ──────────────────────────────────────────────────────────────

def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """用 URI ``mode=ro`` 打开数据库，禁止任何写操作。"""
    path = Path(db_path).expanduser()
    if not path.is_file():
        raise AuditError(f"database not found: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise AuditError(f"cannot open database read-only: {path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # table is one of the module-level constants, never user input.
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


# ─── 文本与匹配 ────────────────────────────────────────────────────────────

def normalize_text(value: Any) -> str:
    """NFKC：全角/半角标点、全角字母数字统一后再比较。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return unicodedata.normalize("NFKC", value.decode("utf-8", errors="replace"))
    return unicodedata.normalize("NFKC", str(value))


def _is_boundary_character(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("P")


def strict_boundaries(normalized_text: str, start: int, end: int) -> bool:
    """匹配两侧必须是串首尾、空白或 Unicode 标点。"""
    if start > 0 and not _is_boundary_character(normalized_text[start - 1]):
        return False
    if end < len(normalized_text) and not _is_boundary_character(normalized_text[end]):
        return False
    return True


class _AhoCorasick:
    """最小 Aho-Corasick 自动机：一次扫描找出所有候选的全部出现位置。

    候选几百条、消息二十万条时，逐候选 ``in`` 扫描是 O(候选×消息)；自动机把
    每条消息的匹配成本压到与消息长度线性相关。
    """

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns = list(patterns)
        self._next: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._outputs: list[list[int]] = [[]]

        for pattern_index, pattern in enumerate(self._patterns):
            node = 0
            for char in pattern:
                child = self._next[node].get(char)
                if child is None:
                    child = len(self._next)
                    self._next[node][char] = child
                    self._next.append({})
                    self._fail.append(0)
                    self._outputs.append([])
                node = child
            self._outputs[node].append(pattern_index)

        queue: deque[int] = deque()
        for child in self._next[0].values():
            queue.append(child)

        while queue:
            node = queue.popleft()
            for char, child in self._next[node].items():
                queue.append(child)
                fallback = self._fail[node]
                while fallback and char not in self._next[fallback]:
                    fallback = self._fail[fallback]
                self._fail[child] = self._next[fallback].get(char, 0)
                if self._fail[child]:
                    self._outputs[child].extend(self._outputs[self._fail[child]])

    def iter_matches(self, text: str) -> Iterator[tuple[int, int, int]]:
        """Yield ``(pattern_index, start, end)`` for every occurrence (overlaps included)."""
        node = 0
        for end, char in enumerate(text, 1):
            while node and char not in self._next[node]:
                node = self._fail[node]
            node = self._next[node].get(char, 0)
            if not node:
                continue
            for pattern_index in self._outputs[node]:
                yield pattern_index, end - len(self._patterns[pattern_index]), end


def _dedup_timestamp(value: Any) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return value


# ─── 读取 ──────────────────────────────────────────────────────────────────

def _load_candidates(
    connection: sqlite3.Connection,
    *,
    bot_id: str,
    session_id: str,
    visibility: str,
) -> list[dict[str, Any]]:
    columns = _table_columns(connection, CANDIDATE_TABLE)
    if not columns:
        raise AuditError(f"table not found: {CANDIDATE_TABLE}")
    required = {"id", "content", "bot_id", "session_id", "visibility"}
    missing = sorted(required - columns)
    if missing:
        raise AuditError(f"{CANDIDATE_TABLE} missing required columns: {', '.join(missing)}")

    # 生产库可能还没跑 dedup 迁移，没有计数列；缺列时输出 null，绝不臆造。
    status_sql = "status" if "status" in columns else "NULL"
    occurrence_sql = "occurrence_count" if "occurrence_count" in columns else "NULL"
    speaker_sql = "speaker_count" if "speaker_count" in columns else "NULL"
    rows = connection.execute(
        f"""SELECT id, content, {status_sql} AS status,
                   {occurrence_sql} AS candidate_occurrence_count,
                   {speaker_sql} AS candidate_speaker_count
              FROM {CANDIDATE_TABLE}
             WHERE bot_id=? AND session_id=? AND visibility=?
             ORDER BY id""",
        (bot_id, session_id, visibility),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "content": "" if row["content"] is None else str(row["content"]),
            "status": None if row["status"] is None else str(row["status"]),
            "candidate_occurrence_count": (
                None if row["candidate_occurrence_count"] is None
                else int(row["candidate_occurrence_count"])
            ),
            "candidate_speaker_count": (
                None if row["candidate_speaker_count"] is None
                else int(row["candidate_speaker_count"])
            ),
        }
        for row in rows
    ]


def _load_memories(
    connection: sqlite3.Connection,
    *,
    bot_id: str,
    session_id: str,
    visibility: str,
) -> tuple[str, list[Any]]:
    columns = _table_columns(connection, MEMORY_TABLE)
    if not columns:
        raise AuditError(f"table not found: {MEMORY_TABLE}")
    required = {"content", "sender_id", "timestamp", "bot_id", "session_id", "visibility"}
    missing = sorted(required - columns)
    if missing:
        raise AuditError(f"{MEMORY_TABLE} missing required columns: {', '.join(missing)}")

    where = [
        "bot_id=?",
        "session_id=?",
        "visibility=?",
        "COALESCE(sender_id, '') != 'bot'",
        "TRIM(COALESCE(content, '')) != ''",
    ]
    parameters: list[Any] = [bot_id, session_id, visibility]
    if "memory_type" in columns:
        placeholders = ", ".join("?" for _ in EXCLUDED_MEMORY_TYPES)
        where.append(f"COALESCE(memory_type, 'message') NOT IN ({placeholders})")
        parameters.extend(EXCLUDED_MEMORY_TYPES)
    if "quarantine" in columns:
        where.append("COALESCE(quarantine, 0) = 0")

    sql = (
        "SELECT sender_id, content, timestamp FROM memories "
        f"WHERE {' AND '.join(where)}"
    )
    return sql, parameters


# ─── 审计 ──────────────────────────────────────────────────────────────────

def audit_corpus(
    db_path: str | Path,
    *,
    bot_id: str,
    session_id: str,
    visibility: str = "group",
    strict: bool = False,
) -> dict[str, Any]:
    """只读审计一个 scope；返回可直接 JSON 序列化的报告。"""
    connection = connect_readonly(db_path)
    try:
        candidates = _load_candidates(
            connection, bot_id=bot_id, session_id=session_id, visibility=visibility
        )

        # 相同归一化文本的候选（例如全角/半角两个版本）共享一次自动机匹配。
        pattern_groups: dict[str, list[int]] = {}
        for candidate_index, candidate in enumerate(candidates):
            pattern = normalize_text(candidate["content"])
            if pattern:
                pattern_groups.setdefault(pattern, []).append(candidate_index)
        unique_patterns = list(pattern_groups)
        group_candidates = [pattern_groups[pattern] for pattern in unique_patterns]
        matcher = _AhoCorasick(unique_patterns) if unique_patterns else None

        sql, parameters = _load_memories(
            connection, bot_id=bot_id, session_id=session_id, visibility=visibility
        )

        loose_counts = [0] * len(candidates)
        loose_speakers: list[set[str]] = [set() for _ in candidates]
        strict_counts = [0] * len(candidates)
        strict_speakers: list[set[str]] = [set() for _ in candidates]

        rows_fetched = 0
        rows_skipped_empty = 0
        rows_skipped_duplicate = 0
        rows_used = 0
        messages_matched_loose = 0
        messages_matched_strict = 0
        seen: set[tuple[str, str, Any]] = set()

        for row in connection.execute(sql, parameters):
            rows_fetched += 1
            raw_content = row["content"]
            if raw_content is None:
                rows_skipped_empty += 1
                continue
            raw_text = str(raw_content)
            if not raw_text.strip():
                rows_skipped_empty += 1
                continue
            normalized = normalize_text(raw_text)
            if not normalized.strip():
                rows_skipped_empty += 1
                continue

            sender_id = "" if row["sender_id"] is None else str(row["sender_id"])
            # 保留 1 位小数：同一秒内 @ 提及产生的重复行只计一条。
            dedup_key = (sender_id, normalized, _dedup_timestamp(row["timestamp"]))
            if dedup_key in seen:
                rows_skipped_duplicate += 1
                continue
            seen.add(dedup_key)
            rows_used += 1

            if matcher is None:
                continue

            loose_hits: set[int] = set()
            strict_hits: set[int] = set()
            for pattern_index, start, end in matcher.iter_matches(normalized):
                for candidate_index in group_candidates[pattern_index]:
                    loose_hits.add(candidate_index)
                    if strict_boundaries(normalized, start, end):
                        strict_hits.add(candidate_index)

            if loose_hits:
                messages_matched_loose += 1
                for candidate_index in loose_hits:
                    loose_counts[candidate_index] += 1
                    if sender_id:
                        loose_speakers[candidate_index].add(sender_id)
            if strict_hits:
                messages_matched_strict += 1
                for candidate_index in strict_hits:
                    strict_counts[candidate_index] += 1
                    if sender_id:
                        strict_speakers[candidate_index].add(sender_id)

        candidate_rows: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(candidates):
            loose_occurrences = loose_counts[candidate_index]
            loose_speaker_count = len(loose_speakers[candidate_index])
            strict_occurrences = strict_counts[candidate_index]
            strict_speaker_count = len(strict_speakers[candidate_index])
            # 未加后缀的字段跟随 --strict 选择的「主口径」；两种口径的数值始终都在。
            primary_occurrences = strict_occurrences if strict else loose_occurrences
            primary_speakers = strict_speaker_count if strict else loose_speaker_count
            candidate_rows.append(
                {
                    "id": candidate["id"],
                    "content": candidate["content"],
                    "status": candidate["status"],
                    "candidate_occurrence_count": candidate["candidate_occurrence_count"],
                    "candidate_speaker_count": candidate["candidate_speaker_count"],
                    "real_occurrence_count": primary_occurrences,
                    "real_speaker_count": primary_speakers,
                    "real_occurrence_count_loose": loose_occurrences,
                    "real_speaker_count_loose": loose_speaker_count,
                    "real_occurrence_count_strict": strict_occurrences,
                    "real_speaker_count_strict": strict_speaker_count,
                    "occurrence_ge_3": primary_occurrences >= 3,
                    "speaker_ge_2": primary_speakers >= 2,
                    "both_ge": primary_occurrences >= 3 and primary_speakers >= 2,
                    "qualified": primary_occurrences >= 3 or primary_speakers >= 2,
                    "occurrence_ge_3_loose": loose_occurrences >= 3,
                    "speaker_ge_2_loose": loose_speaker_count >= 2,
                    "both_ge_loose": loose_occurrences >= 3 and loose_speaker_count >= 2,
                    "qualified_loose": loose_occurrences >= 3 or loose_speaker_count >= 2,
                    "occurrence_ge_3_strict": strict_occurrences >= 3,
                    "speaker_ge_2_strict": strict_speaker_count >= 2,
                    "both_ge_strict": strict_occurrences >= 3 and strict_speaker_count >= 2,
                    "qualified_strict": strict_occurrences >= 3 or strict_speaker_count >= 2,
                }
            )

        def summary_for(suffix: str) -> dict[str, int]:
            return {
                "occurrence_ge_3": sum(
                    1 for row in candidate_rows if row[f"occurrence_ge_3_{suffix}"]
                ),
                "speaker_ge_2": sum(
                    1 for row in candidate_rows if row[f"speaker_ge_2_{suffix}"]
                ),
                "both_ge": sum(1 for row in candidate_rows if row[f"both_ge_{suffix}"]),
                "qualified": sum(
                    1 for row in candidate_rows if row[f"qualified_{suffix}"]
                ),
                "with_any_match": sum(
                    1
                    for row in candidate_rows
                    if row[f"real_occurrence_count_{suffix}"] > 0
                ),
            }

        summary: dict[str, Any] = {
            "candidate_total": len(candidate_rows),
            "candidates_with_candidate_counts": sum(
                1
                for row in candidate_rows
                if row["candidate_occurrence_count"] is not None
            ),
            "loose": summary_for("loose"),
            "strict": summary_for("strict"),
        }
        summary["primary"] = summary_for("strict" if strict else "loose")

        return {
            "scope": {
                "db": str(Path(db_path).expanduser().resolve()),
                "bot_id": bot_id,
                "session_id": session_id,
                "visibility": visibility,
            },
            "semantics": {
                "normalization": "unicodedata.normalize('NFKC', text)",
                "dedup_key": "(sender_id, normalized_content, round(timestamp, 1))",
                "count_unit": "one deduped message counts at most once per candidate",
                "loose": "normalized substring occurrence at any position",
                "strict": (
                    "substring match whose both sides are string start/end, "
                    "whitespace, or Unicode punctuation"
                ),
                "primary_mode": "strict" if strict else "loose",
                "strict_requested": strict,
                "unsuffixed_fields": "follow primary_mode; loose/strict fields are always present",
            },
            "corpus": {
                "rows_fetched": rows_fetched,
                "rows_skipped_empty": rows_skipped_empty,
                "rows_skipped_duplicate": rows_skipped_duplicate,
                "rows_used": rows_used,
                "messages_matched_loose": messages_matched_loose,
                "messages_matched_strict": messages_matched_strict,
                "unique_dedup_keys": len(seen),
            },
            "summary": summary,
            "candidates": candidate_rows,
        }
    finally:
        connection.close()


# ─── 输出 ──────────────────────────────────────────────────────────────────

def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_report(report: dict[str, Any], out_path: str | Path) -> Path:
    """按扩展名写 JSON（含汇总）或 CSV（逐候选；汇总打印到终端）。"""
    path = Path(out_path).expanduser()
    if path.resolve() == Path(report["scope"]["db"]).resolve():
        raise AuditError("refusing to overwrite the database with the audit report")
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif suffix == ".csv":
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in report["candidates"]:
                writer.writerow({field: _csv_cell(row.get(field)) for field in CSV_FIELDS})
    else:
        raise AuditError(f"unsupported output extension {path.suffix!r}; use .csv or .json")
    return path


def format_summary(report: dict[str, Any]) -> str:
    scope = report["scope"]
    corpus = report["corpus"]
    summary = report["summary"]
    loose = summary["loose"]
    strict = summary["strict"]
    primary = summary["primary"]
    primary_mode = report["semantics"]["primary_mode"]
    lines = [
        f"scope: bot={scope['bot_id']} session={scope['session_id']} "
        f"visibility={scope['visibility']}",
        f"corpus: fetched={corpus['rows_fetched']} used={corpus['rows_used']} "
        f"duplicate={corpus['rows_skipped_duplicate']} empty={corpus['rows_skipped_empty']} "
        f"matched_loose={corpus['messages_matched_loose']} "
        f"matched_strict={corpus['messages_matched_strict']}",
        f"candidates: total={summary['candidate_total']}",
        f"primary ({primary_mode}): occurrence>=3 {primary['occurrence_ge_3']}  "
        f"speaker>=2 {primary['speaker_ge_2']}  both {primary['both_ge']}  "
        f"either {primary['qualified']}",
        f"loose  (substring): occurrence>=3 {loose['occurrence_ge_3']}  "
        f"speaker>=2 {loose['speaker_ge_2']}  both {loose['both_ge']}  "
        f"either {loose['qualified']}",
        f"strict (boundary): occurrence>=3 {strict['occurrence_ge_3']}  "
        f"speaker>=2 {strict['speaker_ge_2']}  both {strict['both_ge']}  "
        f"either {strict['qualified']}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of reply expression candidates against the full "
            "group memory corpus."
        )
    )
    parser.add_argument("--db", required=True, help="path to wave_memory.db (opened read-only)")
    parser.add_argument("--bot-id", required=True, help="bot profile db_id")
    parser.add_argument(
        "--session",
        required=True,
        dest="session_id",
        help="canonical session id, e.g. aiocqhttp:group:123456",
    )
    parser.add_argument(
        "--visibility",
        default="group",
        choices=("group", "private"),
        help="scope visibility (default: group)",
    )
    parser.add_argument("--out", required=True, help="report path (.csv or .json)")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="mark strict boundary counts as the primary mode (both are always reported)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_corpus(
            args.db,
            bot_id=args.bot_id,
            session_id=args.session_id,
            visibility=args.visibility,
            strict=args.strict,
        )
        path = write_report(report, args.out)
    except (AuditError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_summary(report))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
