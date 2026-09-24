"""SocialRepo — user_profiles + bot_mood + person_registry"""

from __future__ import annotations

import json
import time
from typing import Optional

from .connection import ConnectionManager


class SocialRepo:
    """社交数据仓库：用户画像、Bot情绪、人物注册表。"""

    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        self._create_tables()

    def _create_tables(self):
        self.cm.executescript("""
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
                bot_id TEXT DEFAULT '',
                UNIQUE(user_id, group_id, bot_id)
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

            CREATE TABLE IF NOT EXISTS person_registry (
                qq_id TEXT PRIMARY KEY,
                display_name TEXT,
                aliases TEXT,
                tag_ids TEXT,
                first_seen REAL,
                last_seen REAL,
                message_count INTEGER DEFAULT 0,
                groups TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                qq_id TEXT NOT NULL,
                role TEXT NOT NULL,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
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

            CREATE INDEX IF NOT EXISTS idx_user_profiles_user ON user_profiles(user_id);
            CREATE INDEX IF NOT EXISTS idx_expression_patterns_group ON expression_patterns(group_id);
        """)
        self.cm.commit()
        # 迁移：旧表可能没有 bot_id 列
        try:
            cols = [c[1] for c in self.cm.execute("PRAGMA table_info(user_profiles)").fetchall()]
            if "bot_id" not in cols:
                # 迁移（v2 之前的单 Bot 库）：旧行全部属于当时唯一的 Bot，沿用当年的 db_id。
                self.cm.execute("ALTER TABLE user_profiles ADD COLUMN bot_id TEXT DEFAULT 'yushu'")
                self.cm.commit()
        except Exception:
            pass
        # 确保索引存在
        try:
            self.cm.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_bot ON user_profiles(bot_id)")
            self.cm.commit()
        except Exception:
            pass

    # ─── Bot Mood ───

    def set_mood(self, group_id: str, mood_type: str, intensity: float = 0.5, description: str = "", duration_hours: float = 2.0):
        now = time.time()
        end_time = now + duration_hours * 3600
        self.cm.execute_write(
            "UPDATE bot_mood SET is_active = 0 WHERE group_id = ? AND is_active = 1",
            (group_id,),
        )
        self.cm.execute_write(
            """INSERT INTO bot_mood (group_id, mood_type, intensity, description, start_time, end_time, is_active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (group_id, mood_type, intensity, description, now, end_time),
        )
        self.cm.commit()

    def get_active_mood(self, group_id: str) -> Optional[dict]:
        now = time.time()
        self.cm.execute_write(
            "UPDATE bot_mood SET is_active = 0 WHERE is_active = 1 AND end_time < ?",
            (now,),
        )
        row = self.cm.execute_read(
            "SELECT mood_type, intensity, description, start_time, end_time FROM bot_mood WHERE group_id = ? AND is_active = 1 ORDER BY start_time DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "mood_type": row[0], "intensity": row[1], "description": row[2],
            "start_time": row[3], "end_time": row[4],
        }

    # ─── Person Registry ───

    def get_person_by_qq(self, qq_id: str) -> Optional[dict]:
        row = self.cm.execute_read(
            "SELECT qq_id, display_name, aliases, tag_ids, first_seen, last_seen, message_count, groups "
            "FROM person_registry WHERE qq_id = ?",
            (qq_id,),
        ).fetchone()
        if not row:
            return None
        aliases = json.loads(row[2]) if row[2] else []
        return {
            "qq_id": row[0], "display_name": row[1],
            # 兼容旧 DTO，但明确标记为只读、未解析 legacy 数据。
            "aliases": aliases,
            "legacy_aliases": aliases,
            "aliases_readonly": True,
            "alias_scope": None,
            "tag_ids": json.loads(row[3]) if row[3] else [],
            "first_seen": row[4], "last_seen": row[5],
            "message_count": row[6],
            "groups": json.loads(row[7]) if row[7] else [],
        }

    def find_person_by_name(self, name: str, *, include_legacy_aliases: bool = False) -> list:
        """按权威显示名查找；legacy aliases 仅供显式审计读取。"""
        rows = self.cm.execute_read(
            "SELECT qq_id, display_name, aliases, message_count FROM person_registry"
        ).fetchall()
        results = []
        name_lower = str(name or "").strip().casefold()
        if not name_lower:
            return results
        for qq_id, display, aliases_json, cnt in rows:
            display_name = str(display or "")
            display_lower = display_name.casefold()
            if display_lower == name_lower:
                results.append({"qq_id": qq_id, "display_name": display, "message_count": cnt, "match": "exact"})
                continue
            if name_lower in display_lower or (len(display_lower) >= 2 and display_lower in name_lower):
                results.append({"qq_id": qq_id, "display_name": display, "message_count": cnt, "match": "fuzzy"})
                continue
            if not include_legacy_aliases:
                continue
            aliases = json.loads(aliases_json) if aliases_json else []
            if any(str(alias).casefold() == name_lower for alias in aliases):
                results.append({
                    "qq_id": qq_id, "display_name": display, "message_count": cnt,
                    "match": "legacy_alias_exact", "legacy": True, "readonly": True, "scope": None,
                })
            elif any(
                name_lower in str(alias).casefold() or str(alias).casefold() in name_lower
                for alias in aliases if len(str(alias)) >= 2
            ):
                results.append({
                    "qq_id": qq_id, "display_name": display, "message_count": cnt,
                    "match": "legacy_alias_fuzzy", "legacy": True, "readonly": True, "scope": None,
                })
        results.sort(key=lambda x: (0 if x["match"] == "exact" else 1, -x["message_count"]))
        return results[:5]

    def get_memories_by_person(self, qq_id: str, role: str = None, limit: int = 50, offset: int = 0) -> list:
        if role:
            rows = self.cm.execute_read(
                """SELECT m.id, m.group_id, m.sender_id, m.sender_name, m.content, m.timestamp, m.importance
                   FROM memories m
                   JOIN memory_mentions mm ON mm.memory_id = m.id
                   WHERE mm.qq_id = ? AND mm.role = ?
                   ORDER BY m.timestamp DESC LIMIT ? OFFSET ?""",
                (qq_id, role, limit, offset),
            ).fetchall()
        else:
            rows = self.cm.execute_read(
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

    def get_person_cooccurrence(self, qq_id: str, top_k: int = 10) -> list:
        rows = self.cm.execute_read(
            """SELECT mm2.qq_id, COUNT(DISTINCT mm1.memory_id) as co_count
               FROM memory_mentions mm1
               JOIN memory_mentions mm2 ON mm1.memory_id = mm2.memory_id
               WHERE mm1.qq_id = ? AND mm2.qq_id != ?
               GROUP BY mm2.qq_id
               ORDER BY co_count DESC LIMIT ?""",
            (qq_id, qq_id, top_k),
        ).fetchall()
        results = []
        for co_qq, co_count in rows:
            person = self.cm.execute_read(
                "SELECT display_name FROM person_registry WHERE qq_id = ?", (co_qq,)
            ).fetchone()
            results.append({
                "qq_id": co_qq,
                "display_name": person[0] if person else co_qq,
                "co_count": co_count,
            })
        return results

    def get_person_stats(self, qq_id: str) -> dict:
        person = self.get_person_by_qq(qq_id)
        if not person:
            return {}
        role_counts = {}
        for role in ('sender', 'mentioned', 'about'):
            cnt = self.cm.execute_read(
                "SELECT count(*) FROM memory_mentions WHERE qq_id = ? AND role = ?",
                (qq_id, role),
            ).fetchone()[0]
            role_counts[role] = cnt
        top_tags = self.cm.execute_read(
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
