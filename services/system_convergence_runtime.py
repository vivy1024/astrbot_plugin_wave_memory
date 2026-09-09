"""Production write gateway for Stage 1 coordinated Memory/Tag mutations."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

try:
    from ..domain.commands import DomainCommand, EntityChange
    from ..domain.quality import QualityDecision, QualityProposal
    from ..domain.scope import RuntimeScope
    from ..engine.db.migrations.memories_v2 import MEMORIES_V2_VERSION
    from ..engine.write_coordinator import (
        CommandRejectedError,
        MutationOutcome,
        OutboxEventDraft,
        WriteCoordinator,
    )
    from ..services.soul_concerns import apply_concern_transition
    from .outbox_dispatcher import OutboxDispatcher
    from .durable_jobs import DurableJobService
    from .relationship_calibration import (
        RELATIONSHIP_CALIBRATE_COMMAND,
        apply_relationship_calibration,
    )
    from .tag_governance import TAG_GOVERNANCE_COMMANDS, apply_tag_governance_command
except ImportError:  # pragma: no cover - focused repository tests import top-level packages
    from domain.commands import DomainCommand, EntityChange
    from domain.quality import QualityDecision, QualityProposal
    from domain.scope import RuntimeScope
    from engine.db.migrations.memories_v2 import MEMORIES_V2_VERSION
    from engine.write_coordinator import (
        CommandRejectedError,
        MutationOutcome,
        OutboxEventDraft,
        WriteCoordinator,
    )
    from services.soul_concerns import apply_concern_transition
    from services.outbox_dispatcher import OutboxDispatcher
    from services.durable_jobs import DurableJobService
    from services.relationship_calibration import (
        RELATIONSHIP_CALIBRATE_COMMAND,
        apply_relationship_calibration,
    )
    from services.tag_governance import TAG_GOVERNANCE_COMMANDS, apply_tag_governance_command


_APPEND_MEMORY = "memory.append.v1"
_BACKFILL_MEMORY_VECTOR = "memory.vector_backfill.v1"
_APPLY_TAG_EXTRACTION = "tag_extraction.apply.v1"
_MUTATE_MEMORIES = "memory.mutate.v1"
_RECORD_EPISODE = "experience_episode.record.v1"
_CONCERN_TRANSITION = "soul_concern.transition.v1"
_CALIBRATE_RELATIONSHIP = RELATIONSHIP_CALIBRATE_COMMAND

# 关切的唯一合法动作集合。note 只负责记录/强化未决挂念；结案、重开、归档必须显式
# 走对应动作，由领域状态机校验跳转合法性，禁止任何路径隐式改状态。
_CONCERN_ACTIONS = frozenset({"note", "progress", "resolve", "reopen", "expire", "archive"})
_CONCERN_REINFORCABLE_STATUSES = frozenset({"active", "dormant", "progressing"})
_CONCERN_REINFORCE_STEP = 0.3


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_memory_scope(scope: RuntimeScope) -> RuntimeScope:
    if (
        not isinstance(scope, RuntimeScope)
        or scope.visibility not in {"group", "private"}
        or scope.session is None
    ):
        raise ValueError("a canonical group/private RuntimeScope is required")
    return scope


def _require_group_scope(scope: RuntimeScope) -> RuntimeScope:
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        raise ValueError("a canonical group RuntimeScope is required")
    return scope


def _scope_tuple(scope: RuntimeScope) -> tuple[str, str, str]:
    scope = _require_memory_scope(scope)
    assert scope.session is not None
    return scope.bot_id, scope.session.id, scope.visibility


def _append_memory_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    scope = _require_memory_scope(command.scope)
    assert scope.session is not None
    payload = command.payload
    group_id = str(payload["group_id"])
    if group_id != scope.session.conversation_id:
        raise ValueError("group_id does not match RuntimeScope")

    provenance = dict(payload.get("provenance") or {})
    origin_metadata = dict(payload.get("origin_metadata") or {})
    timestamp = float(payload.get("timestamp") or now)
    source = str(payload.get("source") or "live")
    content = str(payload.get("content") or "")
    sender_id = str(payload.get("sender_id") or "")
    sender_name = str(payload.get("sender_name") or "")
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
        "timestamp": timestamp,
        "source": source,
        "metadata": origin_metadata,
    }
    origin_fingerprint = hashlib.sha256(_canonical_json(origin_payload).encode("utf-8")).hexdigest()
    provenance_payload = {
        "kind": "wave_memory_provenance",
        "version": MEMORIES_V2_VERSION,
        "fingerprint_algorithm": "sha256",
        "origin_fingerprint": origin_fingerprint,
        "scope": scope_payload,
        "metadata": provenance,
    }
    quarantined = bool(payload.get("quarantine", False))
    memory_type = "archived" if quarantined else "message"
    summary = "quarantined: transient roleplay/identity confusion" if quarantined else None
    cursor = connection.execute(
        """INSERT INTO memories (
               group_id, sender_id, sender_name, content, vector, timestamp, importance, source,
               memory_type, summary, bot_id, session_id, visibility, origin_fingerprint,
               provenance, version, quarantine, resolution_state
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'resolved')""",
        (
            group_id,
            sender_id,
            sender_name,
            content,
            payload.get("vector_blob"),
            timestamp,
            float(payload.get("importance", 1.0)),
            source,
            memory_type,
            summary,
            scope.bot_id,
            scope.session.id,
            scope.visibility,
            origin_fingerprint,
            _canonical_json(provenance_payload),
            MEMORIES_V2_VERSION,
            int(quarantined),
        ),
    )
    memory_id = int(cursor.lastrowid)
    return MutationOutcome(
        entities=(EntityChange("memory", str(memory_id), MEMORIES_V2_VERSION, "created"),),
        events=(
            OutboxEventDraft(
                aggregate_kind="memory",
                aggregate_id=str(memory_id),
                aggregate_version=MEMORIES_V2_VERSION,
                event_type="memory.created",
                payload={
                    "memory_id": memory_id,
                    "source": source,
                    "quarantine": quarantined,
                    "scope": scope_payload,
                },
            ),
        ),
    )


def _backfill_memory_vector_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    """Attach a recovered embedding to one still-live, exact-scope memory.

    A missing/deleted/replaced row is deliberately a no-op: the durable backfill
    runner must be able to continue past stale work without widening its Scope.
    """
    scope = _require_memory_scope(command.scope)
    payload = command.payload
    memory_id = int(payload["memory_id"])
    vector_blob = payload.get("vector_blob")
    if not isinstance(vector_blob, (bytes, bytearray, memoryview)) or not vector_blob:
        raise ValueError("memory vector backfill requires a non-empty vector blob")
    if len(vector_blob) % np.dtype(np.float32).itemsize:
        raise ValueError("memory vector backfill requires float32-aligned data")

    row = connection.execute(
        """SELECT version, vector FROM memories
             WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
               AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0""",
        (memory_id, *_scope_tuple(scope)),
    ).fetchone()
    if row is None:
        return MutationOutcome(entities=(), events=(), warnings=("memory_missing_or_stale",))
    if row[1] is not None:
        return MutationOutcome(entities=(), events=(), warnings=("memory_vector_already_present",))

    previous_version = int(row[0] or MEMORIES_V2_VERSION)
    next_version = previous_version + 1
    connection.execute(
        """UPDATE memories SET vector=?, version=?
             WHERE id=? AND bot_id=? AND session_id=? AND visibility=? AND group_id=?""",
        (bytes(vector_blob), next_version, memory_id, *_scope_tuple(scope), scope.session.conversation_id),
    )
    scope_payload = {
        "bot_id": scope.bot_id,
        "session_id": scope.session.id,
        "visibility": scope.visibility,
        "group_id": scope.session.conversation_id,
    }
    return MutationOutcome(
        entities=(EntityChange("memory", str(memory_id), next_version, "vector_backfilled"),),
        events=(
            OutboxEventDraft(
                aggregate_kind="memory",
                aggregate_id=str(memory_id),
                aggregate_version=next_version,
                event_type="memory.vector_backfilled",
                payload={"memory_id": memory_id, "scope": scope_payload},
            ),
        ),
    )


def _catalog_neighbors_for_admission(connection, *, tag_type: str, limit: int = 64):
    """Load a bounded Catalog neighborhood for exact/semantic admission decisions."""
    try:
        from .tag_admission import CatalogNeighbor
    except ImportError:  # pragma: no cover
        from services.tag_admission import CatalogNeighbor

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'"
    ).fetchone() is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT id, normalized_name, display_name, tag_type, embedding
              FROM tag_catalog
             WHERE status='active' AND tag_type=?
             ORDER BY COALESCE(updated_at, created_at, 0) DESC, id ASC
             LIMIT ?
            """,
            (str(tag_type or "keyword"), max(1, int(limit))),
        ).fetchall()
    except Exception:
        return []
    neighbors: list[CatalogNeighbor] = []
    for row in rows:
        embedding = None
        if row[4] is not None:
            try:
                embedding = np.frombuffer(row[4], dtype=np.float32).astype(float).tolist()
            except (TypeError, ValueError):
                embedding = None
        neighbors.append(
            CatalogNeighbor(
                catalog_id=int(row[0]),
                normalized_name=str(row[1] or ""),
                display_name=str(row[2] or row[1] or ""),
                tag_type=str(row[3] or "keyword"),
                embedding=embedding,
            )
        )
    return neighbors


