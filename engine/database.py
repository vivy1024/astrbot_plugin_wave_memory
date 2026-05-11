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

        # tags 表添加 is_core 列
        if "is_core" not in existing_cols:
            self.conn.execute("ALTER TABLE tags ADD COLUMN is_core BOOLEAN DEFAULT 0")

        # 创建 tag_extraction_status 表
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tag_extraction_status (
                memory_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                updated_at REAL
            )
        """)
        # Migration: 旧表可能缺少 updated_at 列
        try:
            self.conn.execute("SELECT updated_at FROM tag_extraction_status LIMIT 1")
        except Exception:
            try:
                self.conn.execute("ALTER TABLE tag_extraction_status ADD COLUMN updated_at REAL")
            except Exception:
                pass

        # 创建 tag_intrinsic_residuals 表（Phase 3 预建）
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tag_intrinsic_residuals (
                tag_id INTEGER PRIMARY KEY,
                residual_energy REAL NOT NULL,
                computed_at REAL NOT NULL,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
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

        # FTS5 全文搜索虚拟表（Phase 6: DeepMemo）
        self.conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_memories USING fts5(
                content, sender_name, group_id,
                content='memories',
                content_rowid='id',
                tokenize='unicode61'
            );

            -- 插入触发器
            CREATE TRIGGER IF NOT EXISTS fts_memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO fts_memories(rowid, content, sender_name, group_id)
                VALUES (new.id, new.content, new.sender_name, new.group_id);
            END;

            -- 删除触发器
            CREATE TRIGGER IF NOT EXISTS fts_memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO fts_memories(fts_memories, rowid, content, sender_name, group_id)
                VALUES ('delete', old.id, old.content, old.sender_name, old.group_id);
            END;

            -- 更新触发器
            CREATE TRIGGER IF NOT EXISTS fts_memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO fts_memories(fts_memories, rowid, content, sender_name, group_id)
                VALUES ('delete', old.id, old.content, old.sender_name, old.group_id);
                INSERT INTO fts_memories(rowid, content, sender_name, group_id)
                VALUES (new.id, new.content, new.sender_name, new.group_id);
            END;
        """)

        # FTS5 初始填充（如果为空）
        fts_count = self.conn.execute("SELECT COUNT(*) FROM fts_memories").fetchone()[0]
        if fts_count == 0:
            mem_count = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if mem_count > 0:
                self.conn.execute("""
                    INSERT INTO fts_memories(rowid, content, sender_name, group_id)
                    SELECT id, content, sender_name, group_id FROM memories
                    WHERE content IS NOT NULL
                """)
                logger.info(f"[WaveMemory] FTS5 initial fill: {mem_count} memories indexed")

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
            f"SELECT id, group_id, sender_id, sender_name, content, timestamp, importance, access_count FROM memories WHERE id IN ({placeholders})",
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
                "access_count": r[7] if len(r) > 7 else 0,
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
            # 支持按 sender_id 精确匹配或 sender_name 模糊匹配
            conditions.append("(m.sender_id = ? OR m.sender_name LIKE ?)")
            params.append(sender)
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
        """获取所有发送者及其消息数量（按 sender_id 分组）。"""
        rows = self.conn.execute(
            """SELECT sender_id, 
                    (SELECT sender_name FROM memories m2 
                     WHERE m2.sender_id = m.sender_id AND m2.sender_name IS NOT NULL AND m2.sender_name != ''
                     ORDER BY m2.timestamp DESC LIMIT 1) as latest_name,
                    COUNT(*) as cnt 
               FROM memories m
               WHERE sender_id IS NOT NULL AND sender_id != ''
                 AND sender_id != 'bot_self'
               GROUP BY sender_id ORDER BY cnt DESC LIMIT 100"""
        ).fetchall()
        return [{"id": r[0], "name": r[1] or r[0], "count": r[2]} for r in rows]

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

    def get_memory_vectors(self, memory_ids: list[int]) -> dict:
        """批量获取记忆向量。返回 {memory_id: np.ndarray}。

        从 VectorIndex 获取（如果可用），否则返回空 dict。
        注意：此方法需要外部传入 memory_index 或从 kv_store 读取。
        这里从 memories 表的 embedding 列读取（如果存在）。
        """
        if not memory_ids:
            return {}

        # 检查是否有 embedding 列
        try:
            placeholders = ",".join("?" * len(memory_ids))
            rows = self.conn.execute(
                f"SELECT id, embedding FROM memories WHERE id IN ({placeholders}) AND embedding IS NOT NULL",
                memory_ids,
            ).fetchall()
            result = {}
            for row in rows:
                try:
                    vec = np.frombuffer(row[1], dtype=np.float32)
                    if len(vec) > 0:
                        result[row[0]] = vec
                except Exception:
                    continue
            return result
        except Exception:
            return {}


    # ═══════════════════════════════════════════════════════
    # 人物关联查询 (Person Association Layer)
    # ═══════════════════════════════════════════════════════

    def get_person_by_qq(self, qq_id: str) -> dict | None:
        """根据 QQ 号获取人物信息。"""
        row = self.conn.execute(
            "SELECT qq_id, display_name, aliases, tag_ids, first_seen, last_seen, message_count, groups "
            "FROM person_registry WHERE qq_id = ?",
            (qq_id,),
        ).fetchone()
        if not row:
            return None
        import json
        return {
            "qq_id": row[0], "display_name": row[1],
            "aliases": json.loads(row[2]) if row[2] else [],
            "tag_ids": json.loads(row[3]) if row[3] else [],
            "first_seen": row[4], "last_seen": row[5],
            "message_count": row[6],
            "groups": json.loads(row[7]) if row[7] else [],
        }

    def find_person_by_name(self, name: str) -> list[dict]:
        """根据昵称模糊查找人物（搜索 aliases JSON）。"""
        import json
        rows = self.conn.execute(
            "SELECT qq_id, display_name, aliases, message_count FROM person_registry"
        ).fetchall()
        results = []
        name_lower = name.lower()
        for qq_id, display, aliases_json, cnt in rows:
            aliases = json.loads(aliases_json) if aliases_json else []
            # 精确匹配
            if any(a.lower() == name_lower for a in aliases):
                results.append({"qq_id": qq_id, "display_name": display, "message_count": cnt, "match": "exact"})
            # 子串匹配
            elif any(name_lower in a.lower() or a.lower() in name_lower for a in aliases if len(a) >= 2):
                results.append({"qq_id": qq_id, "display_name": display, "message_count": cnt, "match": "fuzzy"})
        # 精确优先，然后按消息数排序
        results.sort(key=lambda x: (0 if x["match"] == "exact" else 1, -x["message_count"]))
        return results[:5]

    def get_memories_by_person(self, qq_id: str, role: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """获取某人相关的所有记忆。
        
        Args:
            qq_id: QQ 号
            role: 过滤角色 ('sender'|'mentioned'|'about')，None 表示全部
            limit: 返回数量
            offset: 偏移
        """
        if role:
            rows = self.conn.execute(
                """SELECT m.id, m.group_id, m.sender_id, m.sender_name, m.content, m.timestamp, m.importance
                   FROM memories m
                   JOIN memory_mentions mm ON mm.memory_id = m.id
                   WHERE mm.qq_id = ? AND mm.role = ?
                   ORDER BY m.timestamp DESC LIMIT ? OFFSET ?""",
                (qq_id, role, limit, offset),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT m.id, m.group_id, m.sender_id, m.sender_name, m.content, m.timestamp, m.importance
                   FROM memories m
                   JOIN memory_mentions mm ON mm.memory_id = m.id
                   WHERE mm.qq_id = ?
                   ORDER BY m.timestamp DESC LIMIT ? OFFSET ?""",
                (qq_id, limit, offset),
            ).fetchall()
        return [
            {"id": r[0], "group_id": r[1], "sender_id": r[2], "sender_name": r[3],
             "content": r[4], "timestamp": r[5], "importance": r[6]}
            for r in rows
        ]

    def get_person_cooccurrence(self, qq_id: str, top_k: int = 10) -> list[dict]:
        """获取某人的社交共现（经常一起出现的人）。"""
        import json
        rows = self.conn.execute(
            """SELECT mm2.qq_id, COUNT(DISTINCT mm1.memory_id) as co_count
               FROM memory_mentions mm1
               JOIN memory_mentions mm2 ON mm1.memory_id = mm2.memory_id
               WHERE mm1.qq_id = ? AND mm2.qq_id != ?
               GROUP BY mm2.qq_id
               ORDER BY co_count DESC
               LIMIT ?""",
            (qq_id, qq_id, top_k),
        ).fetchall()
        results = []
        for co_qq, co_count in rows:
            person = self.conn.execute(
                "SELECT display_name FROM person_registry WHERE qq_id = ?", (co_qq,)
            ).fetchone()
            results.append({
                "qq_id": co_qq,
                "display_name": person[0] if person else co_qq,
                "co_count": co_count,
            })
        return results

    def get_person_stats(self, qq_id: str) -> dict:
        """获取某人的统计摘要。"""
        import json
        person = self.get_person_by_qq(qq_id)
        if not person:
            return {}
        
        # 各角色计数
        role_counts = {}
        for role in ('sender', 'mentioned', 'about'):
            cnt = self.conn.execute(
                "SELECT count(*) FROM memory_mentions WHERE qq_id = ? AND role = ?",
                (qq_id, role),
            ).fetchone()[0]
            role_counts[role] = cnt
        
        # top tags
        top_tags = self.conn.execute(
            """SELECT t.name, COUNT(*) as cnt
               FROM memory_tags mt
               JOIN tags t ON t.id = mt.tag_id
               JOIN memories m ON m.id = mt.memory_id
               WHERE m.sender_id = ? AND t.tag_type NOT IN ('person', 'time')
               GROUP BY t.name ORDER BY cnt DESC LIMIT 8""",
            (qq_id,),
        ).fetchall()
        
        return {
            **person,
            "role_counts": role_counts,
            "top_tags": [{"name": t[0], "count": t[1]} for t in top_tags],
        }

    def close(self):
        self.conn.close()
