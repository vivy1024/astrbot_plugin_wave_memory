"""回填 scoped_jargon 已确认行的标签链（provenance.source_tags / tag_chain_status）。

黑话入库时标签抽取是异步的，早期已确认行停在 ``tag_chain_status='missing'``、
``source_tags=[]``。本脚本按来源记忆重新查询同 Scope 的有效标签：

- 有标签：写入 ``source_tags``（结构同 ``list_scoped_memory_tags``）并标 ``complete``；
- 没标签：标 ``empty``，保留空数组，行为不变；
- 来源记忆已删除、未解析、被隔离或跨 Scope：跳过，不伪造标签链。

默认只读预演；写入必须显式 ``--apply --yes``，在单个 ``BEGIN IMMEDIATE`` 事务内
重新校验并更新，提交前生成逐行回滚 SQL。

用法::

    python scripts/backfill_jargon_tag_chain.py --db wave_memory.db
    python scripts/backfill_jargon_tag_chain.py --db wave_memory.db --apply --yes
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

STATUS_COMPLETE = "complete"
STATUS_EMPTY = "empty"
STATUS_MISSING_MEMORY = "missing_memory"
STATUS_UNHEALTHY_MEMORY = "unhealthy_memory"
STATUS_TAGS_CHANGED = "tags_changed"
STATUS_CONCURRENT = "concurrent_change"


@dataclass
class ScanReport:
    db_path: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    scanned: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def targets(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["status"] in {STATUS_COMPLETE, STATUS_EMPTY}]

    def to_payload(self) -> dict[str, Any]:
        complete = sum(1 for row in self.rows if row["status"] == STATUS_COMPLETE)
        empty = sum(1 for row in self.rows if row["status"] == STATUS_EMPTY)
        return {
            "dry_run": True,
            "read_only": True,
            "database": self.db_path,
            "scanned": self.scanned,
            "candidates": len(self.rows),
            "complete": complete,
            "empty": empty,
            "skipped": len(self.rows) - complete - empty,
            "error": self.error,
            "rows": list(self.rows),
        }


@dataclass
class ApplyOutcome:
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
        return {
            "dry_run": False,
            "read_only": False,
            "database": self.db_path,
            "planned": self.planned,
            "applied": len(self.applied),
            "skipped": len(self.skipped),
            "backup": self.backup,
            "undo_log": self.undo_log,
            "error": self.error,
            "applied_jargon": list(self.applied),
            "skipped_jargon": list(self.skipped),
        }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return []
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("provenance is not a JSON object")
    return dict(parsed)


def _tags_for(
    conn: sqlite3.Connection,
    *,
    bot_id: str,
    session_id: str,
    visibility: str,
    memory_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT smt.memory_id, smt.tag_id, st.name, st.tag_type, smt.relevance
             FROM scoped_memory_tags smt JOIN scoped_tags st ON st.id=smt.tag_id
            WHERE smt.bot_id=? AND smt.session_id=? AND smt.visibility=?
              AND smt.memory_id=? AND st.status='active'
            ORDER BY smt.memory_id, smt.position, smt.tag_id""",
        (bot_id, session_id, visibility, memory_id),
    ).fetchall()
    return [
        {
            "memory_id": int(row[0]),
            "tag_id": int(row[1]),
            "name": str(row[2]),
            "tag_type": str(row[3]),
            "relevance": row[4],
        }
        for row in rows
    ]


