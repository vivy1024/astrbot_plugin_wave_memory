"""Wave Memory 数据迁移 — 从旧记忆插件导入历史数据"""

from __future__ import annotations

import os
import sqlite3
import time
import json
from typing import Optional

import numpy as np

from astrbot.api import logger

from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex
from ..engine.embedding import EmbeddingService


class MigrationService:
    """从旧记忆插件迁移数据到 Wave Memory。

    支持来源：
    - angel_memory_trash/simple_memory.db
    - astrbot_plugin_livingmemory/
    - astrbot_plugin_self_learning/
    """

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service: EmbeddingService,
        astrbot_data_path: str,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.data_path = astrbot_data_path

    async def migrate_all(self, skip_existing: bool = True) -> dict:
        """执行全部迁移，返回统计。"""
        stats = {
            "angel_memory": 0,
            "livingmemory": 0,
            "self_learning": 0,
            "errors": 0,
        }

        # 检查是否已迁移过
        if skip_existing and self.db.get_memory_count() > 0:
            existing = self.db.get_kv("migration_done")
            if existing:
                logger.info("[WaveMemory] Migration already done, skipping")
                return stats

        # 1. Angel Memory
        angel_count = await self._migrate_angel_memory()
        stats["angel_memory"] = angel_count

        # 2. LivingMemory
        living_count = await self._migrate_livingmemory()
        stats["livingmemory"] = living_count

        # 3. Self Learning
        sl_count = await self._migrate_self_learning()
        stats["self_learning"] = sl_count

        # 标记迁移完成
        total = sum(v for k, v in stats.items() if k != "errors")
        self.db.put_kv("migration_done", json.dumps({
            "timestamp": time.time(),
            "stats": stats,
        }))

        logger.info(
            f"[WaveMemory] Migration complete: "
            f"angel={angel_count}, living={living_count}, sl={sl_count}, "
            f"total={total}"
        )
        return stats

    async def _migrate_angel_memory(self) -> int:
        """从 AngelMemory simple_memory.db 导入。"""
        db_path = os.path.join(
            self.data_path, "plugin_data", "angel_memory_trash", "simple_memory.db"
        )
        if not os.path.exists(db_path):
            # 尝试旧路径
            db_path = os.path.join(
                self.data_path, "plugin_data", "astrbot_plugin_angel_memory", "simple_memory.db"
            )
        if not os.path.exists(db_path):
            logger.debug("[WaveMemory] Angel Memory DB not found, skipping")
            return 0

        count = 0
        try:
            conn = sqlite3.connect(db_path)
            # AngelMemory 的表结构可能是 memories(id, content, category, importance, ...)
            # 尝试多种可能的表名
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]

            if "memories" in table_names:
                rows = conn.execute(
                    "SELECT content, category, importance, created_at FROM memories"
                ).fetchall()
                for content, category, importance, created_at in rows:
                    if not content or len(content.strip()) < 4:
                        continue
                    self.db.add_memory(
                        group_id=category or "angel_import",
                        content=content,
                        sender_id="angel_import",
                        sender_name="旧记忆",
                        timestamp=created_at or time.time(),
                        importance=importance or 1.0,
                    )
                    count += 1

            elif "simple_memory" in table_names:
                rows = conn.execute(
                    "SELECT content, scope, importance FROM simple_memory"
                ).fetchall()
                for content, scope, importance in rows:
                    if not content or len(content.strip()) < 4:
                        continue
                    self.db.add_memory(
                        group_id=scope or "angel_import",
                        content=content,
                        sender_id="angel_import",
                        sender_name="旧记忆",
                        timestamp=time.time(),
                        importance=importance or 1.0,
                    )
                    count += 1

            conn.close()
        except Exception as e:
            logger.warning(f"[WaveMemory] Angel Memory migration error: {e}")

        if count > 0:
            logger.info(f"[WaveMemory] Migrated {count} from Angel Memory")
        return count

    async def _migrate_livingmemory(self) -> int:
        """从 LivingMemory conversations.db 导入。"""
        db_path = os.path.join(
            self.data_path, "plugin_data", "astrbot_plugin_livingmemory", "conversations.db"
        )
        if not os.path.exists(db_path):
            logger.debug("[WaveMemory] LivingMemory DB not found, skipping")
            return 0

        count = 0
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]

            if "conversations" in table_names:
                rows = conn.execute(
                    "SELECT group_id, sender_name, content, timestamp FROM conversations ORDER BY timestamp DESC LIMIT 50000"
                ).fetchall()
                for group_id, sender_name, content, timestamp in rows:
                    if not content or len(content.strip()) < 4:
                        continue
                    self.db.add_memory(
                        group_id=group_id or "living_import",
                        content=content,
                        sender_id="",
                        sender_name=sender_name or "",
                        timestamp=timestamp or time.time(),
                    )
                    count += 1

            elif "messages" in table_names:
                rows = conn.execute(
                    "SELECT group_id, sender_name, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 50000"
                ).fetchall()
                for group_id, sender_name, content, timestamp in rows:
                    if not content or len(content.strip()) < 4:
                        continue
                    self.db.add_memory(
                        group_id=group_id or "living_import",
                        content=content,
                        sender_id="",
                        sender_name=sender_name or "",
                        timestamp=timestamp or time.time(),
                    )
                    count += 1

            conn.close()
        except Exception as e:
            logger.warning(f"[WaveMemory] LivingMemory migration error: {e}")

        if count > 0:
            logger.info(f"[WaveMemory] Migrated {count} from LivingMemory")
        return count

    async def _migrate_self_learning(self) -> int:
        """从 self_learning raw_messages 导入。"""
        db_path = os.path.join(
            self.data_path, "plugin_data", "astrbot_plugin_self_learning", "messages.db"
        )
        if not os.path.exists(db_path):
            # 尝试 self_learning_data 路径
            db_path = os.path.join(self.data_path, "self_learning_data", "messages.db")
        if not os.path.exists(db_path):
            logger.debug("[WaveMemory] Self Learning DB not found, skipping")
            return 0

        count = 0
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]

            if "raw_messages" in table_names:
                rows = conn.execute(
                    "SELECT group_id, sender_id, sender_name, message, timestamp FROM raw_messages ORDER BY timestamp DESC LIMIT 50000"
                ).fetchall()
                for group_id, sender_id, sender_name, message, timestamp in rows:
                    if not message or len(message.strip()) < 4:
                        continue
                    self.db.add_memory(
                        group_id=group_id or "sl_import",
                        content=message,
                        sender_id=sender_id or "",
                        sender_name=sender_name or "",
                        timestamp=timestamp or time.time(),
                    )
                    count += 1

            conn.close()
        except Exception as e:
            logger.warning(f"[WaveMemory] Self Learning migration error: {e}")

        if count > 0:
            logger.info(f"[WaveMemory] Migrated {count} from Self Learning")
        return count

    async def vectorize_unvectorized(self, batch_size: int = 50, max_batches: int = 100) -> int:
        """为没有向量的记忆补充 embedding（迁移后批量处理）。"""
        total = 0

        for _ in range(max_batches):
            rows = self.db.conn.execute(
                "SELECT id, content FROM memories WHERE vector IS NULL LIMIT ?",
                (batch_size,),
            ).fetchall()

            if not rows:
                break

            texts = [r[1] for r in rows]
            ids = [r[0] for r in rows]

            vectors = await self.embedding.get_embeddings(texts)

            for mid, vec in zip(ids, vectors):
                if vec is not None:
                    blob = vec.astype(np.float32).tobytes()
                    self.db.conn.execute(
                        "UPDATE memories SET vector=? WHERE id=?", (blob, mid)
                    )
                    self.memory_index.add([mid], vec.reshape(1, -1))

            self.db.conn.commit()
            total += len(rows)

            # 让出事件循环
            import asyncio
            await asyncio.sleep(0.1)

        if total > 0:
            self.memory_index.save()
            logger.info(f"[WaveMemory] Vectorized {total} memories")

        return total
