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
                last_accessed REAL,
                memory_type TEXT DEFAULT 'message',
                source TEXT DEFAULT 'live',
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                tag_type TEXT DEFAULT 'keyword',
                vector BLOB,
                parent_id INTEGER,
                aliases TEXT,
                description TEXT,
                frequency INTEGER DEFAULT 0,
                last_seen REAL,
                confidence REAL DEFAULT 1.0,
                metadata TEXT,
                created_at REAL NOT NULL,
                updated_at REAL,
                FOREIGN KEY (parent_id) REFERENCES tags(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_tags (
                memory_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                position INTEGER DEFAULT 0,
                relevance REAL DEFAULT 1.0,
                PRIMARY KEY (memory_id, tag_id),
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tag_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_tag_id INTEGER NOT NULL,
                target_tag_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                confidence REAL DEFAULT 1.0,
                metadata TEXT,
                created_at REAL,
                FOREIGN KEY (source_tag_id) REFERENCES tags(id) ON DELETE CASCADE,
                FOREIGN KEY (target_tag_id) REFERENCES tags(id) ON DELETE CASCADE,
                UNIQUE(source_tag_id, target_tag_id, relation_type)
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                vector BLOB
            );

            -- 用户画像：好感度、交互统计、人格标签
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                UNIQUE(user_id, group_id)
            );

            -- Bot 情绪状态
            CREATE TABLE IF NOT EXISTS bot_mood (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                mood_type TEXT NOT NULL,
                intensity REAL DEFAULT 0.5,
                description TEXT,
                start_time REAL,
                end_time REAL,
                is_active INTEGER DEFAULT 1
            );

            -- 表达模式：学到的回复风格
            CREATE TABLE IF NOT EXISTS expression_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT,
                situation TEXT NOT NULL,
                expression TEXT NOT NULL,
                tag_ids TEXT,
                weight REAL DEFAULT 1.0,
                use_count INTEGER DEFAULT 0,
                last_used REAL,
                created_at REAL
            );

            -- 事实库：从对话中提取的结构化事实
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

            CREATE INDEX IF NOT EXISTS idx_memories_group ON memories(group_id);
            CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
            CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_id);
            CREATE INDEX IF NOT EXISTS idx_user_profiles_user ON user_profiles(user_id);
            CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
            CREATE INDEX IF NOT EXISTS idx_facts_object ON facts(object);
            CREATE INDEX IF NOT EXISTS idx_expression_patterns_group ON expression_patterns(group_id);
        """)
        self.conn.commit()

        # 迁移：给旧表添加新列（如果不存在）
        self._migrate_tags_table()

        # 迁移后创建新列的索引
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_tags_type ON tags(tag_type);
            CREATE INDEX IF NOT EXISTS idx_tags_parent ON tags(parent_id);
            CREATE INDEX IF NOT EXISTS idx_tag_relations_source ON tag_relations(source_tag_id);
            CREATE INDEX IF NOT EXISTS idx_tag_relations_target ON tag_relations(target_tag_id);
        """)
        self.conn.commit()

    def _migrate_tags_table(self):
        """给旧版表添加新列，兼容已有数据。"""
        # memories 表新列
        mem_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(memories)").fetchall()}
        mem_migrations = [
            ("memory_type", "TEXT DEFAULT 'message'"),
            ("source", "TEXT DEFAULT 'live'"),
            ("summary", "TEXT"),
        ]
        for col_name, col_def in mem_migrations:
            if col_name not in mem_cols:
                self.conn.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_def}")

        # tags 表新列
        existing_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tags)").fetchall()}
        migrations = [
            ("tag_type", "TEXT DEFAULT 'keyword'"),
            ("parent_id", "INTEGER"),
            ("aliases", "TEXT"),
            ("description", "TEXT"),
            ("frequency", "INTEGER DEFAULT 0"),
            ("last_seen", "REAL"),
            ("confidence", "REAL DEFAULT 1.0"),
            ("metadata", "TEXT"),
            ("updated_at", "REAL"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                self.conn.execute(f"ALTER TABLE tags ADD COLUMN {col_name} {col_def}")

        # memory_tags 添加 relevance 列
        mt_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(memory_tags)").fetchall()}
        if "relevance" not in mt_cols:
            self.conn.execute("ALTER TABLE memory_tags ADD COLUMN relevance REAL DEFAULT 1.0")

        # 创建 tag_relations 表（如果不存在）
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tag_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_tag_id INTEGER NOT NULL,
                target_tag_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                confidence REAL DEFAULT 1.0,
                metadata TEXT,
                created_at REAL,
                FOREIGN KEY (source_tag_id) REFERENCES tags(id) ON DELETE CASCADE,
                FOREIGN KEY (target_tag_id) REFERENCES tags(id) ON DELETE CASCADE,
                UNIQUE(source_tag_id, target_tag_id, relation_type)
            )
        """)

        # 创建新表（如果不存在）
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                UNIQUE(user_id, group_id)
            );
            CREATE TABLE IF NOT EXISTS bot_mood (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                mood_type TEXT NOT NULL,
                intensity REAL DEFAULT 0.5,
                description TEXT,
                start_time REAL,
                end_time REAL,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS expression_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT,
                situation TEXT NOT NULL,
                expression TEXT NOT NULL,
                tag_ids TEXT,
                weight REAL DEFAULT 1.0,
                use_count INTEGER DEFAULT 0,
                last_used REAL,
                created_at REAL
            );
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
        """)
        self.conn.commit()

    # ─── Tag 扩展操作 ───

    def add_tag_extended(
        self,
        name: str,
        tag_type: str = "keyword",
        vector: Optional[np.ndarray] = None,
        parent_id: Optional[int] = None,
        aliases: Optional[list[str]] = None,
        description: str = "",
        confidence: float = 1.0,
        metadata: Optional[dict] = None,
    ) -> int:
        """添加或更新 Tag（扩展版）。"""
        import json as _json
        vec_blob = vector.astype(np.float32).tobytes() if vector is not None else None
        aliases_str = ",".join(aliases) if aliases else None
        meta_str = _json.dumps(metadata, ensure_ascii=False) if metadata else None
        now = time.time()

        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tags (name, tag_type, vector, parent_id, aliases, description, frequency, last_seen, confidence, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (name, tag_type, vec_blob, parent_id, aliases_str, description, now, confidence, meta_str, now, now),
        )
        if cur.lastrowid == 0:
            # 已存在，更新
            row = self.conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
            if row:
                updates = ["frequency = frequency + 1", "last_seen = ?", "updated_at = ?"]
                params = [now, now]
                if vec_blob:
                    updates.append("vector = ?")
                    params.append(vec_blob)
                if tag_type != "keyword":
                    updates.append("tag_type = ?")
                    params.append(tag_type)
                if description:
                    updates.append("description = ?")
                    params.append(description)
                params.append(row[0])
                self.conn.execute(f"UPDATE tags SET {', '.join(updates)} WHERE id = ?", params)
                self.conn.commit()
                return row[0]
        self.conn.commit()
        return cur.lastrowid

    def add_tag_relation(
        self,
        source_tag_id: int,
        target_tag_id: int,
        relation_type: str,
        weight: float = 1.0,
        confidence: float = 1.0,
        metadata: Optional[dict] = None,
    ):
        """添加 Tag 之间的关系。"""
        import json as _json
        meta_str = _json.dumps(metadata, ensure_ascii=False) if metadata else None
        self.conn.execute(
            """INSERT OR REPLACE INTO tag_relations (source_tag_id, target_tag_id, relation_type, weight, confidence, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_tag_id, target_tag_id, relation_type, weight, confidence, meta_str, time.time()),
        )
        self.conn.commit()

    def get_tag_children(self, parent_id: int) -> list[dict]:
        """获取某个 Tag 的子 Tag。"""
        rows = self.conn.execute(
            "SELECT id, name, tag_type, frequency FROM tags WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        return [{"id": r[0], "name": r[1], "tag_type": r[2], "frequency": r[3]} for r in rows]

    def get_tag_relations(self, tag_id: int) -> list[dict]:
        """获取某个 Tag 的所有关系。"""
        rows = self.conn.execute(
            """SELECT tr.id, tr.target_tag_id, t.name, tr.relation_type, tr.weight, tr.confidence
               FROM tag_relations tr
               JOIN tags t ON t.id = tr.target_tag_id
               WHERE tr.source_tag_id = ?
               UNION
               SELECT tr.id, tr.source_tag_id, t.name, tr.relation_type, tr.weight, tr.confidence
               FROM tag_relations tr
               JOIN tags t ON t.id = tr.source_tag_id
               WHERE tr.target_tag_id = ?""",
            (tag_id, tag_id),
        ).fetchall()
        return [
            {"id": r[0], "related_tag_id": r[1], "related_tag_name": r[2],
             "relation_type": r[3], "weight": r[4], "confidence": r[5]}
            for r in rows
        ]

    def find_tag_by_alias(self, alias: str) -> Optional[int]:
        """通过别名查找 Tag ID。"""
        row = self.conn.execute(
            "SELECT id FROM tags WHERE name = ? OR aliases LIKE ?",
            (alias, f"%{alias}%"),
        ).fetchone()
        return row[0] if row else None

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

    # ─── WebUI 查询方法 ───

    def get_memory_count_with_vector(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE vector IS NOT NULL"
        ).fetchone()[0]

    def get_group_list(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT group_id FROM memories ORDER BY group_id"
        ).fetchall()
        return [r[0] for r in rows]

    def get_today_new_count(self) -> int:
        import datetime
        today_start = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        return self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE timestamp >= ?", (today_start,)
        ).fetchone()[0]

    def list_memories(
        self,
        offset: int = 0,
        limit: int = 20,
        group_id: str = None,
        sender: str = None,
        from_ts: float = None,
        to_ts: float = None,
        search: str = None,
        has_tags: bool = None,
        has_vector: bool = None,
    ) -> tuple[list[dict], int]:
        """分页查询记忆列表，返回 (items, total)。"""
        conditions = []
        params = []

        if group_id:
            conditions.append("m.group_id = ?")
            params.append(group_id)
        if sender:
            conditions.append("m.sender_name LIKE ?")
            params.append(f"%{sender}%")
        if from_ts:
            conditions.append("m.timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("m.timestamp <= ?")
            params.append(to_ts)
        if search:
            conditions.append("m.content LIKE ?")
            params.append(f"%{search}%")
        if has_tags is True:
            conditions.append("m.id IN (SELECT DISTINCT memory_id FROM memory_tags)")
        elif has_tags is False:
            conditions.append("m.id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)")
        if has_vector is True:
            conditions.append("m.vector IS NOT NULL")
        elif has_vector is False:
            conditions.append("m.vector IS NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM memories m {where}", params
        ).fetchone()[0]

        rows = self.conn.execute(
            f"""SELECT m.id, m.group_id, m.sender_id, m.sender_name, m.content, m.vector IS NOT NULL,
                       m.timestamp, m.importance, m.access_count, m.last_accessed
                FROM memories m {where}
                ORDER BY m.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        items = []
        for r in rows:
            # 获取关联 tags
            tags = self.conn.execute(
                """SELECT t.name, t.tag_type FROM tags t
                   JOIN memory_tags mt ON t.id = mt.tag_id
                   WHERE mt.memory_id = ? ORDER BY mt.position LIMIT 5""",
                (r[0],),
            ).fetchall()
            items.append({
                "id": r[0],
                "group_id": r[1],
                "sender_id": r[2],
                "sender_name": r[3],
                "content": r[4][:100] if r[4] else "",
                "has_vector": bool(r[5]),
                "timestamp": r[6],
                "importance": r[7],
                "access_count": r[8],
                "last_accessed": r[9],
                "tags": [{"name": t[0], "type": t[1]} for t in tags],
            })

        return items, total

    def get_memory_detail(self, memory_id: int) -> Optional[dict]:
        """获取记忆详情 + 关联 Tag。"""
        row = self.conn.execute(
            """SELECT id, group_id, sender_id, sender_name, content, vector IS NOT NULL,
                      timestamp, importance, access_count, last_accessed
               FROM memories WHERE id=?""",
            (memory_id,),
        ).fetchone()
        if not row:
            return None

        # 关联 Tag
        tags = self.conn.execute(
            """SELECT t.id, t.name FROM tags t
               JOIN memory_tags mt ON t.id = mt.tag_id
               WHERE mt.memory_id = ?
               ORDER BY mt.position""",
            (memory_id,),
        ).fetchall()

        return {
            "id": row[0],
            "group_id": row[1],
            "sender_id": row[2],
            "sender_name": row[3],
            "content": row[4],
            "has_vector": bool(row[5]),
            "timestamp": row[6],
            "importance": row[7],
            "access_count": row[8],
            "last_accessed": row[9],
            "tags": [{"id": t[0], "name": t[1]} for t in tags],
        }

    def get_memory_brief(self, memory_id: int) -> Optional[dict]:
        """获取记忆简要信息（用于查询结果）。"""
        row = self.conn.execute(
            "SELECT id, content, sender_name, group_id, timestamp FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "memory_id": row[0],
            "content": row[1][:200] if row[1] else "",
            "sender_name": row[2],
            "group_id": row[3],
            "timestamp": row[4],
        }

    def list_tags(self, offset: int = 0, limit: int = 50) -> tuple[list[dict], int]:
        """分页查询 Tag 列表。"""
        total = self.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        rows = self.conn.execute(
            """SELECT t.id, t.name, t.created_at, t.vector IS NOT NULL,
                      (SELECT COUNT(*) FROM memory_tags mt WHERE mt.tag_id = t.id) as mem_count
               FROM tags t
               ORDER BY mem_count DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        items = [
            {
                "id": r[0],
                "name": r[1],
                "created_at": r[2],
                "has_vector": bool(r[3]),
                "memory_count": r[4],
            }
            for r in rows
        ]
        return items, total

    def get_tag_graph_data(self, max_nodes: int = 200) -> tuple[list[dict], list[dict]]:
        """返回 vis-network 格式的图谱数据。限制节点数避免渲染过慢。"""
        # 边：共现关系（只取有共现的 Tag）
        edge_rows = self.conn.execute("""
            SELECT a.tag_id, b.tag_id, COUNT(*) as cnt
            FROM memory_tags a
            JOIN memory_tags b ON a.memory_id = b.memory_id AND a.tag_id < b.tag_id
            GROUP BY a.tag_id, b.tag_id
            ORDER BY cnt DESC
            LIMIT 500
        """).fetchall()
        edges = [{"from": r[0], "to": r[1], "value": r[2]} for r in edge_rows]

        # 收集出现在边中的 Tag ID
        tag_ids_in_edges = set()
        for r in edge_rows:
            tag_ids_in_edges.add(r[0])
            tag_ids_in_edges.add(r[1])

        # 节点：只取有共现关系的 Tag（限制数量）
        if tag_ids_in_edges:
            placeholders = ",".join("?" * min(len(tag_ids_in_edges), max_nodes))
            limited_ids = list(tag_ids_in_edges)[:max_nodes]
            tag_rows = self.conn.execute(
                f"""SELECT t.id, t.name,
                          (SELECT COUNT(*) FROM memory_tags mt WHERE mt.tag_id = t.id) as mem_count
                   FROM tags t WHERE t.id IN ({placeholders})""",
                limited_ids,
            ).fetchall()
        else:
            tag_rows = []

        nodes = [{"id": r[0], "label": r[1], "value": r[2]} for r in tag_rows]

        return nodes, edges

    def memory_exists_by_hash(self, content_hash: str) -> bool:
        """检查是否已存在相同内容的记忆（用于导入去重）。"""
        row = self.conn.execute(
            "SELECT 1 FROM kv_store WHERE key = ?", (f"hash:{content_hash}",)
        ).fetchone()
        return row is not None

    def mark_imported(self, content_hash: str):
        """标记内容已导入。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO kv_store (key, value) VALUES (?, ?)",
            (f"hash:{content_hash}", "1"),
        )
        self.conn.commit()

    # ─── 记忆管理操作 ───

    def delete_memory(self, memory_id: int) -> bool:
        """删除单条记忆及其关联 Tag 链接。"""
        existing = self.conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            return False
        self.conn.execute("DELETE FROM memory_tags WHERE memory_id=?", (memory_id,))
        self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.conn.commit()
        return True

    def delete_memories(self, ids: list[int]) -> int:
        """批量删除记忆，返回实际删除数量。"""
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM memory_tags WHERE memory_id IN ({placeholders})", ids)
        cursor = self.conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return cursor.rowcount

    def update_memory(self, memory_id: int, content: str = None, importance: float = None) -> bool:
        """更新记忆内容和/或重要度。"""
        updates = []
        params = []
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if importance is not None:
            updates.append("importance = ?")
            params.append(importance)
        if not updates:
            return False
        params.append(memory_id)
        self.conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id=?", params)
        self.conn.commit()
        return True

    def update_memory_vector(self, memory_id: int, vector: np.ndarray):
        """更新记忆的向量。"""
        self.conn.execute(
            "UPDATE memories SET vector=? WHERE id=?",
            (vector.tobytes(), memory_id),
        )
        self.conn.commit()

    def get_senders_list(self) -> list[dict]:
        """获取所有发送者及其消息数量。"""
        rows = self.conn.execute(
            """SELECT sender_name, COUNT(*) as cnt FROM memories
               WHERE sender_name IS NOT NULL AND sender_name != ''
               GROUP BY sender_name ORDER BY cnt DESC LIMIT 100"""
        ).fetchall()
        return [{"name": r[0], "count": r[1]} for r in rows]

    def get_memories_without_tags(self, limit: int = 100) -> list[int]:
        """获取无 Tag 的记忆 ID 列表。"""
        rows = self.conn.execute(
            """SELECT id FROM memories
               WHERE id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)
               AND LENGTH(content) >= 10
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_memories_without_vector(self, limit: int = 100) -> list[int]:
        """获取无向量的记忆 ID 列表。"""
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE vector IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def close(self):
        self.conn.close()
