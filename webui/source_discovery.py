"""Wave Memory 通用数据源发现与导入 — 自动扫描 + LLM 分析未知插件"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import numpy as np

from astrbot.api import logger


# ─── 已知插件的导入适配器（免 LLM 分析） ───

KNOWN_ADAPTERS = {
    "astrbot_plugin_livingmemory": {
        "name": "LivingMemory",
        "databases": [
            {
                "file": "conversations.db",
                "label": "对话记录",
                "table": "messages",
                "fields": {"content": "content", "sender": "sender_name", "timestamp": "timestamp"},
                "filter": "LENGTH(content) >= 10",
            },
            {
                "file": "livingmemory.db",
                "label": "记忆文档",
                "table": "documents",
                "fields": {"content": "text", "metadata": "metadata"},
                "filter": "LENGTH(text) >= 10",
            },
        ],
    },
    "angel_memory_trash": {
        "name": "Angel Memory",
        "databases": [
            {
                "file": "memory_center/index/simple_memory.db",
                "label": "记忆记录",
                "table": "memory_records",
                "fields": {"content": "judgment", "extra": "reasoning"},
                "filter": "LENGTH(judgment) >= 10",
            },
        ],
    },
    "astrbot_plugin_self_learning": {
        "name": "Self Learning (内置)",
        "alt_paths": ["/AstrBot/data/self_learning_data/messages.db"],
        "databases": [
            {
                "file": "messages.db",
                "label": "原始消息",
                "table": "raw_messages",
                "fields": {"content": "message", "sender": "sender_name", "timestamp": "timestamp"},
                "filter": "LENGTH(message) >= 10 AND message NOT LIKE '[图片%' AND message NOT LIKE '[语音%'",
            },
            {
                "file": "messages.db",
                "label": "范例库",
                "table": "exemplar",
                "fields": {"content": "content", "sender": "sender_id", "group": "group_id"},
                "filter": "LENGTH(content) >= 6",
            },
            {
                "file": "messages.db",
                "label": "黑话词典",
                "table": "jargon",
                "fields": {"content": "content", "extra": "meaning"},
                "filter": "is_complete = 1 OR meaning IS NOT NULL",
            },
        ],
    },
}


class SourceDiscovery:
    """通用数据源发现器 — 扫描 plugin_data 目录，识别可导入的数据库。"""

    def __init__(self, plugin_data_dir: str = "/AstrBot/data/plugin_data",
                 extra_dirs: list[str] = None):
        self.plugin_data_dir = Path(plugin_data_dir)
        self.extra_dirs = [Path(d) for d in (extra_dirs or ["/AstrBot/data/self_learning_data"])]
        self._analysis_cache: dict[str, dict] = {}  # path -> analysis result

    def discover_all(self) -> list[dict]:
        """发现所有可导入的数据源。返回统一格式的 source 列表。"""
        sources = []

        # 1. 已知适配器
        for plugin_dir_name, adapter in KNOWN_ADAPTERS.items():
            plugin_dir = self.plugin_data_dir / plugin_dir_name
            # 也检查 alt_paths
            alt_paths = adapter.get("alt_paths", [])

            for db_spec in adapter["databases"]:
                db_path = None
                # 优先检查 plugin_data 下
                candidate = plugin_dir / db_spec["file"]
                if candidate.exists():
                    db_path = candidate
                else:
                    # 检查 alt_paths
                    for alt in alt_paths:
                        alt_p = Path(alt)
                        if alt_p.exists():
                            db_path = alt_p
                            break

                if not db_path:
                    continue

                try:
                    conn = sqlite3.connect(str(db_path))
                    # 检查表是否存在
                    table_exists = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (db_spec["table"],)
                    ).fetchone()
                    if not table_exists:
                        conn.close()
                        continue

                    where = db_spec.get("filter", "1=1")
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {db_spec['table']} WHERE {where}"
                    ).fetchone()[0]
                    conn.close()

                    if count == 0:
                        continue

                    source_id = f"{plugin_dir_name}__{db_spec['table']}"
                    sources.append({
                        "id": source_id,
                        "name": f"{adapter['name']} · {db_spec['label']}",
                        "description": f"{db_spec['table']} 表 ({count} 条可导入记录)",
                        "count": count,
                        "type": "known",
                        "db_path": str(db_path),
                        "adapter": db_spec,
                    })
                except Exception as e:
                    logger.debug(f"[WaveMemory] Skip {db_path}: {e}")

        # 2. 未知插件 — 扫描所有 .db 文件
        scanned_paths = set(s["db_path"] for s in sources)
        unknown_dbs = self._scan_unknown_dbs(scanned_paths)
        for db_info in unknown_dbs:
            sources.append(db_info)

        return sources

    def _scan_unknown_dbs(self, known_paths: set[str]) -> list[dict]:
        """扫描未被已知适配器覆盖的 .db 文件。"""
        results = []
        scan_dirs = [self.plugin_data_dir] + self.extra_dirs

        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue
            for db_file in scan_dir.rglob("*.db"):
                if str(db_file) in known_paths:
                    continue
                # 跳过备份、缓存、索引、自身
                path_str = str(db_file).lower()
                if any(skip in path_str for skip in ["backup", "cache", "avatar", ".hnsw", "qdrant", "wave_memory"]):
                    continue
                # 跳过太小的文件 (< 10KB)
                if db_file.stat().st_size < 10240:
                    continue

                analysis = self._quick_analyze(db_file)
                if analysis and analysis.get("importable_tables"):
                    plugin_name = self._guess_plugin_name(db_file)
                    results.append({
                        "id": f"unknown__{hashlib.md5(str(db_file).encode()).hexdigest()[:8]}",
                        "name": f"[未知] {plugin_name}",
                        "description": f"{db_file.name}: {', '.join(t['name'] + '(' + str(t['count']) + ')' for t in analysis['importable_tables'])}",
                        "count": sum(t["count"] for t in analysis["importable_tables"]),
                        "type": "unknown",
                        "db_path": str(db_file),
                        "analysis": analysis,
                        "needs_llm": True,
                    })

        return results

    def _quick_analyze(self, db_path: Path) -> Optional[dict]:
        """快速分析一个 SQLite 数据库，判断是否包含可导入的记忆数据。"""
        try:
            conn = sqlite3.connect(str(db_path))
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()

            importable = []
            all_tables = []

            for (table_name,) in tables:
                try:
                    cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    col_names = [c[1].lower() for c in cols]
                    count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]

                    table_info = {
                        "name": table_name,
                        "columns": [c[1] for c in cols],
                        "count": count,
                    }
                    all_tables.append(table_info)

                    # 启发式判断：包含文本内容字段的表可能是记忆数据
                    text_indicators = {"content", "text", "message", "memory", "summary",
                                       "judgment", "description", "note", "answer", "question"}
                    has_text = bool(text_indicators & set(col_names))

                    if has_text and count >= 5:
                        importable.append(table_info)
                except Exception:
                    continue

            conn.close()

            if not all_tables:
                return None

            return {
                "all_tables": all_tables,
                "importable_tables": importable,
            }
        except Exception:
            return None

    def _guess_plugin_name(self, db_path: Path) -> str:
        """从路径猜测插件名。"""
        parts = db_path.parts
        for i, part in enumerate(parts):
            if part == "plugin_data" and i + 1 < len(parts):
                return parts[i + 1].replace("astrbot_plugin_", "").replace("_", " ").title()
            if part == "self_learning_data":
                return "Self Learning"
        return db_path.stem

    def get_table_schema(self, db_path: str, table_name: str) -> dict:
        """获取表的详细 schema + 样本数据（供 LLM 分析）。"""
        conn = sqlite3.connect(db_path)
        cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]

        # 取 5 条样本
        samples = []
        rows = conn.execute(f"SELECT * FROM {table_name} LIMIT 5").fetchall()
        col_names = [c[1] for c in cols]
        for row in rows:
            sample = {}
            for i, val in enumerate(row):
                if i < len(col_names):
                    # 截断长文本
                    if isinstance(val, str) and len(val) > 200:
                        val = val[:200] + "..."
                    sample[col_names[i]] = val
            samples.append(sample)
        conn.close()

        return {
            "table": table_name,
            "columns": [{"name": c[1], "type": c[2], "nullable": not c[3], "pk": bool(c[5])} for c in cols],
            "row_count": count,
            "samples": samples,
        }

    def estimate_imported(self, source: dict, wave_db) -> dict:
        """采样估算某个数据源已导入到 wave_memory 的比例。

        返回 {"sampled": N, "existing": M, "estimated_pct": float, "estimated_remaining": int}
        使用内容前缀批量 IN 查询，避免逐条全表扫描。
        """
        sample_size = 20
        try:
            db_path = source.get("db_path")
            if not db_path:
                return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}

            conn = sqlite3.connect(db_path)

            if source["type"] == "known":
                adapter = source["adapter"]
                table = adapter["table"]
                content_field = adapter["fields"].get("content", "content")
                where = adapter.get("filter", "1=1")
                rows = conn.execute(
                    f"SELECT {content_field} FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ?",
                    (sample_size,)
                ).fetchall()
            else:
                # 未知源：取第一个 importable table
                analysis = source.get("analysis", {})
                importable = analysis.get("importable_tables", [])
                if not importable:
                    conn.close()
                    return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}
                table = importable[0]["name"]
                cols = [c.lower() for c in importable[0].get("columns", [])]
                content_field = next((c for c in cols if c in ("content", "text", "message", "judgment", "summary")), cols[0] if cols else "content")
                rows = conn.execute(
                    f"SELECT {content_field} FROM {table} ORDER BY rowid DESC LIMIT ?",
                    (sample_size,)
                ).fetchall()

            conn.close()

            if not rows:
                return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": 0}

            # 批量检查：用 content 精确匹配，一次 IN 查询
            contents = [r[0] for r in rows if r[0]]
            if not contents:
                return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}

            placeholders = ",".join(["?"] * len(contents))
            existing = wave_db.conn.execute(
                f"SELECT COUNT(*) FROM memories WHERE content IN ({placeholders})",
                contents
            ).fetchone()[0]

            sampled = len(contents)
            pct = existing / sampled if sampled > 0 else 0
            total = source.get("count", 0)
            estimated_remaining = max(0, int(total * (1 - pct)))

            return {
                "sampled": sampled,
                "existing": existing,
                "estimated_pct": round(pct * 100, 1),
                "estimated_remaining": estimated_remaining,
            }
        except Exception as e:
            logger.debug(f"[WaveMemory] estimate_imported error: {e}")
            return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}


class UniversalImporter:
    """通用导入器 — 根据适配器配置或 LLM 分析结果导入数据。"""

    def __init__(self, db, embedding_service, tag_extractor=None, memory_index=None):
        self.db = db
        self.embedding_service = embedding_service
        self.tag_extractor = tag_extractor
        self.memory_index = memory_index

    async def import_known(self, source: dict, limit: int = 500,
                           extract_tags: bool = False) -> AsyncGenerator[str, None]:
        """导入已知适配器的数据源。"""
        adapter = source["adapter"]
        db_path = source["db_path"]
        table = adapter["table"]
        fields = adapter["fields"]
        where = adapter.get("filter", "1=1")

        conn = sqlite3.connect(db_path)
        content_field = fields.get("content", "content")
        select_fields = [content_field]

        sender_field = fields.get("sender")
        ts_field = fields.get("timestamp")
        extra_field = fields.get("extra")
        metadata_field = fields.get("metadata")

        if sender_field:
            select_fields.append(sender_field)
        if ts_field:
            select_fields.append(ts_field)
        if extra_field:
            select_fields.append(extra_field)
        if metadata_field:
            select_fields.append(metadata_field)

        query = f"SELECT {', '.join(select_fields)} FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
        conn.close()

        total = len(rows)
        if total == 0:
            yield json.dumps({"progress": 1.0, "message": "No data to import"})
            return

        yield json.dumps({"progress": 0, "total": total, "message": f"Importing {total} records from {source['name']}..."})

        imported = 0
        skipped = 0
        errors = 0
        batch_size = 10

        for i in range(0, total, batch_size):
            batch = rows[i:i + batch_size]
            texts_to_embed = []
            records = []

            for row in batch:
                idx = 0
                content = row[idx] or ""
                idx += 1

                sender = ""
                if sender_field:
                    sender = row[idx] or ""
                    idx += 1

                ts = None
                if ts_field:
                    ts = row[idx]
                    idx += 1

                extra = ""
                if extra_field:
                    extra = row[idx] or ""
                    idx += 1

                metadata = None
                if metadata_field:
                    raw_meta = row[idx] or ""
                    idx += 1
                    try:
                        metadata = json.loads(raw_meta) if raw_meta else {}
                        if not sender and metadata.get("sender_name"):
                            sender = metadata["sender_name"]
                    except Exception:
                        pass

                # 拼接 extra 到 content
                if extra and extra.strip():
                    content = f"{content}\n{extra}"

                # 内容去重
                content_hash = hashlib.md5(content.encode()).hexdigest()
                existing = self.db.conn.execute(
                    "SELECT id FROM memories WHERE content = ? LIMIT 1", (content,)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue

                texts_to_embed.append(content[:500])
                records.append({"content": content, "sender": sender, "timestamp": ts})

            # 批量 embedding
            if texts_to_embed:
                try:
                    vectors = await self.embedding_service.get_embeddings(texts_to_embed)
                    for rec, vec in zip(records, vectors):
                        try:
                            mem_id = self.db.add_memory(
                                content=rec["content"],
                                sender_name=rec["sender"],
                                vector=vec,
                                timestamp=rec["timestamp"] or time.time(),
                            )
                            if vec is not None and self.memory_index:
                                self.memory_index.add([mem_id], np.array(vec).reshape(1, -1))
                            imported += 1
                        except Exception:
                            errors += 1
                except Exception as e:
                    errors += len(texts_to_embed)
                    logger.warning(f"[WaveMemory] Batch embed error: {e}")

            progress = min((i + batch_size) / total, 1.0)
            yield json.dumps({
                "progress": round(progress, 3),
                "imported": imported,
                "skipped": skipped,
                "errors": errors,
                "message": f"{imported + skipped + errors}/{total} (导入:{imported} 跳过:{skipped})"
            })

            import asyncio
            await asyncio.sleep(0.02)

        if self.memory_index:
            self.memory_index.save()

        yield json.dumps({
            "progress": 1.0,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "message": f"✓ 完成: 导入 {imported} 条, 跳过 {skipped} 条 (重复), 错误 {errors} 条"
        })

    async def import_with_llm_mapping(self, source: dict, mapping: dict,
                                      limit: int = 500) -> AsyncGenerator[str, None]:
        """根据 LLM 生成的字段映射导入未知数据源。

        mapping 格式:
        {
            "table": "table_name",
            "content_field": "field_name",
            "sender_field": "field_name" | null,
            "timestamp_field": "field_name" | null,
            "filter": "WHERE clause" | null,
        }
        """
        db_path = source["db_path"]
        table = mapping["table"]
        content_field = mapping["content_field"]
        sender_field = mapping.get("sender_field")
        ts_field = mapping.get("timestamp_field")
        where = mapping.get("filter", "1=1")

        select_fields = [content_field]
        if sender_field:
            select_fields.append(sender_field)
        if ts_field:
            select_fields.append(ts_field)

        conn = sqlite3.connect(db_path)
        query = f"SELECT {', '.join(select_fields)} FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
        conn.close()

        total = len(rows)
        if total == 0:
            yield json.dumps({"progress": 1.0, "message": "No data to import"})
            return

        yield json.dumps({"progress": 0, "total": total, "message": f"Importing {total} records (LLM mapping)..."})

        imported = 0
        skipped = 0
        errors = 0

        for i, row in enumerate(rows):
            try:
                content = str(row[0] or "")
                if len(content) < 6:
                    skipped += 1
                    continue

                sender = str(row[1]) if sender_field and len(row) > 1 else ""
                ts = row[2] if ts_field and len(row) > 2 else time.time()

                existing = self.db.conn.execute(
                    "SELECT id FROM memories WHERE content = ? LIMIT 1", (content,)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue

                vec = await self.embedding_service.get_embedding(content[:500])
                self.db.add_memory(content=content, sender_name=sender, vector=vec, timestamp=ts)
                imported += 1
            except Exception:
                errors += 1

            if (i + 1) % 20 == 0:
                yield json.dumps({
                    "progress": round((i + 1) / total, 3),
                    "imported": imported,
                    "skipped": skipped,
                    "errors": errors,
                })
                import asyncio
                await asyncio.sleep(0.02)

        yield json.dumps({
            "progress": 1.0,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "message": f"✓ 完成: 导入 {imported} 条, 跳过 {skipped} 条, 错误 {errors} 条"
        })


# ─── LLM 数据源分析 Prompt ───

ANALYZE_SOURCE_PROMPT = """你是一个数据库分析专家。我需要你分析以下 SQLite 数据库表结构，判断哪些数据适合作为"记忆"导入到记忆系统中。

记忆系统需要的数据格式：
- content: 文本内容（必须，至少 10 字符）
- sender: 发送者名称（可选）
- timestamp: 时间戳（可选）

以下是数据库的表结构和样本数据：

{schema_json}

请分析并返回 JSON（不要 markdown 代码块）：
{{
    "importable": true/false,
    "reason": "简短说明为什么适合/不适合导入",
    "mappings": [
        {{
            "table": "表名",
            "content_field": "内容字段名",
            "sender_field": "发送者字段名或null",
            "timestamp_field": "时间戳字段名或null",
            "filter": "SQL WHERE 条件或null",
            "description": "这个表包含什么数据"
        }}
    ]
}}

只返回 JSON，不要其他内容。"""
