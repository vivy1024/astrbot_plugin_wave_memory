"""BeliefEngine — 信念系统核心

从 consolidation 摘要中提取稳定判断，维护信念的强化/动摇生命周期，
查询时注入相关信念作为 bot 的"底色"。
"""

from __future__ import annotations

import json
import time
from typing import Optional

from astrbot.api import logger

from ..engine.database import WaveMemoryDB
from .llm_fallback import LLMFallbackClient
from .identity_safety import is_identity_contamination


EXTRACT_PROMPT = """分析以下记忆摘要，提取 0-2 条**高质量**稳定判断（宁缺毋滥，没有就返回 []）。

稳定判断 = 反复出现的模式、对某人/某事的一致性看法、或对自己的认知。
不是事实陈述（"今天下雨"不是），是主观判断（"这个人说话不可信"是）。

【严格排除以下情况，命中则不要提取】
1. 跑团/角色扮演/小说情节：TRPG、COC、DND、模组、剧透、"角色""设定""模组""队友当储备粮"等。
   这些是**虚构游戏内行为**，绝不能当成对真人的判断。
   例：群友在玩跑团说"搜刮尸体"，这是游戏行为，不是"此人是逐利狂"。
2. 实体边界错误：主语必须是**清晰的真实人物/群体名**，不能把定语黏进昵称
   （如"在雪山救了白狐的感恩芒果"应是"感恩芒果"），不能拿群名/区名/书名当人。
3. 琐碎偏好：口味、零食、表情等无意义细节（如"喜欢炒饭配玉米"）一律不提取。
4. 来自小说/书籍内化的世界观（书名、虚构地名人名），不是真实社交判断。

【只提取】对真实群友的稳定社交判断、bot 真实的自我认知、反复验证的真实世界观。

记忆摘要：
{summary}

已有信念（避免重复或与之矛盾的也列出来）：
{existing_beliefs}

输出格式（JSON 数组，没有合格的就返回 []）：
[{{"content": "一句话判断（主语是真实人物/自己）", "type": "person_judgment|world_view|self_identity|preference", "challenges": []}}]

type 说明：
- person_judgment: 对某个真实群友的判断（如"斯扎拉克对跑团细节要求严格"）
- world_view: 对真实世界/事物的看法
- self_identity: 对自己的认知（如"我不喜欢被当成工具"）
- preference: bot 自己的重要偏好（非琐碎口味）

challenges: 如果这条新判断与已有信念矛盾，列出矛盾信念的 ID。

只返回 JSON，不要其他文字。"""


