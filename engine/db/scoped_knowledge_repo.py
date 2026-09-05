"""RuntimeScope 严格隔离的派生知识仓储。

这是新 ``scoped_*`` 数据面的唯一正式 API。它不会向 legacy 表回退，也不会仅凭
``group_id`` 推断归属；每次读写都需要一个 canonical group ``RuntimeScope``。
"""

from __future__ import annotations

import json
import time
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from ...domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - repository tests import engine as top-level
    from domain.scope import RuntimeScope

from .connection import ConnectionManager
from .scoped_tag_projection import effective_tag_rows
try:
    from ...services.facts_conflict import FactConflictClassifier
    from ...services.belief_confidence import calculate_confidence
except ImportError:  # pragma: no cover
    from services.facts_conflict import FactConflictClassifier
    from services.belief_confidence import calculate_confidence


def _belief_revision(row: Mapping[str, Any]) -> int:
    """Keep candidate target revisions identical to the formal WebUI ObjectRef rule."""
    try:
        value = float(row.get("updated_at") or row.get("created_at") or 1)
    except (TypeError, ValueError):
        value = 1.0
    return max(1, int(value * 1000))


class ScopedKnowledgeScopeError(ValueError):
    """派生知识 API 的稳定 fail-closed Scope 拒绝。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message or code)


def _require_group_scope(scope: RuntimeScope | None) -> RuntimeScope:
    """接受仅可持久化为 scoped group resource 的完整 RuntimeScope。"""
    if not isinstance(scope, RuntimeScope):
        raise ScopedKnowledgeScopeError(
            "scope_required",
            "a canonical group RuntimeScope is required for scoped derived knowledge",
        )
    if scope.visibility != "group" or scope.session is None or scope.session.kind != "group":
        raise ScopedKnowledgeScopeError(
            "derived_scope_visibility_unsupported",
            "scoped derived knowledge only accepts group RuntimeScope values",
        )
    return scope


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    # RuntimeScope 已在构造时验证 canonical SessionRef；不要从 group_id 或 caller
    # 提供的裸字符串重建 scope。subject 是消息主体，不是 group 派生对象的归属维度。
    assert scope.session is not None
    return (scope.bot_id, scope.session.id, scope.visibility)


def _require_exact_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty exact string")
    return value


def _canonical_json(value: Mapping[str, Any] | None, field_name: str) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping when provided")
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_contexts(value: Sequence[Any] | None) -> str:
    if value is None:
        return "[]"
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("contexts must be a non-string sequence when provided")
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def normalize_tag_name(value: Any) -> str:
    """Normalize a Tag name for semantic Catalog uniqueness on the write path.

    Stronger than bare NFKC: strips zero-width chars, collapses whitespace, casefolds
    Latin text, and removes trailing particles so near-duplicate phrases collide.
    Display names remain the original caller-provided text at insert time.
    """
    try:
        from ...services.tag_admission import normalize_admission_name
    except ImportError:  # pragma: no cover - focused package imports
        try:
            from services.tag_admission import normalize_admission_name
        except ImportError:
            return unicodedata.normalize("NFKC", str(value or "")).strip()
    return normalize_admission_name(value)


def _positive_ints(values: Sequence[Any]) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            converted = int(value)
        except (TypeError, ValueError):
            continue
        if converted > 0:
            result.append(converted)
    return result


class ScopedKnowledgeRepo:
    """读写 scoped_jargon/facts/tags/beliefs/cursors 的 fail-closed 边界。"""

    _MEMORY_SCOPE_COLUMNS = frozenset(
        {"bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
    )

    def __init__(self, cm: ConnectionManager):
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm

    def _require_scoped_memory(self, scope: RuntimeScope, memory_id: int | None) -> None:
        """验证关联 memory 已解析且与目标 Scope 三元组精确一致。

        legacy memories 缺少 v2 scope 列，或任一字段不一致时都拒绝建立派生链接。
        不设置 legacy 外键，避免 schema 层把未解析的历史行伪装成可用证据。
        """
        if memory_id is None:
            return
        if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0:
            raise ValueError("source_memory_id must be a positive integer when provided")
        columns = {
            row[1] for row in self.cm.execute_read("PRAGMA table_info(memories)").fetchall()
        }
        if not self._MEMORY_SCOPE_COLUMNS <= columns:
            raise ScopedKnowledgeScopeError(
                "memory_scope_schema_missing",
                "memories v2 scope columns are required before linking derived knowledge",
            )
        row = self.cm.execute_read(
            """SELECT id FROM memories
                 WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
                   AND resolution_state='resolved' AND COALESCE(quarantine, 0)=0""",
            (memory_id, *_scope_params(scope)),
        ).fetchone()
        if row is None:
            raise ScopedKnowledgeScopeError(
                "memory_scope_mismatch",
                "source memory is unresolved, quarantined, or belongs to another RuntimeScope",
            )

    def _tag_in_scope(self, scope: RuntimeScope, tag_id: int) -> None:
        if isinstance(tag_id, bool) or not isinstance(tag_id, int) or tag_id <= 0:
            raise ValueError("tag_id must be a positive integer")
        row = self.cm.execute_read(
            """SELECT id FROM scoped_tags
                 WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (tag_id, *_scope_params(scope)),
        ).fetchone()
        if row is None:
            raise ScopedKnowledgeScopeError("tag_scope_mismatch", "tag does not belong to the RuntimeScope")

    def _select_id(self, table: str, where: str, params: tuple[Any, ...]) -> int:
        row = self.cm.execute_read(f"SELECT id FROM {table} WHERE {where}", params).fetchone()
        if row is None:  # pragma: no cover - guards against unexpected SQLite failures
            raise RuntimeError(f"upsert into {table} did not produce a row")
        return int(row[0])

    def upsert_scoped_jargon(
        self,
        scope: RuntimeScope,
        *,
        word: str,
        meaning: str = "",
        status: str = "pending",
        is_jargon: bool | None = None,
        frequency: int = 0,
        confidence: float = 0.0,
        contexts: Sequence[Any] | None = None,
        source_memory_id: int | None = None,
        source_context: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> int:
        scope = _require_group_scope(scope)
        word = _require_exact_string(word, "word")
        if not isinstance(meaning, str) or not isinstance(status, str):
            raise TypeError("meaning and status must be strings")
        if is_jargon is not None and not isinstance(is_jargon, bool):
            raise TypeError("is_jargon must be bool or None")
        if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency < 0:
            raise ValueError("frequency must be a non-negative integer")
        self._require_scoped_memory(scope, source_memory_id)
        now = time.time()
        self.cm.execute_write(
            """INSERT INTO scoped_jargon (
                    bot_id, session_id, visibility, word, meaning, status, is_jargon,
                    frequency, confidence, contexts, source_memory_id, source_context,
                    provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, session_id, visibility, word) DO UPDATE SET
                    meaning=excluded.meaning, status=excluded.status, is_jargon=excluded.is_jargon,
                    frequency=excluded.frequency, confidence=excluded.confidence,
                    contexts=excluded.contexts, source_memory_id=excluded.source_memory_id,
                    source_context=excluded.source_context, provenance=excluded.provenance,
                    updated_at=excluded.updated_at""",
            (
                *_scope_params(scope), word, meaning, status,
                None if is_jargon is None else int(is_jargon), frequency, float(confidence),
                _canonical_contexts(contexts), source_memory_id, source_context,
                _canonical_json(provenance, "provenance"), now, now,
            ),
        )
        self.cm.commit()
        return self._select_id(
            "scoped_jargon",
            "bot_id=? AND session_id=? AND visibility=? AND word=?",
            (*_scope_params(scope), word),
        )

    def list_scoped_jargon(self, scope: RuntimeScope, *, status: str | None = None, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
        scope = _require_group_scope(scope)
        if status is not None and not isinstance(status, str):
            raise TypeError("status must be a string when provided")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        conditions = ["bot_id=?", "session_id=?", "visibility=?"]
        params: list[Any] = list(_scope_params(scope))
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        elif not include_archived:
            conditions.append("status!='archived'")
        rows = self.cm.execute_read(
            f"""SELECT id, word, meaning, status, is_jargon, frequency, confidence, contexts,
                       source_memory_id, source_context, provenance, created_at, updated_at
                  FROM scoped_jargon WHERE {' AND '.join(conditions)}
                 ORDER BY updated_at DESC, id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        return [
            {
                "id": row[0], "word": row[1], "meaning": row[2], "status": row[3],
                "is_jargon": None if row[4] is None else bool(row[4]), "frequency": row[5],
                "confidence": row[6], "contexts": json.loads(row[7]),
                "source_memory_id": row[8], "source_context": row[9],
                "provenance": json.loads(row[10]), "created_at": row[11], "updated_at": row[12],
            }
            for row in rows
        ]

    def upsert_scoped_fact(
        self,
        scope: RuntimeScope,
        *,
        subject: str,
        predicate: str,
        object: str,
        confidence: float = 0.0,
        status: str = "pending",
        source_memory_id: int | None = None,
        provenance: Mapping[str, Any] | None = None,
        valid_from: float | None = None,
        valid_until: float | None = None,
    ) -> int:
        scope = _require_group_scope(scope)
        subject, predicate, object = (
            _require_exact_string(subject, "subject"),
            _require_exact_string(predicate, "predicate"),
            _require_exact_string(object, "object"),
        )
        if not isinstance(status, str):
            raise TypeError("status must be a string")
        self._require_scoped_memory(scope, source_memory_id)
        now = time.time()
        self.cm.execute_write(
            """INSERT INTO scoped_facts (
                    bot_id, session_id, visibility, subject, predicate, object, confidence, status,
                    source_memory_id, provenance, valid_from, valid_until, created_at, updated_at,
                    revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(bot_id, session_id, visibility, subject, predicate, object) DO UPDATE SET
                    confidence=excluded.confidence, status=excluded.status,
                    source_memory_id=excluded.source_memory_id, provenance=excluded.provenance,
                    valid_from=excluded.valid_from, valid_until=excluded.valid_until,
                    updated_at=excluded.updated_at, revision=scoped_facts.revision+1
                WHERE scoped_facts.status NOT IN ('deleted', 'superseded')""",
            (
                *_scope_params(scope), subject, predicate, object, float(confidence), status,
                source_memory_id, _canonical_json(provenance, "provenance"), valid_from,
                valid_until, now, now,
            ),
        )
        self.cm.commit()
        return self._select_id(
            "scoped_facts",
            "bot_id=? AND session_id=? AND visibility=? AND subject=? AND predicate=? AND object=?",
            (*_scope_params(scope), subject, predicate, object),
        )

    def list_scoped_facts(self, scope: RuntimeScope, *, subject: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        scope = _require_group_scope(scope)
        if subject is not None:
            subject = _require_exact_string(subject, "subject")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        conditions = [
            "bot_id=?", "session_id=?", "visibility=?",
            "status NOT IN ('deleted', 'superseded')",
        ]
        params: list[Any] = list(_scope_params(scope))
        if subject is not None:
            conditions.append("subject=?")
            params.append(subject)
        rows = self.cm.execute_read(
            f"""SELECT id, subject, predicate, object, confidence, status, source_memory_id,
                       provenance, valid_from, valid_until, created_at, updated_at, revision
                  FROM scoped_facts WHERE {' AND '.join(conditions)}
                 ORDER BY updated_at DESC, id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        return [
            {
                "id": row[0], "subject": row[1], "predicate": row[2], "object": row[3],
                "confidence": row[4], "status": row[5], "source_memory_id": row[6],
                "provenance": json.loads(row[7]), "valid_from": row[8], "valid_until": row[9],
                "created_at": row[10], "updated_at": row[11], "revision": int(row[12]),
            }
            for row in rows
        ]

    def _table_columns(self, table: str) -> set[str]:
        try:
            return {str(row[1]) for row in self.cm.execute_read(f'PRAGMA table_info("{table}")').fetchall()}
        except Exception:
            return set()

    def _upsert_tag_catalog(
        self,
        *,
        name: str,
        tag_type: str,
        description: str,
    ) -> int | None:
        """Return the global semantic catalog id when the additive schema exists."""
        if not self._table_columns("tag_catalog"):
            return None
        normalized_name = normalize_tag_name(name)
        if not normalized_name:
            return None
        now = time.time()
        try:
            self.cm.execute_write(
                """INSERT INTO tag_catalog(
                       normalized_name, display_name, tag_type, description,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                   ON CONFLICT(normalized_name, tag_type) DO UPDATE SET
                       display_name=CASE WHEN tag_catalog.display_name='' THEN excluded.display_name
                                         ELSE tag_catalog.display_name END,
                       description=CASE WHEN tag_catalog.description='' THEN excluded.description
                                        ELSE tag_catalog.description END,
                       updated_at=excluded.updated_at""",
                (normalized_name, str(name), str(tag_type or "keyword"), str(description or ""), now, now),
            )
            self.cm.commit()
            row = self.cm.execute_read(
                "SELECT id FROM tag_catalog WHERE normalized_name=? AND tag_type=?",
                (normalized_name, str(tag_type or "keyword")),
            ).fetchone()
            return int(row[0]) if row is not None else None
        except Exception:
            # Focused tests and older read-only fixtures can still expose scoped_tags
            # without the additive Catalog table.  Their formal scoped writes remain valid.
            return None

    def list_scoped_catalog_links(
        self,
        scope: RuntimeScope,
        catalog_ids: Sequence[int],
        *,
        allow_cross_group_recall: bool = False,
    ) -> list[dict[str, Any]]:
        """Map semantic Catalog hits back to scoped Tag IDs before graph/query use."""
        scope = _require_group_scope(scope)
        ids = _positive_ints(catalog_ids)
        if not ids or "catalog_id" not in self._table_columns("scoped_tags"):
            return []
        placeholders = ",".join("?" for _ in ids)
        status_clause = " AND COALESCE(status, 'active') NOT IN ('inactive', 'deleted', 'archived')" if "status" in self._table_columns("scoped_tags") else ""
        if allow_cross_group_recall:
            # Only the QueryEngine's explicit recall policy may request this
            # broad mapping. A Catalog id is never used as a legacy tag id.
            where = (
                "COALESCE(bot_id, '') != '' AND COALESCE(session_id, '') != '' "
                "AND visibility='group'"
            )
            params: list[Any] = list(ids)
        else:
            where = "bot_id=? AND session_id=? AND visibility=?"
            params = [*_scope_params(scope), *ids]
        rows = self.cm.execute_read(
            f"""SELECT id, catalog_id, name, tag_type, confidence
                   FROM scoped_tags
                  WHERE {where} AND catalog_id IN ({placeholders})
                    {status_clause}
                  ORDER BY id""",
            params,
        ).fetchall()
        return [
            {
                "scoped_tag_id": int(row[0]),
                "catalog_id": int(row[1]),
                "name": str(row[2] or ""),
                "tag_type": str(row[3] or "keyword"),
                "confidence": float(row[4] or 0.0),
            }
            for row in rows
            if row[1] is not None
        ]

    def upsert_scoped_tag(
        self,
        scope: RuntimeScope,
        *,
        name: str,
        tag_type: str = "keyword",
        description: str = "",
        confidence: float = 0.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        scope = _require_group_scope(scope)
        name = _require_exact_string(name, "name")
        if not isinstance(tag_type, str) or not isinstance(description, str):
            raise TypeError("tag_type and description must be strings")
        now = time.time()
        catalog_id = self._upsert_tag_catalog(
            name=name,
            tag_type=tag_type,
            description=description,
        )
        columns = self._table_columns("scoped_tags")
        if catalog_id is not None and "catalog_id" in columns:
            self.cm.execute_write(
                """INSERT INTO scoped_tags (
                        catalog_id, bot_id, session_id, visibility, name, tag_type, description, confidence,
                        metadata, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bot_id, session_id, visibility, name) DO UPDATE SET
                        catalog_id=COALESCE(excluded.catalog_id, scoped_tags.catalog_id),
                        tag_type=excluded.tag_type, description=excluded.description,
                        confidence=excluded.confidence, metadata=excluded.metadata,
                        updated_at=excluded.updated_at""",
                (catalog_id, *_scope_params(scope), name, tag_type, description, float(confidence),
                 _canonical_json(metadata, "metadata"), now, now),
            )
        else:
            # Compatibility path for focused fixtures created before tag_catalog.
            self.cm.execute_write(
                """INSERT INTO scoped_tags (
                        bot_id, session_id, visibility, name, tag_type, description, confidence,
                        metadata, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bot_id, session_id, visibility, name) DO UPDATE SET
                        tag_type=excluded.tag_type, description=excluded.description,
                        confidence=excluded.confidence, metadata=excluded.metadata,
                        updated_at=excluded.updated_at""",
                (*_scope_params(scope), name, tag_type, description, float(confidence),
                 _canonical_json(metadata, "metadata"), now, now),
            )
        self.cm.commit()
        return self._select_id(
            "scoped_tags",
            "bot_id=? AND session_id=? AND visibility=? AND name=?",
            (*_scope_params(scope), name),
        )

    def get_scoped_tag_catalog_id(self, scope: RuntimeScope, tag_id: int) -> int | None:
        scope = _require_group_scope(scope)
        if "catalog_id" not in self._table_columns("scoped_tags"):
            return None
        row = self.cm.execute_read(
            """SELECT catalog_id FROM scoped_tags
                WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (int(tag_id), *_scope_params(scope)),
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def update_tag_catalog_embedding(
        self,
        catalog_id: int,
        vector: Any,
        *,
        embedding_model: str = "",
        embedding_dim: int | None = None,
    ) -> bool:
        """Persist an embedding on the semantic Catalog, never on a legacy tag row."""
        if "id" not in self._table_columns("tag_catalog"):
            return False
        try:
            import numpy as np
            array = np.asarray(vector, dtype=np.float32).reshape(-1)
            if array.size == 0:
                return False
            self.cm.execute_write(
                """UPDATE tag_catalog SET embedding=?, embedding_model=?, embedding_dim=?, updated_at=?
                    WHERE id=? AND status='active'""",
                (array.tobytes(), str(embedding_model or ""), int(embedding_dim or array.size), time.time(), int(catalog_id)),
            )
            self.cm.commit()
            return True
        except Exception:
            return False

    def link_scoped_memory_tag(
        self,
        scope: RuntimeScope,
        *,
        memory_id: int,
        tag_id: int,
        position: int = 0,
        relevance: float = 1.0,
    ) -> None:
        scope = _require_group_scope(scope)
        self._require_scoped_memory(scope, memory_id)
        self._tag_in_scope(scope, tag_id)
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("position must be a non-negative integer")
        self.cm.execute_write(
            """INSERT INTO scoped_memory_tags (
                    bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, session_id, visibility, memory_id, tag_id) DO UPDATE SET
                    position=excluded.position, relevance=excluded.relevance""",
            (*_scope_params(scope), memory_id, tag_id, position, float(relevance), time.time()),
        )
        self.cm.commit()

    def upsert_scoped_tag_relation(
        self,
        scope: RuntimeScope,
        *,
        source_tag_id: int,
        target_tag_id: int,
        relation_type: str,
        weight: float = 1.0,
        confidence: float = 0.0,
        metadata: Mapping[str, Any] | None = None,
        status: str = "active",
        valid_until: float | None = None,
    ) -> int:
        scope = _require_group_scope(scope)
        self._tag_in_scope(scope, source_tag_id)
        self._tag_in_scope(scope, target_tag_id)
        relation_type = _require_exact_string(relation_type, "relation_type")
        if not isinstance(status, str):
            raise TypeError("status must be a string")
        now = time.time()
        self.cm.execute_write(
            """INSERT INTO scoped_tag_relations (
                    bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type,
                    weight, confidence, metadata, status, valid_until, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type)
                DO UPDATE SET weight=excluded.weight, confidence=excluded.confidence,
                    metadata=excluded.metadata, status=excluded.status,
                    valid_until=excluded.valid_until, updated_at=excluded.updated_at,
                    revision=scoped_tag_relations.revision+1
                WHERE scoped_tag_relations.status NOT IN ('deleted', 'superseded')""",
            (*_scope_params(scope), source_tag_id, target_tag_id, relation_type, float(weight),
             float(confidence), _canonical_json(metadata, "metadata"), status, valid_until, now, now),
        )
        self.cm.commit()
        return self._select_id(
            "scoped_tag_relations",
            "bot_id=? AND session_id=? AND visibility=? AND source_tag_id=? AND target_tag_id=? AND relation_type=?",
            (*_scope_params(scope), source_tag_id, target_tag_id, relation_type),
        )

    def get_scoped_tag_vectors_by_ids(
        self,
        scope: RuntimeScope,
        tag_ids: Sequence[int],
    ) -> dict[int, Any]:
        """Load vectors through scoped_tag -> tag_catalog, never legacy ``tags``."""
        scope = _require_group_scope(scope)
        ids = [int(value) for value in tag_ids if not isinstance(value, bool) and int(value) > 0]
        if not ids or "catalog_id" not in self._table_columns("scoped_tags"):
            return {}
        try:
            import numpy as np
            placeholders = ",".join("?" for _ in ids)
            rows = self.cm.execute_read(
                f"""SELECT st.id, tc.embedding, tc.embedding_dim
                       FROM scoped_tags st JOIN tag_catalog tc ON tc.id=st.catalog_id
                      WHERE st.bot_id=? AND st.session_id=? AND st.visibility=?
                        AND st.id IN ({placeholders}) AND tc.status='active'""",
                [*_scope_params(scope), *ids],
            ).fetchall()
            result: dict[int, Any] = {}
            for row in rows:
                raw = row[1]
                if raw is None:
                    continue
                if isinstance(raw, memoryview):
                    raw = raw.tobytes()
                vector = np.frombuffer(raw, dtype=np.float32).reshape(-1) if isinstance(raw, bytes) else np.asarray(raw, dtype=np.float32).reshape(-1)
                if vector.size:
                    result[int(row[0])] = vector
            return result
        except Exception:
            return {}

    def list_scoped_memory_tags(self, scope: RuntimeScope, memory_ids: Sequence[int]) -> list[dict[str, Any]]:
        scope = _require_group_scope(scope)
        ids = list(memory_ids)
        if not ids:
            return []
        if any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
            raise ValueError("memory_ids must contain positive integers")
        marks = ','.join('?' for _ in ids)
        rows = self.cm.execute_read(
            f"""SELECT smt.memory_id, smt.tag_id, st.name, st.tag_type, smt.relevance
                FROM scoped_memory_tags smt JOIN scoped_tags st ON st.id=smt.tag_id
                WHERE smt.bot_id=? AND smt.session_id=? AND smt.visibility=? AND smt.memory_id IN ({marks})
                ORDER BY smt.memory_id, smt.position, smt.tag_id""",
            [*_scope_params(scope), *ids],
        ).fetchall()
        return [{"memory_id": r[0], "tag_id": r[1], "name": r[2], "tag_type": r[3], "relevance": r[4]} for r in rows]

    def list_scoped_cold_memory_candidates(
        self,
        scope: RuntimeScope,
        tag_ids: Sequence[int],
        *,
        limit: int = 128,
        allow_cross_group_recall: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a bounded exact-Scope cold candidate set for semantic reranking.

        The global Tag catalog is intentionally absent from this API.  Callers
        must first map catalog hits to this Scope's tag IDs, and this method then
        uses the effective Tag read model (including manual corrections) before
        loading a small set of canonical vectors.
        """
        scope = _require_group_scope(scope)
        ids = _positive_ints(tag_ids)
        if not ids:
            return []
        if isinstance(limit, bool):
            raise ValueError("limit must be a positive integer")
        try:
            bounded_limit = min(512, max(1, int(limit)))
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be a positive integer") from exc

        # `effective_tag_rows` deliberately falls back to the canonical automatic
        # baseline when the materialized projection is not backfilled yet. Cross
        # group recall is explicit and still admits only complete group tag scopes.
        try:
            # Push the candidate tag filter into SQL; a full scoped-link scan made
            # cold recall cost seconds on large corpora.
            effective = effective_tag_rows(
                self.cm,
                scope=None if allow_cross_group_recall else scope,
                tag_ids=ids,
            )
        except TypeError:
            # Older/compat projection helpers without tag pushdown.
            effective = effective_tag_rows(self.cm, scope=None if allow_cross_group_recall else scope)
        except Exception:
            return []
        wanted = set(ids)
        tag_scores: dict[int, tuple[float, int]] = {}
        for tag in effective:
            try:
                tag_id = int(tag["tag_id"])
                memory_id = int(tag["memory_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if allow_cross_group_recall:
                if (
                    str(tag.get("visibility") or "") != "group"
                    or not str(tag.get("bot_id") or "").strip()
                    or not str(tag.get("session_id") or "").strip()
                ):
                    continue
            if tag_id not in wanted or memory_id <= 0:
                continue
            relevance = float(tag.get("relevance", 1.0) or 0.0)
            prior_score, prior_count = tag_scores.get(memory_id, (0.0, 0))
            tag_scores[memory_id] = (prior_score + max(0.0, relevance), prior_count + 1)
        if not tag_scores:
            return []

        # Limit before the canonical vector fetch so a broad tag can never turn
        # into an unbounded process-memory or SQLite placeholder allocation.
        candidate_ids = [
            memory_id
            for memory_id, _score in sorted(
                tag_scores.items(),
                key=lambda item: (-item[1][0], -item[1][1], item[0]),
            )[: bounded_limit * 4]
        ]
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        memory_columns = self._table_columns("memories")
        required_columns = {"id", "vector", "bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
        if not required_columns <= memory_columns:
            return []
        source_expression = "COALESCE(source, '')" if "source" in memory_columns else "''"
        type_expression = "COALESCE(memory_type, 'message')" if "memory_type" in memory_columns else "'message'"
        importance_expression = "COALESCE(importance, 1.0)" if "importance" in memory_columns else "1.0"
        access_expression = "COALESCE(access_count, 0)" if "access_count" in memory_columns else "0"
        timestamp_expression = "COALESCE(timestamp, 0.0)" if "timestamp" in memory_columns else "0.0"
        sender_id_expression = "COALESCE(sender_id, '')" if "sender_id" in memory_columns else "''"
        sender_name_expression = "COALESCE(sender_name, '')" if "sender_name" in memory_columns else "''"
        group_expression = "COALESCE(group_id, '')" if "group_id" in memory_columns else "''"
        # Read path: do not require formal bot/session/resolution_state.
        # Prefer current group when not expanding cross-group; still allow rows
        # with partial or empty Scope fields as long as they are active.
        if allow_cross_group_recall:
            scope_where = "1=1"
            scope_params: list[Any] = []
        else:
            scope_where = "COALESCE(group_id, '') = ?"
            scope_params = [scope.session.conversation_id]
        origin_expression = (
            "COALESCE(origin_fingerprint, '')" if "origin_fingerprint" in memory_columns else "''"
        )
        provenance_expression = (
            "COALESCE(provenance, '')" if "provenance" in memory_columns else "''"
        )
        rows = self.cm.execute_read(
            f"""SELECT id, vector, content, {timestamp_expression}, {importance_expression},
                       {access_expression}, {source_expression}, {type_expression},
                       {sender_id_expression}, {sender_name_expression}, {group_expression},
                       {origin_expression}, {provenance_expression}
                  FROM memories
                 WHERE id IN ({placeholders})
                   AND {scope_where}
                   AND COALESCE(quarantine, 0)=0
                   AND {source_expression} != 'noise'
                   AND {type_expression} NOT IN ('archived', 'evicted', 'deleted', 'noise')""",
            [*candidate_ids, *scope_params],
        ).fetchall()
        by_id = {int(row[0]): row for row in rows if row[1] is not None}
        result: list[dict[str, Any]] = []
        for memory_id in candidate_ids:
            row = by_id.get(memory_id)
            if row is None:
                continue
            tag_score, tag_count = tag_scores[memory_id]
            provenance: dict[str, Any] = {}
            raw_prov = row[12]
            if isinstance(raw_prov, str) and raw_prov.strip():
                try:
                    loaded = json.loads(raw_prov)
                    if isinstance(loaded, dict):
                        provenance = loaded
                except Exception:
                    provenance = {}
            item = {
                "id": memory_id,
                "vector": row[1],
                "content": str(row[2] or ""),
                "timestamp": row[3],
                "importance": row[4],
                "access_count": row[5],
                "source": str(row[6] or ""),
                "memory_type": str(row[7] or "message"),
                "sender_id": str(row[8] or ""),
                "sender_name": str(row[9] or ""),
                "group_id": str(row[10] or ""),
                "origin_fingerprint": str(row[11] or ""),
                "provenance": provenance,
                "tag_score": tag_score,
                "tag_count": tag_count,
            }
            if str(provenance.get("projection_kind") or "") == "fanout_duplicate":
                item["_fanout_duplicate"] = True
                item["fanout_family_id"] = provenance.get("fanout_family_id")
            result.append(item)
            if len(result) >= bounded_limit:
                break
        return result

    def record_scoped_fact_observation(
        self, scope: RuntimeScope, *, subject: str, predicate: str, object: str,
        confidence: float = 0.0, review_status: str = "pending", status: str | None = None,
        source_memory_id: int | None = None, provenance: Mapping[str, Any] | None = None,
        candidate_snapshot: Mapping[str, Any] | None = None, existing_snapshot: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None, source_tags: Sequence[Any] | None = None,
        query_trace_id: str = "", query_trace: Mapping[str, Any] | None = None,
        valid_from: float | None = None, valid_until: float | None = None,
        idempotency_key: str | None = None, observed_at: float | None = None,
    ) -> int:
        scope = _require_group_scope(scope)
        subject, predicate, object = tuple(_require_exact_string(v, n) for v, n in ((subject, "subject"), (predicate, "predicate"), (object, "object")))
        if review_status not in {"pending", "approved", "rejected"}:
            raise ValueError("invalid review_status")
        self._require_scoped_memory(scope, source_memory_id)
        candidate = {"subject": subject, "predicate": predicate, "object": object, "valid_from": valid_from, "valid_until": valid_until, "provenance": provenance or {}}
        existing_rows = self.list_scoped_facts(scope, subject=subject, limit=500)
        matches = [row for row in existing_rows if row.get("predicate") == predicate] or [None]
        candidate_fact_id = self.upsert_scoped_fact(scope, subject=subject, predicate=predicate, object=object, confidence=confidence, status=status or "pending", source_memory_id=source_memory_id, provenance=provenance, valid_from=valid_from, valid_until=valid_until)
        now = observed_at or time.time()
        first_history_id = None
        classifier = FactConflictClassifier()
        for existing in matches:
            result = classifier.classify(candidate, existing or [])
            existing_id = existing.get("id") if existing else None
            key_base = idempotency_key or f"fact-observation:{source_memory_id or 0}:{subject}\x00{predicate}\x00{object}\x00{valid_from}\x00{valid_until}"
            key = f"{key_base}:existing-{existing_id or 0}"
            self.cm.execute_write(
                """INSERT INTO scoped_fact_history
                (bot_id,session_id,visibility,candidate_fact_id,existing_fact_id,subject,predicate,object,relation,review_status,confidence,candidate_snapshot,existing_snapshot,evidence,source_tags,query_trace_id,source_memory_id,provenance,valid_from,valid_until,supersedes_id,idempotency_key,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bot_id,session_id,visibility,idempotency_key) DO UPDATE SET evidence=excluded.evidence,source_tags=excluded.source_tags,query_trace_id=excluded.query_trace_id,provenance=excluded.provenance""",
                (*_scope_params(scope), candidate_fact_id, existing_id, subject, predicate, object, result.relation, review_status, float(confidence), _canonical_json(candidate_snapshot or candidate, "candidate_snapshot"), _canonical_json(existing or {}, "existing_snapshot"), _canonical_json(evidence, "evidence"), _canonical_contexts(source_tags), str(query_trace_id or (query_trace or {}).get("trace_id") or ""), source_memory_id, _canonical_json(provenance, "provenance"), valid_from, valid_until, result.existing_id if result.relation == "supersedes" else None, key, now),
            )
            self.cm.commit()
            history_id = self._select_id("scoped_fact_history", "bot_id=? AND session_id=? AND visibility=? AND idempotency_key=?", (*_scope_params(scope), key))
            first_history_id = first_history_id or history_id
        return int(first_history_id or candidate_fact_id)

    def list_scoped_fact_history(self, scope: RuntimeScope, *, subject: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        scope = _require_group_scope(scope); params: list[Any] = list(_scope_params(scope)); where = "bot_id=? AND session_id=? AND visibility=?"
        if subject is not None: where += " AND subject=?"; params.append(_require_exact_string(subject,"subject"))
        rows = self.cm.execute_read(f"SELECT id,subject,predicate,object,relation,review_status,confidence,candidate_snapshot,existing_snapshot,evidence,source_tags,query_trace_id,source_memory_id,provenance,valid_from,valid_until,supersedes_id,idempotency_key,observed_at,reviewed_at FROM scoped_fact_history WHERE {where} ORDER BY observed_at DESC,id DESC LIMIT ?", [*params,limit]).fetchall()
        return [{"id":r[0],"subject":r[1],"predicate":r[2],"object":r[3],"relation":r[4],"review_status":r[5],"confidence":r[6],"candidate_snapshot":json.loads(r[7]),"existing_snapshot":json.loads(r[8]),"evidence":json.loads(r[9]),"source_tags":json.loads(r[10]),"query_trace_id":r[11],"source_memory_id":r[12],"provenance":json.loads(r[13]),"valid_from":r[14],"valid_until":r[15],"supersedes_id":r[16],"idempotency_key":r[17],"observed_at":r[18],"reviewed_at":r[19]} for r in rows]

    def review_scoped_fact_history(self, scope: RuntimeScope, observation_id: int, *, review_status: str, query_trace_id: str = "") -> None:
        scope = _require_group_scope(scope)
        if review_status not in {"pending", "approved", "rejected"}:
            raise ValueError("invalid review_status")
        row = self.cm.execute_read(
            "SELECT candidate_fact_id, existing_fact_id, relation, review_status FROM scoped_fact_history WHERE id=? AND bot_id=? AND session_id=? AND visibility=?",
            (observation_id, *_scope_params(scope)),
        ).fetchone()
        if row is None:
            raise LookupError("scoped_fact_history_not_found")
        if row[3] != "pending" and review_status != "pending":
            raise ValueError("invalid_fact_review_transition")
        candidate_id, existing_id, relation = row[0], row[1], row[2]
        if review_status == "approved" and candidate_id is not None:
            candidate_status = "conflict" if relation == "conflicts" else "active"
            self.cm.execute_write("UPDATE scoped_facts SET status=?, updated_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=?", (candidate_status, time.time(), candidate_id, *_scope_params(scope)))
            if relation == "supersedes" and existing_id is not None:
                self.cm.execute_write("UPDATE scoped_facts SET status='superseded', updated_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=?", (time.time(), existing_id, *_scope_params(scope)))
        elif review_status == "rejected" and candidate_id is not None:
            self.cm.execute_write("UPDATE scoped_facts SET status='rejected', updated_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=?", (time.time(), candidate_id, *_scope_params(scope)))
        self.cm.execute_write("UPDATE scoped_fact_history SET review_status=?, query_trace_id=COALESCE(NULLIF(?, ''), query_trace_id), reviewed_at=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=?", (review_status, str(query_trace_id or ""), time.time(), observation_id, *_scope_params(scope)))
        self.cm.commit()

    def review_scoped_fact_observation(self, scope: RuntimeScope, observation_id: int, *, review_status: str = "pending", status: str | None = None) -> None:
        self.review_scoped_fact_history(scope, observation_id, review_status=review_status if status is None else status)

    def transition_scoped_fact_observation(self, scope: RuntimeScope, observation_id: int, *, relation: str, review_status: str = "pending", status: str | None = None) -> None:
        scope = _require_group_scope(scope)
        if relation not in {"compatible","scoped","conflicts","supersedes"}: raise ValueError("invalid fact relation")
        self.cm.execute_write("UPDATE scoped_fact_history SET relation=?,review_status=? WHERE id=? AND bot_id=? AND session_id=? AND visibility=?",(relation,review_status,observation_id,*_scope_params(scope))); self.cm.commit()

    def upsert_scoped_belief(
        self,
        scope: RuntimeScope,
        *,
        belief_key: str,
        content: str,
        belief_type: str = "world_view",
        strength: float = 0.0,
        status: str = "pending",
        source_memory_id: int | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> int:
        scope = _require_group_scope(scope)
        belief_key = _require_exact_string(belief_key, "belief_key")
        content = _require_exact_string(content, "content")
        if not isinstance(belief_type, str) or not isinstance(status, str):
            raise TypeError("belief_type and status must be strings")
        self._require_scoped_memory(scope, source_memory_id)
        now = time.time()
        self.cm.execute_write(
            """INSERT INTO scoped_beliefs (
                    bot_id, session_id, visibility, belief_key, content, belief_type, strength,
                    status, source_memory_id, provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, session_id, visibility, belief_key) DO UPDATE SET
                    content=excluded.content, belief_type=excluded.belief_type,
                    strength=excluded.strength, status=excluded.status,
                    source_memory_id=excluded.source_memory_id, provenance=excluded.provenance,
                    updated_at=excluded.updated_at""",
            (*_scope_params(scope), belief_key, content, belief_type, float(strength), status,
             source_memory_id, _canonical_json(provenance, "provenance"), now, now),
        )
        self.cm.commit()
        return self._select_id(
            "scoped_beliefs",
            "bot_id=? AND session_id=? AND visibility=? AND belief_key=?",
            (*_scope_params(scope), belief_key),
        )

    def list_scoped_beliefs(self, scope: RuntimeScope, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        scope = _require_group_scope(scope)
        if status is not None and not isinstance(status, str):
            raise TypeError("status must be a string when provided")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        conditions = ["bot_id=?", "session_id=?", "visibility=?"]
        params: list[Any] = list(_scope_params(scope))
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        rows = self.cm.execute_read(
            f"""SELECT id, belief_key, content, belief_type, strength, status, source_memory_id,
                       provenance, created_at, updated_at
                  FROM scoped_beliefs WHERE {' AND '.join(conditions)}
                 ORDER BY updated_at DESC, id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        return [
            {
                "id": row[0], "belief_key": row[1], "content": row[2], "belief_type": row[3],
                "strength": row[4], "status": row[5], "source_memory_id": row[6],
                "provenance": json.loads(row[7]), "created_at": row[8], "updated_at": row[9],
            }
            for row in rows
        ]

    def record_scoped_belief_observation(
        self,
        scope: RuntimeScope,
        *,
        belief_id: int,
        window_key: str,
        polarity: str,
        memory_ids: Sequence[int],
        participants: Sequence[str] | None = None,
        source_tags: Sequence[Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        window_started_at: float | None = None,
        window_ended_at: float | None = None,
        observed_at: float | None = None,
    ) -> int:
        """记录一段可去重的 scoped Belief 经历观察。

        同一个 belief、consolidation window 和 polarity 的重试会覆写同一行，
        因此不会因任务重试或 LLM 重放重复增加支持度。
        """
        scope = _require_group_scope(scope)
        if isinstance(belief_id, bool):
            raise ValueError("belief_id must be a positive integer")
        try:
            belief_id = int(belief_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("belief_id must be a positive integer") from exc
        if belief_id <= 0:
            raise ValueError("belief_id must be a positive integer")
        window_key = _require_exact_string(window_key, "window_key")
        polarity = _require_exact_string(polarity, "polarity").lower()
        if polarity not in {"support", "challenge"}:
            raise ValueError("polarity must be support or challenge")
        normalized_ids = list(dict.fromkeys(_positive_ints(memory_ids)))
        if not normalized_ids:
            raise ValueError("memory_ids must contain at least one positive integer")
        for memory_id in normalized_ids:
            self._require_scoped_memory(scope, memory_id)
        belief_row = self.cm.execute_read(
            "SELECT id FROM scoped_beliefs WHERE id=? AND bot_id=? AND session_id=? AND visibility=?",
            (belief_id, *_scope_params(scope)),
        ).fetchone()
        if belief_row is None:
            raise ScopedKnowledgeScopeError("belief_scope_mismatch", "belief does not belong to the RuntimeScope")
        normalized_participants = [
            str(value or "").strip()
            for value in (participants or [])
            if str(value or "").strip()
        ]
        normalized_participants = list(dict.fromkeys(normalized_participants))
        now = float(observed_at if observed_at is not None else time.time())
        try:
            started = float(window_started_at) if window_started_at is not None else now
            ended = float(window_ended_at) if window_ended_at is not None else now
        except (TypeError, ValueError) as exc:
            raise ValueError("window timestamps must be numeric when provided") from exc
        if ended < started:
            started, ended = ended, started
        self.cm.execute_write(
            """INSERT INTO scoped_belief_observations (
                    bot_id, session_id, visibility, belief_id, window_key, polarity,
                    memory_ids, participants, source_tags, metadata, window_started_at,
                    window_ended_at, observed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, session_id, visibility, belief_id, window_key, polarity)
                DO UPDATE SET memory_ids=excluded.memory_ids, participants=excluded.participants,
                    source_tags=excluded.source_tags, metadata=excluded.metadata,
                    window_started_at=excluded.window_started_at, window_ended_at=excluded.window_ended_at,
                    observed_at=excluded.observed_at, updated_at=excluded.updated_at""",
            (
                *_scope_params(scope), belief_id, window_key, polarity,
                _canonical_contexts(normalized_ids), _canonical_contexts(normalized_participants),
                _canonical_contexts(source_tags), _canonical_json(metadata, "metadata"),
                started, ended, now, now, now,
            ),
        )
        self.cm.commit()
        return self._select_id(
            "scoped_belief_observations",
            "bot_id=? AND session_id=? AND visibility=? AND belief_id=? AND window_key=? AND polarity=?",
            (*_scope_params(scope), belief_id, window_key, polarity),
        )

    def list_scoped_belief_ids_citing_memory(
        self,
        scope: RuntimeScope,
        memory_id: int,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[int]:
        """Return scoped beliefs whose stored evidence JSON cites this memory.

        ``memory_ids`` is a compact JSON array of integers. SQLite has no JSON1
        guarantee here, so the lookup uses exact integer tokens bounded by JSON
        separators. Callers still re-validate membership after load.
        """
        scope = _require_group_scope(scope)
        if isinstance(memory_id, bool):
            raise ValueError("memory_id must be a positive integer")
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("memory_id must be a positive integer") from exc
        if memory_id <= 0:
            raise ValueError("memory_id must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        allowed = ("pending", "quarantined") if statuses is None else tuple(statuses)
        if not allowed or any(not isinstance(status, str) or not status for status in allowed):
            raise ValueError("statuses must be non-empty strings")
        status_marks = ",".join("?" for _ in allowed)
        token = str(memory_id)
        rows = self.cm.execute_read(
            f"""SELECT DISTINCT o.belief_id, o.memory_ids
                  FROM scoped_belief_observations o
                  JOIN scoped_beliefs b
                    ON b.id=o.belief_id
                   AND b.bot_id=o.bot_id
                   AND b.session_id=o.session_id
                   AND b.visibility=o.visibility
                 WHERE o.bot_id=? AND o.session_id=? AND o.visibility=?
                   AND b.status IN ({status_marks})
                   AND instr(o.memory_ids, ?) > 0
                 ORDER BY o.belief_id ASC
                 LIMIT ?""",
            (*_scope_params(scope), *allowed, token, max(limit * 8, 64)),
        ).fetchall()
        seen: list[int] = []
        for row in rows:
            if row is None or row[0] is None:
                continue
            try:
                cited = json.loads(row[1] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(cited, list):
                continue
            cited_ids: list[int] = []
            for item in cited:
                try:
                    cited_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            if memory_id not in cited_ids:
                continue
            belief_id = int(row[0])
            if belief_id not in seen:
                seen.append(belief_id)
            if len(seen) >= limit:
                break
        return seen

    def list_scoped_belief_observations(
        self, scope: RuntimeScope, *, belief_id: int | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """列出某 Scope 下可审计的 Belief 支持/反证经历。"""
        scope = _require_group_scope(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        conditions = ["bot_id=?", "session_id=?", "visibility=?"]
        params: list[Any] = list(_scope_params(scope))
        if belief_id is not None:
            if isinstance(belief_id, bool):
                raise ValueError("belief_id must be a positive integer")
            try:
                belief_id = int(belief_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("belief_id must be a positive integer") from exc
            if belief_id <= 0:
                raise ValueError("belief_id must be a positive integer")
            conditions.append("belief_id=?")
            params.append(belief_id)
        rows = self.cm.execute_read(
            f"""SELECT id, belief_id, window_key, polarity, memory_ids, participants,
                       source_tags, metadata, window_started_at, window_ended_at,
                       observed_at, created_at, updated_at
                  FROM scoped_belief_observations WHERE {' AND '.join(conditions)}
                 ORDER BY observed_at ASC, id ASC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                memory_ids = json.loads(row[4] or "[]")
                participants = json.loads(row[5] or "[]")
                source_tags = json.loads(row[6] or "[]")
                metadata = json.loads(row[7] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            result.append({
                "id": row[0], "belief_id": row[1], "window_key": row[2], "polarity": row[3],
                "memory_ids": memory_ids if isinstance(memory_ids, list) else [],
                "participants": participants if isinstance(participants, list) else [],
                "source_tags": source_tags if isinstance(source_tags, list) else [],
                "metadata": metadata if isinstance(metadata, dict) else {},
                "window_started_at": row[8], "window_ended_at": row[9],
                "observed_at": row[10], "created_at": row[11], "updated_at": row[12],
            })
        return result

    def get_scoped_belief(self, scope: RuntimeScope, belief_id: int) -> dict[str, Any] | None:
        """Read one formal belief strictly inside the supplied group Scope."""
        scope = _require_group_scope(scope)
        if isinstance(belief_id, bool):
            raise ValueError("belief_id must be a positive integer")
        try:
            belief_id = int(belief_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("belief_id must be a positive integer") from exc
        if belief_id <= 0:
            raise ValueError("belief_id must be a positive integer")
        row = self.cm.execute_read(
            """SELECT id, belief_key, content, belief_type, strength, status, source_memory_id,
                      provenance, created_at, updated_at
                 FROM scoped_beliefs
                WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
            (belief_id, *_scope_params(scope)),
        ).fetchone()
        if row is None:
            return None
        try:
            provenance = json.loads(row[7] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            provenance = {}
        return {
            "id": row[0], "belief_key": row[1], "content": row[2], "belief_type": row[3],
            "strength": row[4], "status": row[5], "source_memory_id": row[6],
            "provenance": provenance if isinstance(provenance, dict) else {},
            "created_at": row[8], "updated_at": row[9],
        }

    @staticmethod
    def _belief_observation_from_row(row) -> dict[str, Any] | None:
        try:
            memory_ids = json.loads(row[4] or "[]")
            participants = json.loads(row[5] or "[]")
            source_tags = json.loads(row[6] or "[]")
            metadata = json.loads(row[7] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return {
            "id": row[0], "belief_id": row[1], "window_key": row[2], "polarity": row[3],
            "memory_ids": memory_ids if isinstance(memory_ids, list) else [],
            "participants": participants if isinstance(participants, list) else [],
            "source_tags": source_tags if isinstance(source_tags, list) else [],
            "metadata": metadata if isinstance(metadata, dict) else {},
            "window_started_at": row[8], "window_ended_at": row[9],
            "observed_at": row[10], "created_at": row[11], "updated_at": row[12],
        }

    def resolve_scoped_belief_candidate(
        self,
        scope: RuntimeScope,
        candidate_id: int,
        *,
        resolution: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Archive a gated candidate while retaining a bounded audit resolution."""
        scope = _require_group_scope(scope)
        if resolution not in {"rejected", "merged", "promoted"}:
            raise ValueError("invalid_candidate_resolution")
        candidate = self.get_scoped_belief(scope, candidate_id)
        if candidate is None:
            raise LookupError("scoped_object_not_found")
        provenance = dict(candidate.get("provenance") or {})
        candidate_meta = dict(provenance.get("candidate") or {})
        candidate_meta.update({"resolution": resolution, "resolution_reason": str(reason_code or "")[:120]})
        provenance["candidate"] = candidate_meta
        self.upsert_scoped_belief(
            scope,
            belief_key=candidate["belief_key"],
            content=candidate["content"],
            belief_type=candidate["belief_type"],
            strength=float(candidate.get("strength") or 0.0),
            status="archived",
            source_memory_id=candidate.get("source_memory_id"),
            provenance=provenance,
        )
        return {"id": int(candidate_id), "status": "archived", "resolution": resolution, "reason_code": reason_code}

    def merge_scoped_belief_candidate(
        self,
        scope: RuntimeScope,
        candidate_id: int,
        *,
        expected_target_revision: int | None = None,
    ) -> dict[str, Any]:
        """Merge an approved reinforce/challenge candidate atomically into its target."""
        scope = _require_group_scope(scope)
        if isinstance(candidate_id, bool):
            raise ValueError("belief_id must be a positive integer")
        candidate_id = int(candidate_id)
        if candidate_id <= 0:
            raise ValueError("belief_id must be a positive integer")

        with self.cm.write_transaction() as tx:
            candidate_row = tx.execute(
                """SELECT id, belief_key, content, belief_type, strength, status, source_memory_id,
                          provenance, created_at, updated_at
                     FROM scoped_beliefs
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
                (candidate_id, *_scope_params(scope)),
            ).fetchone()
            if candidate_row is None:
                raise LookupError("scoped_object_not_found")
            try:
                candidate_provenance = json.loads(candidate_row[7] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate_provenance = {}
            candidate_provenance = candidate_provenance if isinstance(candidate_provenance, dict) else {}
            candidate_meta = candidate_provenance.get("candidate")
            if not isinstance(candidate_meta, Mapping):
                raise ScopedKnowledgeScopeError("candidate_metadata_missing")
            relation = str(candidate_meta.get("relation") or "").strip().lower()
            if relation not in {"reinforce", "challenge"}:
                raise ScopedKnowledgeScopeError("candidate_relation_unsupported")
            if str(candidate_row[5]) not in {"pending", "quarantined"}:
                raise ScopedKnowledgeScopeError("invalid_candidate_transition")
            try:
                target_id = int(candidate_meta.get("target_belief_id"))
            except (TypeError, ValueError):
                raise ScopedKnowledgeScopeError("candidate_target_unavailable") from None
            target_row = tx.execute(
                """SELECT id, belief_key, content, belief_type, strength, status, source_memory_id,
                          provenance, created_at, updated_at
                     FROM scoped_beliefs
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
                (target_id, *_scope_params(scope)),
            ).fetchone()
            if target_row is None or str(target_row[5]) == "archived":
                raise ScopedKnowledgeScopeError("candidate_target_unavailable")
            target = {
                "id": target_row[0], "belief_key": target_row[1], "content": target_row[2],
                "belief_type": target_row[3], "strength": target_row[4], "status": target_row[5],
                "source_memory_id": target_row[6],
            }
            try:
                target_provenance = json.loads(target_row[7] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                target_provenance = {}
            target_provenance = target_provenance if isinstance(target_provenance, dict) else {}
            current_target_revision = _belief_revision({"created_at": target_row[8], "updated_at": target_row[9]})
            captured_revision = expected_target_revision
            if captured_revision is None:
                captured_revision = candidate_meta.get("target_revision_at_capture")
            if captured_revision is not None and int(captured_revision) != current_target_revision:
                raise ScopedKnowledgeScopeError("belief_candidate_target_revision_conflict")

            candidate_observations = tx.execute(
                """SELECT id, belief_id, window_key, polarity, memory_ids, participants,
                          source_tags, metadata, window_started_at, window_ended_at,
                          observed_at, created_at, updated_at
                     FROM scoped_belief_observations
                    WHERE bot_id=? AND session_id=? AND visibility=? AND belief_id=?
                    ORDER BY observed_at ASC, id ASC""",
                (*_scope_params(scope), candidate_id),
            ).fetchall()
            for row in candidate_observations:
                copied_polarity = "challenge" if relation == "challenge" else str(row[3])
                tx.execute(
                    """INSERT INTO scoped_belief_observations(
                           bot_id, session_id, visibility, belief_id, window_key, polarity,
                           memory_ids, participants, source_tags, metadata, window_started_at,
                           window_ended_at, observed_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bot_id, session_id, visibility, belief_id, window_key, polarity)
                       DO NOTHING""",
                    (*_scope_params(scope), target_id, row[2], copied_polarity, *row[4:]),
                )
            target_observation_rows = tx.execute(
                """SELECT id, belief_id, window_key, polarity, memory_ids, participants,
                          source_tags, metadata, window_started_at, window_ended_at,
                          observed_at, created_at, updated_at
                     FROM scoped_belief_observations
                    WHERE bot_id=? AND session_id=? AND visibility=? AND belief_id=?
                    ORDER BY observed_at ASC, id ASC""",
                (*_scope_params(scope), target_id),
            ).fetchall()
            observations = [
                item for item in (self._belief_observation_from_row(row) for row in target_observation_rows)
                if item is not None
            ]
            evaluation = calculate_confidence(observations)
            evidence_ids: list[int] = []
            support_ids: list[int] = []
            challenge_ids: list[int] = []
            source_tags: list[Any] = []
            source_tag_keys: set[str] = set()
            tagged_ids: set[int] = set()
            for observation in observations:
                destination = support_ids if observation.get("polarity") == "support" else challenge_ids
                for raw_id in observation.get("memory_ids") or []:
                    try:
                        memory_id = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    if memory_id > 0 and memory_id not in evidence_ids:
                        evidence_ids.append(memory_id)
                    if memory_id > 0 and memory_id not in destination:
                        destination.append(memory_id)
                for tag in observation.get("source_tags") or []:
                    if not isinstance(tag, Mapping):
                        continue
                    try:
                        memory_id = int(tag.get("memory_id"))
                    except (TypeError, ValueError):
                        memory_id = 0
                    if memory_id > 0:
                        tagged_ids.add(memory_id)
                    marker = json.dumps(dict(tag), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if marker not in source_tag_keys:
                        source_tag_keys.add(marker)
                        source_tags.append(dict(tag))
            tag_chain_status = "complete" if evidence_ids and set(evidence_ids) <= tagged_ids else "empty"
            merged_provenance = dict(target_provenance)
            merged_provenance.update({
                "producer": "consolidation",
                "confidence_policy_version": evaluation["policy_version"],
                "confidence_components": evaluation["components"],
                "confidence_evidence": evaluation["summary"],
                "activation_eligible": bool(evaluation["activation_eligible"] and tag_chain_status == "complete"),
                "source_memory_ids": support_ids,
                "source_tags": source_tags,
                "evidence": {
                    "memory_ids": evidence_ids,
                    "support_memory_ids": support_ids,
                    "challenge_memory_ids": challenge_ids,
                    "observation_ids": [item.get("id") for item in observations],
                    "window_keys": [item.get("window_key") for item in observations],
                },
                "tag_chain_status": tag_chain_status,
                "last_candidate_merge": {
                    "candidate_id": candidate_id,
                    "relation": relation,
                    "candidate_revision": _belief_revision({"created_at": candidate_row[8], "updated_at": candidate_row[9]}),
                },
            })
            target_source_memory_id = support_ids[0] if support_ids else target.get("source_memory_id")
            now = time.time()
            target_status = str(target.get("status") or "pending")
            if bool(merged_provenance["activation_eligible"]):
                target_status = "active"
            tx.execute(
                """UPDATE scoped_beliefs
                      SET strength=?, status=?, source_memory_id=?, provenance=?, updated_at=?
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
                (float(evaluation["components"]["confidence"]), target_status, target_source_memory_id,
                 _canonical_json(merged_provenance, "provenance"), now,
                 target_id, *_scope_params(scope)),
            )
            candidate_meta = dict(candidate_meta)
            candidate_meta.update({
                "resolution": "merged",
                "resolution_reason": "candidate_merged",
                "merged_target_revision": _belief_revision({"updated_at": now}),
            })
            candidate_provenance["candidate"] = candidate_meta
            tx.execute(
                """UPDATE scoped_beliefs SET status='archived', provenance=?, updated_at=?
                    WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
                (_canonical_json(candidate_provenance, "provenance"), now,
                 candidate_id, *_scope_params(scope)),
            )
            return {
                "id": candidate_id,
                "status": "archived",
                "resolution": "merged",
                "target_id": target_id,
                "target_status": target_status,
                "target_confidence": float(evaluation["components"]["confidence"]),
                "target_activation_eligible": bool(merged_provenance["activation_eligible"]),
            }

    def get_scoped_consolidation_cursor(self, scope: RuntimeScope, *, cursor_name: str) -> str | None:
        scope = _require_group_scope(scope)
        cursor_name = _require_exact_string(cursor_name, "cursor_name")
        row = self.cm.execute_read(
            """SELECT cursor_value FROM scoped_consolidation_cursors
                 WHERE bot_id=? AND session_id=? AND visibility=? AND cursor_name=?""",
            (*_scope_params(scope), cursor_name),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def advance_scoped_consolidation_cursor(
        self,
        scope: RuntimeScope,
        *,
        cursor_name: str,
        cursor_value: str,
    ) -> None:
        scope = _require_group_scope(scope)
        cursor_name = _require_exact_string(cursor_name, "cursor_name")
        cursor_value = _require_exact_string(cursor_value, "cursor_value")
        self.cm.execute_write(
            """INSERT INTO scoped_consolidation_cursors (
                    bot_id, session_id, visibility, cursor_name, cursor_value, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, session_id, visibility, cursor_name) DO UPDATE SET
                    cursor_value=excluded.cursor_value, updated_at=excluded.updated_at""",
            (*_scope_params(scope), cursor_name, cursor_value, time.time()),
        )
        self.cm.commit()


__all__ = ["ScopedKnowledgeRepo", "ScopedKnowledgeScopeError"]