def _memory_healthy(
    conn: sqlite3.Connection,
    *,
    bot_id: str,
    session_id: str,
    visibility: str,
    memory_id: int,
) -> str | None:
    row = conn.execute(
        """SELECT resolution_state, COALESCE(quarantine, 0)
             FROM memories
            WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
        (memory_id, bot_id, session_id, visibility),
    ).fetchone()
    if row is None:
        return STATUS_MISSING_MEMORY
    if str(row[0] or "") != "resolved" or int(row[1] or 0) != 0:
        return STATUS_UNHEALTHY_MEMORY
    return None


def scan(db_path: str | Path, *, limit: int | None = None) -> ScanReport:
    path = Path(db_path)
    report = ScanReport(db_path=str(path))
    if not path.is_file():
        report.error = f"database not found: {path}"
        return report
    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        report.error = f"unable to open database read-only: {exc}"
        return report
    try:
        jargon_columns = _table_columns(conn, "scoped_jargon")
        required = {"id", "bot_id", "session_id", "visibility", "word", "status", "source_memory_id", "provenance"}
        if not jargon_columns:
            report.error = "scoped_jargon table not found"
            return report
        if not required <= set(jargon_columns):
            report.error = "scoped_jargon lacks required columns"
            return report
        if not _table_columns(conn, "scoped_memory_tags") or not _table_columns(conn, "scoped_tags"):
            report.error = "scoped_memory_tags/scoped_tags tables not found"
            return report

        sql = (
            "SELECT id,bot_id,session_id,visibility,word,source_memory_id,provenance "
            "FROM scoped_jargon WHERE status='confirmed' ORDER BY id"
        )
        rows = conn.execute(sql).fetchall()
        if limit is not None:
            rows = rows[: max(0, int(limit))]

        for jargon_id, bot_id, session_id, visibility, word, memory_id, provenance_text in rows:
            report.scanned += 1
            try:
                provenance = _json_object(provenance_text)
            except (TypeError, ValueError):
                report.rows.append({
                    "jargon_id": jargon_id, "word": word, "bot_id": bot_id,
                    "session_id": session_id, "visibility": visibility,
                    "source_memory_id": memory_id, "status": "provenance_unparsable",
                })
                continue
            if provenance.get("source_tags"):
                continue
            if memory_id is None:
                report.rows.append({
                    "jargon_id": jargon_id, "word": word, "bot_id": bot_id,
                    "session_id": session_id, "visibility": visibility,
                    "source_memory_id": None, "status": STATUS_MISSING_MEMORY,
                })
                continue
            try:
                memory_id = int(memory_id)
            except (TypeError, ValueError):
                report.rows.append({
                    "jargon_id": jargon_id, "word": word, "bot_id": bot_id,
                    "session_id": session_id, "visibility": visibility,
                    "source_memory_id": memory_id, "status": STATUS_MISSING_MEMORY,
                })
                continue
            unhealthy = _memory_healthy(
                conn, bot_id=bot_id, session_id=session_id,
                visibility=visibility, memory_id=memory_id,
            )
            if unhealthy is not None:
                report.rows.append({
                    "jargon_id": jargon_id, "word": word, "bot_id": bot_id,
                    "session_id": session_id, "visibility": visibility,
                    "source_memory_id": memory_id, "status": unhealthy,
                })
                continue
            tags = _tags_for(
                conn, bot_id=bot_id, session_id=session_id,
                visibility=visibility, memory_id=memory_id,
            )
            report.rows.append({
                "jargon_id": jargon_id,
                "word": word,
                "bot_id": bot_id,
                "session_id": session_id,
                "visibility": visibility,
                "source_memory_id": memory_id,
                "status": STATUS_COMPLETE if tags else STATUS_EMPTY,
                "tags": tags,
                "previous_tag_chain_status": provenance.get("tag_chain_status"),
                "previous_source_tags": provenance.get("source_tags"),
            })
        return report
    except sqlite3.Error as exc:
        report.error = f"sqlite error: {exc}"
        return report
    finally:
        conn.close()


def _backup_database(db_path: Path, target: Path) -> Path:
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


def apply_backfill(
    db_path: str | Path,
    *,
    limit: int | None = None,
    backup_path: str | Path | None = None,
) -> ApplyOutcome:
    path = Path(db_path)
    outcome = ApplyOutcome(db_path=str(path))
    report = scan(path, limit=limit)
    if not report.ok:
        outcome.error = report.error
        return outcome
    planned = {int(row["jargon_id"]): row for row in report.targets}
    outcome.planned = len(planned)
    if not planned:
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
    undo_path = path.with_name(f"{path.name}.jargon-tag-chain-undo-{stamp}.sql")
    pending_undo = undo_path.with_name(undo_path.name + ".pending")
    undo_lines = [
        f"-- jargon tag chain backfill undo, generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"-- database: {path}",
        "BEGIN IMMEDIATE;",
    ]

    try:
        columns = _table_columns(conn, "scoped_jargon")
        if not {"id", "status", "provenance", "updated_at"} <= set(columns):
            outcome.error = "scoped_jargon schema changed; refusing to write"
            return outcome
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        for jargon_id in sorted(planned):
            expected = planned[jargon_id]
            row = conn.execute(
                "SELECT status,bot_id,session_id,visibility,source_memory_id,provenance,updated_at "
                "FROM scoped_jargon WHERE id=?",
                (jargon_id,),
            ).fetchone()
            if row is None:
                outcome.skipped.append({"jargon_id": jargon_id, "reason": "row_missing"})
                continue
            status, bot_id, session_id, visibility, memory_id, provenance_text, updated_at = row
            if status != "confirmed":
                outcome.skipped.append({"jargon_id": jargon_id, "reason": "status_changed"})
                continue
            try:
                memory_id = int(memory_id)
            except (TypeError, ValueError):
                outcome.skipped.append({"jargon_id": jargon_id, "reason": STATUS_MISSING_MEMORY})
                continue
            if (
                bot_id != expected.get("bot_id")
                or session_id != expected.get("session_id")
                or visibility != expected.get("visibility")
                or memory_id != expected.get("source_memory_id")
            ):
                outcome.skipped.append({"jargon_id": jargon_id, "reason": "scope_changed"})
                continue
            unhealthy = _memory_healthy(
                conn, bot_id=bot_id, session_id=session_id,
                visibility=visibility, memory_id=memory_id,
            )
            if unhealthy is not None:
                outcome.skipped.append({"jargon_id": jargon_id, "reason": unhealthy})
                continue
            tags = _tags_for(
                conn, bot_id=bot_id, session_id=session_id,
                visibility=visibility, memory_id=memory_id,
            )
            expected_status = STATUS_COMPLETE if tags else STATUS_EMPTY
            if expected_status != expected.get("status"):
                outcome.skipped.append({"jargon_id": jargon_id, "reason": STATUS_TAGS_CHANGED})
                continue
            try:
                provenance = _json_object(provenance_text)
            except (TypeError, ValueError):
                outcome.skipped.append({"jargon_id": jargon_id, "reason": "provenance_unparsable"})
                continue
            provenance["source_tags"] = tags
            provenance["tag_chain_status"] = expected_status
            encoded = json.dumps(
                provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            cursor = conn.execute(
                "UPDATE scoped_jargon SET provenance=?, updated_at=? "
                "WHERE id=? AND status='confirmed' AND updated_at=?",
                (encoded, now, jargon_id, updated_at),
            )
            if cursor.rowcount != 1:
                outcome.skipped.append({"jargon_id": jargon_id, "reason": STATUS_CONCURRENT})
                continue
            undo_lines.append(
                'UPDATE "scoped_jargon" SET '
                f'"provenance"={_sql_literal(provenance_text)}, '
                f'"updated_at"={_sql_literal(updated_at)} '
                f'WHERE "id"={_sql_literal(jargon_id)} AND "updated_at"={_sql_literal(now)};'
            )
            outcome.applied.append({
                "jargon_id": jargon_id,
                "word": expected.get("word"),
                "source_memory_id": memory_id,
                "tag_count": len(tags),
                "tag_chain_status": expected_status,
            })

        if not outcome.applied:
            conn.rollback()
            return outcome

        undo_lines.extend(["COMMIT;", ""])
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


def write_report(report: ScanReport, target: str | Path) -> Path:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"generated_at": time.time(), **report.to_payload()},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return path


def _scan_line(report: ScanReport) -> str:
    complete = sum(1 for row in report.rows if row["status"] == STATUS_COMPLETE)
    empty = sum(1 for row in report.rows if row["status"] == STATUS_EMPTY)
    skipped = len(report.rows) - complete - empty
    return (
        f"scanned={report.scanned} candidates={len(report.rows)} "
        f"complete={complete} empty={empty} skipped={skipped}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill confirmed jargon tag chains; default is read-only."
    )
    parser.add_argument("--db", required=True, type=Path, help="wave_memory.db 路径")
    parser.add_argument("--limit", type=int, default=None, help="只扫描前 N 条已确认黑话")
    parser.add_argument("--out", type=Path, default=None, help="预演报告输出路径（JSON）")
    parser.add_argument("--apply", action="store_true", help="写入标签链（必须同时给 --yes）")
    parser.add_argument("--yes", action="store_true", help="确认写入")
    parser.add_argument("--backup", type=Path, default=None, help="写入前完整备份路径（可选）")
    args = parser.parse_args(argv)

    if args.apply and not args.yes:
        parser.error("--apply requires --yes")
    if args.yes and not args.apply:
        parser.error("--yes is only valid with --apply")

    if args.apply:
        try:
            outcome = apply_backfill(
                args.db, limit=args.limit, backup_path=args.backup
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(outcome.to_payload(), ensure_ascii=False, indent=2))
        print(
            f"planned={outcome.planned} applied={len(outcome.applied)} "
            f"skipped={len(outcome.skipped)} undo_log={outcome.undo_log or '-'}",
            file=sys.stderr,
        )
        return 0 if outcome.ok else 1

    try:
        report = scan(args.db, limit=args.limit)
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
        print(f"{_scan_line(report)} out={target}")
    else:
        print(json.dumps(report.to_payload(), ensure_ascii=False, indent=2))
        print(_scan_line(report), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
