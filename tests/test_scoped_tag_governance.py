from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.migrations.scoped_memory_tag_corrections import ensure_scoped_memory_tag_correction_schema_connection
from engine.db.migrations.scoped_tag_governance import ensure_scoped_tag_governance_schema_connection
from engine.db.migrations.scoped_tag_projection import ensure_scoped_tag_projection_schema_connection
from engine.db.outbox_repo import OutboxRepository
from services.tag_governance import TagGovernanceError, TagGovernanceGateway


def scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef(
            id="qq:group:g1",
            platform_id="qq",
            kind="group",
            conversation_id="g1",
        ),
    )


class Coordinator:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._consumer_names = ("tag_index", "runtime_refresh")
        self._results: dict[str, object] = {}

    async def transaction(self, callback, *, actor=None):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = callback(self.connection)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    async def submit(self, command):
        from domain.commands import DomainWriteResult
        from engine.db.outbox_repo import OutboxRepository

        existing = self._results.get(command.idempotency_key)
        if existing is not None:
            return existing
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            import time
            now = time.time()
            sequence = OutboxRepository.next_write_sequence(self.connection)
            self.connection.execute(
                """INSERT INTO write_operations(
                       operation_id, idempotency_key, request_hash, command_type, scope_json,
                       status, write_sequence, created_at)
                   VALUES (?, ?, ?, ?, '{}', 'pending', ?, ?)""",
                (command.operation_id, command.idempotency_key, command.request_hash, command.command_type, sequence, now),
            )
            gateway = getattr(self, "gateway", None)
            mutate = getattr(self, "mutate", None)
            if gateway is None or mutate is None:
                raise RuntimeError("test coordinator missing governance mutate")
            outcome = gateway.apply_mutate(self.connection, command, now, mutate)
            for index, draft in enumerate(outcome.events):
                event_id = f"{command.operation_id}:{index}"
                self.connection.execute(
                    """INSERT INTO domain_outbox(
                           event_id, operation_id, aggregate_kind, aggregate_id,
                           aggregate_version, event_type, payload_version, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, '{}', ?)""",
                    (event_id, command.operation_id, draft.aggregate_kind, draft.aggregate_id, draft.aggregate_version, draft.event_type, now),
                )
            result = DomainWriteResult(
                operation_id=command.operation_id,
                committed_at=now,
                write_sequence=sequence,
                entities=outcome.entities,
                details=dict(outcome.details),
            )
            self.connection.execute(
                "UPDATE write_operations SET status='committed', result_json='{}', committed_at=? WHERE operation_id=?",
                (now, command.operation_id),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        self._results[command.idempotency_key] = result
        return result


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    OutboxRepository.migrate(conn)
    conn.executescript(
        """
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            name TEXT NOT NULL, tag_type TEXT NOT NULL DEFAULT 'keyword',
            description TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(bot_id, session_id, visibility, name)
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            memory_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0,
            relevance REAL NOT NULL DEFAULT 1, created_at REAL NOT NULL,
            PRIMARY KEY(bot_id, session_id, visibility, memory_id, tag_id)
        );
        CREATE TABLE scoped_tag_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL,
            source_tag_id INTEGER NOT NULL, target_tag_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1,
            confidence REAL NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active', valid_until REAL,
            revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        """
    )
    ensure_scoped_tag_governance_schema_connection(conn)
    ensure_scoped_memory_tag_correction_schema_connection(conn)
    ensure_scoped_tag_projection_schema_connection(conn)
    s = scope()
    for name, tag_type in (("旧名称", "topic"), ("新名称", "topic"), ("第三标签", "keyword")):
        conn.execute(
            """INSERT INTO scoped_tags(bot_id, session_id, visibility, name, tag_type, confidence, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 0.9, '{}', 1, 1)""",
            (s.bot_id, s.session.id, s.visibility, name, tag_type),
        )
    ids = [row[0] for row in conn.execute("SELECT id FROM scoped_tags ORDER BY id").fetchall()]
    conn.executemany(
        """INSERT INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at)
           VALUES (?, ?, ?, ?, ?, 1, 1, 1)""",
        [(s.bot_id, s.session.id, s.visibility, memory_id, tag_id) for memory_id, tag_id in ((1, ids[0]), (1, ids[1]), (2, ids[2]))],
    )
    conn.execute(
        """INSERT INTO scoped_tag_relations(bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'cooccurs', 1, 1)""",
        (s.bot_id, s.session.id, s.visibility, ids[0], ids[2]),
    )
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_scoped_tag_governance_preview_then_merge_is_audited_and_idempotent():
    conn = connection()
    gateway = TagGovernanceGateway(SimpleNamespace(coordinator=Coordinator(conn)))
    s = scope()
    ids = [row[0] for row in conn.execute("SELECT id FROM scoped_tags ORDER BY id").fetchall()]
    created = await gateway.create_suggestion(
        scope=s,
        action="merge",
        tag_ids=ids[:2],
        target_tag_id=ids[1],
        target_name="规范名称",
        target_type="topic",
        reason="两个 scoped Tag 是同义词",
        evidence={"source": "test", "memory_ids": [1]},
    )
    assert created.status == "pending"
    preview = gateway.preview(conn, scope=s, suggestion_id=created.suggestion_id, expected_revision=1)
    assert preview["preview"]["impact"]["memory_count"] == 1
    assert preview["preview"]["impact"]["relation_count"] == 1
    resolved = await gateway.resolve(
        scope=s,
        suggestion_id=created.suggestion_id,
        expected_revision=1,
        decision="approve",
        preview_token=preview["preflight_token"],
        reason="人工确认同义合并",
    )
    assert resolved.status == "approved"
    assert conn.execute("SELECT COUNT(*) FROM scoped_tags").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM scoped_memory_tags WHERE tag_id=?", (ids[1],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM scoped_memory_tags WHERE tag_id=?", (ids[0],)).fetchone()[0] == 0
    assert conn.execute("SELECT status, revision FROM scoped_tags WHERE id=?", (ids[1],)).fetchone() == ("active", 2)
    assert conn.execute("SELECT event_type FROM domain_outbox WHERE operation_id=?", (resolved.operation_id,)).fetchone()[0] == "tag.merge"
    repeated = await gateway.resolve(
        scope=s,
        suggestion_id=created.suggestion_id,
        expected_revision=1,
        decision="approve",
        preview_token=preview["preflight_token"],
        reason="人工确认同义合并",
    )
    assert repeated.operation_id == resolved.operation_id
    compensated = await gateway.compensate(
        scope=s,
        suggestion_id=created.suggestion_id,
        expected_revision=resolved.revision or 2,
        reason="撤销同义合并",
    )
    assert compensated.status == "compensated"
    assert conn.execute("SELECT COUNT(*) FROM scoped_tags").fetchone()[0] == 3
    assert conn.execute("SELECT name FROM scoped_tags ORDER BY id").fetchall() == [("旧名称",), ("新名称",), ("第三标签",)]
    assert conn.execute("SELECT COUNT(*) FROM scoped_memory_tags WHERE tag_id=?", (ids[0],)).fetchone()[0] == 1
    assert conn.execute("SELECT event_type FROM domain_outbox WHERE operation_id=?", (compensated.operation_id,)).fetchone()[0] == "tag.governance.compensated"


