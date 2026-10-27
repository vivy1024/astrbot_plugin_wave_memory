"""MetaThinking — Bot 的兴趣词与 @ 刷屏状态持有者。

旧的"先想一下再决定是否回复 / 主动插话"判断与写 user_profiles 好感的链路已删除；
现在只提供兴趣词匹配（is_interesting）、@ 频率窗口（供 app/injection.py 刷屏检测）
以及按 Bot 取模型客户端（_llm_for / llm，供模型分工展示与测试）。
"""

import json
import re
from typing import Any

import logging; logger = logging.getLogger("wavememory")

from .llm_fallback import LLMFallbackClient
from .llm_routing import legacy_meta_chain


# 兜底硬规则
EXTREME_ATTACK = re.compile(r'(你[妈马]|nmsl|死[全妈]|全家|操你|fuck\s*you|滚去死|杀了你|弄死你)')


class MetaThinking:
    """配置驱动的内心判断层 — 支持多 bot 身份。"""

    # 通用兴趣词（所有 bot 通用）
    _BASE_INTERESTS = frozenset(['好感度'])

    # 过滤掉的泛化标签（不作为触发器）
    BORING_TAGS = frozenset([
        '群内互动', '用户互动', '群友互怼', '群内冲突', '群内玩梗',
        '人身攻击', '日常闲聊', '闲聊', '灌水',
    ])

    def __init__(
        self,
        db,
        context,
        bot_qq_id: str = "",
        bot_qq_ids: list[str] = None,
        bot_prompts: dict[str, str] = None,
        bot_names: dict[str, str] = None,
        bot_db_ids: dict[str, str] = None,
        admin_ids: list[str] = None,
        config: dict | None = None,
        global_fallback_ids: str | list[str] | None = None,
        extra_interests: list[str] = None,
        llm_client: Any = None,
    ):
        self.db = db
        self.context = context
        self.bot_qq_id = bot_qq_id
        self.bot_qq_ids = set(bot_qq_ids or [bot_qq_id]) - {""}
        # 每个 bot 可以有自己的 MetaThinking prompt；没设置的用默认
        self.bot_prompts = bot_prompts or {}
        # bot_id → 显示名映射（用于生成回复时的身份选择）
        self.bot_names = bot_names or {}
        # bot_qq_id → db_id 映射（用于数据库 user_profiles.bot_id 写入）
        self.bot_db_ids = bot_db_ids or {bid: name.lower() for bid, name in self.bot_names.items()}
        self.admin_ids = set(admin_ids or [])
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.spam_threshold = int(self.config.get("spam_threshold", 6))
        self.spam_window_seconds = int(self.config.get("spam_window_seconds", 60))
        self.interest_sample_size = int(self.config.get("interest_sample_size", 20))

        # 兴趣词：基础通用词 + 从 bot 配置注入的关键词
        self.FIXED_INTERESTS = self._BASE_INTERESTS | frozenset(extra_interests or [])

        # Provider 链：插件启动时由模型分工（services.llm_routing，任务 meta_thinking）注入路由客户端；
        # 单独构造时沿用旧逻辑：优先 default_model，fallback 到旧格式 provider_1/2/3。
        if llm_client is None:
            llm_client = LLMFallbackClient(
                self.context,
                legacy_meta_chain(self.config, global_fallback_ids),
                log_prefix="[MetaThinking]",
            )
        self.llm = llm_client

        # @ 频率追踪（app/injection.py 的刷屏检测读写）
        self._at_timestamps: dict[str, list[float]] = {}  # sender_id → [timestamps]

        # 兴趣关键词（从 DB 加载高频标签 + 固定词）
        self._interest_keywords: set[str] = set(self.FIXED_INTERESTS)
        self._load_interest_keywords()

    @property
    def provider_ids(self) -> list[str]:
        """当前回退链（路由客户端按模型分工现读）。"""
        return list(getattr(self.llm, "provider_ids", None) or [])

    def _llm_for(self, bot_id: str | None = None):
        """按 Bot 取客户端：路由客户端支持按 Bot 覆盖模型（Bot Profile 的 model / models）。"""
        qq_id = bot_id or self.bot_qq_id
        db_id = self.bot_db_ids.get(qq_id, "") if qq_id else ""
        for_bot = getattr(self.llm, "for_bot", None)
        return for_bot(db_id) if callable(for_bot) and db_id else self.llm

    def update_bots(
        self,
        *,
        bot_qq_ids: list[str],
        bot_prompts: dict[str, str],
        bot_names: dict[str, str],
        bot_db_ids: dict[str, str],
        extra_interests: list[str] | None = None,
    ) -> None:
        """Bot 注册表热重载：替换身份映射，保留 @ 频率与主动对话计数。"""
        self.bot_qq_ids = set(bot_qq_ids or []) - {""}
        if self.bot_qq_id not in self.bot_qq_ids:
            self.bot_qq_id = next(iter(bot_qq_ids), "") if bot_qq_ids else ""
        self.bot_prompts = dict(bot_prompts or {})
        self.bot_names = dict(bot_names or {})
        self.bot_db_ids = dict(bot_db_ids or {})
        fixed = self._BASE_INTERESTS | frozenset(extra_interests or [])
        self._interest_keywords.difference_update(self.FIXED_INTERESTS - fixed)
        self._interest_keywords.update(fixed)
        self.FIXED_INTERESTS = fixed

    def _load_interest_keywords(self):
        """从 memory_tags 加载高频标签 + 从 kv_store 加载自定义兴趣词。"""
        try:
            # 高频标签
            rows = self.db.conn.execute('''
                SELECT t.name FROM memory_tags mt 
                JOIN tags t ON mt.tag_id = t.id 
                GROUP BY t.id HAVING COUNT(*) > 10
                ORDER BY COUNT(*) DESC LIMIT 80
            ''').fetchall()
            for (name,) in rows:
                if name not in self.BORING_TAGS and len(name) >= 2:
                    self._interest_keywords.add(name)

            # 自定义兴趣词（Bot 自己添加的）
            row = self.db.conn.execute(
                "SELECT value FROM kv_store WHERE key = 'meta_thinking_interests'"
            ).fetchone()
            if row and row[0]:
                custom = json.loads(row[0])
                self._interest_keywords.update(custom.get("add", []))
                for rm in custom.get("remove", []):
                    self._interest_keywords.discard(rm)

            logger.info(f"[MetaThinking] 兴趣关键词: {len(self._interest_keywords)} 个")
        except Exception as e:
            logger.warning(f"[MetaThinking] 加载兴趣词失败: {e}")

    def is_interesting(self, message: str) -> bool:
        """判断一条群消息是否触发 Bot 的兴趣（轻量匹配，不调 LLM）。"""
        if not message:
            return False
        # 超长消息只取前 500 字做匹配，避免热路径上对长文本反复扫描
        msg_lower = message[:500].lower()
        for kw in self._interest_keywords:
            if kw in msg_lower:
                return True
        return False
