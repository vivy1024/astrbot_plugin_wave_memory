"""Wave Memory 通用数据源发现与导入 — 自动扫描 + LLM 分析未知插件"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
                "fields": {"content": "content", "sender": "sender_name", "timestamp": "timestamp", "group": "group_id"},
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
                "fields": {"content": "message", "sender": "sender_name", "timestamp": "timestamp", "group": "group_id"},
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
        """采样估算某个数据源中已有多少内容存在于 wave_memory。

        如果游标已到末尾（全部处理过），直接返回 100%。
        否则采样最多 200 条，用比例推算整体。
        """
        try:
            db_path = source.get("db_path")
            if not db_path:
                return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}

            total_count = source.get("count", 0)

            # 检查游标：如果已处理到末尾，直接返回 100%
            cursor_key = f"import_cursor:{source['id']}"
            try:
                cursor_row = wave_db.conn.execute(
                    "SELECT value FROM kv_store WHERE key = ?", (cursor_key,)
                ).fetchone()
                if cursor_row:
                    last_rowid = int(cursor_row[0])
                    # 检查源表是否还有 rowid > last_rowid 的记录
                    conn_check = sqlite3.connect(db_path)
                    if source["type"] == "known":
                        adapter = source["adapter"]
                        table = adapter["table"]
                        where = adapter.get("filter", "1=1")
                        remaining_rows = conn_check.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE {where} AND rowid > ?", (last_rowid,)
                        ).fetchone()[0]
                    else:
                        analysis = source.get("analysis", {})
                        importable = analysis.get("importable_tables", [])
                        if importable:
                            table = importable[0]["name"]
                            remaining_rows = conn_check.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE rowid > ?", (last_rowid,)
                            ).fetchone()[0]
                        else:
                            remaining_rows = 0
                    conn_check.close()

                    if remaining_rows == 0:
                        return {"sampled": 0, "existing": 0, "estimated_pct": 100.0, "estimated_remaining": 0}
                    else:
                        # 有新记录，返回估算的剩余量
                        pct = max(0, (total_count - remaining_rows) / total_count * 100) if total_count > 0 else 0
                        return {"sampled": 0, "existing": 0, "estimated_pct": round(pct, 1), "estimated_remaining": remaining_rows}
            except Exception:
                pass

            conn = sqlite3.connect(db_path)

            if source["type"] == "known":
                adapter = source["adapter"]
                table = adapter["table"]
                fields = adapter.get("fields", {})
                content_field = fields.get("content", "content")
                extra_field = fields.get("extra")
                where = adapter.get("filter", "1=1")

                # 采样：均匀取 200 条
                sample_size = min(200, total_count) if total_count > 0 else 200
                if extra_field:
                    rows = conn.execute(
                        f"SELECT {content_field}, {extra_field} FROM {table} WHERE {where} ORDER BY rowid ASC LIMIT ?",
                        (sample_size,)
                    ).fetchall()
                    contents = [f"{r[0]}\n{r[1]}" if r[1] and str(r[1]).strip() else r[0] for r in rows if r[0]]
                else:
                    rows = conn.execute(
                        f"SELECT {content_field} FROM {table} WHERE {where} ORDER BY rowid ASC LIMIT ?",
                        (sample_size,)
                    ).fetchall()
                    contents = [r[0] for r in rows if r[0]]
            else:
                analysis = source.get("analysis", {})
                importable = analysis.get("importable_tables", [])
                if not importable:
                    conn.close()
                    return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}
                table = importable[0]["name"]
                cols = [c.lower() for c in importable[0].get("columns", [])]
                content_field = next((c for c in cols if c in ("content", "text", "message", "judgment", "summary")), cols[0] if cols else "content")
                sample_size = min(200, total_count) if total_count > 0 else 200
                rows = conn.execute(f"SELECT {content_field} FROM {table} LIMIT ?", (sample_size,)).fetchall()
                contents = [r[0] for r in rows if r[0]]

            conn.close()

            sampled = len(contents)
            if sampled == 0:
                return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": 0}

            # 批量检查采样内容是否已存在
            existing = 0
            chunk_size = 200
            for i in range(0, sampled, chunk_size):
                chunk = contents[i:i + chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                existing += wave_db.conn.execute(
                    f"SELECT COUNT(*) FROM memories WHERE content IN ({placeholders})",
                    chunk,
                ).fetchone()[0]

            pct = min(existing / sampled, 1.0)
            estimated_remaining = max(0, int(total_count * (1 - pct)))

            return {
                "sampled": sampled,
                "existing": existing,
                "estimated_pct": round(pct * 100, 1),
                "estimated_remaining": estimated_remaining,
            }
        except Exception as e:
            logger.debug(f"[WaveMemory] estimate_imported error: {e}")
            return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}

            conn = sqlite3.connect(db_path)
            rows = []

            if source["type"] == "known":
                adapter = source["adapter"]
                table = adapter["table"]
                fields = adapter.get("fields", {})
                content_field = fields.get("content", "content")
                extra_field = fields.get("extra")
                where = adapter.get("filter", "1=1")
                if extra_field:
                    rows = conn.execute(
                        f"SELECT {content_field}, {extra_field} FROM {table} WHERE {where}"
                    ).fetchall()
                    contents = [f"{r[0]}\n{r[1]}" if r[1] and str(r[1]).strip() else r[0] for r in rows if r[0]]
                else:
                    rows = conn.execute(
                        f"SELECT {content_field} FROM {table} WHERE {where}"
                    ).fetchall()
                    contents = [r[0] for r in rows if r[0]]
            else:
                analysis = source.get("analysis", {})
                importable = analysis.get("importable_tables", [])
                if not importable:
                    conn.close()
                    return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": source.get("count", 0)}
                table = importable[0]["name"]
                cols = [c.lower() for c in importable[0].get("columns", [])]
                content_field = next((c for c in cols if c in ("content", "text", "message", "judgment", "summary")), cols[0] if cols else "content")
                rows = conn.execute(f"SELECT {content_field} FROM {table}").fetchall()
                contents = [r[0] for r in rows if r[0]]

            conn.close()

            distinct_contents = list({str(c) for c in contents if c})
            total = len(distinct_contents)
            if total == 0:
                return {"sampled": 0, "existing": 0, "estimated_pct": 0, "estimated_remaining": 0}

            existing = 0
            chunk_size = 400
            for i in range(0, total, chunk_size):
                chunk = distinct_contents[i:i + chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                existing += wave_db.conn.execute(
                    f"SELECT COUNT(DISTINCT content) FROM memories WHERE content IN ({placeholders})",
                    chunk,
                ).fetchone()[0]

            pct = min(existing / total, 1.0)
            estimated_remaining = max(0, total - existing)

            return {
                "sampled": total,
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

    async def validate_mapping(self, db_path: str, table: str, fields: dict) -> dict:
        """用 LLM 验证字段映射是否正确。

        返回:
            {"valid": True} 或 {"valid": False, "corrected": {...}, "issues": [...]}
        如果没有 LLM 可用，返回 {"valid": True, "skipped": True}
        """
        if not self.tag_extractor or not self.tag_extractor.provider_id:
            return {"valid": True, "skipped": True}

        try:
            provider = self.tag_extractor.context.get_provider_by_id(self.tag_extractor.provider_id)
            if not provider:
                return {"valid": True, "skipped": True}

            conn = sqlite3.connect(db_path)
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_names = [c[1] for c in cols]

            # 取 3 条样本
            rows = conn.execute(f"SELECT * FROM {table} LIMIT 3").fetchall()
            samples = []
            for row in rows:
                sample = {}
                for i, val in enumerate(row):
                    if i < len(col_names):
                        if isinstance(val, str) and len(val) > 100:
                            val = val[:100] + "..."
                        sample[col_names[i]] = val
                samples.append(sample)
            conn.close()

            mapping_json = json.dumps(fields, ensure_ascii=False, indent=2)
            samples_json = json.dumps(samples, ensure_ascii=False, indent=2)

            prompt = VALIDATE_MAPPING_PROMPT.format(
                mapping_json=mapping_json,
                table_name=table,
                columns=", ".join(col_names),
                samples=samples_json,
            )

            response = await provider.text_chat(prompt=prompt)
            if not response or not response.completion_text:
                return {"valid": True, "skipped": True}

            text = response.completion_text.strip()
            text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
            text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                result = json.loads(json_match.group())
                if not result.get("valid", True):
                    corrected = result.get("corrected_mapping", {})
                    return {
                        "valid": False,
                        "issues": result.get("issues", []),
                        "corrected": corrected,
                    }
                return {"valid": True}

            return {"valid": True, "skipped": True}
        except Exception as e:
            logger.debug(f"[WaveMemory] validate_mapping error: {e}")
            return {"valid": True, "skipped": True}

    async def import_known(self, source: dict, limit: int = 5000,
                           extract_tags: bool = False) -> AsyncGenerator[str, None]:
        """导入已知适配器的数据源。"""
        adapter = source["adapter"]
        db_path = source["db_path"]
        table = adapter["table"]
        fields = adapter["fields"].copy()  # 可能被 LLM 修正
        where = adapter.get("filter", "1=1")

        # ─── LLM 预检：验证字段映射 ───
        validation = await self.validate_mapping(db_path, table, fields)
        if not validation.get("valid", True):
            issues = validation.get("issues", [])
            corrected = validation.get("corrected", {})
            logger.warning(f"[WaveMemory] Mapping validation failed for {table}: {issues}")
            yield json.dumps({
                "progress": 0, "message": f"⚠️ LLM 预检发现映射问题: {'; '.join(issues)}，自动修正中..."
            })

            # 获取实际表列名，用于验证 LLM 修正
            conn_check = sqlite3.connect(db_path)
            actual_cols = {c[1].lower() for c in conn_check.execute(f"PRAGMA table_info({table})").fetchall()}
            conn_check.close()

            # 应用 LLM 修正（仅当字段名存在于实际表列中）
            if corrected.get("content_field") and corrected["content_field"].lower() in actual_cols:
                fields["content"] = corrected["content_field"]
            if corrected.get("sender_field") and corrected["sender_field"].lower() in actual_cols:
                fields["sender"] = corrected["sender_field"]
            elif corrected.get("sender_field") is None and "sender" in fields:
                del fields["sender"]
            if corrected.get("timestamp_field") and corrected["timestamp_field"].lower() in actual_cols:
                fields["timestamp"] = corrected["timestamp_field"]
            elif corrected.get("timestamp_field") is None and "timestamp" in fields:
                del fields["timestamp"]
            if corrected.get("group_field") and corrected["group_field"].lower() in actual_cols:
                fields["group"] = corrected["group_field"]
            elif corrected.get("group_field") is None and "group" in fields:
                del fields["group"]
            if corrected.get("filter"):
                where = corrected["filter"]

        conn = sqlite3.connect(db_path)
        content_field = fields.get("content", "content")
        select_fields = [content_field]

        sender_field = fields.get("sender")
        ts_field = fields.get("timestamp")
        extra_field = fields.get("extra")
        metadata_field = fields.get("metadata")
        group_field = fields.get("group")

        if sender_field:
            select_fields.append(sender_field)
        if ts_field:
            select_fields.append(ts_field)
        if extra_field:
            select_fields.append(extra_field)
        if metadata_field:
            select_fields.append(metadata_field)
        if group_field:
            select_fields.append(group_field)

        # 用 rowid 游标：记录上次扫描到的位置，下次直接跳过已处理的
        cursor_key = f"import_cursor:{source['id']}"
        last_rowid = 0
        try:
            row = self.db.conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (cursor_key,)
            ).fetchone()
            if row:
                last_rowid = int(row[0])
                # 安全检查：如果 wave_memory 是空的但游标不为 0，说明数据被清空了，重置游标
                mem_count = self.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                if mem_count == 0 and last_rowid > 0:
                    logger.warning(f"[WaveMemory] memories 表为空但游标={last_rowid}，重置游标")
                    last_rowid = 0
        except Exception:
            pass

        conn = sqlite3.connect(db_path)
        query = f"SELECT rowid, {', '.join(select_fields)} FROM {table} WHERE {where} AND rowid > ? ORDER BY rowid ASC"
        rows = conn.execute(query, (last_rowid,)).fetchall()
        conn.close()

        total = len(rows)
        if total == 0:
            yield json.dumps({"progress": 1.0, "message": "✓ 没有新记录需要导入（已全部处理过）"})
            return

        logger.info(f"[WaveMemory] Import started: {source['name']} ({total} new records after rowid {last_rowid}, limit={limit})")
        yield json.dumps({"progress": 0, "total": total, "message": f"从断点继续: {source['name']} (新记录: {total}, limit: {limit})..."})

        imported = 0
        skipped = 0
        errors = 0
        batch_size = 50
        consecutive_errors = 0  # 连续错误计数
        llm_fallback_attempted = False  # 是否已尝试 LLM 降级
        processed_rows = 0  # 已扫描的源记录数
        max_rowid_seen = last_rowid  # 跟踪处理到的最大 rowid

        for i in range(0, total, batch_size):
            # 已导入够 limit 条，提前结束
            if imported >= limit:
                break

            batch = rows[i:i + batch_size]
            processed_rows += len(batch)
            errors_before_batch = errors  # 记录本批开始前的错误数

            # batch 最后一条的 rowid（第 0 列），用于游标
            batch_last_rowid = batch[-1][0] if batch else max_rowid_seen
            texts_to_embed = []
            records = []

            # 批量去重：一次查询整批 content
            batch_contents = []
            batch_parsed = []

            for row in batch:
                idx = 1  # 第 0 列是 rowid
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

                group_id = "default"
                if group_field:
                    raw_group = row[idx] or ""
                    idx += 1
                    # group_id 可能是 "defaultnapcat:GroupMessage:1015727706" 格式，提取纯数字部分
                    if raw_group:
                        parts = raw_group.split(":")
                        group_id = parts[-1] if parts else raw_group

                # 拼接 extra 到 content
                if extra and extra.strip():
                    content = f"{content}\n{extra}"

                batch_contents.append(content)
                batch_parsed.append({"content": content, "sender": sender, "timestamp": ts, "group_id": group_id})

            # 批量去重查询
            if batch_contents:
                placeholders = ",".join(["?"] * len(batch_contents))
                existing_set = set()
                try:
                    cursor = self.db.conn.execute(
                        f"SELECT content FROM memories WHERE content IN ({placeholders})",
                        batch_contents
                    )
                    existing_set = {r[0] for r in cursor.fetchall()}
                except Exception:
                    # fallback: 逐条查
                    for c in batch_contents:
                        if self.db.conn.execute("SELECT 1 FROM memories WHERE content = ? LIMIT 1", (c,)).fetchone():
                            existing_set.add(c)

                for rec in batch_parsed:
                    if rec["content"] in existing_set:
                        skipped += 1
                    else:
                        texts_to_embed.append(rec["content"][:500])
                        records.append(rec)

            # 批量 embedding
            if texts_to_embed:
                try:
                    vectors = await self.embedding_service.get_embeddings(texts_to_embed)
                    if len(vectors) != len(records):
                        logger.warning(f"[WaveMemory] Import: vectors({len(vectors)}) != records({len(records)}), skip batch")
                        errors += len(records)
                    else:
                        for rec, vec in zip(records, vectors):
                            try:
                                mem_id = self.db.add_memory(
                                    group_id=rec["group_id"],
                                    content=rec["content"],
                                    sender_name=rec["sender"],
                                    vector=vec,
                                    timestamp=rec["timestamp"] or time.time(),
                                )
                                if vec is not None and self.memory_index:
                                    self.memory_index.add([mem_id], np.array(vec).reshape(1, -1))
                                imported += 1
                                consecutive_errors = 0  # 成功则重置
                            except Exception as e:
                                errors += 1
                                consecutive_errors += 1
                                if errors <= 3:
                                    logger.warning(f"[WaveMemory] Import add_memory error: {e}")

                        # ─── 连续错误降级：LLM 重新分析 ───
                        if consecutive_errors >= 5 and not llm_fallback_attempted:
                            llm_fallback_attempted = True
                            logger.warning(f"[WaveMemory] 连续 {consecutive_errors} 条失败，尝试 LLM 重新分析映射...")
                            yield json.dumps({
                                "progress": round((i + batch_size) / total, 3),
                                "imported": imported, "skipped": skipped, "errors": errors,
                                "message": f"⚠️ 连续失败 {consecutive_errors} 条，正在用 LLM 重新分析字段映射..."
                            })
                            # 用 LLM 重新验证
                            revalidation = await self.validate_mapping(db_path, table, fields)
                            if not revalidation.get("valid", True) and revalidation.get("corrected"):
                                corrected = revalidation["corrected"]
                                logger.info(f"[WaveMemory] LLM 修正映射: {corrected}")
                                # 这里无法重建 SELECT（已经取完数据），但记录修正信息供用户下次使用
                                yield json.dumps({
                                    "progress": round((i + batch_size) / total, 3),
                                    "imported": imported, "skipped": skipped, "errors": errors,
                                    "message": f"🔧 LLM 建议修正映射: {revalidation.get('issues', [])}。建议重新导入。"
                                })
                except Exception as e:
                    errors += len(texts_to_embed)
                    logger.warning(f"[WaveMemory] Batch embed error: {e}")

            # 这批没有新增 error 才推进游标（有 error 的批次下次会重试）
            if errors == errors_before_batch and batch_last_rowid > max_rowid_seen:
                max_rowid_seen = batch_last_rowid

            progress = min(processed_rows / total, 1.0)
            yield json.dumps({
                "progress": round(progress, 3),
                "imported": imported,
                "skipped": skipped,
                "errors": errors,
                "message": f"扫描 {processed_rows}/{total} | 导入:{imported}/{limit} 跳过:{skipped} 失败:{errors}"
            })

            import asyncio
            await asyncio.sleep(0.02)

        # 保存游标位置（无论导入多少，都记录扫描到的最大 rowid）
        if max_rowid_seen > last_rowid:
            try:
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                    (cursor_key, str(max_rowid_seen))
                )
                self.db.conn.commit()
            except Exception as e:
                logger.warning(f"[WaveMemory] Failed to save import cursor: {e}")

        if self.memory_index:
            self.memory_index.save()

        logger.info(f"[WaveMemory] Import done: {source['name']} — imported={imported}, skipped={skipped}, errors={errors}, cursor={max_rowid_seen}")

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
            "group_field": "field_name" | null,
            "filter": "WHERE clause" | null,
        }
        """
        db_path = source["db_path"]
        table = mapping["table"]
        content_field = mapping["content_field"]
        sender_field = mapping.get("sender_field")
        ts_field = mapping.get("timestamp_field")
        group_field = mapping.get("group_field")
        where = mapping.get("filter", "1=1")

        select_fields = [content_field]
        if sender_field:
            select_fields.append(sender_field)
        if ts_field:
            select_fields.append(ts_field)
        if group_field:
            select_fields.append(group_field)

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
                idx = 0
                content = str(row[idx] or "")
                idx += 1
                if len(content) < 6:
                    skipped += 1
                    continue

                sender = ""
                if sender_field:
                    sender = str(row[idx] or "")
                    idx += 1

                ts = time.time()
                if ts_field:
                    ts = row[idx] or time.time()
                    idx += 1

                group_id = "default"
                if group_field:
                    raw_group = str(row[idx] or "")
                    idx += 1
                    if raw_group:
                        parts = raw_group.split(":")
                        group_id = parts[-1] if parts else raw_group

                existing = self.db.conn.execute(
                    "SELECT id FROM memories WHERE content = ? LIMIT 1", (content,)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue

                vec = await self.embedding_service.get_embedding(content[:500])
                self.db.add_memory(group_id=group_id, content=content, sender_name=sender, vector=vec, timestamp=ts)
                imported += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    logger.warning(f"[WaveMemory] LLM mapping import error: {e}")

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
- timestamp: 时间戳（可选，Unix 时间戳或 ISO 格式）
- group_id: 群组/会话标识（可选，用于区分不同对话场景）

group_id 说明：
- 通常是群号、session_id、conversation_id 等标识对话来源的字段
- 如果字段值包含前缀（如 "defaultnapcat:GroupMessage:1015727706"），系统会自动提取最后的数字部分
- 如果表中没有明确的群组字段，设为 null

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
            "group_field": "群组/会话ID字段名或null",
            "filter": "SQL WHERE 条件或null",
            "description": "这个表包含什么数据"
        }}
    ]
}}

