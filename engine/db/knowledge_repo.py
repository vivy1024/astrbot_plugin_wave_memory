"""KnowledgeRepo — facts + kv_store"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from .connection import ConnectionManager
from ..fact_classifier import get_decay_rate
try:
    from ...services.identity_safety import is_identity_contamination
except ImportError:  # tests import engine.* as top-level modules
    from services.identity_safety import is_identity_contamination


class KnowledgeRepo:
    """知识数据仓库：事实三元组 + KV 存储。"""

    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        self._decay_rate: float = 0.005  # 默认衰减速率，可通过 set_decay_rate 修改
        self._create_tables()

    def set_decay_rate(self, rate: float):
        """设置 facts 时间衰减速率。0=禁用衰减。"""
        self._decay_rate = max(0.0, rate)

    def _create_tables(self):
        self.cm.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                group_id TEXT,
                source_memory_id INTEGER,
                confidence REAL DEFAULT 1.0,
                valid_from REAL,
                valid_until REAL,
                created_at REAL,
                FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                vector BLOB
            );

            CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
            CREATE INDEX IF NOT EXISTS idx_facts_object ON facts(object);
        """)
        columns = {row[1] for row in self.cm.execute_read("PRAGMA table_info(facts)").fetchall()}
        if "valid_from" not in columns:
            self.cm.execute_write("ALTER TABLE facts ADD COLUMN valid_from REAL")
        if "valid_until" not in columns:
            self.cm.execute_write("ALTER TABLE facts ADD COLUMN valid_until REAL")
        if "last_reinforced" not in columns:
            self.cm.execute_write("ALTER TABLE facts ADD COLUMN last_reinforced REAL")
        if "fact_type" not in columns:
            self.cm.execute_write("ALTER TABLE facts ADD COLUMN fact_type TEXT DEFAULT 'FACTUAL'")
        self.cm.execute_write("UPDATE facts SET last_reinforced = COALESCE(last_reinforced, created_at) WHERE last_reinforced IS NULL")
        self.cm.commit()

    def put_kv(self, key: str, value: str, vector: Optional[np.ndarray] = None):
        vec_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        self.cm.execute_write(
            "INSERT OR REPLACE INTO kv_store (key, value, vector) VALUES (?, ?, ?)",
            (key, value, vec_blob),
        )
        self.cm.commit()

    def get_kv(self, key: str) -> Optional[tuple]:
        row = self.cm.execute_read("SELECT value, vector FROM kv_store WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        vec = np.frombuffer(row[1], dtype=np.float32) if row[1] else None
        return (row[0], vec)

    def insert_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        group_id: str = None,
        source_memory_id: int = None,
        confidence: float = 0.8,
        fact_type: str = None,
    ) -> int:
        """旧表 ``facts`` 的唯一写入点（历史路径，新逻辑一律走 ``scoped_facts``）。

        .. warning:: 证据锚点缺口（线上实测，暂时保留现状、不改数据）

            ``source_memory_id`` 可以为 ``None``，这一条就是「事实无法回溯」的来源：
            线上 ``facts`` 7993 条中只有 4075 条（51.0%）带来源记忆 id，其余近半数
            既无 ``source_memory_id`` 也无原话引用，溯源链在此处就断了。
            ``scoped_facts`` 情况好一些但仍缺 1/4（1108 条中 834 条，75.3%）。

            成因：提审时模型给的是**总结过的短语**而非群友原话，按原话反查不到
            ``memories.id``，于是直接落 ``None``，没有任何软降级或后向匹配兜底。

            后续方向（**本次不实施**，用户已确认旧表先不动）：
              1. ``tools/fact_proposal.py`` 把 ``source_quote`` 提为硬性必填，
                 摘要式短语一律拒收，逼迫调用方给原文；
              2. 在 ``resolve_source_memory_id`` 之外补一层「按发言时间戳 + 说话人
                 后向模糊匹配」，把已经悬空的事实回挂到最可能的 ``memories.id``；
              3. 旧表 ``facts`` 不迁移、不改写，新数据只进 ``scoped_facts``。
        """
        now = time.time()
        combined = f"{subject} {predicate} {obj}"
        if is_identity_contamination(combined):
            confidence = min(float(confidence or 0.8), 0.01)
            fact_type = "QUARANTINED_ROLEPLAY"
            valid_until = now
        else:
            valid_until = None
        existing = self.cm.execute_read(
            "SELECT id FROM facts WHERE subject = ? AND predicate = ? AND object = ?",
            (subject, predicate, obj),
        ).fetchone()
        if existing:
            # 已存在：普通事实强化；身份接管污染必须强制降权并过期，不能 MAX 保留高置信度。
            if valid_until is not None:
                self.cm.execute_write(
                    "UPDATE facts SET confidence = ?, valid_from = ?, valid_until = ?, last_reinforced = ?, fact_type = ? WHERE id = ?",
                    (confidence, now, valid_until, now, fact_type, existing[0]),
                )
            elif fact_type:
                self.cm.execute_write(
                    "UPDATE facts SET confidence = MAX(confidence, ?), valid_from = ?, last_reinforced = ?, fact_type = ? WHERE id = ?",
                    (confidence, now, now, fact_type, existing[0]),
                )
            else:
                self.cm.execute_write(
                    "UPDATE facts SET confidence = MAX(confidence, ?), valid_from = ?, last_reinforced = ? WHERE id = ?",
                    (confidence, now, now, existing[0]),
                )
            self.cm.commit()
            return existing[0]
        cursor = self.cm.execute_write(
            """INSERT INTO facts (subject, predicate, object, group_id, source_memory_id, confidence, valid_until, created_at, last_reinforced, fact_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject, predicate, obj, group_id, source_memory_id, confidence, valid_until, now, now, fact_type or "FACTUAL"),
        )
        self.cm.commit()
        return cursor.lastrowid

    def get_facts_by_subject(self, subject: str, limit: int = 20) -> list:
        rows = self.cm.execute_read(
            """SELECT id, subject, predicate, object, confidence, created_at, last_reinforced, fact_type
               FROM facts
               WHERE subject = ?
                 AND COALESCE(fact_type, '') != 'QUARANTINED_ROLEPLAY'
                 AND (valid_until IS NULL OR valid_until > ?)
               ORDER BY confidence DESC LIMIT ?""",
            (subject, time.time(), limit),
        ).fetchall()
        facts = [
            {"id": r[0], "subject": r[1], "predicate": r[2], "object": r[3],
             "confidence": r[4], "created_at": r[5], "last_reinforced": r[6],
             "fact_type": r[7] or "FACTUAL"}
            for r in rows
        ]
        return self._apply_decay(facts)

    def _apply_decay(self, facts: list, decay_rate: float = None) -> list:
        """对 facts 应用时间衰减（按 fact_type 选择速率）。

        如果 fact 有 fact_type 字段，使用该类型对应的速率；
        否则 fallback 到实例 _decay_rate 或参数 decay_rate。
        衰减只影响排序权重，不删除数据。
        """
        base_rate = decay_rate if decay_rate is not None else self._decay_rate
        if base_rate <= 0 and not any(f.get("fact_type") for f in facts):
            return facts
        now = time.time()
        for fact in facts:
            last_reinforced = fact.get("last_reinforced") or fact.get("created_at") or now
            age_days = (now - last_reinforced) / 86400
            # 按类型选速率，无类型则用 base_rate
            fact_type = fact.get("fact_type", "FACTUAL")
            rate = get_decay_rate(fact_type, base_rate)
            if rate <= 0:
                fact["effective_confidence"] = fact.get("confidence") or 1.0
            else:
                decay = max(0.1, 1.0 - age_days * rate)
                fact["effective_confidence"] = (fact.get("confidence") or 1.0) * decay
        facts.sort(key=lambda f: f.get("effective_confidence", 0), reverse=True)
        return facts

    def memory_exists_by_hash(self, content_hash: str) -> bool:
        row = self.cm.execute_read(
            "SELECT 1 FROM kv_store WHERE key = ?", (f"hash:{content_hash}",)
        ).fetchone()
        return row is not None

    def mark_imported(self, content_hash: str):
        self.cm.execute_write(
            "INSERT OR IGNORE INTO kv_store (key, value) VALUES (?, ?)",
            (f"hash:{content_hash}", "1"),
        )
        self.cm.commit()
