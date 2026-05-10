"""Wave Memory 生命周期服务 — 好感度 + 表达模式 + 衰减"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import defaultdict
from typing import Optional

from astrbot.api import logger

from ..engine.database import WaveMemoryDB


# ═══════════════════════════════════════════════════════════════
# 好感度维度常量
# ═══════════════════════════════════════════════════════════════

DIMENSION_WEIGHTS = {
    "familiarity": 0.25,
    "trust": 0.30,
    "fun": 0.20,
    "depth": 0.25,
}
HOSTILITY_WEIGHT = 0.5

DIM_RANGES = {
    "familiarity": (0, 100),
    "trust": (-50, 100),
    "fun": (0, 80),
    "hostility": (0, 100),
    "depth": (0, 80),
}

HALF_LIVES = {
    "familiarity": 200,
    "trust": 90,
    "fun": 30,
    "hostility": 60,
    "depth": 150,
}

DAILY_DECAY = {k: 0.5 ** (1.0 / v) for k, v in HALF_LIVES.items()}

POSITIVE_EMOTION_KW = frozenset([
    '夸', '鼓励', '积极', '开心', '感谢', '认同', '推崇', '好奇', '热情',
    '喜欢', '赞', '欣赏', '温暖', '幽默', '搞笑', '顿悟', '期待', '兴奋',
    '称赞', '佩服', '支持', '友好', '满意', '惊喜',
])
NEGATIVE_EMOTION_KW = frozenset([
    '厌恶', '攻击', '嘲', '怒', '烦', '无奈', '挫败', '孤独', '冷',
    '讽', '骂', '恶', '不满', '失望', '焦虑', '愤怒', '嫌弃', '敌意',
    '指责', '贬低', '鄙视',
])
FUN_EMOTION_KW = frozenset([
    '玩梗', '整活', '搞笑', '抖机灵', '幽默', '恶搞', '沙雕', '逗',
    '玩笑', '段子', '梗',
])

BOT_PRAISE_KW = re.compile(r'(厉害|牛|好用|聪明|强|可以的|不错|真棒|好厉害|太强了|nb|666)')
BOT_ATTACK_KW = re.compile(r'(傻[逼比]|垃圾|废物|智障|弱智|滚|闭嘴|sb|脑残|人工智障)')


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _compute_affection(dims: dict) -> int:
    score = sum(dims.get(k, 0) * w for k, w in DIMENSION_WEIGHTS.items())
    score -= dims.get("hostility", 0) * HOSTILITY_WEIGHT
    return int(_clamp(score, -100, 100))


def _get_attitude_level(affection: int) -> str:
    if affection >= 60:
        return "intimate"
    elif affection >= 30:
        return "friendly"
    elif affection >= 0:
        return "neutral"
    elif affection >= -30:
        return "cold"
    else:
        return "hostile"


# ═══════════════════════════════════════════════════════════════
# AffinityEngine — 好感度计算核心
# ═══════════════════════════════════════════════════════════════

class AffinityEngine:
    """多维好感度引擎。内存缓冲 + 定时持久化。"""

    def __init__(self, db: WaveMemoryDB, bot_qq_id: str = "2500447291"):
        self.db = db
        self.bot_qq_id = bot_qq_id
        self._buffer: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self._emotion_cache: Optional[dict] = None

    def _get_emotion_classification(self) -> dict:
        if self._emotion_cache is not None:
            return self._emotion_cache
        rows = self.db.conn.execute(
            "SELECT id, name FROM tags WHERE tag_type = 'emotion'"
        ).fetchall()
        classification = {}
        for tid, name in rows:
            if any(kw in name for kw in FUN_EMOTION_KW):
                classification[tid] = 'fun'
            elif any(kw in name for kw in POSITIVE_EMOTION_KW):
                classification[tid] = 'positive'
            elif any(kw in name for kw in NEGATIVE_EMOTION_KW):
                classification[tid] = 'negative'
        self._emotion_cache = classification
        return classification

    def process_message(
        self,
        sender_id: str,
        group_id: str,
        content: str,
        emotion_tag_ids: list[int] = None,
        is_reply_to_bot: bool = False,
        is_at_bot: bool = False,
        conversation_depth: int = 0,
        hour: int = -1,
    ):
        """处理一条消息，累加好感度增量到缓冲。"""
        if not sender_id or sender_id == self.bot_qq_id:
            return

        key = (sender_id, group_id)
        buf = self._buffer[key]

        # 基础：每条消息 familiarity +0.5
        buf["familiarity"] += 0.5

        # 主动@bot
        if is_at_bot:
            buf["trust"] += 2.0
            buf["familiarity"] += 1.0

        # 回复bot
        if is_reply_to_bot:
            buf["trust"] += 1.5
            buf["familiarity"] += 0.5

        # 对话深度（连续 >=3 轮）
        if conversation_depth >= 3:
            buf["depth"] += 2.0 + min(conversation_depth - 3, 5) * 0.5
            buf["trust"] += 1.0

        # 分享链接/长文
        if len(content) > 200 or re.search(r'https?://', content):
            buf["trust"] += 1.5
            buf["depth"] += 1.0

        # 情感标签
        if emotion_tag_ids:
            classification = self._get_emotion_classification()
            for tid in emotion_tag_ids:
                cls = classification.get(tid)
                if cls == 'positive':
                    buf["trust"] += 0.5
                elif cls == 'fun':
                    buf["fun"] += 2.0

        # 对bot正面评价
        if BOT_PRAISE_KW.search(content) and (
            self.bot_qq_id in content or is_reply_to_bot or is_at_bot
        ):
            buf["trust"] += 3.0
            buf["fun"] += 2.0

        # 对bot攻击
        if BOT_ATTACK_KW.search(content) and (
            self.bot_qq_id in content or is_reply_to_bot or is_at_bot
        ):
            buf["hostility"] += 8.0
            buf["trust"] -= 3.0

        # 深夜陪聊 (0-4点)
        if 0 <= hour <= 4:
            buf["familiarity"] += 1.5
            buf["depth"] += 1.0

    def flush(self):
        """将缓冲增量持久化到数据库，并执行衰减。"""
        if not self._buffer:
            return 0

        now = time.time()
        updated = 0

        for (user_id, group_id), deltas in self._buffer.items():
            # 读取当前维度
            row = self.db.conn.execute(
                "SELECT affection, metadata, last_seen FROM user_profiles WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            ).fetchone()

            if not row:
                # 新用户，创建 profile
                dims = {"familiarity": 0, "trust": 0, "fun": 0, "hostility": 0, "depth": 0}
                last_seen = now
            else:
                meta = json.loads(row[1]) if row[1] else {}
                dims = meta.get("dimensions", {"familiarity": 0, "trust": 0, "fun": 0, "hostility": 0, "depth": 0})
                last_seen = row[2] or now

            # 执行时间衰减
            days_silent = (now - last_seen) / 86400.0
            if days_silent > 3:
                effective_days = days_silent - 3
                for dim_name in dims:
                    decay = DAILY_DECAY[dim_name]
                    if days_silent > 14:
                        # 长期冷淡加速
                        normal_decay = decay ** 11
                        extra_decay = (decay * 0.995) ** (effective_days - 11)
                        dims[dim_name] *= normal_decay * extra_decay
                    else:
                        dims[dim_name] *= decay ** effective_days

            # 应用增量
            for dim_name, delta in deltas.items():
                if dim_name in dims:
                    dims[dim_name] += delta

            # 钳位
            for dim_name in dims:
                lo, hi = DIM_RANGES.get(dim_name, (-100, 100))
                dims[dim_name] = _clamp(dims[dim_name], lo, hi)

            # 合成综合分
            affection = _compute_affection(dims)
            attitude = _get_attitude_level(affection)

            # 构建 metadata
            meta = {
                "dimensions": {k: round(v, 2) for k, v in dims.items()},
                "last_decay_at": now,
                "attitude_level": attitude,
            }

            # 写入
            self.db.conn.execute(
                """INSERT INTO user_profiles (user_id, group_id, nickname, affection, interaction_count, first_seen, last_seen, personality_tags, notes, metadata)
                   VALUES (?, ?, ?, ?, 0, ?, ?, '', '', ?)
                   ON CONFLICT(user_id, group_id) DO UPDATE SET
                   affection = excluded.affection,
                   last_seen = excluded.last_seen,
                   metadata = excluded.metadata,
                   interaction_count = interaction_count + 1""",
                (user_id, group_id, "", affection, now, now, json.dumps(meta, ensure_ascii=False)),
            )
            updated += 1

        self.db.conn.commit()
        self._buffer.clear()
        return updated


# ═══════════════════════════════════════════════════════════════
# PatternAggregator — 表达模式聚合
# ═══════════════════════════════════════════════════════════════

class PatternAggregator:
    """从历史消息统计用户表达模式，写入 expression_patterns 表。"""

    def __init__(self, db: WaveMemoryDB):
        self.db = db

    def aggregate_user(self, user_id: str, group_id: str) -> dict:
        """聚合某用户在某群的表达模式。"""
        rows = self.db.conn.execute(
            """SELECT content, timestamp FROM memories
               WHERE sender_id = ? AND group_id = ? AND content IS NOT NULL
               ORDER BY timestamp DESC LIMIT 200""",
            (user_id, group_id),
        ).fetchall()

        if len(rows) < 5:
            return {}

        contents = [r[0] for r in rows]
        timestamps = [r[1] for r in rows]

        # 消息长度
        lengths = [len(c) for c in contents]
        avg_length = sum(lengths) / len(lengths)

        # 表情使用率（emoji + 颜文字）
        emoji_pattern = re.compile(r'[😀-🙏🌀-🗿🚀-🛿🇠-🇿]|[（(][^）)]{1,5}[）)]')
        emoji_count = sum(1 for c in contents if emoji_pattern.search(c))
        emoji_rate = emoji_count / len(contents)

        # 提问率
        question_count = sum(1 for c in contents if '?' in c or '？' in c or '吗' in c or '呢' in c)
        question_rate = question_count / len(contents)

        # 感叹率
        excl_count = sum(1 for c in contents if '!' in c or '！' in c)
        exclamation_rate = excl_count / len(contents)

        # 活跃时段
        hours = defaultdict(int)
        for ts in timestamps:
            h = int(time.strftime('%H', time.localtime(ts)))
            hours[h] += 1
        active_hours = sorted(hours, key=hours.get, reverse=True)[:5]

        # 高频词（简单分词：按标点和空格切）
        word_freq = defaultdict(int)
        for c in contents:
            words = re.findall(r'[一-鿿]{2,4}|[a-zA-Z]{2,}|\d+', c)
            for w in words:
                word_freq[w] += 1
        top_words = sorted(word_freq, key=word_freq.get, reverse=True)[:10]

        # 词汇丰富度（unique / total）
        all_words = []
        for c in contents:
            all_words.extend(re.findall(r'[一-鿿]{2,4}|[a-zA-Z]{2,}', c))
        vocab_richness = len(set(all_words)) / max(len(all_words), 1)

        # 情感倾向
        emotion_tags = self.db.conn.execute(
            """SELECT t.name, COUNT(*) FROM memory_tags mt
               JOIN tags t ON t.id = mt.tag_id
               JOIN memories m ON m.id = mt.memory_id
               WHERE m.sender_id = ? AND m.group_id = ? AND t.tag_type = 'emotion'
               GROUP BY t.name ORDER BY COUNT(*) DESC LIMIT 10""",
            (user_id, group_id),
        ).fetchall()

        pos_count = sum(c for n, c in emotion_tags if any(kw in n for kw in POSITIVE_EMOTION_KW))
        neg_count = sum(c for n, c in emotion_tags if any(kw in n for kw in NEGATIVE_EMOTION_KW))
        total_emo = pos_count + neg_count
        sentiment_bias = (pos_count - neg_count) / max(total_emo, 1)

        return {
            "avg_msg_length": round(avg_length, 1),
            "emoji_rate": round(emoji_rate, 3),
            "question_rate": round(question_rate, 3),
            "exclamation_rate": round(exclamation_rate, 3),
            "active_hours": active_hours,
            "vocab_richness": round(vocab_richness, 3),
            "top_words": top_words,
            "sentiment_bias": round(sentiment_bias, 3),
            "sample_size": len(contents),
        }

    def aggregate_all(self, min_messages: int = 20) -> int:
        """批量聚合所有活跃用户的表达模式。"""
        users = self.db.conn.execute(
            """SELECT sender_id, group_id, COUNT(*) as cnt
               FROM memories
               WHERE sender_id IS NOT NULL AND sender_id != ''
                 AND sender_id NOT IN ('bot_self', 'angel_memory_import', 'livingmemory_import', 'legacy_import', 'bot_remember')
               GROUP BY sender_id, group_id
               HAVING cnt >= ?""",
            (min_messages,),
        ).fetchall()

        updated = 0
        for user_id, group_id, cnt in users:
            pattern = self.aggregate_user(user_id, group_id)
            if not pattern:
                continue

            # 写入 expression_patterns 表
            expression_json = json.dumps(pattern, ensure_ascii=False)
            now = time.time()

            existing = self.db.conn.execute(
                "SELECT id FROM expression_patterns WHERE group_id = ? AND situation = ?",
                (group_id, f"user:{user_id}"),
            ).fetchone()

            if existing:
                self.db.conn.execute(
                    "UPDATE expression_patterns SET expression = ?, last_used = ? WHERE id = ?",
                    (expression_json, now, existing[0]),
                )
            else:
                self.db.conn.execute(
                    """INSERT INTO expression_patterns (group_id, situation, expression, tag_ids, weight, use_count, last_used, created_at)
                       VALUES (?, ?, ?, '', 1.0, 0, ?, ?)""",
                    (group_id, f"user:{user_id}", expression_json, now, now),
                )
            updated += 1

        self.db.conn.commit()
        return updated


# ═══════════════════════════════════════════════════════════════
# LifecycleService — 统一后台调度
# ═══════════════════════════════════════════════════════════════

class LifecycleService:
    """统一后台服务：好感度持久化 + 表达模式更新 + 记忆衰减。

    调度周期：
    - 好感度 flush: 每 30 分钟
    - 表达模式聚合: 每 6 小时
    - 记忆衰减标记: 每 24 小时
    """

    def __init__(self, db: WaveMemoryDB, bot_qq_id: str = "2500447291"):
        self.db = db
        self.affinity = AffinityEngine(db, bot_qq_id=bot_qq_id)
        self.patterns = PatternAggregator(db)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_pattern_update: float = 0
        self._last_decay_run: float = 0

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[WaveMemory] LifecycleService started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        # 最后一次 flush
        try:
            self.affinity.flush()
        except Exception:
            pass

    async def _loop(self):
        """主循环：每 30 分钟执行一次。"""
        while self._running:
            try:
                await asyncio.sleep(1800)  # 30 min
                self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WaveMemory] Lifecycle error: {e}")
                await asyncio.sleep(60)

    def _tick(self):
        """一次 tick：flush 好感度 + 可能触发模式更新/衰减。"""
        now = time.time()

        # 1. 好感度 flush（每次 tick 都做）
        flushed = self.affinity.flush()
        if flushed > 0:
            logger.info(f"[WaveMemory] Affinity flushed: {flushed} users")

        # 2. 表达模式聚合（每 6 小时）
        if now - self._last_pattern_update > 21600:
            try:
                updated = self.patterns.aggregate_all(min_messages=20)
                self._last_pattern_update = now
                logger.info(f"[WaveMemory] Patterns updated: {updated} users")
            except Exception as e:
                logger.warning(f"[WaveMemory] Pattern aggregation failed: {e}")

        # 3. 记忆衰减标记（每 24 小时）
        if now - self._last_decay_run > 86400:
            try:
                archived = self._run_decay()
                self._last_decay_run = now
                if archived > 0:
                    logger.info(f"[WaveMemory] Decay: {archived} memories archived")
            except Exception as e:
                logger.warning(f"[WaveMemory] Decay failed: {e}")

    def _run_decay(self) -> int:
        """标记过期记忆为 archived。

        条件：importance < 0.15 且 > 180 天未访问且 access_count = 0
        """
        now = time.time()
        threshold_time = now - 180 * 86400  # 180 天前

        result = self.db.conn.execute(
            """UPDATE memories SET memory_type = 'archived'
               WHERE memory_type = 'message'
                 AND importance < 0.15
                 AND timestamp < ?
                 AND access_count = 0
                 AND (last_accessed IS NULL OR last_accessed < ?)""",
            (threshold_time, threshold_time),
        )
        self.db.conn.commit()
        return result.rowcount

    def get_user_affinity(self, user_id: str, group_id: str) -> dict:
        """获取用户好感度信息（含缓冲中的未持久化增量）。"""
        row = self.db.conn.execute(
            "SELECT affection, metadata FROM user_profiles WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()

        if not row:
            return {"affection": 0, "attitude": "neutral", "dimensions": {}}

        meta = json.loads(row[1]) if row[1] else {}
        dims = meta.get("dimensions", {})

        # 加上缓冲中的增量
        key = (user_id, group_id)
        if key in self.affinity._buffer:
            for dim, delta in self.affinity._buffer[key].items():
                dims[dim] = dims.get(dim, 0) + delta

        return {
            "affection": row[0],
            "attitude": meta.get("attitude_level", "neutral"),
            "dimensions": dims,
        }