@pytest.mark.asyncio
async def test_scoped_tag_governance_records_conflict_status_for_stale_preview():
    conn = connection()
    gateway = TagGovernanceGateway(SimpleNamespace(coordinator=Coordinator(conn)))
    s = scope()
    tag_id = conn.execute("SELECT id FROM scoped_tags ORDER BY id LIMIT 1").fetchone()[0]
    suggestion = await gateway.create_suggestion(scope=s, action="retype", tag_ids=[tag_id], target_type="entity", reason="修正类型")
    preview = gateway.preview(conn, scope=s, suggestion_id=suggestion.suggestion_id)
    conn.execute("UPDATE scoped_tags SET revision=revision+1 WHERE id=?", (tag_id,))
    conn.commit()
    result = await gateway.resolve(scope=s, suggestion_id=suggestion.suggestion_id, expected_revision=1, decision="approve", preview_token=preview["preflight_token"], reason="预检已失效")
    assert result.status == "conflict"
    assert conn.execute("SELECT status FROM scoped_tag_audit_suggestions WHERE suggestion_id=?", (suggestion.suggestion_id,)).fetchone()[0] == "conflict"


@pytest.mark.asyncio
async def test_scoped_tag_governance_rejects_stale_preview_and_batch_is_all_or_nothing():
    conn = connection()
    gateway = TagGovernanceGateway(SimpleNamespace(coordinator=Coordinator(conn)))
    s = scope()
    ids = [row[0] for row in conn.execute("SELECT id FROM scoped_tags ORDER BY id").fetchall()]
    first = await gateway.create_suggestion(scope=s, action="retype", tag_ids=[ids[2]], target_type="entity", reason="修正类型")
    second = await gateway.create_suggestion(scope=s, action="deactivate", tag_ids=[ids[0]], reason="停止使用")
    p1 = gateway.preview(conn, scope=s, suggestion_id=first.suggestion_id)
    p2 = gateway.preview(conn, scope=s, suggestion_id=second.suggestion_id)
    with pytest.raises(TagGovernanceError) as caught:
        await gateway.resolve_batch(
            scope=s,
            items=[
                {"suggestion_id": first.suggestion_id, "revision": 1, "preflight_token": p1["preflight_token"]},
                {"suggestion_id": second.suggestion_id, "revision": 99, "preflight_token": p2["preflight_token"]},
            ],
            decision="approve",
            reason="批量确认",
        )
    assert caught.value.code == "batch_validation_failed"
    assert conn.execute("SELECT tag_type FROM scoped_tags WHERE id=?", (ids[2],)).fetchone()[0] == "keyword"
    assert conn.execute("SELECT status FROM scoped_tags WHERE id=?", (ids[0],)).fetchone()[0] == "active"
    alias = await gateway.create_suggestion(scope=s, action="alias", tag_ids=[ids[0]], aliases=["旧别名"], reason="保留搜索兼容")
    alias_preview = gateway.preview(conn, scope=s, suggestion_id=alias.suggestion_id)
    await gateway.resolve(scope=s, suggestion_id=alias.suggestion_id, expected_revision=1, decision="approve", preview_token=alias_preview["preflight_token"], reason="确认别名")
    assert json.loads(conn.execute("SELECT aliases FROM scoped_tags WHERE id=?", (ids[0],)).fetchone()[0]) == ["旧别名"]
