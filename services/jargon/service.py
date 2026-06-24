"""黑话服务 — 挖掘调度 + DB 读写 + 跨群合并 (US-4.1~4.5)"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .statistical_filter import JargonStatisticalFilter
from .inference import JargonInferenceEngine, JargonInjector
from .holyman_reference import HolymanReference


class JargonService:
    """黑话系统主服务。"""

    def __init__(self, db: Any, llm_client: Any = None, enabled: bool = True, config: dict = None):
        self._db = db
        self._enabled = enabled
        self._config = config or {}
        self._llm = llm_client

        # 从配置读取参数（改动4：全部参数配置化）
        self._min_frequency = int(self._config.get("min_frequency", 5))
        self._global_threshold = int(self._config.get("global_threshold", 3))
        self._min_messages = int(self._config.get("min_messages", 10))
        self._mine_cooldown = int(self._config.get("mine_cooldown", 20))
        self._top_k = int(self._config.get("top_k", 20))
        self._max_context = int(self._config.get("max_context", 15))
        self._context_keep = int(self._config.get("context_keep", 10))
        self._window_days = int(self._config.get("window_days", 7))
        self._jieba_threshold = int(self._config.get("jieba_threshold", 100))
        _llm_validate_cfg = self._config.get("llm_validate", True)
        self._llm_validate = True if _llm_validate_cfg is None else bool(_llm_validate_cfg)
        self._confidence_threshold = float(self._config.get("confidence_threshold", 0.5))
        self._holyman = HolymanReference(self._config.get("holyman_path") or None) if self._config.get("holyman_enabled", True) else None

        # 递进推断阈值（改动1）
        thresholds_str = str(self._config.get("inference_thresholds", "3,6,10,20,40,60,100"))
        self._inference_thresholds = [int(x.strip()) for x in thresholds_str.split(",") if x.strip()]

        # 权重参数
        self._weight_idf = float(self._config.get("weight_idf", 0.4))
        self._weight_burst = float(self._config.get("weight_burst", 0.3))
        self._weight_concentration = float(self._config.get("weight_concentration", 0.3))

        # 构造子组件，传入配置
        self._filter = JargonStatisticalFilter(
            context_keep=self._context_keep,
            window_days=self._window_days,
            jieba_threshold=self._jieba_threshold,
            weight_idf=self._weight_idf,
            weight_burst=self._weight_burst,
            weight_concentration=self._weight_concentration,
        )
        self._inference = JargonInferenceEngine(llm_client, max_context=self._max_context) if llm_client else None
        self._injector = JargonInjector(db, max_inject=int(self._config.get("max_inject", 3)))
        # 挖掘冷却: group_id -> last_mine_ts
        self._last_mine: Dict[str, float] = {}
        # 消息计数: group_id -> count since last mine
        self._msg_count: Dict[str, int] = {}

        # 确保 jargon 表存在
        self._ensure_table()

        # 预热标记（延迟到首次 feed_message 时触发，避免阻塞插件加载）
        self._warmed_up = False

    def _warmup_from_memories(self, days: int = 7, max_rows: int = 20000) -> None:
        """从近 N 天 memories 重放消息，重建内存词频统计。"""
        cutoff = time.time() - days * 86400
        rows = self._db.conn.execute(
            """SELECT content, group_id, sender_id FROM memories
               WHERE timestamp >= ? AND group_id IS NOT NULL AND group_id != ''
               ORDER BY timestamp DESC LIMIT ?""",
            (cutoff, max_rows),
        ).fetchall()
        n = 0
        for content, group_id, sender_id in rows:
            if content and group_id:
                self._filter.feed(content, str(group_id), str(sender_id or ""))
                n += 1
        if n:
            groups = len(self._filter._group_freq)
            logger.info(f"[Jargon] 预热完成：重放 {n} 条消息，覆盖 {groups} 个群的词频")

    def _ensure_table(self) -> None:
        """创建 jargon 表（如不存在）。"""
        try:
            self._db.conn.execute("""
                CREATE TABLE IF NOT EXISTS jargon (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL,
                    meaning TEXT DEFAULT '',
                    is_jargon INTEGER DEFAULT NULL,
                    frequency INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0,
                    is_global INTEGER DEFAULT 0,
                    group_id TEXT NOT NULL,
                    contexts TEXT DEFAULT '[]',
                    created_at INTEGER,
                    updated_at INTEGER,
                    status TEXT DEFAULT 'pending',
                    scope TEXT DEFAULT 'local',
                    source TEXT DEFAULT 'wave_memory',
                    last_infer_freq INTEGER DEFAULT 0,
                    reject_reason TEXT,
                    UNIQUE(group_id, word)
                )
            """)
            cols = {r[1] for r in self._db.conn.execute("PRAGMA table_info(jargon)").fetchall()}
            for col, ddl in {
                "status": "TEXT DEFAULT 'pending'",
                "scope": "TEXT DEFAULT 'local'",
                "source": "TEXT DEFAULT 'wave_memory'",
                "last_infer_freq": "INTEGER DEFAULT 0",
                "reject_reason": "TEXT",
            }.items():
                if col not in cols:
                    self._db.conn.execute(f"ALTER TABLE jargon ADD COLUMN {col} {ddl}")
            self._db.conn.commit()
        except Exception as e:
            logger.debug(f"[Jargon] table ensure: {e}")

    # ─── 公开接口 ───

    def feed_message(self, text: str, group_id: str, sender_id: str = "") -> None:
        """喂入一条消息（由 on_message 调用）。"""
        if not self._enabled:
            return
        # 首次调用时触发延迟预热（避免阻塞插件加载）
        if not self._warmed_up:
            self._warmed_up = True
            try:
                self._warmup_from_memories(days=3, max_rows=10000)
            except Exception as e:
                logger.debug(f"[Jargon] warmup skipped: {e}")
        self._filter.feed(text, group_id, sender_id)
        self._msg_count[group_id] = self._msg_count.get(group_id, 0) + 1

    def should_mine(self, group_id: str) -> bool:
        """判断是否应该触发挖掘（min_messages 条消息 + mine_cooldown 冷却）。"""
        count = self._msg_count.get(group_id, 0)
        if count < self._min_messages:
            return False
        last = self._last_mine.get(group_id, 0)
        if time.time() - last < self._mine_cooldown:
            return False
        return True

    async def mine(self, group_id: str) -> List[Dict]:
        """执行一次挖掘：统计筛选 + (可选)LLM验证 + LLM 推断。返回新发现的黑话列表。"""
        if not self._enabled:
            return []

        self._msg_count[group_id] = 0
        self._last_mine[group_id] = time.time()

        # Step 1: 统计候选
        candidates = self._filter.get_candidates(group_id, min_freq=self._min_frequency, top_k=self._top_k)
        if not candidates:
            return []

        logger.info(f"[Jargon] 挖掘 {group_id}: {len(candidates)} 候选")

        # Step 1.5: LLM 批量验证（改动3，开关控制）
        if self._llm_validate and self._llm and candidates:
            candidates = await self._llm_validate_candidates(candidates, group_id)
            if not candidates:
                return []
            logger.info(f"[Jargon] LLM 验证通过: {len(candidates)} 候选")

        results = []
        now = int(time.time())

        for cand in candidates:
            word = cand["word"]
            contexts = cand["contexts"]

            # 检查是否已存在
            existing = self._db.conn.execute(
                "SELECT id, frequency, is_jargon, last_infer_freq FROM jargon WHERE group_id = ? AND word = ?",
                (group_id, word),
            ).fetchone()

            if existing:
                row_id, old_freq, old_is_jargon, last_infer_freq = existing
                current_freq = cand["frequency"]
                last_infer_freq = last_infer_freq or 0

                # 改动1：递进重推机制
                if self._should_reinfer(current_freq, last_infer_freq):
                    logger.debug(f"[Jargon] 重推 '{word}': freq {last_infer_freq} → {current_freq}")
                    is_jargon = None
                    meaning = ""
                    confidence = 0.0

                    if self._inference and contexts:
                        try:
                            result = await self._inference.infer(word, contexts)
                            is_jargon = result.get("is_jargon")
                            meaning = result.get("meaning", "")
                            confidence = result.get("confidence", 0.0)
                        except Exception as e:
                            logger.debug(f"[Jargon] reinfer error for '{word}': {e}")

                    status = "confirmed" if is_jargon is True and meaning and confidence >= self._confidence_threshold else ("rejected" if is_jargon is False else "pending")
                    reject_reason = "llm_inference_rejected" if status == "rejected" else None
                    # 更新 DB：frequency + meaning + is_jargon + status + last_infer_freq + reject_reason
                    if reject_reason:
                        self._db.conn.execute(
                            """UPDATE jargon SET frequency = ?, meaning = ?, is_jargon = ?,
                               confidence = ?, status = ?, reject_reason = ?, last_infer_freq = ?, updated_at = ? WHERE id = ?""",
                            (current_freq, meaning, is_jargon, confidence, status, reject_reason, current_freq, now, row_id),
                        )
                    else:
                        self._db.conn.execute(
                            """UPDATE jargon SET frequency = ?, meaning = ?, is_jargon = ?,
                               confidence = ?, status = ?, reject_reason = NULL, last_infer_freq = ?, updated_at = ? WHERE id = ?""",
                            (current_freq, meaning, is_jargon, confidence, status, current_freq, now, row_id),
                        )

                    if is_jargon:
                        results.append({"word": word, "meaning": meaning, "confidence": confidence})
                else:
                    # 仅更新频率
                    self._db.conn.execute(
                        "UPDATE jargon SET frequency = ?, updated_at = ? WHERE id = ?",
                        (current_freq, now, row_id),
                    )
                continue

            # 新候选：先尝试 holyman 广域抽象文化参考，再尝试 LLM 推断
            is_jargon = None
            meaning = ""
            confidence = 0.0
            status = "pending"
            scope = "local"
            source = "wave_memory"

            holyman_match = self._holyman.match(word, "\n".join(contexts)) if self._holyman else {"matched": False}
            if holyman_match.get("matched"):
                is_jargon = 1
                meaning = holyman_match.get("explanation", "")
                confidence = float(holyman_match.get("confidence", 0.0))
                status = "confirmed"
                scope = "global"
                source = "holyman_skills"

            if is_jargon is None and self._inference and contexts:
                try:
                    result = await self._inference.infer(word, contexts)
                    is_jargon = result.get("is_jargon")
                    meaning = result.get("meaning", "")
                    confidence = result.get("confidence", 0.0)
                    if is_jargon is True and meaning and confidence >= self._confidence_threshold:
                        status = "confirmed"
                    elif is_jargon is False:
                        status = "rejected"
                    else:
                        status = "pending"
                except Exception as e:
                    logger.debug(f"[Jargon] inference error for '{word}': {e}")

            reject_reason = "llm_inference_rejected" if status == "rejected" else None

            # 写入 DB（last_infer_freq = 当前频次）
            self._db.conn.execute(
                """INSERT OR IGNORE INTO jargon
                   (word, meaning, is_jargon, frequency, confidence, group_id, contexts,
                    last_infer_freq, created_at, updated_at, status, scope, source, reject_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (word, meaning, is_jargon, cand["frequency"], confidence, group_id,
                 json.dumps(contexts, ensure_ascii=False), cand["frequency"], now, now, status, scope, source, reject_reason),
            )

            if is_jargon:
                results.append({"word": word, "meaning": meaning, "confidence": confidence})

        self._db.conn.commit()

        # US-4.5: 跨群检查（同词在 >= 3 群确认 → 全局）
        self._check_global_promotion()

        return results

    def _should_reinfer(self, current_freq: int, last_infer_freq: int) -> bool:
        """判断是否需要重新推断（改动1：递进重推机制）。"""
        for threshold in self._inference_thresholds:
            if last_infer_freq < threshold <= current_freq:
                return True
        return False

    async def _llm_validate_candidates(self, candidates: List[Dict], group_id: str) -> List[Dict]:
        """改动3：LLM 批量验证候选词是否真是黑话。"""
        # 构建近期聊天片段
        all_contexts = []
        for cand in candidates:
            all_contexts.extend(cand.get("contexts", []))
        chat_snippet = "\n".join(f"- {c}" for c in all_contexts[:20])

        term_list = ", ".join(cand["word"] for cand in candidates)

        validate_prompt = f"""**近期聊天片段**
{chat_snippet}

**候选词列表**
{term_list}

请判断以上候选词中，哪些是该群组的黑话/俚语/暗语/缩写。

必须同时满足：
- 脱离该群组语境后普通人无法理解
- 在近期聊天中有明确上下文支撑
- 不是普通词、昵称、人名、品牌名

以JSON数组输出确认是黑话的词条：["词1", "词2"]
如果没有，输出空数组 []"""

        try:
            import re
            response = await self._llm.text_chat(prompt=validate_prompt)
            if not response or not response.completion_text:
                return []  # fail-closed：LLM 失败不新增候选
            text = response.completion_text.strip()
            # 提取 JSON 数组
            text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
            text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
            match = re.search(r'\[[\s\S]*?\]', text)
            if match:
                validated_words = set(json.loads(match.group()))
                return [c for c in candidates if c["word"] in validated_words]
        except Exception as e:
            logger.debug(f"[Jargon] LLM validate error: {e}")

        return []  # fail-closed：解析失败不新增候选

    def get_injection(self, text: str, group_id: str) -> str:
        """获取黑话注入文本 (US-4.3)。"""
        if not self._enabled:
            return ""
        return self._injector.get_injection(text, group_id)

    def _check_global_promotion(self) -> None:
        """US-4.5: 同词在 >= N 群确认 → 自动全局化。"""
        try:
            threshold = self._global_threshold
            rows = self._db.conn.execute(
                f"""SELECT word, COUNT(DISTINCT group_id) as cnt
                   FROM jargon WHERE is_jargon = 1 AND is_global = 0
                   AND COALESCE(status, 'pending') = 'confirmed'
                   GROUP BY word HAVING cnt >= ?""",
                (threshold,),
            ).fetchall()
            if rows:
                now = int(time.time())
                for word, cnt in rows:
                    self._db.conn.execute(
                        "UPDATE jargon SET is_global = 1, updated_at = ? WHERE word = ? AND is_jargon = 1 AND COALESCE(status, 'pending') = 'confirmed'",
                        (now, word),
                    )
                    logger.info(f"[Jargon] 全局化: '{word}' (确认群数: {cnt})")
                self._db.conn.commit()
        except Exception as e:
            logger.debug(f"[Jargon] global promotion error: {e}")