只返回 JSON，不要其他内容。"""


# ─── LLM 导入预检 Prompt ───

VALIDATE_MAPPING_PROMPT = """你是一个数据库导入验证专家。请验证以下字段映射是否正确。

## 目标：将源表数据导入记忆系统
记忆系统 add_memory 需要：
- group_id: str（群组/会话标识，必须）
- content: str（文本内容，必须）
- sender_name: str（发送者，可选）
- timestamp: float（时间戳，可选）

## 当前映射配置
{mapping_json}

## 实际表结构
表名: {table_name}
字段: {columns}

## 样本数据（前 3 条）
{samples}

## 重要约束
- corrected_mapping 中的所有字段名必须是表的**顶层列名**（即上面"字段"列表中的名称）
- 不要使用 JSON 内嵌字段（如 metadata 中的子字段）作为映射目标
- 如果需要的信息只存在于 JSON 字段内部，将该映射设为 null（系统会用默认值）
- 如果没有合适的群组字段，group_field 设为 null（系统默认 "default"）

## 请验证并返回 JSON：
{{
    "valid": true/false,
    "issues": ["问题描述1", ...],
    "corrected_mapping": {{
        "content_field": "正确的内容字段（必须是顶层列名）",
        "sender_field": "正确的发送者字段或null",
        "timestamp_field": "正确的时间戳字段或null",
        "group_field": "正确的群组字段或null",
        "filter": "建议的 WHERE 条件或null"
    }}
}}

如果映射正确，valid=true 且 corrected_mapping 与原映射一致。
如果有问题，valid=false 并给出修正后的映射。
只返回 JSON，不要其他内容。"""