class BeliefEngine:
    """信念系统 — 提取、维护、注入。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        llm_client: LLMFallbackClient,
        bot_id: str,
        max_beliefs: int = 50,
    ):
        self.db = db
        self.llm = llm_client
        self.bot_id = bot_id
        self.max_beliefs = max_beliefs

    async def extract_from_summary(self, summary: str, source_memory_ids: list[int] = None) -> list[dict]:
        """从 consolidation 摘要中提取信念。返回新增的信念列表。"""
        if not summary or len(summary) < 20:
            return []
        if is_identity_contamination(summary):
            logger.info("[BeliefEngine] Skip identity roleplay contaminated summary")
            return []

        # 获取已有信念作为去重参考
        existing = self.db.get_beliefs(bot_id=self.bot_id, limit=30)
        existing_text = "\n".join(
            f"[ID:{b['id']}] {b['content']} (type={b['type']}, strength={b['strength']:.0%})"
            for b in existing
        ) or "（暂无）"

        prompt = EXTRACT_PROMPT.format(summary=summary[:1000], existing_beliefs=existing_text)

        try:
            resp = await self.llm.text_chat(prompt=prompt)
            text = resp.completion_text.strip()

            # 解析 JSON
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            beliefs_data = json.loads(text)

            if not isinstance(beliefs_data, list):
                return []

            new_beliefs = []
            for item in beliefs_data[:2]:  # 最多 2 条
                content = item.get("content", "").strip()
                belief_type = item.get("type", "world_view")
                challenges = item.get("challenges", [])

                if not content or len(content) < 5:
                    continue
                if is_identity_contamination(content):
                    continue
                if belief_type not in ("person_judgment", "world_view", "self_identity", "preference"):
                    belief_type = "world_view"

                # 去重：检查是否已有非常相似的信念
                if self._is_duplicate(content, existing):
                    # 如果内容相似，强化已有信念而非新建
                    similar = self._find_similar(content, existing)
                    if similar:
                        self.db.reinforce_belief(similar["id"])
                        if source_memory_ids:
                            for mid in source_memory_ids[:5]:
                                self.db.add_belief_source(similar["id"], mid)
                    continue

                # consolidation 摘要只能产生 legacy 待审信念；active 信念必须来自经历/关系事件涌现
                belief_id = self.db.add_belief(
                    content=content,
                    belief_type=belief_type,
                    bot_id=self.bot_id,
                    strength=0.4,  # 初始强度较低，需要多次强化
                    sources=source_memory_ids[:10] if source_memory_ids else [],
                    status="pending_legacy",
                )
                new_beliefs.append({"id": belief_id, "content": content, "type": belief_type})

                # 处理冲突
                for conflict_id in challenges:
                    if isinstance(conflict_id, int):
                        self.db.weaken_belief(conflict_id, amount=0.15)

                logger.info(f"[BeliefEngine] New belief: {content[:50]}... (type={belief_type})")

            return new_beliefs

        except json.JSONDecodeError:
            logger.debug("[BeliefEngine] Failed to parse LLM output as JSON")
            return []
        except Exception as e:
            logger.debug(f"[BeliefEngine] Extract failed: {e}")
            return []

    def get_injection(self, sender_id: str = None, keywords: list[str] = None) -> str:
        """获取与当前对话相关的信念注入文本。

        只注入 status='active' 的信念，排除 pending_legacy（LLM 摘要提取的待审信念）。
        """
        beliefs = []

        # 1. 自我认知（始终注入）— get_beliefs 默认 status='active'，排除 pending_legacy
        beliefs += self.db.get_beliefs(bot_id=self.bot_id, belief_type="self_identity", limit=3)

        # 2. 对特定人的判断
        if sender_id:
            person_beliefs = self.db.search_beliefs([sender_id], bot_id=self.bot_id, limit=2)
            beliefs += person_beliefs

        # 3. 与话题相关的世界观/偏好
        if keywords:
            topic_beliefs = self.db.search_beliefs(keywords[:3], bot_id=self.bot_id, limit=3)
            beliefs += topic_beliefs

        # 去重 + 排除 pending_legacy（防御层：DB 层已过滤 status='active'）
        seen_ids = set()
        unique_beliefs = []
        for b in beliefs:
            if b["id"] not in seen_ids:
                seen_ids.add(b["id"])
                # 只注入 active 信念，pending_legacy 不注入
                if b.get("status") == "pending_legacy":
                    continue
                unique_beliefs.append(b)

        # 按 strength 阈值过滤：挡掉被动摇到很低的低质信念（已批准 active 默认 0.4 仍通过）
        _MIN_INJECT_STRENGTH = 0.35
        unique_beliefs = [b for b in unique_beliefs if (b.get("strength") or 0) >= _MIN_INJECT_STRENGTH]

        if not unique_beliefs:
            return ""

        lines = ["<beliefs>"]
        for b in unique_beliefs[:5]:
            strength_label = "确信" if b["strength"] > 0.7 else "觉得" if b["strength"] > 0.4 else "隐约觉得"
            lines.append(f"- {strength_label}：{b['content']}")
        lines.append("</beliefs>")
        return "\n".join(lines)

    def _is_duplicate(self, content: str, existing: list[dict]) -> bool:
        """简单文本相似度去重。"""
        content_lower = content.lower()
        for b in existing:
            existing_lower = b["content"].lower()
            # 简单 Jaccard
            words_new = set(content_lower)
            words_old = set(existing_lower)
            if len(words_new & words_old) / max(len(words_new | words_old), 1) > 0.6:
                return True
        return False

    def _find_similar(self, content: str, existing: list[dict]) -> Optional[dict]:
        """找到最相似的已有信念。"""
        content_lower = content.lower()
        best = None
        best_score = 0
        for b in existing:
            existing_lower = b["content"].lower()
            words_new = set(content_lower)
            words_old = set(existing_lower)
            score = len(words_new & words_old) / max(len(words_new | words_old), 1)
            if score > best_score:
                best_score = score
                best = b
        return best if best_score > 0.6 else None
