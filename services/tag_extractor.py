"""Wave Memory Tag 提取器 — 异步后台用 LLM 从消息中提取关键标签"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import numpy as np

from astrbot.api import logger


TAG_EXTRACTION_PROMPT = """从以下消息中提取 3-8 个关键标签（Tag），用于记忆检索。

要求：
- 提取人名、事件、话题、情绪、地点等关键信息
- 每个标签 2-6 个字，简洁精准
- 用逗号分隔，不要编号，不要解释
- 如果消息太短或无意义（如"嗯""哦""草"），返回空

消息：{message}

标签："""


class TagExtractor:
    """异步 Tag 提取服务，后台运行不阻塞回复。"""

    def __init__(self, context, provider_id: str, max_tags: int = 10):
        self.context = context
        self.provider_id = provider_id
        self.max_tags = max_tags

    async def extract_tags(self, message: str) -> list[str]:
        """从消息中提取 Tag 列表。"""
        if not self.provider_id:
            return self._fallback_extract(message)

        if len(message.strip()) < 6:
            return []

        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                return self._fallback_extract(message)

            prompt = TAG_EXTRACTION_PROMPT.format(message=message[:500])

            response = await provider.text_chat(
                prompt=prompt,
                contexts=[],
            )

            if not response or not response.completion_text:
                return self._fallback_extract(message)

            return self._parse_tags(response.completion_text)

        except Exception as e:
            logger.debug(f"[WaveMemory] Tag extraction failed: {e}")
            return self._fallback_extract(message)

    def _parse_tags(self, text: str) -> list[str]:
        """解析 LLM 返回的标签文本。"""
        # 清理
        text = text.strip().strip("。.，,")

        # 分割（支持逗号、顿号、分号、竖线）
        tags = re.split(r"[,，、;；|｜\n]", text)

        # 清洗
        cleaned = []
        for tag in tags:
            tag = tag.strip().strip("#").strip("- ").strip("\"'")
            # 长度限制
            if len(tag) < 2 or len(tag) > 15:
                continue
            # 过滤纯数字、纯标点
            if re.match(r"^[\d\s\W]+$", tag):
                continue
            cleaned.append(tag)

        return cleaned[: self.max_tags]

    def _fallback_extract(self, message: str) -> list[str]:
        """无 LLM 时的简单规则提取。"""
        if len(message) < 6:
            return []

        # 简单分词：提取中文词组和英文单词
        # 这只是 fallback，精度不高但不调 LLM
        words = re.findall(r"[\u4e00-\u9fff]{2,6}|[a-zA-Z]{3,15}", message)

        # 去重保序
        seen = set()
        result = []
        for w in words:
            if w.lower() not in seen:
                seen.add(w.lower())
                result.append(w)

        return result[:5]
