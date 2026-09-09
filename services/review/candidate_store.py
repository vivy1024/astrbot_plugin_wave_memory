"""Agent-submitted review candidates.

候选是待人工裁决的观察，**不是**正式认知：批准只记录人工意见，不会自动写入
事实/信念/黑话等高风险对象。每条候选都必须绑定 RuntimeScope，禁止跨 Scope
读取或裁决；缺少 Scope 的历史行只作为 legacy 存在，不泄漏进任何具体 Scope 查询。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

VALID_REVIEW_CANDIDATE_TYPES = frozenset({"memory", "fact", "belief", "style", "jargon"})
VALID_REVIEW_STATUSES = frozenset({"pending", "approved", "rejected", "ignored"})

# 旧库升级用：这些列在早期 schema 中不存在，必须幂等补齐，且允许为 NULL
# （NULL 即 legacy 行，只能被显式 include_legacy 查询看见）。
_ADDED_COLUMNS = (
    ("bot_id", "TEXT"),
    ("session_id", "TEXT"),
    ("visibility", "TEXT"),
    ("source_kind", "TEXT"),
    ("source_id", "TEXT"),
    ("idempotency_key", "TEXT"),
    ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ("scope_json", "TEXT"),
    ("updated_at", "REAL"),
)

_SELECT_COLUMNS = """id, candidate_type, content, evidence_json, reason, review_status,
                          promoted, actor, created_at, metadata_json, bot_id, session_id,
                          visibility, source_kind, source_id, idempotency_key, revision,
                          scope_json, updated_at"""


class ReviewCandidateStore:
    """SQLite-backed pending review queue, Scope-bound and idempotent."""

    def __init__(self, conn):
        self.conn = conn

    # ---- schema ----------------------------------------------------------

    def ensure_schema(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS review_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_type TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'pending',
                promoted INTEGER NOT NULL DEFAULT 0,
                actor TEXT DEFAULT 'agent',
                created_at REAL NOT NULL,
                metadata_json TEXT,
                bot_id TEXT,
                session_id TEXT,
                visibility TEXT,
                source_kind TEXT,
                source_id TEXT,
                idempotency_key TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                scope_json TEXT,
                updated_at REAL
            )"""
        )
        existing = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(review_candidates)").fetchall()
        }
        for name, definition in _ADDED_COLUMNS:
            if name not in existing:
                self.conn.execute(
                    f"ALTER TABLE review_candidates ADD COLUMN {name} {definition}"
                )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_candidates_status ON review_candidates(review_status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_candidates_type ON review_candidates(candidate_type)"
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_review_candidates_scope
               ON review_candidates(bot_id, session_id, visibility, review_status)"""
        )
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_review_candidates_idempotency
               ON review_candidates(bot_id, session_id, visibility, idempotency_key)
               WHERE idempotency_key IS NOT NULL"""
        )
        self.conn.commit()

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _scope_tuple(scope) -> tuple[str | None, str | None, str | None]:
        if scope is None:
            return (None, None, None)
        session = getattr(scope, "session", None)
        if session is None or not str(getattr(session, "id", "") or ""):
            raise ValueError("candidate_scope_requires_session")
        return (str(scope.bot_id), str(session.id), str(getattr(scope, "visibility", "") or ""))

    @classmethod
    def derive_idempotency_key(
        cls, *, candidate_type: str, content: str, evidence: list[str], scope=None
    ) -> str:
        shape = {
            "scope": list(cls._scope_tuple(scope)),
            "candidate_type": str(candidate_type or "").strip().lower(),
            "content": str(content or "").strip(),
            "evidence": sorted(str(item) for item in evidence),
        }
        digest = hashlib.sha256(
            json.dumps(shape, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"review:{shape['candidate_type']}:{digest[:32]}"

    # ---- write -----------------------------------------------------------

    def create(
        self,
        *,
        candidate_type: str,
        content: str,
        evidence: list[str],
        reason: str,
        actor: str = "agent",
        metadata: dict[str, Any] | None = None,
        now: float | None = None,
        scope=None,
        source_kind: str = "",
        source_id: str = "",
        idempotency_key: str = "",
    ) -> int:
        """提交候选；同一 Scope 下幂等键命中时返回原 id，不新增行。"""
        candidate_type = str(candidate_type or "").strip().lower()
        if candidate_type not in VALID_REVIEW_CANDIDATE_TYPES:
            raise ValueError(f"invalid review candidate type: {candidate_type}")
        if not str(content or "").strip():
            raise ValueError("content is required")
        if not evidence:
            raise ValueError("evidence is required")
        if not str(reason or "").strip():
            raise ValueError("reason is required")
        self.ensure_schema()

        bot_id, session_id, visibility = self._scope_tuple(scope)
        timestamp = float(now if now is not None else time.time())
        key = str(idempotency_key or "").strip()
        if scope is not None and not key:
            key = self.derive_idempotency_key(
                candidate_type=candidate_type, content=content, evidence=evidence, scope=scope
            )
        if key:
            existing = self.conn.execute(
                """SELECT id FROM review_candidates
                    WHERE bot_id IS ? AND session_id IS ? AND visibility IS ?
                      AND idempotency_key = ?""",
                (bot_id, session_id, visibility, key),
            ).fetchone()
            if existing:
                return int(existing[0])

        payload = dict(metadata or {})
        if scope is not None and "source_runtime_scope" not in payload:
            # 冗余保留可读 scope 供旧客户端展示；隔离判定以真实列为准。
            payload["source_runtime_scope"] = {
                "bot_id": bot_id,
                "session_id": session_id,
                "visibility": visibility,
            }
        scope_json = (
            json.dumps(
                {"bot_id": bot_id, "session_id": session_id, "visibility": visibility},
                ensure_ascii=False,
                sort_keys=True,
            )
            if scope is not None
            else None
        )
        try:
            cur = self.conn.execute(
                """INSERT INTO review_candidates
                   (candidate_type, content, evidence_json, reason, review_status, promoted,
                    actor, created_at, metadata_json, bot_id, session_id, visibility,
                    source_kind, source_id, idempotency_key, revision, scope_json, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    candidate_type,
                    str(content or ""),
                    json.dumps(list(evidence), ensure_ascii=False),
                    str(reason or ""),
                    str(actor or "agent"),
                    timestamp,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    bot_id,
                    session_id,
                    visibility,
                    str(source_kind or "").strip() or candidate_type,
                    str(source_id or "").strip() or None,
                    key or None,
                    scope_json,
                    timestamp,
                ),
            )
            self.conn.commit()
        except Exception:
            # 并发下唯一索引可能抢先落库：回查返回既有候选，保持幂等语义。
            self.conn.rollback()
            if key:
                existing = self.conn.execute(
                    """SELECT id FROM review_candidates
                        WHERE bot_id IS ? AND session_id IS ? AND visibility IS ?
                          AND idempotency_key = ?""",
                    (bot_id, session_id, visibility, key),
                ).fetchone()
                if existing:
                    return int(existing[0])
            raise
        return int(cur.lastrowid)

    def update_review_status(
        self, candidate_id: int, status: str, *, promoted: bool = False, scope=None, reviewer: str = ""
    ) -> dict[str, Any] | None:
        status = str(status or "").strip().lower()
        if status not in VALID_REVIEW_STATUSES:
            raise ValueError(f"invalid review status: {status}")
        self.ensure_schema()
        current = self.get(candidate_id, scope=scope)
        if current is None:
            return None
        now = float(time.time())
        note = str(reviewer or "").strip()
        metadata = dict(current.get("metadata") or {})
        if note:
            metadata["last_reviewer"] = note
            metadata["reviewed_at"] = now
        self.conn.execute(
            """UPDATE review_candidates
                  SET review_status = ?, promoted = ?, revision = revision + 1,
                      updated_at = ?, metadata_json = ?
                WHERE id = ?""",
            (status, 1 if promoted else 0, now, json.dumps(metadata, ensure_ascii=False, sort_keys=True), int(candidate_id)),
        )
        self.conn.commit()
        return self.get(candidate_id, scope=scope)

    # ---- read ------------------------------------------------------------

    @staticmethod
    def _scope_where(scope, *, include_legacy: bool) -> tuple[str, list[Any]]:
        """Scope 过滤：给了 scope 就严格等值；未给 scope 时默认排除 legacy 行。"""
        if scope is not None:
            bot_id, session_id, visibility = ReviewCandidateStore._scope_tuple(scope)
            return " AND bot_id=? AND session_id=? AND visibility=?", [bot_id, session_id, visibility]
        if include_legacy:
            return "", []
        return " AND bot_id IS NOT NULL", []

    def list_pending(
        self, *, limit: int = 50, scope=None, include_legacy: bool = True
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        where, params = self._scope_where(scope, include_legacy=include_legacy)
        rows = self.conn.execute(
            f"""SELECT {_SELECT_COLUMNS}
                  FROM review_candidates
                 WHERE review_status = 'pending'{where}
                 ORDER BY id ASC LIMIT ?""",
            (*params, int(limit)),
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_all(
        self, *, limit: int = 100, status: str | None = None, scope=None, include_legacy: bool = True
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        where, params = self._scope_where(scope, include_legacy=include_legacy)
        if status:
            rows = self.conn.execute(
                f"""SELECT {_SELECT_COLUMNS}
                      FROM review_candidates
                     WHERE review_status = ?{where}
                     ORDER BY id DESC LIMIT ?""",
                (str(status), *params, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""SELECT {_SELECT_COLUMNS}
                      FROM review_candidates
                     WHERE 1=1{where}
                     ORDER BY id DESC LIMIT ?""",
                (*params, int(limit)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, candidate_id: int, *, scope=None) -> dict[str, Any] | None:
        """按 id 读取；带 scope 时跨 Scope 命中视为不存在（不泄漏存在性）。"""
        self.ensure_schema()
        if scope is None:
            row = self.conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM review_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
        else:
            bot_id, session_id, visibility = self._scope_tuple(scope)
            row = self.conn.execute(
                f"""SELECT {_SELECT_COLUMNS} FROM review_candidates
                     WHERE id = ? AND bot_id=? AND session_id=? AND visibility=?""",
                (int(candidate_id), bot_id, session_id, visibility),
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> dict[str, Any]:
        try:
            evidence = json.loads(row[3] or "[]")
        except Exception:
            evidence = []
        try:
            metadata = json.loads(row[9] or "{}")
        except Exception:
            metadata = {}
        bot_id, session_id, visibility = row[10], row[11], row[12]
        legacy = bot_id is None and session_id is None and visibility is None
        return {
            "id": row[0],
            "candidate_type": row[1],
            "content": row[2],
            "evidence": evidence,
            "reason": row[4],
            "review_status": row[5],
            "promoted": bool(row[6]),
            "actor": row[7] or "agent",
            "created_at": row[8],
            "metadata": metadata,
            "bot_id": bot_id,
            "session_id": session_id,
            "visibility": visibility,
            "source_kind": row[13],
            "source_id": row[14],
            "idempotency_key": row[15],
            "revision": int(row[16] or 1),
            "updated_at": row[18],
            "scope": None if legacy else {"bot_id": bot_id, "session_id": session_id, "visibility": visibility},
            "legacy": legacy,
        }


__all__ = ["ReviewCandidateStore", "VALID_REVIEW_CANDIDATE_TYPES", "VALID_REVIEW_STATUSES"]
