"""Wave Memory 数据导入器 — 从 livingmemory / self_learning 导入数据"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import AsyncGenerator, Optional

import numpy as np

from astrbot.api import logger


class WaveMemoryImporter:
    """从其他记忆插件导入数据到 wave_memory。"""

    def __init__(self, db, embedding_service, tag_extractor=None, memory_index=None, writer=None):
        self.db = db
        self.embedding_service = embedding_service
        self.tag_extractor = tag_extractor
        self.memory_index = memory_index
        self.writer = writer

        # 数据源路径
        self.livingmemory_db = "/AstrBot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db"
        self.self_learning_db = "/AstrBot/data/self_learning_data/messages.db"
        self.angel_memory_db = "/AstrBot/data/plugin_data/angel_memory_trash/memory_center/index/simple_memory.db"

    async def preview(self, source: str) -> dict:
        """预览待导入数据。"""
        if source == "livingmemory":
            return self._preview_livingmemory()
        elif source == "self_learning":
            return self._preview_self_learning()
        elif source == "angel_memory":
            return self._preview_angel_memory()
        return {"error": f"Unknown source: {source}"}

    def _preview_livingmemory(self) -> dict:
        if not os.path.exists(self.livingmemory_db):
            return {"error": "livingmemory.db not found", "total_count": 0, "samples": []}

        conn = sqlite3.connect(self.livingmemory_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM documents")
        total = c.fetchone()[0]

        c.execute("SELECT text, metadata FROM documents LIMIT 5")
        samples = []
        for row in c.fetchall():
            meta = json.loads(row[1]) if row[1] else {}
            samples.append({
                "content": row[0][:200] if row[0] else "",
                "session_id": meta.get("session_id", ""),
                "importance": meta.get("importance", 0),
            })
        conn.close()

        return {
            "total_count": total,
            "samples": samples,
            "estimated_time_sec": total * 0.5,  # ~0.5s per item with embedding
        }

    def _preview_self_learning(self) -> dict:
        if not os.path.exists(self.self_learning_db):
            return {"error": "messages.db not found", "total_count": 0, "samples": []}

        conn = sqlite3.connect(self.self_learning_db)
        c = conn.cursor()

        # 过滤过短消息和纯图片/链接（字段名是 message 不是 content）
        c.execute("""SELECT COUNT(*) FROM raw_messages
                     WHERE LENGTH(message) >= 10
                     AND message NOT LIKE '[图片%'
                     AND message NOT LIKE '[语音%'
                     AND message NOT LIKE 'http%'""")
        total = c.fetchone()[0]

        c.execute(
            """SELECT message, sender_name, group_id, timestamp FROM raw_messages
               WHERE LENGTH(message) >= 10
               AND message NOT LIKE '[图片%'
               AND message NOT LIKE '[语音%'
               AND message NOT LIKE 'http%'
               LIMIT 5"""
        )
        samples = []
        for row in c.fetchall():
            samples.append({
                "content": row[0][:200] if row[0] else "",
                "sender_name": row[1] or "",
                "group_id": row[2] or "",
                "timestamp": row[3] or 0,
            })
        conn.close()

        return {
            "total_count": total,
            "samples": samples,
            "estimated_time_sec": total * 0.3,
        }

    async def run(
        self,
        source: str,
        re_embed: bool = True,
        extract_tags: bool = True,
        batch_size: int = 20,
    ) -> AsyncGenerator[str, None]:
        """执行导入，yield SSE 事件 JSON。"""
        if source == "livingmemory":
            async for event in self._import_livingmemory(re_embed, extract_tags, batch_size):
                yield event
        elif source == "self_learning":
            async for event in self._import_self_learning(re_embed, extract_tags, batch_size):
                yield event
        elif source == "angel_memory":
            async for event in self._import_angel_memory(re_embed, extract_tags, batch_size):
                yield event
        else:
            yield json.dumps({"progress": 1.0, "message": f"Unknown source: {source}"})

    async def _import_livingmemory(
        self, re_embed: bool, extract_tags: bool, batch_size: int
    ) -> AsyncGenerator[str, None]:
        if not os.path.exists(self.livingmemory_db):
            yield json.dumps({"progress": 1.0, "message": "Error: livingmemory.db not found"})
            return

        conn = sqlite3.connect(self.livingmemory_db)
        c = conn.cursor()
        c.execute("SELECT text, metadata FROM documents")
        all_docs = c.fetchall()
        conn.close()

        total = len(all_docs)
        processed = 0
        skipped = 0
        imported = 0

        yield json.dumps({"progress": 0, "processed": 0, "total": total, "message": f"Starting import of {total} documents..."})

        for i in range(0, total, batch_size):
            batch = all_docs[i:i + batch_size]
            texts = []
            metas = []

            for text, meta_str in batch:
                if not text or len(text.strip()) < 10:
                    skipped += 1
                    processed += 1
                    continue

                content_hash = hashlib.md5(text.encode()).hexdigest()
                if self.db.memory_exists_by_hash(content_hash):
                    skipped += 1
                    processed += 1
                    continue

                meta = json.loads(meta_str) if meta_str else {}
                texts.append(text)
                metas.append((text, meta, content_hash))

            if texts and re_embed:
                vectors = await self.embedding_service.get_embeddings(texts)
            else:
                vectors = [None] * len(texts)

            for idx, (text, meta, content_hash) in enumerate(metas):
                vec = vectors[idx] if idx < len(vectors) else None
                session_id = meta.get("session_id", "")
                # 从 session_id 提取 group_id
                group_id = session_id.split(":")[-1] if ":" in session_id else session_id
                importance = meta.get("importance", 0.5)

                memory_id = self.db.add_memory(
                    group_id=group_id,
                    content=text,
                    vector=vec,
                    sender_id="livingmemory_import",
                    sender_name="[LM导入]",
                    timestamp=meta.get("create_time", time.time()),
                    importance=importance,
                )

                if vec is not None and self.memory_index:
                    self.memory_index.add([memory_id], vec.reshape(1, -1))

                self.db.mark_imported(content_hash)
                imported += 1
                processed += 1

                # Tag 提取
                if extract_tags and self.tag_extractor and imported % 5 == 0:
                    try:
                        tags = await self.tag_extractor.extract_tags(text[:500])
                        if tags:
                            tag_vecs = await self.embedding_service.get_embeddings(tags)
                            tag_ids = []
                            for tag_name, tag_vec in zip(tags, tag_vecs):
                                tid = self.db.add_tag(tag_name, tag_vec)
                                tag_ids.append(tid)
                            self.db.link_memory_tags(memory_id, tag_ids)
                    except Exception:
                        pass

            progress = processed / total if total > 0 else 1.0
            yield json.dumps({
                "progress": round(progress, 3),
                "processed": processed,
                "total": total,
                "imported": imported,
                "skipped": skipped,
                "message": f"Batch {i // batch_size + 1}: processed {processed}/{total}",
            })

        # 保存索引
        if self.memory_index:
            self.memory_index.save()

        yield json.dumps({
            "progress": 1.0,
            "processed": total,
            "total": total,
            "imported": imported,
            "skipped": skipped,
            "message": f"Import complete: {imported} imported, {skipped} skipped",
        })

    async def _import_self_learning(
        self, re_embed: bool, extract_tags: bool, batch_size: int
    ) -> AsyncGenerator[str, None]:
        if not os.path.exists(self.self_learning_db):
            yield json.dumps({"progress": 1.0, "message": "Error: messages.db not found"})
            return

        conn = sqlite3.connect(self.self_learning_db)
        c = conn.cursor()
        c.execute(
            """SELECT message, sender_name, group_id, timestamp FROM raw_messages
               WHERE LENGTH(message) >= 10
               AND message NOT LIKE '[图片%'
               AND message NOT LIKE '[语音%'
               AND message NOT LIKE 'http%'"""
        )
        all_msgs = c.fetchall()
        conn.close()

        total = len(all_msgs)
        processed = 0
        skipped = 0
        imported = 0

        yield json.dumps({"progress": 0, "processed": 0, "total": total, "message": f"Starting import of {total} messages..."})

        for i in range(0, total, batch_size):
            batch = all_msgs[i:i + batch_size]
            texts = []
            msg_data = []

            for content, sender_name, group_id, timestamp in batch:
                if not content or len(content.strip()) < 10:
                    skipped += 1
                    processed += 1
                    continue

                content_hash = hashlib.md5(content.encode()).hexdigest()
                if self.db.memory_exists_by_hash(content_hash):
                    skipped += 1
                    processed += 1
                    continue

                texts.append(content)
                msg_data.append((content, sender_name or "", group_id or "", timestamp or time.time(), content_hash))

            if texts and re_embed:
                vectors = await self.embedding_service.get_embeddings(texts)
            else:
                vectors = [None] * len(texts)

            for idx, (content, sender_name, group_id, timestamp, content_hash) in enumerate(msg_data):
                vec = vectors[idx] if idx < len(vectors) else None

                memory_id = self.db.add_memory(
                    group_id=group_id,
                    content=content,
                    vector=vec,
                    sender_id="self_learning_import",
                    sender_name=sender_name,
                    timestamp=timestamp,
                    importance=0.5,
                )

                if vec is not None and self.memory_index:
                    self.memory_index.add([memory_id], vec.reshape(1, -1))

                self.db.mark_imported(content_hash)
                imported += 1
                processed += 1

            progress = processed / total if total > 0 else 1.0
            yield json.dumps({
                "progress": round(progress, 3),
                "processed": processed,
                "total": total,
                "imported": imported,
                "skipped": skipped,
                "message": f"Batch {i // batch_size + 1}: processed {processed}/{total}",
            })

        if self.memory_index:
            self.memory_index.save()

        yield json.dumps({
            "progress": 1.0,
            "processed": total,
            "total": total,
            "imported": imported,
            "skipped": skipped,
            "message": f"Import complete: {imported} imported, {skipped} skipped",
        })

    # ─── Angel Memory ───

    def _preview_angel_memory(self) -> dict:
        if not os.path.exists(self.angel_memory_db):
            return {"error": "angel_memory simple_memory.db not found", "total_count": 0, "samples": []}

        conn = sqlite3.connect(self.angel_memory_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM memory_records")
        total = c.fetchone()[0]

        c.execute("SELECT memory_type, judgment, reasoning, strength FROM memory_records LIMIT 5")
        samples = []
        for row in c.fetchall():
            samples.append({
                "content": row[1][:200] if row[1] else "",
                "memory_type": row[0] or "",
                "reasoning": row[2][:100] if row[2] else "",
                "strength": row[3] or 0,
            })

        c.execute("SELECT COUNT(*) FROM global_tags")
        tag_count = c.fetchone()[0]
        conn.close()

        return {"total_count": total, "tag_count": tag_count, "samples": samples, "estimated_time_sec": total * 0.5}

    async def _import_angel_memory(self, re_embed: bool, extract_tags: bool, batch_size: int) -> AsyncGenerator[str, None]:
        if not os.path.exists(self.angel_memory_db):
            yield json.dumps({"progress": 1.0, "message": "Error: angel_memory DB not found"})
            return

        conn = sqlite3.connect(self.angel_memory_db)
        c = conn.cursor()
        c.execute("SELECT id, memory_type, judgment, reasoning, strength, memory_scope FROM memory_records")
        all_records = c.fetchall()
        c.execute("SELECT id, name FROM global_tags")
        tag_map = {row[0]: row[1] for row in c.fetchall()}
        c.execute("SELECT memory_id, tag_id FROM memory_tag_rel")
        memory_tags_map = {}
        for mem_id, tag_id in c.fetchall():
            memory_tags_map.setdefault(str(mem_id), []).append(tag_id)
        conn.close()

        total = len(all_records)
        processed = 0
        skipped = 0
        imported = 0
        tags_imported = 0
        type_mapping = {"knowledge": "fact", "知识记忆": "fact", "event": "event", "事件记忆": "event", "emotional": "emotion", "skill": "keyword", "技能记忆": "keyword"}

        yield json.dumps({"progress": 0, "processed": 0, "total": total, "message": f"Starting angel_memory: {total} records, {len(tag_map)} tags..."})

        for i in range(0, total, batch_size):
            batch = all_records[i:i + batch_size]
            texts = []
            records_data = []

            for record in batch:
                rec_id, mem_type, judgment, reasoning, strength, scope = record
                content = judgment or ""
                if reasoning:
                    content = f"{content}\n原因：{reasoning}"
                if not content or len(content.strip()) < 10:
                    skipped += 1
                    processed += 1
                    continue
                content_hash = hashlib.md5(content.encode()).hexdigest()
                if self.db.memory_exists_by_hash(content_hash):
                    skipped += 1
                    processed += 1
                    continue
                texts.append(content)
                records_data.append((str(rec_id), content, mem_type, strength or 0.5, scope or "angel_import", content_hash))

            if texts and re_embed:
                vectors = await self.embedding_service.get_embeddings(texts)
            else:
                vectors = [None] * len(texts)

            for idx, (rec_id, content, mem_type, strength, scope, content_hash) in enumerate(records_data):
                vec = vectors[idx] if idx < len(vectors) else None
                memory_id = self.db.add_memory(
                    group_id=scope, content=content, vector=vec,
                    sender_id="angel_memory_import", sender_name=f"[AM:{mem_type}]",
                    timestamp=time.time(), importance=min(float(strength), 1.0),
                )
                if vec is not None and self.memory_index:
                    self.memory_index.add([memory_id], vec.reshape(1, -1))
                # 导入关联 Tag
                tag_ids_for_record = memory_tags_map.get(rec_id, [])
                if tag_ids_for_record:
                    wave_tag_ids = []
                    for angel_tag_id in tag_ids_for_record[:15]:
                        tag_name = tag_map.get(angel_tag_id)
                        if tag_name and len(tag_name) >= 2:
                            tid = self.db.add_tag_extended(name=tag_name, tag_type=type_mapping.get(mem_type, "keyword"), confidence=0.85)
                            wave_tag_ids.append(tid)
                            tags_imported += 1
                    if wave_tag_ids:
                        self.db.link_memory_tags(memory_id, wave_tag_ids)
                self.db.mark_imported(content_hash)
                imported += 1
                processed += 1

            yield json.dumps({"progress": round(processed / total, 3), "processed": processed, "total": total, "imported": imported, "skipped": skipped, "tags_imported": tags_imported, "message": f"Batch {i // batch_size + 1}: {processed}/{total} (tags: {tags_imported})"})

        if self.memory_index:
            self.memory_index.save()

        yield json.dumps({"progress": 1.0, "processed": total, "total": total, "imported": imported, "skipped": skipped, "tags_imported": tags_imported, "message": f"Complete: {imported} memories + {tags_imported} tags, {skipped} skipped"})
