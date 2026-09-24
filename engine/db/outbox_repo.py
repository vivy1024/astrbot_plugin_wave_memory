"""SQLite schema and transaction-scoped repository for operations and outbox delivery."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    operation_id: str
    write_sequence: int
    aggregate_kind: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    payload_version: int
    payload: dict[str, Any]
    consumer_name: str
    attempt: int


class OutboxRepository:
    RETRY_DELAY_SECONDS = 1.0
    LEASE_SECONDS = 30.0

    @staticmethod
    def migrate(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS write_operations (
                operation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                command_type TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                write_sequence INTEGER NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                committed_at REAL
            );
            CREATE TABLE IF NOT EXISTS domain_outbox (
                event_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL REFERENCES write_operations(operation_id),
                aggregate_kind TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                archived_at REAL
            );
            CREATE TABLE IF NOT EXISTS outbox_deliveries (
                event_id TEXT NOT NULL REFERENCES domain_outbox(event_id) ON DELETE CASCADE,
                consumer_name TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL,
                lease_owner TEXT,
                lease_until REAL,
                last_error TEXT,
                processed_at REAL,
                PRIMARY KEY(event_id, consumer_name)
            );
            CREATE TABLE IF NOT EXISTS derived_projection_state (
                consumer_name TEXT NOT NULL,
                aggregate_kind TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                applied_version INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                checkpoint_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(consumer_name, aggregate_kind, aggregate_id)
            );
            CREATE TABLE IF NOT EXISTS quality_decisions (
                proposal_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('allow','quarantine','reject','defer')),
                reason_code TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                raw_artifact_json TEXT NOT NULL,
                target_scope_json TEXT,
                normalized_content_hash TEXT NOT NULL,
                decided_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_requests (
                request_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS background_job_runs (
                run_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES job_requests(request_id) ON DELETE CASCADE,
                schedule_slot TEXT NOT NULL,
                cursor_generation INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                cursor_json TEXT NOT NULL DEFAULT '{}',
                attempt INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_until REAL,
                progress_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(request_id, schedule_slot, cursor_generation)
            );
            CREATE TABLE IF NOT EXISTS system_convergence_probe_entities (
                aggregate_id TEXT PRIMARY KEY,
                aggregate_version INTEGER NOT NULL,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_delivery_ready
                ON outbox_deliveries(state, available_at, lease_until);
            CREATE INDEX IF NOT EXISTS idx_outbox_operation ON domain_outbox(operation_id);
            CREATE INDEX IF NOT EXISTS idx_background_job_runs_ready
                ON background_job_runs(status, lease_until, updated_at);
            CREATE INDEX IF NOT EXISTS idx_quality_decisions_outcome_time
                ON quality_decisions(outcome, decided_at DESC);
            """
        )

    @staticmethod
    def prune_history(connection: sqlite3.Connection, *, before: float, limit: int = 5000) -> dict[str, int]:
        """删除早于 ``before`` 的已归档 outbox 事件、其已完成投递，以及不再被引用的已提交写操作。

        这三张表只追加不清理，线上库里三者合计约 0.9 GB（占全库两成多）。只删已经全部
        投递完成的事件；写操作只删已提交、且没有 outbox 事件引用的行，并始终保留
        write_sequence 最大的一行，保证下一个写序号单调递增。幂等键随写操作一起过期，
        超过保留期的同一请求重放会被当作新请求执行。
        """
        limit = max(1, int(limit))
        event_ids = [
            row[0]
            for row in connection.execute(
                """SELECT o.event_id FROM domain_outbox o
                    WHERE o.archived_at IS NOT NULL AND o.archived_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM outbox_deliveries d
                           WHERE d.event_id = o.event_id AND d.state != 'completed')
                    ORDER BY o.rowid
                    LIMIT ?""",
                (float(before), limit),
            ).fetchall()
        ]
        deliveries = events = 0
        for start in range(0, len(event_ids), 500):
            chunk = event_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            deliveries += connection.execute(
                f"DELETE FROM outbox_deliveries WHERE event_id IN ({placeholders})", chunk
            ).rowcount or 0
            events += connection.execute(
                f"DELETE FROM domain_outbox WHERE event_id IN ({placeholders})", chunk
            ).rowcount or 0
        operations = connection.execute(
            """DELETE FROM write_operations WHERE rowid IN (
                   SELECT w.rowid FROM write_operations w
                    WHERE w.status = 'committed' AND COALESCE(w.committed_at, w.created_at) < ?
                      AND w.write_sequence < (SELECT MAX(write_sequence) FROM write_operations)
                      AND NOT EXISTS (SELECT 1 FROM domain_outbox o WHERE o.operation_id = w.operation_id)
                    ORDER BY w.rowid
                    LIMIT ?)""",
            (float(before), limit),
        ).rowcount or 0
        return {"events": int(events), "deliveries": int(deliveries), "operations": int(operations)}

    @staticmethod
    def next_write_sequence(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(write_sequence), 0) + 1 FROM write_operations"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def committed_watermark(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(write_sequence), 0) FROM write_operations WHERE status='committed'"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def add_deliveries(
        connection: sqlite3.Connection,
        event_id: str,
        consumer_names: Iterable[str],
        available_at: float,
    ) -> None:
        connection.executemany(
            "INSERT INTO outbox_deliveries(event_id, consumer_name, state, available_at) "
            "VALUES (?, ?, 'pending', ?)",
            ((event_id, name, available_at) for name in consumer_names),
        )

    @staticmethod
    def claim_next(
        connection: sqlite3.Connection,
        *,
        consumer_names: tuple[str, ...],
        watermark: int,
        now: float,
        lease_owner: str,
        excluded: set[tuple[str, str]],
    ) -> OutboxEvent | None:
        if not consumer_names:
            return None
        placeholders = ",".join("?" for _ in consumer_names)
        rows = connection.execute(
            f"""
            SELECT o.event_id, o.operation_id, w.write_sequence, o.aggregate_kind,
                   o.aggregate_id, o.aggregate_version, o.event_type, o.payload_version,
                   o.payload_json, d.consumer_name, d.attempt
            FROM outbox_deliveries d
            JOIN domain_outbox o ON o.event_id=d.event_id
            JOIN write_operations w ON w.operation_id=o.operation_id
            WHERE d.consumer_name IN ({placeholders})
              AND w.status='committed' AND w.write_sequence<=?
              AND d.processed_at IS NULL AND d.available_at<=?
              AND (d.state IN ('pending','retry')
                   OR (d.state='processing' AND COALESCE(d.lease_until,0)<=?))
            ORDER BY w.write_sequence, o.created_at, d.consumer_name
            """,
            (*consumer_names, watermark, now, now),
        ).fetchall()
        row = next(
            (item for item in rows if (str(item[0]), str(item[9])) not in excluded),
            None,
        )
        if row is None:
            return None
        changed = connection.execute(
            """UPDATE outbox_deliveries
               SET state='processing', attempt=attempt+1, lease_owner=?, lease_until=?
               WHERE event_id=? AND consumer_name=? AND processed_at IS NULL""",
            (lease_owner, now + OutboxRepository.LEASE_SECONDS, row[0], row[9]),
        ).rowcount
        if changed != 1:
            return None
        return OutboxEvent(
            event_id=str(row[0]), operation_id=str(row[1]), write_sequence=int(row[2]),
            aggregate_kind=str(row[3]), aggregate_id=str(row[4]), aggregate_version=int(row[5]),
            event_type=str(row[6]), payload_version=int(row[7]), payload=json.loads(row[8]),
            consumer_name=str(row[9]), attempt=int(row[10]) + 1,
        )

    @staticmethod
    def applied_version(connection: sqlite3.Connection, event: OutboxEvent) -> int | None:
        row = connection.execute(
            """SELECT applied_version FROM derived_projection_state
               WHERE consumer_name=? AND aggregate_kind=? AND aggregate_id=?""",
            (event.consumer_name, event.aggregate_kind, event.aggregate_id),
        ).fetchone()
        return None if row is None else int(row[0])

    @staticmethod
    def mark_succeeded(connection: sqlite3.Connection, event: OutboxEvent, now: float) -> None:
        checkpoint = json.dumps(
            {"event_id": event.event_id, "operation_id": event.operation_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """INSERT INTO derived_projection_state(
                   consumer_name, aggregate_kind, aggregate_id, applied_version,
                   generation, checkpoint_json, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(consumer_name, aggregate_kind, aggregate_id) DO UPDATE SET
                   applied_version=excluded.applied_version,
                   generation=derived_projection_state.generation+1,
                   checkpoint_json=excluded.checkpoint_json,
                   updated_at=excluded.updated_at
               WHERE excluded.applied_version > derived_projection_state.applied_version""",
            (event.consumer_name, event.aggregate_kind, event.aggregate_id,
             event.aggregate_version, checkpoint, now),
        )
        connection.execute(
            """UPDATE outbox_deliveries SET state='completed', lease_owner=NULL,
                   lease_until=NULL, last_error=NULL, processed_at=?
               WHERE event_id=? AND consumer_name=?""",
            (now, event.event_id, event.consumer_name),
        )
        OutboxRepository._archive_if_complete(connection, event.event_id, now)

    @staticmethod
    def mark_stale(connection: sqlite3.Connection, event: OutboxEvent, now: float) -> None:
        connection.execute(
            """UPDATE outbox_deliveries SET state='completed', lease_owner=NULL,
                   lease_until=NULL, last_error='stale_aggregate_version', processed_at=?
               WHERE event_id=? AND consumer_name=?""",
            (now, event.event_id, event.consumer_name),
        )
        OutboxRepository._archive_if_complete(connection, event.event_id, now)

    @staticmethod
    def mark_failed(connection: sqlite3.Connection, event: OutboxEvent, now: float, error: str) -> None:
        connection.execute(
            """UPDATE outbox_deliveries SET state='retry', available_at=?, lease_owner=NULL,
                   lease_until=NULL, last_error=?
               WHERE event_id=? AND consumer_name=?""",
            (now + OutboxRepository.RETRY_DELAY_SECONDS, error[:2000],
             event.event_id, event.consumer_name),
        )

    @staticmethod
    def next_attempt_at(connection: sqlite3.Connection, consumer_names: tuple[str, ...]) -> float | None:
        if not consumer_names:
            return None
        placeholders = ",".join("?" for _ in consumer_names)
        row = connection.execute(
            f"""SELECT MIN(CASE WHEN state='processing' THEN lease_until ELSE available_at END)
                 FROM outbox_deliveries
                 WHERE consumer_name IN ({placeholders}) AND processed_at IS NULL""",
            consumer_names,
        ).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    @staticmethod
    def _archive_if_complete(connection: sqlite3.Connection, event_id: str, now: float) -> None:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM outbox_deliveries WHERE event_id=? AND processed_at IS NULL",
            (event_id,),
        ).fetchone()[0]
        if int(remaining) == 0:
            connection.execute(
                "UPDATE domain_outbox SET archived_at=COALESCE(archived_at, ?) WHERE event_id=?",
                (now, event_id),
            )
