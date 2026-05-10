"""Wave Memory 数据库层 — SQLite + WAL 模式"""

import os
import sqlite3
import time
from typing import Optional

import numpy as np


class WaveMemoryDB:
    """SQLite 数据库管理，存储记忆、标签和关联关系。"""

    def __init__(self, db_path: str, dimension: int = 1024):
        self.db_path = db_path
        self.dimension = dimension
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                sender_id TEXT,
                sender_name TEXT,
                content TEXT NOT NULL,
                vector BLOB,
                timestamp REAL NOT NULL,
                importance REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                vector BLOB,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_tags (
                memory_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                position INTEGER DEFAULT 0,
                PRIMARY KEY (memory_id, tag_id),
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                vector BLOB
            );

            CREATE INDEX IF NOT EXISTS idx_memories_group ON memories(group_id);
            CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
            CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_id);
        """)
        self.conn.commit()

    def add_memory(
        self,
        group_id: str,
        content: str,
        vector: Optional[np.ndarray] = None,
        sender_id: str = "",
        sender_name: str = "",
        timestamp: Optional[float] = None,
        importance: float = 1.0,
    ) -> int:
        ts = timestamp or time.time()
        vec_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        cur = self.conn.execute(
            """INSERT INTO memories (group_id, sender_id, sender_name, content, vector, timestamp, importance)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (group_id, sender_id, sender_name, content, vec_blob, ts, importance),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_memory_by_id(self, memory_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, group_id, sender_id, sender_name, content, vector, timestamp, importance, access_count FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "group_id": row[1],
            "sender_id": row[2],
            "sender_name": row[3],
            "content": row[4],
            "vector": np.frombuffer(row[5], dtype=np.float32) if row[5] else None,
            "timestamp": row[6],
            "importance": row[7],
            "access_count": row[8],
        }

    def get_all_memory_vectors(self, group_id: Optional[str] = None) -> list[tuple[int, np.ndarray]]:
        """返回 (id, vector) 列表，用于构建向量索引。"""
        if group_id:
            rows = self.conn.execute(
                "SELECT id, vector FROM memories WHERE group_id=? AND vector IS NOT NULL",
                (group_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, vector FROM memories WHERE vector IS NOT NULL"
            ).fetchall()
        return [(r[0], np.frombuffer(r[1], dtype=np.float32)) for r in rows]

    def get_memories_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id, group_id, sender_id, sender_name, content, timestamp, importance FROM memories WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return [
            {
                "id": r[0],
                "group_id": r[1],
                "sender_id": r[2],
                "sender_name": r[3],
                "content": r[4],
                "timestamp": r[5],
                "importance": r[6],
            }
            for r in rows
        ]

    def touch_memories(self, ids: list[int]):
        """更新访问计数和时间。"""
        now = time.time()
        for mid in ids:
            self.conn.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, mid),
            )
        self.conn.commit()

    def add_tag(self, name: str, vector: Optional[np.ndarray] = None) -> int:
        vec_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tags (name, vector, created_at) VALUES (?, ?, ?)",
            (name, vec_blob, time.time()),
        )
        if cur.lastrowid == 0:
            row = self.conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
            if row:
                if vec_blob:
                    self.conn.execute("UPDATE tags SET vector=? WHERE id=?", (vec_blob, row[0]))
                self.conn.commit()
                return row[0]
        self.conn.commit()
        return cur.lastrowid

    def link_memory_tags(self, memory_id: int, tag_ids: list[int]):
        for pos, tid in enumerate(tag_ids, 1):
            self.conn.execute(
                "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position) VALUES (?, ?, ?)",
                (memory_id, tid, pos),
            )
        self.conn.commit()

    def get_all_tag_vectors(self) -> list[tuple[int, str, np.ndarray]]:
        rows = self.conn.execute(
            "SELECT id, name, vector FROM tags WHERE vector IS NOT NULL"
        ).fetchall()
        return [(r[0], r[1], np.frombuffer(r[2], dtype=np.float32)) for r in rows]

    def get_cooccurrence_data(self) -> list[tuple[int, int, int]]:
        """返回 (tag_a_id, tag_b_id, co_count) 用于构建共现矩阵。"""
        rows = self.conn.execute("""
            SELECT a.tag_id, b.tag_id, COUNT(*) as cnt
            FROM memory_tags a
            JOIN memory_tags b ON a.memory_id = b.memory_id AND a.tag_id < b.tag_id
            GROUP BY a.tag_id, b.tag_id
            HAVING cnt >= 2
        """).fetchall()
        return rows

    def get_memory_count(self, group_id: Optional[str] = None) -> int:
        if group_id:
            return self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE group_id=?", (group_id,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def get_tag_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]

    def put_kv(self, key: str, value: str, vector: Optional[np.ndarray] = None):
        vec_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        self.conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, vector) VALUES (?, ?, ?)",
            (key, value, vec_blob),
        )
        self.conn.commit()

    def get_kv(self, key: str) -> Optional[tuple[str, Optional[np.ndarray]]]:
        row = self.conn.execute("SELECT value, vector FROM kv_store WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        vec = np.frombuffer(row[1], dtype=np.float32) if row[1] else None
        return (row[0], vec)

    def close(self):
        self.conn.close()
