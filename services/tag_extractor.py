"""Wave Memory Tag 提取器 V3 — 结构化语义标注 + 已有 Tag 复用"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

from astrbot.api import logger


# ─── 结构化 Tag 提取 Prompt ───

TAG_EXTRACTION_PROMPT = """你是一个记忆标注系统。从对话消息中提取结构化语义标签，用于后续记忆检索和知识图谱构建。

## 输出格式（严格 JSON 数组）
[{{"name": "标签名", "type": "类型", "confidence": 0.9}}, ...]

## 标签类型（type 字段）
- person: 人名、昵称、群友称呼
- topic: 话题、讨论主题
- entity: 具体事物（游戏、小说、食物、品牌等）
- event: 事件、行为、发生的事
- emotion: 情绪、态度、语气
- fact: 可提取的事实性信息
- location: 地点、场所
- time: 时间相关标记

## 规则
1. 每条消息提取 3-10 个标签，按重要性排序
2. 标签名 2-8 字，简洁精准，是可复用的语义锚点
3. confidence 范围 0.5-1.0，越确定越高
4. 优先提取：人名 > 具体事物 > 话题 > 事件 > 情绪
5. 避免提取：纯语气词、无意义碎片、过于宽泛的词
6. 如果消息无实质内容（纯表情、"嗯""哦"），返回空数组 []
7. **优先复用已有标签**：如果消息内容匹配下方已有标签，直接使用已有标签名，不要创建语义重复的新标签
{reference_section}
## 消息
发送者：{sender}
内容：{message}

## 输出（仅 JSON 数组，无其他文字）"""


# ─── 批量提取 Prompt（一个 JSON 文档批量处理，节省 LLM 调用）───

BATCH_TAG_PROMPT = """你是一个记忆标注系统。输入是一份 JSON 批处理文档，请为每条记忆提取结构化语义标签并打分。

## 输入 JSON
{batch_json}

## 输出格式（严格 JSON 对象）
{{
  "items": [
    {{
      "id": 123,
      "tags": [
        {{"name": "标签名", "type": "类型", "score": 0.9}}
      ]
    }}
  ]
}}

## 标签类型
person(人名/昵称/群友) | topic(话题) | entity(具体事物/作品/游戏/工具) | event(事件/行为) | emotion(情绪/态度) | fact(事实) | location(地点) | time(时间)

## 规则
- 必须按输入 item.id 原样返回 id，不能重新编号，不能漏掉 id
- 每条记忆 0-8 个标签，按重要性排序
- 标签名 2-12 字，简洁可复用，不要长句
- score 范围 0.0-1.0，表示标签与该记忆的相关性/置信度
- 无实质内容、纯表情、纯寒暄返回空 tags: []
- 只输出 JSON，不要 markdown 代码块，不要解释文字
- **优先复用已有标签**：如果内容匹配下方已有标签，直接使用已有标签名，不要创建语义重复的新标签
{reference_section}"""


# 有效的 Tag 类型
VALID_TAG_TYPES = {"person", "topic", "entity", "event", "emotion", "fact", "location", "time", "keyword"}


