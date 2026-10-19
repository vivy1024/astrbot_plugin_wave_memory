"""KnowledgeRepo — kv_store（键值缓存 + 导入去重哈希）"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .connection import ConnectionManager


class KnowledgeRepo:
    """知识数据仓库：KV 存储（EPA 基底缓存与导入去重哈希共用此表）。"""

    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        self._create_tables()

    def _create_tables(self):
        self.cm.executescript("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                vector BLOB
            );
        """)

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
