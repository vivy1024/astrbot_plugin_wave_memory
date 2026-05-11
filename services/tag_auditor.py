"""Wave Memory Tag 审计引擎 — LLM 驱动的 Tag 质量审计"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

from astrbot.api import logger


AUDIT_PROMPT = """你是一个知识图谱质量审计员。以下是一批标签（tag），请检查并给出清理建议。

检查规则：
1. **同义重复**：语义相同但名称不同的 tag，建议合并到最简洁、最规范的那个
2. **类型错误**：tag_type 明显不对的（如人名标为 fact/keyword，应为 person/entity）
3. **低质量**：JSON碎片、纯数字、过长句子（>15字）、无意义词、语法碎片

标签列表（JSON）：
{tags_json}

请输出 JSON 数组，每个元素是一条建议：
```json
[
  {{"action": "merge", "source_ids": [1,2,3], "target_name": "最佳名称", "target_type": "正确类型", "reason": "原因"}},
  {{"action": "retype", "tag_id": 123, "new_type": "person", "reason": "原因"}},
  {{"action": "delete", "tag_id": 456, "reason": "原因"}}
]
```

规则：
- 只输出有问题的 tag，没问题的不要列出
- merge 时 source_ids 包含所有要合并的 tag id（包括目标），target_name 是合并后保留的名称
- 如果一批里没有问题，输出空数组 []
- 不要编造不存在的 tag_id"""


class TagAuditor:
    """LLM 驱动的 Tag 质量审计。"""

    def __init__(self, db, context=None, provider_id: str = ""):
        self.db = db
        self.context = context
        self.provider_id = provider_id

    async def audit_batch(self, tags: list[dict]) -> list[dict]:
        """对一批 tag 调 LLM 做质量审计，返回建议列表。"""
        if not self.provider_id or not self.context:
            return []

        provider = self.context.get_provider_by_id(self.provider_id)
        if not provider:
            logger.warning(f"[WaveMemory] Audit: provider '{self.provider_id}' not found")
            return []

        # 构建 tag 列表 JSON
        tags_json = json.dumps(
            [{"id": t["id"], "name": t["name"], "type": t["type"], "frequency": t["frequency"]}
             for t in tags],
            ensure_ascii=False, indent=2
        )

        prompt = AUDIT_PROMPT.replace("{tags_json}", tags_json)

        response = await provider.text_chat(
            prompt=prompt,
            system_prompt="你是知识图谱质量审计系统，只输出 JSON 数组。",
        )

        if not response or not response.completion_text:
            return []

        return self._parse_suggestions(response.completion_text)

    async def run_audit(self, batch_size: int = 50, strategy: str = "mixed", total_count: int = 500) -> AsyncIterator[dict]:
        """运行完整审计流程，yield 进度事件。

        strategy:
          - "low_quality": 优先审计低质量 tag
          - "high_freq": 优先审计高频 tag（同义合并价值大）
          - "mixed": 混合抽样
        """
        # 获取待审计 tag
        tags = self._get_audit_candidates(strategy, limit=total_count)
        total = len(tags)

        if total == 0:
            yield {"progress": 100, "total": 0, "message": "没有需要审计的 Tag"}
            return

        yield {"progress": 0, "total": total, "message": f"开始审计 {total} 个 Tag (策略: {strategy})"}

        processed = 0
        suggestions_total = 0

        for i in range(0, total, batch_size):
            batch = tags[i:i + batch_size]

            try:
                suggestions = await self.audit_batch(batch)
            except Exception as e:
                logger.warning(f"[WaveMemory] Audit batch error: {e}")
                suggestions = []

            # 存入数据库
            for s in suggestions:
                self._save_suggestion(s)
                suggestions_total += 1

            processed += len(batch)
            progress = round(processed / total * 100)

            yield {
                "progress": progress,
                "processed": processed,
                "total": total,
                "batch_suggestions": len(suggestions),
                "total_suggestions": suggestions_total,
                "message": f"已审计 {processed}/{total}, 生成 {suggestions_total} 条建议",
            }

        yield {
            "progress": 100,
            "processed": total,
            "total": total,
            "total_suggestions": suggestions_total,
            "message": f"审计完成: {total} 个 Tag, {suggestions_total} 条建议",
        }

    def _get_audit_candidates(self, strategy: str, limit: int = 500) -> list[dict]:
        """按策略获取待审计 tag。"""
        if strategy == "low_quality":
            rows = self.db.conn.execute("""
                SELECT t.id, t.name, t.tag_type, COUNT(mt.memory_id) as freq
                FROM tags t
                LEFT JOIN memory_tags mt ON t.id = mt.tag_id
                WHERE t.confidence < 0.7 OR t.frequency <= 2
                GROUP BY t.id
                ORDER BY freq ASC, t.confidence ASC
                LIMIT ?
            """, (limit,)).fetchall()
        elif strategy == "high_freq":
            rows = self.db.conn.execute("""
                SELECT t.id, t.name, t.tag_type, COUNT(mt.memory_id) as freq
                FROM tags t
                LEFT JOIN memory_tags mt ON t.id = mt.tag_id
                GROUP BY t.id
                HAVING freq >= 3
                ORDER BY freq DESC
                LIMIT ?
            """, (limit,)).fetchall()
        else:  # mixed
            # 50% 低质量 + 50% 高频
            half = limit // 2
            low = self.db.conn.execute("""
                SELECT t.id, t.name, t.tag_type, COUNT(mt.memory_id) as freq
                FROM tags t
                LEFT JOIN memory_tags mt ON t.id = mt.tag_id
                WHERE t.confidence < 0.7 OR t.frequency <= 2
                GROUP BY t.id
                ORDER BY RANDOM()
                LIMIT ?
            """, (half,)).fetchall()
            high = self.db.conn.execute("""
                SELECT t.id, t.name, t.tag_type, COUNT(mt.memory_id) as freq
                FROM tags t
                LEFT JOIN memory_tags mt ON t.id = mt.tag_id
                GROUP BY t.id
                HAVING freq >= 3
                ORDER BY RANDOM()
                LIMIT ?
            """, (half,)).fetchall()
            rows = low + high

        return [
            {"id": r[0], "name": r[1], "type": r[2] or "keyword", "frequency": r[3]}
            for r in rows
        ]

    def _save_suggestion(self, suggestion: dict):
        """保存一条审计建议到数据库。"""
        action = suggestion.get("action")
        if not action:
            return

        if action == "merge":
            tag_ids = json.dumps(suggestion.get("source_ids", []))
            target_name = suggestion.get("target_name", "")
            target_type = suggestion.get("target_type", "")
        elif action == "retype":
            tag_ids = json.dumps([suggestion.get("tag_id")])
            target_name = None
            target_type = suggestion.get("new_type", "")
        elif action == "delete":
            tag_ids = json.dumps([suggestion.get("tag_id")])
            target_name = None
            target_type = None
        else:
            return

        reason = suggestion.get("reason", "")

        self.db.conn.execute("""
            INSERT INTO tag_audit_suggestions (action, tag_ids, target_name, target_type, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (action, tag_ids, target_name, target_type, reason, time.time()))
        self.db.conn.commit()

    def get_suggestions(self, status: str = "pending", limit: int = 50, offset: int = 0, action: str = None) -> list[dict]:
        """获取审计建议列表。"""
        if action:
            rows = self.db.conn.execute("""
                SELECT id, action, tag_ids, target_name, target_type, reason, status, created_at, resolved_at
                FROM tag_audit_suggestions
                WHERE status = ? AND action = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (status, action, limit, offset)).fetchall()
        else:
            rows = self.db.conn.execute("""
                SELECT id, action, tag_ids, target_name, target_type, reason, status, created_at, resolved_at
                FROM tag_audit_suggestions
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (status, limit, offset)).fetchall()

        suggestions = []
        for r in rows:
            tag_ids = json.loads(r[2]) if r[2] else []
            # 补充 tag 名称信息
            tag_names = {}
            if tag_ids:
                placeholders = ",".join("?" * len(tag_ids))
                name_rows = self.db.conn.execute(
                    f"SELECT id, name, tag_type FROM tags WHERE id IN ({placeholders})", tag_ids
                ).fetchall()
                tag_names = {nr[0]: {"name": nr[1], "type": nr[2]} for nr in name_rows}

            suggestions.append({
                "id": r[0],
                "action": r[1],
                "tag_ids": tag_ids,
                "tag_names": tag_names,
                "target_name": r[3],
                "target_type": r[4],
                "reason": r[5],
                "status": r[6],
                "created_at": r[7],
                "resolved_at": r[8],
            })

        return suggestions

    def get_suggestion_counts(self) -> dict:
        """获取各状态的建议数量。"""
        rows = self.db.conn.execute("""
            SELECT status, COUNT(*) FROM tag_audit_suggestions GROUP BY status
        """).fetchall()
        return {r[0]: r[1] for r in rows}

    def resolve_suggestion(self, suggestion_id: int, decision: str) -> dict:
        """处理一条建议：approve 执行操作，reject 标记拒绝。"""
        row = self.db.conn.execute(
            "SELECT id, action, tag_ids, target_name, target_type, status FROM tag_audit_suggestions WHERE id = ?",
            (suggestion_id,)
        ).fetchone()

        if not row:
            return {"error": f"Suggestion {suggestion_id} not found"}
        if row[5] != "pending":
            return {"error": f"Suggestion already {row[5]}"}

        action = row[1]
        tag_ids = json.loads(row[2]) if row[2] else []
        target_name = row[3]
        target_type = row[4]

        result = {"suggestion_id": suggestion_id, "decision": decision}

        if decision == "approve":
            if action == "merge":
                result.update(self._execute_merge(tag_ids, target_name, target_type))
            elif action == "retype":
                result.update(self._execute_retype(tag_ids[0] if tag_ids else None, target_type))
            elif action == "delete":
                result.update(self._execute_delete(tag_ids))

        # 更新状态
        new_status = "approved" if decision == "approve" else "rejected"
        self.db.conn.execute(
            "UPDATE tag_audit_suggestions SET status = ?, resolved_at = ? WHERE id = ?",
            (new_status, time.time(), suggestion_id)
        )
        self.db.conn.commit()

        result["status"] = new_status
        return result

    def _execute_merge(self, tag_ids: list[int], target_name: str, target_type: str) -> dict:
        """执行合并操作。"""
        if not tag_ids or not target_name:
            return {"error": "Invalid merge params"}

        # 找到或创建目标 tag
        target_row = self.db.conn.execute(
            "SELECT id FROM tags WHERE name = ?", (target_name,)
        ).fetchone()

        if target_row:
            target_id = target_row[0]
        else:
            # 用第一个 source 的 id 作为目标，改名
            target_id = tag_ids[0]
            self.db.conn.execute(
                "UPDATE tags SET name = ?, tag_type = ? WHERE id = ?",
                (target_name, target_type or "keyword", target_id)
            )

        source_ids = [tid for tid in tag_ids if tid != target_id]
        merged_names = []

        for src_id in source_ids:
            src = self.db.conn.execute("SELECT name FROM tags WHERE id = ?", (src_id,)).fetchone()
            if not src:
                continue
            merged_names.append(src[0])

            # 转移 memory_tags
            self.db.conn.execute(
                "UPDATE OR IGNORE memory_tags SET tag_id = ? WHERE tag_id = ?",
                (target_id, src_id)
            )
            # 删除冲突的重复关联
            self.db.conn.execute("DELETE FROM memory_tags WHERE tag_id = ?", (src_id,))
            # 转移 tag_relations
            self.db.conn.execute(
                "UPDATE OR IGNORE tag_relations SET source_tag_id = ? WHERE source_tag_id = ?",
                (target_id, src_id)
            )
            self.db.conn.execute(
                "UPDATE OR IGNORE tag_relations SET target_tag_id = ? WHERE target_tag_id = ?",
                (target_id, src_id)
            )
            # 清理残留 relations
            self.db.conn.execute("DELETE FROM tag_relations WHERE source_tag_id = ? OR target_tag_id = ?", (src_id, src_id))
            # 删除源 tag
            self.db.conn.execute("DELETE FROM tags WHERE id = ?", (src_id,))

        # 更新目标 tag 的 aliases 和 frequency
        existing_aliases = self.db.conn.execute(
            "SELECT aliases FROM tags WHERE id = ?", (target_id,)
        ).fetchone()
        old_aliases = (existing_aliases[0] or "").split(",") if existing_aliases and existing_aliases[0] else []
        all_aliases = list(set(old_aliases + merged_names))
        all_aliases = [a for a in all_aliases if a and a != target_name]

        # 重算 frequency
        new_freq = self.db.conn.execute(
            "SELECT COUNT(*) FROM memory_tags WHERE tag_id = ?", (target_id,)
        ).fetchone()[0]

        self.db.conn.execute(
            "UPDATE tags SET aliases = ?, frequency = ?, tag_type = ? WHERE id = ?",
            (",".join(all_aliases), new_freq, target_type or "keyword", target_id)
        )
        self.db.conn.commit()

        return {"merged": len(source_ids), "target_id": target_id, "target_name": target_name}

    def _execute_retype(self, tag_id: int, new_type: str) -> dict:
        """执行类型修正。"""
        if not tag_id or not new_type:
            return {"error": "Invalid retype params"}

        self.db.conn.execute(
            "UPDATE tags SET tag_type = ? WHERE id = ?", (new_type, tag_id)
        )
        self.db.conn.commit()
        return {"retyped": tag_id, "new_type": new_type}

    def _execute_delete(self, tag_ids: list[int]) -> dict:
        """执行删除操作。"""
        if not tag_ids:
            return {"error": "No tag_ids to delete"}

        placeholders = ",".join("?" * len(tag_ids))
        self.db.conn.execute(f"DELETE FROM memory_tags WHERE tag_id IN ({placeholders})", tag_ids)
        self.db.conn.execute(f"DELETE FROM tag_relations WHERE source_tag_id IN ({placeholders}) OR target_tag_id IN ({placeholders})", tag_ids + tag_ids)
        self.db.conn.execute(f"DELETE FROM tags WHERE id IN ({placeholders})", tag_ids)
        self.db.conn.commit()
        return {"deleted": len(tag_ids)}

    @staticmethod
    def _parse_suggestions(text: str) -> list[dict]:
        """解析 LLM 返回的 JSON 建议。"""
        import re
        text = text.strip()
        # 去掉 markdown code block
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)

        # 尝试找 JSON 数组
        json_match = re.search(r'\[[\s\S]*\]', text)
        if json_match:
            try:
                result = json.loads(json_match.group())
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        # fallback: 尝试整体解析
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        logger.warning(f"[WaveMemory] Failed to parse audit response: {text[:200]}")
        return []