def _apply_tag_extraction_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    scope = _require_group_scope(command.scope)
    payload = command.payload
    memory_id = int(payload["memory_id"])
    row = connection.execute(
        """SELECT version FROM memories
             WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
               AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0""",
        (memory_id, *_scope_tuple(scope)),
    ).fetchone()
    if row is None:
        raise ValueError("memory is not a resolved member of the RuntimeScope")

    try:
        from .tag_admission import admit_tag_batch, normalize_admission_name
    except ImportError:  # pragma: no cover
        from services.tag_admission import admit_tag_batch, normalize_admission_name

    # Neighbor pool is loaded once per batch; exact/semantic decisions stay pure.
    raw_tags = [item for item in (payload.get("tags") or ()) if isinstance(item, Mapping)]
    neighbor_types = {
        str(item.get("type") or item.get("tag_type") or "keyword").strip().casefold() or "keyword"
        for item in raw_tags
    }
    catalog_neighbors = []
    for tag_type in neighbor_types:
        catalog_neighbors.extend(_catalog_neighbors_for_admission(connection, tag_type=tag_type))
    admitted_tags, decisions = admit_tag_batch(raw_tags, catalog=catalog_neighbors)
    rejected = [decision.reason for decision in decisions if decision.action == "reject"]

    entities: list[EntityChange] = []
    tag_ids: list[int] = []
    catalog_ids: list[int] = []
    tag_columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(scoped_tags)").fetchall()}
    catalog_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_catalog'"
    ).fetchone()

    for position, raw_tag in enumerate(admitted_tags, 1):
        name = str(raw_tag.get("name") or "").strip()
        if not name:
            continue
        tag_type = str(raw_tag.get("type") or "keyword")
        confidence = float(raw_tag.get("confidence", 0.8))
        metadata = _canonical_json({
            "producer": "tag_worker",
            "memory_id": memory_id,
            "admission": raw_tag.get("admission"),
            "admission_reason": raw_tag.get("admission_reason"),
        })
        catalog_id = None
        try:
            explicit_catalog = raw_tag.get("catalog_id")
            if explicit_catalog is not None:
                catalog_id = int(explicit_catalog)
        except (TypeError, ValueError):
            catalog_id = None

        if catalog_table is not None and "catalog_id" in tag_columns:
            normalized_name = normalize_admission_name(name)
            if catalog_id is None:
                connection.execute(
                    """INSERT INTO tag_catalog(
                           normalized_name, display_name, tag_type, description,
                           status, created_at, updated_at
                       ) VALUES (?, ?, ?, '', 'active', ?, ?)
                       ON CONFLICT(normalized_name, tag_type) DO UPDATE SET
                           display_name=CASE WHEN tag_catalog.display_name='' THEN excluded.display_name
                                             ELSE tag_catalog.display_name END,
                           updated_at=excluded.updated_at""",
                    (normalized_name, name, tag_type, now, now),
                )
                catalog_row = connection.execute(
                    "SELECT id, display_name FROM tag_catalog WHERE normalized_name=? AND tag_type=?",
                    (normalized_name, tag_type),
                ).fetchone()
            else:
                catalog_row = connection.execute(
                    "SELECT id, display_name FROM tag_catalog WHERE id=? AND status='active'",
                    (catalog_id,),
                ).fetchone()
                if catalog_row is None:
                    # Stale catalog_id from the caller must not invent a new row by id.
                    connection.execute(
                        """INSERT INTO tag_catalog(
                               normalized_name, display_name, tag_type, description,
                               status, created_at, updated_at
                           ) VALUES (?, ?, ?, '', 'active', ?, ?)
                           ON CONFLICT(normalized_name, tag_type) DO UPDATE SET
                               updated_at=excluded.updated_at""",
                        (normalized_name, name, tag_type, now, now),
                    )
                    catalog_row = connection.execute(
                        "SELECT id, display_name FROM tag_catalog WHERE normalized_name=? AND tag_type=?",
                        (normalized_name, tag_type),
                    ).fetchone()
            if catalog_row is not None:
                catalog_id = int(catalog_row[0])
                # Prefer the Catalog display name so semantic reuse collapses aliases.
                reused_name = str(catalog_row[1] or "").strip()
                if reused_name:
                    name = reused_name
            raw_vector = raw_tag.get("embedding")
            if catalog_id is not None and raw_vector is not None:
                try:
                    vector = np.asarray(raw_vector, dtype=np.float32).reshape(-1)
                    if vector.size:
                        connection.execute(
                            """UPDATE tag_catalog SET embedding=?, embedding_dim=?, updated_at=?
                                WHERE id=? AND status='active' AND embedding IS NULL""",
                            (vector.tobytes(), int(vector.size), now, catalog_id),
                        )
                except (TypeError, ValueError):
                    pass

        if catalog_id is not None:
            connection.execute(
                """INSERT INTO scoped_tags (
                       catalog_id, bot_id, session_id, visibility, name, tag_type, description, confidence,
                       metadata, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility, name) DO UPDATE SET
                       catalog_id=COALESCE(excluded.catalog_id, scoped_tags.catalog_id),
                       tag_type=excluded.tag_type, confidence=excluded.confidence,
                       metadata=excluded.metadata, updated_at=excluded.updated_at""",
                (catalog_id, *_scope_tuple(scope), name, tag_type, confidence, metadata, now, now),
            )
        else:
            connection.execute(
                """INSERT INTO scoped_tags (
                       bot_id, session_id, visibility, name, tag_type, description, confidence,
                       metadata, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?)
                   ON CONFLICT(bot_id, session_id, visibility, name) DO UPDATE SET
                       tag_type=excluded.tag_type, confidence=excluded.confidence,
                       metadata=excluded.metadata, updated_at=excluded.updated_at""",
                (*_scope_tuple(scope), name, tag_type, confidence, metadata, now, now),
            )
        tag_select = "id, catalog_id" if "catalog_id" in tag_columns else "id, NULL"
        tag_row = connection.execute(
            f"""SELECT {tag_select} FROM scoped_tags
                 WHERE bot_id=? AND session_id=? AND visibility=? AND name=?""",
            (*_scope_tuple(scope), name),
        ).fetchone()
        if tag_row is None:
            raise RuntimeError("scoped tag upsert did not return a row")
        tag_id = int(tag_row[0])
        catalog_id = int(tag_row[1]) if tag_row[1] is not None else catalog_id
        if catalog_id is not None:
            catalog_ids.append(catalog_id)
        connection.execute(
            """INSERT INTO scoped_memory_tags (
                   bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, 1.0, ?)
               ON CONFLICT(bot_id, session_id, visibility, memory_id, tag_id) DO UPDATE SET
                   position=excluded.position, relevance=excluded.relevance""",
            (*_scope_tuple(scope), memory_id, tag_id, position, now),
        )
        tag_ids.append(tag_id)
        entities.append(EntityChange("scoped_tag", str(tag_id), 1, "upserted"))

    status = str(payload.get("status") or ("done" if tag_ids else "skipped"))
    # All candidates rejected by admission still counts as a completed extraction.
    if not tag_ids and rejected and not str(payload.get("status") or "").strip():
        status = "skipped"
    error = payload.get("error")
    connection.execute(
        """INSERT INTO tag_extraction_status(memory_id, status, attempts, last_error, updated_at)
           VALUES (?, ?, 0, ?, ?)
           ON CONFLICT(memory_id) DO UPDATE SET
               status=excluded.status, last_error=excluded.last_error, updated_at=excluded.updated_at""",
        (memory_id, status, None if error is None else str(error)[:2000], now),
    )
    if bool(payload.get("upgrade_source")):
        connection.execute(
            """UPDATE memories SET source='core'
                 WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
                   AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0 AND source='chat'""",
            (memory_id, *_scope_tuple(scope)),
        )

    version = int(row[0] or 1) + 1
    connection.execute(
        """UPDATE memories SET version=?
             WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
        (version, memory_id, *_scope_tuple(scope)),
    )
    entities.append(EntityChange("memory", str(memory_id), version, "tag_extraction_updated"))
    return MutationOutcome(
        entities=tuple(entities),
        events=(
            OutboxEventDraft(
                aggregate_kind="memory",
                aggregate_id=str(memory_id),
                aggregate_version=version,
                event_type="memory.tags_applied",
                payload={
                    "memory_id": memory_id,
                    "tag_ids": tag_ids,
                    "catalog_ids": sorted(set(catalog_ids)),
                    "status": status,
                    "rejected_count": len(rejected),
                    "scope": scope.to_dict(),
                },
            ),
        ),
    )


_CONCERN_COLUMNS = """id, topic, intensity, origin_memory_id, origin_episode_id, concern_type,
                        status, urgency, last_progress_at, expected_resolution_at,
                        resolution_note, created_at, last_triggered, revision"""


def _concern_row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "topic": str(row[1] or ""),
        "intensity": float(row[2] or 0.0),
        "origin_memory_id": row[3],
        "origin_episode_id": row[4],
        "concern_type": str(row[5] or ""),
        "status": str(row[6] or "active"),
        "urgency": float(row[7] or 0.0),
        "last_progress_at": row[8],
        "expected_resolution_at": row[9],
        "resolution_note": str(row[10] or ""),
        "created_at": row[11],
        "last_triggered": row[12],
        "revision": int(row[13] or 1),
    }


def _find_concern(connection, scope: RuntimeScope, *, concern_id: Any, topic: str) -> dict[str, Any] | None:
    """按 id 或 topic 定位当前 Scope 内的关切；跨 Scope 一律视为不存在。"""
    selector = "id=?" if concern_id is not None else "topic=?"
    value = int(concern_id) if concern_id is not None else topic
    row = connection.execute(
        f"SELECT {_CONCERN_COLUMNS} FROM scoped_soul_concerns "
        f"WHERE bot_id=? AND session_id=? AND visibility=? AND {selector}",
        (*_scope_tuple(scope), value),
    ).fetchone()
    return None if row is None else _concern_row_to_dict(row)


def _concern_mutation(
    *, concern_id: int, revision: int, action: str, before: str, after: str, scope: RuntimeScope
) -> MutationOutcome:
    return MutationOutcome(
        entities=(EntityChange("soul_concern", str(concern_id), revision, action),),
        events=(
            OutboxEventDraft(
                "soul_concern",
                str(concern_id),
                revision,
                f"soul_concern.{action}",
                {
                    "concern_id": concern_id,
                    "from_status": before,
                    "to_status": after,
                    "scope": scope.to_dict(),
                },
            ),
        ),
    )


def _concern_transition_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    """Concern 的唯一写入口：记录/强化未决挂念，或按领域状态机推进其生命周期。

    刻意不做全量替换：任何动作只碰自己那一行，并发下不同关切不会互相覆盖；
    非法跳转由领域状态机拒绝，archived 是终态。
    """
    scope = _require_group_scope(command.scope)
    payload = dict(command.payload)
    action = str(payload.get("action") or "").strip().lower()
    if action not in _CONCERN_ACTIONS:
        raise CommandRejectedError("unsupported_concern_action", f"unsupported concern action: {action}")

    concern_id = payload.get("concern_id")
    topic = str(payload.get("topic") or "").strip()
    if concern_id is None and not topic:
        raise CommandRejectedError("concern_target_required", "concern_id or topic is required")
    current = _find_concern(connection, scope, concern_id=concern_id, topic=topic)

    new_evidence = payload.get("evidence")
    evidence_json = (
        json.dumps(list(new_evidence), ensure_ascii=False, sort_keys=True)
        if new_evidence
        else None
    )

    if action == "note":
        intensity = payload.get("intensity")
        intensity = 0.7 if intensity is None else max(0.0, min(1.0, float(intensity)))
        expected_resolution_at = payload.get("expected_resolution_at")
        if current is not None:
            if current["status"] not in _CONCERN_REINFORCABLE_STATUSES:
                # 已结案/已归档的挂念不因再次提及而悄悄复活，必须由显式 reopen 走状态机。
                return _concern_mutation(
                    concern_id=int(current["id"]),
                    revision=int(current["revision"]),
                    action="note_ignored_closed",
                    before=current["status"],
                    after=current["status"],
                    scope=scope,
                )
            next_status = "active" if current["status"] == "dormant" else current["status"]
            new_revision = int(current["revision"]) + 1
            connection.execute(
                """UPDATE scoped_soul_concerns
                      SET intensity=?, status=?, urgency=MAX(urgency, ?), last_triggered=?,
                          last_progress_at=COALESCE(last_progress_at, ?), revision=?,
                          expected_resolution_at=COALESCE(?, expected_resolution_at),
                          evidence=COALESCE(?, evidence)
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
                (
                    min(1.0, float(current["intensity"]) + _CONCERN_REINFORCE_STEP),
                    next_status,
                    intensity,
                    now,
                    now,
                    new_revision,
                    expected_resolution_at,
                    evidence_json,
                    int(current["id"]),
                    *_scope_tuple(scope),
                ),
            )
            return _concern_mutation(
                concern_id=int(current["id"]),
                revision=new_revision,
                action="reinforced",
                before=current["status"],
                after=next_status,
                scope=scope,
            )
        if not topic:
            raise CommandRejectedError("concern_note_requires_topic", "new concern requires a topic")
        created = connection.execute(
            """INSERT INTO scoped_soul_concerns
               (bot_id, session_id, visibility, topic, intensity, origin_memory_id, origin_episode_id,
                concern_type, status, urgency, last_progress_at, expected_resolution_at,
                resolution_note, created_at, last_triggered, revision, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, '', ?, ?, 1, ?)""",
            (
                scope.bot_id,
                scope.session.id,
                scope.visibility,
                topic,
                intensity,
                payload.get("origin_memory_id"),
                payload.get("origin_episode_id"),
                str(payload.get("concern_type") or "").strip(),
                intensity,
                now,
                expected_resolution_at,
                now,
                now,
                evidence_json or "[]",
            ),
        )
        return _concern_mutation(
            concern_id=int(created.lastrowid),
            revision=1,
            action="created",
            before="",
            after="active",
            scope=scope,
        )

    if current is None:
        raise CommandRejectedError("concern_not_found_in_scope")
    try:
        updated = apply_concern_transition(
            current, action=action, now=now, resolution_note=str(payload.get("note") or "")
        )
    except ValueError as exc:
        raise CommandRejectedError(
            "invalid_concern_transition",
            f"concern {current['status']} cannot accept action {action}",
        ) from exc
    new_revision = int(current["revision"]) + 1
    connection.execute(
        """UPDATE scoped_soul_concerns
              SET status=?, urgency=?, last_progress_at=?, resolution_note=?,
                  last_triggered=?, revision=?, evidence=COALESCE(?, evidence)
            WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
        (
            updated["status"],
            float(updated.get("urgency") or current.get("urgency") or 0.0),
            updated.get("last_progress_at"),
            str(updated.get("resolution_note") or ""),
            float(updated.get("last_triggered") or now),
            new_revision,
            evidence_json,
            int(current["id"]),
            *_scope_tuple(scope),
        ),
    )
    return _concern_mutation(
        concern_id=int(current["id"]),
        revision=new_revision,
        action=action,
        before=current["status"],
        after=str(updated["status"]),
        scope=scope,
    )


def _record_episode_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    scope = _require_group_scope(command.scope)
    assert scope.session is not None
    payload = {**dict(command.payload), **dict(command.payload.get("fields") or {})}
    if str(payload.get("group_id") or "") != scope.session.conversation_id:
        raise ValueError("group_id does not match RuntimeScope")
    source_ids = tuple(dict.fromkeys(int(value) for value in payload.get("source_memory_ids") or ()))
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            f"""SELECT id FROM memories WHERE id IN ({placeholders})
                AND bot_id=? AND session_id=? AND visibility=?
                AND group_id=? AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0""",
            (*source_ids, *_scope_tuple(scope), scope.session.conversation_id),
        ).fetchall()
        if {int(row[0]) for row in rows} != set(source_ids):
            raise ValueError("episode source memory is outside RuntimeScope")
    idem = str(command.idempotency_key)
    existing = connection.execute(
        "SELECT id FROM experience_episodes WHERE bot_id=? AND group_id=? AND idempotency_key=?",
        (scope.bot_id, scope.session.conversation_id, idem),
    ).fetchone()
    if existing:
        episode_id = int(existing[0])
        return MutationOutcome(
            entities=(EntityChange("experience_episode", str(episode_id), 1, "replayed"),),
            events=(),
        )
    columns = ("bot_id", "group_id", "user_id", "episode_type", "trigger_text", "bot_inner_thought", "bot_action", "bot_reply", "user_reaction", "outcome", "source_memory_ids", "emotional_weight", "idempotency_key", "created_at", "updated_at")
    values = (scope.bot_id, scope.session.conversation_id, payload.get("user_id"), payload.get("episode_type"), payload.get("trigger_text"), payload.get("bot_inner_thought"), payload.get("bot_action"), payload.get("bot_reply"), payload.get("user_reaction"), payload.get("outcome"), json.dumps(list(source_ids), ensure_ascii=False), float(payload.get("emotional_weight") or 0), idem, now, now)
    cur = connection.execute(f"INSERT INTO experience_episodes ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
    episode_id = int(cur.lastrowid)
    return MutationOutcome(
        entities=(EntityChange("experience_episode", str(episode_id), 1, "created"),),
        events=(OutboxEventDraft("experience_episode", str(episode_id), 1, "experience_episode.created", {"episode_id": episode_id, "scope": scope.to_dict()}),),
    )


def _mutate_memories_handler(connection, command: DomainCommand, now: float) -> MutationOutcome:
    scope = _require_memory_scope(command.scope)
    payload = command.payload
    memory_ids = tuple(dict.fromkeys(int(value) for value in payload.get("memory_ids") or ()))
    if not memory_ids:
        return MutationOutcome(entities=(), events=())
    placeholders = ",".join("?" for _ in memory_ids)
    rows = connection.execute(
        f"""SELECT id, COALESCE(version, 1) FROM memories
              WHERE id IN ({placeholders}) AND bot_id=? AND session_id=? AND visibility=?
                AND group_id=? AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0""",
        (*memory_ids, *_scope_tuple(scope), scope.session.conversation_id),
    ).fetchall()
    versions = {int(row[0]): int(row[1]) for row in rows}
    allowed_ids = tuple(memory_id for memory_id in memory_ids if memory_id in versions)
    if not allowed_ids:
        return MutationOutcome(entities=(), events=(), warnings=("no_scoped_memories_matched",))
    allowed_placeholders = ",".join("?" for _ in allowed_ids)
    action = str(payload.get("action") or "")
    if action == "touch":
        boost = float(payload.get("importance_boost", 0.01))
        connection.execute(
            f"""UPDATE memories
                   SET access_count=COALESCE(access_count, 0)+1,
                       last_accessed=?, importance=MIN(3.0, COALESCE(importance, 1.0)+?)
                 WHERE id IN ({allowed_placeholders})
                   AND bot_id=? AND session_id=? AND visibility=? AND group_id=?""",
            (now, boost, *allowed_ids, *_scope_tuple(scope), scope.session.conversation_id),
        )
    elif action == "set_importance":
        importance = float(payload["importance"])
        connection.execute(
            f"""UPDATE memories SET importance=? WHERE id IN ({allowed_placeholders})
                AND bot_id=? AND session_id=? AND visibility=? AND group_id=?""",
            (importance, *allowed_ids, *_scope_tuple(scope), scope.session.conversation_id),
        )
    elif action in {"archive", "evict"}:
        memory_type = "archived" if action == "archive" else "evicted"
        connection.execute(
            f"""UPDATE memories SET memory_type=? WHERE id IN ({allowed_placeholders})
                AND bot_id=? AND session_id=? AND visibility=? AND group_id=?""",
            (memory_type, *allowed_ids, *_scope_tuple(scope), scope.session.conversation_id),
        )
    elif action == "delete":
        connection.execute(
            f"DELETE FROM scoped_memory_tags WHERE memory_id IN ({allowed_placeholders})",
            allowed_ids,
        )
        connection.execute(
            f"DELETE FROM memory_tags WHERE memory_id IN ({allowed_placeholders})",
            allowed_ids,
        )
        connection.execute(
            f"""DELETE FROM memories WHERE id IN ({allowed_placeholders})
                AND bot_id=? AND session_id=? AND visibility=? AND group_id=?""",
            (*allowed_ids, *_scope_tuple(scope), scope.session.conversation_id),
        )
    else:
        raise ValueError(f"unsupported memory mutation: {action}")

    new_versions = {memory_id: versions[memory_id] + 1 for memory_id in allowed_ids}
    if action != "delete":
        connection.execute(
            f"""UPDATE memories SET version=COALESCE(version, 1)+1
                WHERE id IN ({allowed_placeholders})
                  AND bot_id=? AND session_id=? AND visibility=? AND group_id=?""",
            (*allowed_ids, *_scope_tuple(scope), scope.session.conversation_id),
        )
    change_type = {
        "touch": "accessed",
        "set_importance": "importance_updated",
        "archive": "archived",
        "evict": "evicted",
        "delete": "deleted",
    }[action]
    entities = tuple(
        EntityChange("memory", str(memory_id), new_versions[memory_id], change_type)
        for memory_id in allowed_ids
    )
    events = tuple(
        OutboxEventDraft(
            aggregate_kind="memory",
            aggregate_id=str(memory_id),
            aggregate_version=new_versions[memory_id],
            event_type=f"memory.{change_type}",
            payload={"memory_id": memory_id, "action": action},
        )
        for memory_id in allowed_ids
    )
    warnings = () if len(allowed_ids) == len(memory_ids) else ("some_memories_failed_scope_check",)
    return MutationOutcome(entities=entities, events=events, warnings=warnings)


class CoordinatorQualityRepository:
    """Persist quality decisions through the same writer-owned SQLite connection."""

    def __init__(self, coordinator: WriteCoordinator, *, clock: Any) -> None:
        self.coordinator = coordinator
        self.clock = clock

    def record(self, proposal: QualityProposal, decision: QualityDecision) -> QualityDecision:
        if proposal.proposal_id != decision.proposal_id:
            raise ValueError("quality decision does not belong to proposal")
        normalized_hash = "sha256:" + hashlib.sha256(
            decision.normalized_content.encode("utf-8")
        ).hexdigest()
        proposal_payload = proposal.to_dict()
        decided_at = float(self.clock.now())

        def persist(connection):
            cursor = connection.execute(
                """INSERT INTO quality_decisions(
                       proposal_id, operation, outcome, reason_code, reason_codes_json,
                       rule_version, raw_artifact_json, target_scope_json,
                       normalized_content_hash, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(proposal_id) DO NOTHING""",
                (
                    proposal.proposal_id,
                    proposal.operation,
                    decision.outcome,
                    decision.reason_code,
                    _canonical_json(decision.reason_codes),
                    decision.rule_version,
                    _canonical_json(proposal_payload["raw_artifact"]),
                    None
                    if proposal_payload["target_scope"] is None
                    else _canonical_json(proposal_payload["target_scope"]),
                    normalized_hash,
                    decided_at,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    """SELECT operation, outcome, reason_code, rule_version,
                              normalized_content_hash, target_scope_json
                         FROM quality_decisions WHERE proposal_id=?""",
                    (proposal.proposal_id,),
                ).fetchone()
                expected = (
                    proposal.operation,
                    decision.outcome,
                    decision.reason_code,
                    decision.rule_version,
                    normalized_hash,
                    None
                    if proposal_payload["target_scope"] is None
                    else _canonical_json(proposal_payload["target_scope"]),
                )
                if existing is None or tuple(existing) != expected:
                    raise ValueError("quality_decision_conflict")
            return decision

        return self.coordinator.transaction_blocking(persist)


class ProductionWriteGateway:
    """Typed production ingress backed by the process-exclusive WriteCoordinator."""

    def __init__(
        self,
        database_path: str,
        *,
        clock: Any | None = None,
        consumers: Mapping[str, Any] | None = None,
    ) -> None:
        self._clock = clock or _SystemClock()
        self._consumers = dict(consumers or {})
        self._closing = False
        self._relationship_repository = None
        self._tag_governance = None
        handlers = {
            _APPEND_MEMORY: _append_memory_handler,
            _BACKFILL_MEMORY_VECTOR: _backfill_memory_vector_handler,
            _APPLY_TAG_EXTRACTION: _apply_tag_extraction_handler,
            _MUTATE_MEMORIES: _mutate_memories_handler,
            _RECORD_EPISODE: _record_episode_handler,
            _CONCERN_TRANSITION: _concern_transition_handler,
            _CALIBRATE_RELATIONSHIP: self._calibrate_relationship_handler,
        }
        for command_type in TAG_GOVERNANCE_COMMANDS:
            handlers[command_type] = self._tag_governance_handler
        self.coordinator = WriteCoordinator(
            database_path,
            command_handlers=handlers,
            consumer_names=tuple(self._consumers),
            clock=self._clock,
        )
        self.dispatcher = OutboxDispatcher(
            self.coordinator,
            self._consumers,
            self._clock,
        )
        self.quality_repository = CoordinatorQualityRepository(
            self.coordinator,
            clock=self._clock,
        )
        self.jobs = DurableJobService(self.coordinator, clock=self._clock)

    def bind_relationship_repository(self, repository: Any) -> None:
        self._relationship_repository = repository

    def bind_tag_governance(self, gateway: Any, mutate=None) -> None:
        self._tag_governance = gateway
        self._tag_governance_mutate = mutate

    def _calibrate_relationship_handler(self, connection, command: DomainCommand, now: float) -> MutationOutcome:
        repository = self._relationship_repository or command.payload.get("repository")
        if repository is None:
            raise CommandRejectedError("relationship_repository_unavailable")
        payload = dict(command.payload)
        return apply_relationship_calibration(
            connection,
            repository=repository,
            scope=command.scope,  # type: ignore[arg-type]
            operation_id=command.operation_id,
            now=now,
            subject=str(payload["subject_principal_id"]),
            expected_revision=int(payload["expected_revision"]),
            action=str(payload["action"]),
            dimension=str(payload["dimension"]),
            delta=payload.get("delta"),
            value=payload.get("value"),
            reason=str(payload["reason"]),
            evidence=list(payload.get("evidence") or ()),
        )

    def _tag_governance_handler(self, connection, command: DomainCommand, now: float) -> MutationOutcome:
        gateway = self._tag_governance or getattr(self.coordinator, "gateway", None)
        mutate = getattr(self, "_tag_governance_mutate", None) or getattr(self.coordinator, "mutate", None)
        if gateway is None or mutate is None:
            raise CommandRejectedError("tag_governance_unavailable")
        return gateway.apply_mutate(connection, command, now, mutate)

    async def calibrate_relationship(self, *, scope: RuntimeScope, **payload: Any):
        repository = payload.pop("repository", None)
        if repository is not None:
            self.bind_relationship_repository(repository)
        request_hash = str(payload.pop("request_hash") or "")
        idempotency_key = str(payload.pop("idempotency_key") or "")
        operation_id = str(payload.pop("operation_id") or "")
        command = DomainCommand(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            actor="webui.relationship.calibration",
            scope=scope,
            command_type=_CALIBRATE_RELATIONSHIP,
            payload=payload,
            request_hash=request_hash,
        )
        return await self.coordinator.submit(command)

    @staticmethod
    def _command(
        *,
        command_type: str,
        actor: str,
        scope: RuntimeScope,
        payload: Mapping[str, Any],
        idempotency_key: str,
        request_shape: Mapping[str, Any],
    ) -> DomainCommand:
        request_hash = _digest(request_shape)
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"wave-memory:{command_type}:{idempotency_key}:{request_hash}",
        ).hex
        return DomainCommand(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            actor=actor,
            scope=scope,
            command_type=command_type,
            payload=payload,
            request_hash=request_hash,
        )

    async def append_memory(
        self,
        *,
        scope: RuntimeScope,
        group_id: str,
        content: str,
        vector: np.ndarray | None,
        sender_id: str,
        sender_name: str,
        timestamp: float,
        importance: float,
        source: str,
        provenance: Mapping[str, Any],
        origin_metadata: Mapping[str, Any],
        quarantine: bool,
        idempotency_hint: str | None = None,
    ) -> int:
        vector_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        request_shape = {
            "scope": scope.to_dict(),
            "group_id": group_id,
            "content": content,
            "vector_sha256": None if vector_blob is None else hashlib.sha256(vector_blob).hexdigest(),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "timestamp": float(timestamp),
            "importance": float(importance),
            "source": source,
            "provenance": dict(provenance),
            "origin_metadata": dict(origin_metadata),
            "quarantine": bool(quarantine),
        }
        stable_hint = str(idempotency_hint or "").strip()
        idempotency_key = (
            f"memory.append:{stable_hint}" if stable_hint else f"memory.append:{_digest(request_shape)}"
        )
        command = self._command(
            command_type=_APPEND_MEMORY,
            actor="message_writer",
            scope=scope,
            payload={**request_shape, "vector_blob": vector_blob},
            idempotency_key=idempotency_key,
            request_shape=request_shape,
        )
        result = await self.coordinator.submit(command)
        memory = next(item for item in result.entities if item.aggregate_kind == "memory")
        return int(memory.aggregate_id)

    async def backfill_memory_vector(
        self,
        *,
        scope: RuntimeScope,
        memory_id: int,
        vector: np.ndarray,
        idempotency_hint: str | None = None,
    ) -> bool:
        """Persist one recovered embedding through the canonical scoped write path."""
        normalized = np.asarray(vector, dtype=np.float32)
        if normalized.ndim != 1 or normalized.size <= 0 or not np.isfinite(normalized).all():
            raise ValueError("memory vector backfill requires a finite one-dimensional vector")
        vector_blob = normalized.tobytes()
        request_shape = {
            "scope": scope.to_dict(),
            "memory_id": int(memory_id),
            "vector_sha256": hashlib.sha256(vector_blob).hexdigest(),
        }
        stable_hint = str(idempotency_hint or _digest(request_shape)).strip()
        command = self._command(
            command_type=_BACKFILL_MEMORY_VECTOR,
            actor="memory_vector_backfill",
            scope=scope,
            payload={**request_shape, "vector_blob": vector_blob},
            idempotency_key=f"memory.vector_backfill:{stable_hint}",
            request_shape=request_shape,
        )
        result = await self.coordinator.submit(command)
        return any(item.aggregate_kind == "memory" for item in result.entities)

    async def apply_tag_extraction(
        self,
        *,
        scope: RuntimeScope,
        memory_id: int,
        tags: Sequence[Mapping[str, Any]],
        status: str,
        upgrade_source: bool = False,
        error: str | None = None,
    ) -> int:
        normalized_tags = [dict(tag) for tag in tags if isinstance(tag, Mapping)]
        request_shape = {
            "scope": scope.to_dict(),
            "memory_id": int(memory_id),
            "tags": normalized_tags,
            "status": status,
            "upgrade_source": bool(upgrade_source),
            "error": error,
        }
        command = self._command(
            command_type=_APPLY_TAG_EXTRACTION,
            actor="tag_worker",
            scope=scope,
            payload=request_shape,
            idempotency_key=f"tag-extraction:{memory_id}:{_digest(request_shape)}",
            request_shape=request_shape,
        )
        result = await self.coordinator.submit(command)
        return sum(1 for item in result.entities if item.aggregate_kind == "scoped_tag")

    async def record_episode(
        self,
        *,
        scope: RuntimeScope,
        group_id: str,
        user_id: str | None,
        episode_type: str,
        fields: Mapping[str, Any],
        source_memory_ids: Sequence[int] = (),
        emotional_weight: float = 0.0,
        idempotency_hint: str | None = None,
    ) -> int:
        request_shape = {
            "scope": scope.to_dict(), "group_id": str(group_id), "user_id": user_id,
            "episode_type": str(episode_type), "fields": dict(fields),
            "source_memory_ids": [int(value) for value in source_memory_ids],
            "emotional_weight": float(emotional_weight),
        }
        stable_hint = str(idempotency_hint or _digest(request_shape)).strip()
        command = self._command(
            command_type=_RECORD_EPISODE,
            actor="agent_episode_tool",
            scope=scope,
            payload={**request_shape, "source_memory_ids": list(request_shape["source_memory_ids"])},
            idempotency_key=f"experience_episode.record:{stable_hint}",
            request_shape=request_shape,
        )
        result = await self.coordinator.submit(command)
        entity = next(item for item in result.entities if item.aggregate_kind == "experience_episode")
        return int(entity.aggregate_id)

    async def transition_concern(
        self,
        *,
        scope: RuntimeScope,
        action: str,
        concern_id: int | None = None,
        topic: str | None = None,
        note: str = "",
        intensity: float | None = None,
        concern_type: str = "",
        origin_memory_id: int | None = None,
        origin_episode_id: int | None = None,
        expected_resolution_at: float | None = None,
        evidence: Sequence[Mapping[str, Any]] = (),
        idempotency_hint: str | None = None,
        actor: str = "concern_tool",
    ) -> dict[str, Any]:
        """记录或推进一条灵魂关切，必须经命令链落库并产出 outbox 事件。

        幂等边界：``idempotency_hint`` 传本轮请求标识（trace/轮次），使同一轮内的
        重试被折叠，而跨轮次再次提及仍可强化；缺省时退化为按请求体摘要幂等。
        """
        action = str(action or "").strip().lower()
        if action not in _CONCERN_ACTIONS:
            raise ValueError(f"unsupported concern action: {action}")
        request_shape = {
            "scope": scope.to_dict(),
            "action": action,
            "concern_id": int(concern_id) if concern_id is not None else None,
            "topic": str(topic or "").strip(),
            "note": str(note or "").strip(),
            "intensity": None if intensity is None else float(intensity),
            "concern_type": str(concern_type or "").strip(),
            "origin_memory_id": origin_memory_id,
            "origin_episode_id": origin_episode_id,
            "expected_resolution_at": (
                None if expected_resolution_at is None else float(expected_resolution_at)
            ),
            "evidence": [dict(item) for item in evidence if isinstance(item, Mapping)],
        }
        stable_hint = str(idempotency_hint or "").strip()
        idempotency_key = (
            f"soul_concern.transition:{action}:{stable_hint}"
            if stable_hint
            else f"soul_concern.transition:{_digest(request_shape)}"
        )
        command = self._command(
            command_type=_CONCERN_TRANSITION,
            actor=actor,
            scope=scope,
            payload=request_shape,
            idempotency_key=idempotency_key,
            request_shape=request_shape,
        )
        result = await self.coordinator.submit(command)
        entity = next(item for item in result.entities if item.aggregate_kind == "soul_concern")
        return {
            "concern_id": int(entity.aggregate_id),
            "action": str(entity.change_type),
            "revision": int(entity.aggregate_version),
        }

    async def mutate_memories(
        self,
        *,
        scope: RuntimeScope,
        memory_ids: Sequence[int],
        action: str,
        importance: float | None = None,
        importance_boost: float = 0.01,
        idempotency_hint: str | None = None,
    ) -> tuple[int, ...]:
        normalized_ids = tuple(dict.fromkeys(int(value) for value in memory_ids))
        request_shape = {
            "scope": scope.to_dict(),
            "memory_ids": normalized_ids,
            "action": action,
            "importance": importance,
            "importance_boost": float(importance_boost),
        }
        if action == "touch" and not idempotency_hint:
            idempotency_hint = uuid.uuid4().hex
        stable_hint = str(idempotency_hint or _digest(request_shape))
        command = self._command(
            command_type=_MUTATE_MEMORIES,
            actor="memory_lifecycle",
            scope=scope,
            payload=request_shape,
            idempotency_key=f"memory.mutate:{action}:{stable_hint}",
            request_shape=request_shape,
        )
        result = await self.coordinator.submit(command)
        return tuple(
            int(item.aggregate_id)
            for item in result.entities
            if item.aggregate_kind == "memory"
        )

    async def touch_memories(
        self,
        *,
        scope: RuntimeScope,
        memory_ids: Sequence[int],
        importance_boost: float = 0.01,
    ) -> tuple[int, ...]:
        return await self.mutate_memories(
            scope=scope,
            memory_ids=memory_ids,
            action="touch",
            importance_boost=importance_boost,
        )

    async def set_memory_importance(
        self,
        *,
        scope: RuntimeScope,
        memory_ids: Sequence[int],
        importance: float,
        idempotency_hint: str | None = None,
    ) -> tuple[int, ...]:
        return await self.mutate_memories(
            scope=scope,
            memory_ids=memory_ids,
            action="set_importance",
            importance=importance,
            idempotency_hint=idempotency_hint,
        )

    async def run_outbox_loop(self, interval_seconds: float = 0.25) -> None:
        import asyncio

        while not self._closing:
            try:
                await self.drain_committed()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(min(max(float(interval_seconds), 0.05), 2.0))
                continue
            await asyncio.sleep(max(float(interval_seconds), 0.05))

    async def drain_committed(self) -> int:
        watermark = await self.coordinator.committed_watermark()
        await self.dispatcher.drain_to_watermark(watermark)
        return watermark

    async def advance_and_drain(self) -> int:
        await self.dispatcher.advance_clock_to_next_attempt()
        return await self.drain_committed()

    async def save_projection_barrier(self, watermark: int) -> None:
        for consumer in self._consumers.values():
            barrier = getattr(consumer, "save_barrier", None)
            if not callable(barrier):
                continue
            result = barrier(db_watermark=int(watermark))
            if hasattr(result, "__await__"):
                await result

    async def shutdown(self) -> None:
        self._closing = True
        await self.coordinator.close_accepting()
        watermark = await self.drain_committed()
        await self.save_projection_barrier(watermark)
        await self.dispatcher.close()
        await self.coordinator.shutdown()


class _SystemClock:
    @staticmethod
    def now() -> float:
        return time.time()


__all__ = ["ProductionWriteGateway"]