class TagExtractor:
    """结构化 Tag 提取服务 V3。

    核心改进：
    1. 结构化输出：每个 Tag 带 type + confidence
    2. 批量提取：多条消息合并一次 LLM 调用
    3. 核心标签识别：高频 Tag 自动标记为核心
    4. 质量过滤：基于 confidence 和规则过滤低质量 Tag
    5. 已有 Tag 复用：注入高频 Tag 词表，LLM 优先复用
    """

    def __init__(self, context, provider_id: str, max_tags: int = 10, batch_size: int = 5, blacklist: str = "", db=None, embedding_service=None, tag_index=None):
        self.context = context
        self.provider_id = provider_id
        self.max_tags = max_tags
        self.batch_size = batch_size
        self.db = db

        # Tag 黑名单
        self._blacklist: set = {
            w.strip() for w in blacklist.split(",") if w.strip()
        }

        # 核心标签缓存（高频 Tag 名称集合）
        self._core_tags: set = set()
        self._tag_freq: dict[str, int] = {}

        # 已有 Tag 参考词表缓存
        self._reference_text: str = ""
        self._reference_tags_set: set[str] = set()  # 用于后处理归一化
        self._reference_call_count: int = 0
        self._reference_refresh_interval: int = 200

        # Tag RAG（P3）：embedding 搜索已有 tag
        self.embedding_service = embedding_service
        self.tag_index = tag_index

    def _build_reference_section(self) -> str:
        """构建已有 Tag 参考词表，注入 prompt。

        从 DB 取关联记忆数 >= 2 的高频 tag，按 type 分组，格式化为紧凑字符串。
        每 200 次调用刷新一次缓存。
        """
        self._reference_call_count += 1

        # 有缓存且未到刷新周期
        if self._reference_text and self._reference_call_count % self._reference_refresh_interval != 0:
            return self._reference_text

        if not self.db:
            return ""

        try:
            conn = self.db.conn if hasattr(self.db, 'conn') else self.db

            # 取关联数 >= 2 的 tag，按关联数降序，每个 type 取 top 30，总计不超过 200
            rows = conn.execute("""
                SELECT t.name, t.tag_type, COUNT(mt.memory_id) as mc
                FROM tags t JOIN memory_tags mt ON t.id = mt.tag_id
                GROUP BY t.id
                HAVING mc >= 2
                ORDER BY mc DESC
                LIMIT 300
            """).fetchall()

            if not rows:
                return ""

            # 按 type 分组
            by_type: dict[str, list[str]] = {}
            total = 0
            for name, tag_type, _mc in rows:
                if total >= 200:
                    break
                by_type.setdefault(tag_type, [])
                if len(by_type[tag_type]) < 30:
                    by_type[tag_type].append(name)
                    total += 1

            # 格式化
            lines = []
            type_order = ["person", "entity", "topic", "event", "emotion", "fact", "location", "time"]
            for t in type_order:
                if t in by_type and by_type[t]:
                    lines.append(f"  {t}: {', '.join(by_type[t])}")

            if not lines:
                return ""

            self._reference_text = "\n## 已有标签（优先复用）\n" + "\n".join(lines) + "\n"

            # 更新归一化用的 set
            self._reference_tags_set = {name for name, _, _ in rows}

            logger.debug(f"[WaveMemory] Reference tags refreshed: {total} tags")
            return self._reference_text

        except Exception as e:
            logger.debug(f"[WaveMemory] Failed to build reference tags: {e}")
            return ""

    async def _build_rag_reference_section(self, message: str) -> str:
        """Tag RAG：用消息 embedding 搜索 tag_index，取 top 20 语义相关 tag 注入 prompt。

        如果 embedding 不可用或 tag_index 为空，返回空字符串（调用方会 fallback 到静态词表）。
        """
        if not self.embedding_service or not self.tag_index:
            return ""

        if self.tag_index.count == 0:
            return ""

        try:
            # 获取消息的 embedding
            query_vec = await self.embedding_service.get_embedding(message[:500])
            if query_vec is None:
                return ""

            # 搜索 tag_index
            results = self.tag_index.search(query_vec, k=30)
            if not results:
                return ""

            # 获取 tag 详情
            tag_ids = [int(tid) for tid, _ in results]
            if not self.db:
                return ""

            conn = self.db.conn if hasattr(self.db, 'conn') else self.db
            placeholders = ",".join("?" * len(tag_ids))
            rows = conn.execute(
                f"SELECT id, name, tag_type FROM tags WHERE id IN ({placeholders})",
                tag_ids
            ).fetchall()

            if not rows:
                return ""

            # 按 type 分组，取 top 20
            by_type: dict[str, list[str]] = {}
            total = 0
            # 保持搜索结果的相关性排序
            id_to_info = {r[0]: (r[1], r[2]) for r in rows}
            for tid, _ in results:
                tid = int(tid)
                if tid not in id_to_info:
                    continue
                name, tag_type = id_to_info[tid]
                tag_type = tag_type or "keyword"
                if total >= 20:
                    break
                by_type.setdefault(tag_type, [])
                if len(by_type[tag_type]) < 8:  # 每类最多 8 个
                    by_type[tag_type].append(name)
                    total += 1

            if not by_type:
                return ""

            # 格式化
            lines = []
            type_order = ["person", "entity", "topic", "event", "emotion", "fact", "location", "time"]
            for t in type_order:
                if t in by_type and by_type[t]:
                    lines.append(f"  {t}: {', '.join(by_type[t])}")

            if not lines:
                return ""

            # 同时更新归一化用的 set
            rag_names = {name for names in by_type.values() for name in names}
            self._reference_tags_set.update(rag_names)

            return "\n## 语义相关标签（优先复用）\n" + "\n".join(lines) + "\n"

        except Exception as e:
            logger.debug(f"[WaveMemory] Tag RAG failed: {e}")
            return ""

    def _normalize_tag_name(self, name: str) -> str:
        """对提取结果做归一化，尝试匹配已有 tag。

        规则：
        - 去首尾空格
        - 如果已有 tag 集合里有完全匹配，直接返回
        - 如果去掉尾部虚词（的、了、着）后匹配已有 tag，返回已有 tag
        - 否则返回原名
        """
        name = name.strip()

        if not self._reference_tags_set:
            return name

        # 完全匹配
        if name in self._reference_tags_set:
            return name

        # 去尾部虚词
        stripped = re.sub(r'[的了着]$', '', name)
        if stripped and stripped != name and stripped in self._reference_tags_set:
            return stripped

        # 尝试不区分大小写匹配
        name_lower = name.lower()
        for ref_tag in self._reference_tags_set:
            if ref_tag.lower() == name_lower:
                return ref_tag

        return name

    async def extract_tags(self, message: str, sender: str = "") -> list[dict]:
        """从单条消息提取结构化 Tag。

        Returns:
            [{"name": str, "type": str, "confidence": float}, ...]
        """
        if not message or len(message.strip()) < 6:
            return []

        if not self.provider_id:
            return self._fallback_extract(message)

        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                logger.warning(f"[WaveMemory] Tag LLM provider '{self.provider_id}' not found, using fallback")
                return self._fallback_extract(message)

            reference_section = self._build_reference_section()

            # Tag RAG：如果 embedding 可用，追加语义相关 tag
            rag_section = await self._build_rag_reference_section(message)
            if rag_section:
                reference_section = rag_section + reference_section

            prompt = TAG_EXTRACTION_PROMPT.format(
                sender=sender or "unknown",
                message=message[:800],
                reference_section=reference_section,
            )

            response = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是一个记忆标注系统，只输出 JSON 数组，不输出其他内容。",
            )

            if not response or not response.completion_text:
                logger.debug(f"[WaveMemory] Tag LLM returned empty response")
                return self._fallback_extract(message)

            logger.info(f"[WaveMemory] Tag LLM response (first 200): {response.completion_text[:200]}")
            tags = self._parse_structured_tags(response.completion_text)
            self._update_frequency(tags)
            return tags

        except Exception as e:
            import traceback
            logger.warning(f"[WaveMemory] Tag extraction error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return self._fallback_extract(message)

    async def extract_tags_batch(self, messages: list[dict]) -> list[list[dict]]:
        """批量提取多条消息的 Tag（一次 LLM 调用）。

        Args:
            messages: [{"content": str, "sender": str}, ...]

        Returns:
            [[tags_for_msg1], [tags_for_msg2], ...]
        """
        if not messages:
            return []

        if not self.provider_id:
            return [self._fallback_extract(m.get("content", "")) for m in messages]

        # 构建一个 JSON 文档作为批处理输入，避免靠自然语言编号对齐
        batch_items = []
        ids = []
        for i, msg in enumerate(messages, 1):
            mem_id = msg.get("id", i)
            ids.append(mem_id)
            batch_items.append({
                "id": mem_id,
                "sender": msg.get("sender", "unknown"),
                "content": msg.get("content", "")[:700],
            })

        batch_doc = {
            "batch_size": len(batch_items),
            "items": batch_items,
        }
        batch_json = json.dumps(batch_doc, ensure_ascii=False, separators=(",", ":"))

        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                return [self._fallback_extract(m.get("content", "")) for m in messages]

            reference_section = self._build_reference_section()

            # Tag RAG：用批量消息的拼接文本搜索语义相关 tag
            combined_text = " ".join(m.get("content", "")[:200] for m in messages)
            rag_section = await self._build_rag_reference_section(combined_text[:500])
            if rag_section:
                reference_section = rag_section + reference_section

            prompt = BATCH_TAG_PROMPT.format(
                batch_json=batch_json,
                reference_section=reference_section,
            )
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是一个记忆标注系统，只输出严格 JSON 对象，不输出 markdown 或解释。",
            )

            if not response or not response.completion_text:
                return [self._fallback_extract(m.get("content", "")) for m in messages]

            logger.info(f"[WaveMemory] Batch Tag LLM response (first 200): {response.completion_text[:200]}")
            return self._parse_batch_response(response.completion_text, len(messages), ids=ids)

        except Exception as e:
            logger.debug(f"[WaveMemory] Batch tag extraction failed: {e}")
            return [self._fallback_extract(m.get("content", "")) for m in messages]

    def _parse_structured_tags(self, text: str) -> list[dict]:
        """解析 LLM 返回的结构化 JSON Tag。"""
        text = text.strip()

        # 移除 markdown 代码块标记
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
        text = text.strip()

        # 尝试提取 JSON 数组
        json_match = re.search(r'\[[\s\S]*\]', text)
        if not json_match:
            # fallback: 尝试逗号分隔的纯文本
            return self._parse_plain_tags(text)

        try:
            raw_tags = json.loads(json_match.group())
        except json.JSONDecodeError:
            return self._parse_plain_tags(text)

        if not isinstance(raw_tags, list):
            return []

        tags = []
        for item in raw_tags:
            if not isinstance(item, dict):
                continue

            # 兼容多种 key 格式（有些 LLM 会返回带引号的 key）
            name = item.get("name") or item.get('"name"') or item.get("tag") or item.get("label") or ""
            name = str(name).strip().strip('"')
            tag_type = item.get("type") or item.get('"type"') or "keyword"
            tag_type = str(tag_type).strip().strip('"').lower()
            confidence = item.get("confidence") or item.get('"confidence"') or 0.8
            try:
                confidence = float(str(confidence).strip().strip('"'))
            except (ValueError, TypeError):
                confidence = 0.8

            # 归一化 tag 名
            name = self._normalize_tag_name(name)

            # 质量过滤
            if not self._is_valid_tag(name, tag_type, confidence):
                continue

            if tag_type not in VALID_TAG_TYPES:
                tag_type = "keyword"

            tags.append({
                "name": name,
                "type": tag_type,
                "confidence": min(max(confidence, 0.0), 1.0),
            })

        return tags[:self.max_tags]

    def _parse_batch_response(self, text: str, count: int, ids: list | None = None) -> list[list[dict]]:
        """解析批量提取的 JSON 响应。

        新格式：{"items": [{"id": memory_id, "tags": [{name,type,score}]}]}
        兼容旧格式：{"1": [{name,type,confidence}], "2": [...]}。
        """
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)

        json_match = re.search(r'\{[\s\S]*\}', text)
        if not json_match:
            return [[] for _ in range(count)]

        try:
            raw = json.loads(json_match.group())
        except json.JSONDecodeError:
            return [[] for _ in range(count)]

        def parse_tags(msg_tags) -> list[dict]:
            parsed = []
            if not isinstance(msg_tags, list):
                return parsed
            for item in msg_tags:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                tag_type = str(item.get("type", "keyword")).strip().lower()
                score = item.get("score", item.get("confidence", 0.8))
                try:
                    confidence = float(score)
                except (TypeError, ValueError):
                    confidence = 0.8
                confidence = min(max(confidence, 0.0), 1.0)
                # 归一化
                name = self._normalize_tag_name(name)
                if self._is_valid_tag(name, tag_type, confidence):
                    if tag_type not in VALID_TAG_TYPES:
                        tag_type = "keyword"
                    parsed.append({"name": name, "type": tag_type, "confidence": confidence})
            return parsed[:self.max_tags]

        results = [[] for _ in range(count)]

        # 新格式：按 id 精确对齐
        if isinstance(raw.get("items"), list) and ids is not None:
            index_by_id = {str(mem_id): idx for idx, mem_id in enumerate(ids)}
            for item in raw["items"]:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id", ""))
                if item_id in index_by_id:
                    results[index_by_id[item_id]] = parse_tags(item.get("tags", []))
        else:
            # 旧格式：按 1..N 顺序兼容
            for i in range(1, count + 1):
                results[i - 1] = parse_tags(raw.get(str(i), []))

        for tags in results:
            self._update_frequency(tags)

        return results

    def _is_valid_tag(self, name: str, tag_type: str, confidence: float) -> bool:
        """Tag 质量验证。"""
        if not name or len(name) < 2 or len(name) > 20:
            return False
        if confidence < 0.4:
            return False
        # 黑名单过滤
        if name in self._blacklist:
            return False
        # 过滤纯数字、纯标点、纯空白
        if re.match(r'^[\d\s\W]+$', name):
            return False
        # 过滤过于宽泛的词
        stop_words = {"东西", "事情", "问题", "情况", "感觉", "觉得", "可能", "应该", "这个", "那个", "什么"}
        if name in stop_words:
            return False
        # 过滤碎片句子（超过 4 个字且包含动词结构的可能是句子而非标签）
        if len(name) > 8 and any(c in name for c in "的了吗呢吧啊呀"):
            return False
        return True

    def _parse_plain_tags(self, text: str) -> list[dict]:
        """Fallback: 解析纯文本逗号分隔的标签。"""
        text = text.strip().strip("。.，,")
        tags = re.split(r'[,，、;；|｜\n]', text)

        result = []
        for tag in tags:
            tag = tag.strip().strip('"\'')
            tag = self._normalize_tag_name(tag)
            if self._is_valid_tag(tag, "keyword", 0.7):
                result.append({"name": tag, "type": "keyword", "confidence": 0.7})

        return result[:self.max_tags]

    def _fallback_extract(self, message: str) -> list[dict]:
        """无 LLM 时的规则提取。"""
        if not message or len(message) < 6:
            return []

        # 提取中文词组和英文单词
        words = re.findall(r'[\u4e00-\u9fff]{2,6}|[a-zA-Z]{3,15}', message)

        seen = set()
        result = []
        for w in words:
            w = self._normalize_tag_name(w)
            if w.lower() not in seen and self._is_valid_tag(w, "keyword", 0.5):
                seen.add(w.lower())
                result.append({"name": w, "type": "keyword", "confidence": 0.5})

        return result[:5]

    def _update_frequency(self, tags: list[dict]):
        """更新 Tag 频率统计，识别核心标签。"""
        for tag in tags:
            name = tag["name"]
            self._tag_freq[name] = self._tag_freq.get(name, 0) + 1
            # 出现 5 次以上自动升级为核心标签
            if self._tag_freq[name] >= 5:
                self._core_tags.add(name)

    @property
    def core_tags(self) -> set:
        """返回当前识别的核心标签集合。"""
        return self._core_tags

    def is_core_tag(self, name: str) -> bool:
        """判断是否为核心标签。"""
        return name in self._core_tags
