"""Wave Memory 生命周期服务 — 好感度 + 表达模式 + 衰减"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from typing import Any, Mapping, Optional

try:  # 允许没有 AstrBot 宿主依赖的仓库测试直接导入。
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 仅用于独立测试环境
    import logging

    logger = logging.getLogger(__name__)

try:  # 兼容插件包导入和仓库测试直接导入
    from ..domain.scope import RuntimeScope, ScopeValidationError
    from ..engine.database import WaveMemoryDB
except ImportError:  # pragma: no cover - 由仓库测试直接导入 services 使用
    from domain.scope import RuntimeScope, ScopeValidationError
    from engine.database import WaveMemoryDB


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
    "familiarity": 60,
    "trust": 45,
    "fun": 14,
    "hostility": 30,
    "depth": 90,
}

DAILY_DECAY = {k: 0.5 ** (1.0 / v) for k, v in HALF_LIVES.items()}

# 分段衰减：intimate 阶段衰减减速系数（老朋友衰减慢）
INTIMATE_DECAY_SLOWDOWN = 0.7
FRIENDLY_DECAY_SLOWDOWN = 0.85

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


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get_attitude_level(affection: int) -> str:
    """态度分档：单一事实来源在 domain.relationship_policy。

    此前这里自研了一份同样的公式与分档，与正式实现重复；任何一侧改动都会让
    生活周期与亲和通道对同一个人给出不同态度。
    """
    try:
        from ..domain.relationship_policy import attitude_level
    except ImportError:  # pragma: no cover - repository tests import top-level packages
        from domain.relationship_policy import attitude_level
    return attitude_level(affection)


def _project_group_subject_scope(scope: RuntimeScope) -> tuple[str, str, str]:
    """Project an ingress-resolved group Scope into legacy affinity keys."""
    if not isinstance(scope, RuntimeScope):
        raise ScopeValidationError("scope_required", "affinity requires RuntimeScope")
    if scope.visibility != "group" or scope.session is None:
        raise ScopeValidationError(
            "scope_visibility_not_allowed",
            "affinity currently accepts group RuntimeScope only",
        )
    prefix = f"{scope.session.platform_id}:user:"
    principal = scope.subject_principal_id or ""
    if not principal.startswith(prefix) or principal == prefix:
        raise ScopeValidationError(
            "scope_subject_required",
            "affinity requires a scoped platform user",
        )
    return scope.bot_id, scope.session.conversation_id, principal[len(prefix):]


# ═══════════════════════════════════════════════════════════════
# AffinityEngine — 好感度计算核心
# ═══════════════════════════════════════════════════════════════

class AffinityEngine:
    """多维好感度引擎。内存缓冲 + 定时持久化。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        bot_qq_id: str = "",
        bot_db_id: str = "yushu",
        target_profiles: dict[str, dict[str, str]] | None = None,
        relationship_service: Any | None = None,
        jargon_service: Any | None = None,
    ):
        self.db = db
        self.relationship_service = relationship_service
        self.jargon_service = jargon_service
        self.bot_qq_id = bot_qq_id
        self.bot_db_id = bot_db_id  # 写 user_profiles 时用的 bot_id 值
        self.target_profiles = target_profiles or {}
        self._buffer: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self._emotion_cache: Optional[dict] = None

    def _get_emotion_classification(self) -> dict:
        """Legacy global tags cannot be projected into a RuntimeScope safely.

        The caller retains keyword-based emotion handling; scoped emotion tags
        can be reintroduced only after a dedicated scoped read-model exists.
        """
        if self._emotion_cache is None:
            self._emotion_cache = {}
        return self._emotion_cache

    def process_message(
        self,
        sender_id: str = "",
        group_id: str = "",
        content: str = "",
        emotion_tag_ids: list[int] = None,
        is_reply_to_bot: bool = False,
        is_at_bot: bool = False,
        conversation_depth: int = 0,
        hour: int = -1,
        *,
        scope: RuntimeScope | None = None,
    ) -> bool:
        """处理一条消息：只记录触达，不把关键词增量写入正式五维。

        新事件路径必须携带 ingress 解析出的 Scope；旧调用暂保留裸键兼容，
        但不会由本方法从原始事件字段重新推断 Scope。
        """
        supplied_sender_id = str(sender_id or "").strip()
        supplied_group_id = str(group_id or "").strip()
        if scope is not None:
            try:
                scoped_bot_id, scoped_group_id, scoped_user_id = _project_group_subject_scope(scope)
            except ScopeValidationError:
                return False
            if scoped_bot_id != self.bot_db_id:
                return False
            if (
                (supplied_sender_id and supplied_sender_id != scoped_user_id)
                or (supplied_group_id and supplied_group_id != scoped_group_id)
            ):
                return False
            sender_id, group_id = scoped_user_id, scoped_group_id
        else:
            sender_id, group_id = supplied_sender_id, supplied_group_id
        if not sender_id or not group_id or sender_id == self.bot_qq_id:
            return False

        # 触达记录只更新缓冲，不把关键词增量写入正式五维。
        self._buffer[(sender_id, group_id)].setdefault("_touched", 1.0)
        return True

    def flush(self):
        """将缓冲增量持久化到数据库，并执行衰减。"""
        if not self._buffer:
            return 0

        now = time.time()
        updated = 0

        for (user_id, group_id), deltas in self._buffer.items():
            # 读取当前维度
            row = self.db.conn.execute(
                "SELECT affection, metadata, last_seen FROM user_profiles WHERE user_id = ? AND group_id = ? AND bot_id = ?",
                (user_id, group_id, self.bot_db_id),
            ).fetchone()

            if not row:
                # 新用户，创建 profile
                dims = {"familiarity": 0, "trust": 0, "fun": 0, "hostility": 0, "depth": 0}
                last_seen = now
            else:
                meta = json.loads(row[1]) if row[1] else {}
                dims = meta.get("dimensions", {"familiarity": 0, "trust": 0, "fun": 0, "hostility": 0, "depth": 0})
                last_seen = row[2] or now

            # 执行时间衰减（分段：intimate 阶段衰减更慢，neutral 更快）
            days_silent = (now - last_seen) / 86400.0
            if days_silent > 3:
                effective_days = days_silent - 3
                # 根据当前态度阶段调整衰减速度
                current_attitude = meta.get("attitude_level", "neutral") if row else "neutral"
                if current_attitude == "intimate":
                    decay_factor = INTIMATE_DECAY_SLOWDOWN
                elif current_attitude == "friendly":
                    decay_factor = FRIENDLY_DECAY_SLOWDOWN
                else:
                    decay_factor = 1.0
                for dim_name in dims:
                    decay = DAILY_DECAY[dim_name]
                    adjusted_decay = 1.0 - (1.0 - decay) * decay_factor
                    if days_silent > 14:
                        normal_decay = adjusted_decay ** 11
                        extra_decay = (adjusted_decay * 0.995) ** (effective_days - 11)
                        dims[dim_name] *= normal_decay * extra_decay
                    else:
                        dims[dim_name] *= adjusted_decay ** effective_days

            # 关键词增量不再并入正式五维；flush 只做时间衰减与档案触达。

            # 钳位
            for dim_name in dims:
                lo, hi = DIM_RANGES.get(dim_name, (-100, 100))
                dims[dim_name] = _clamp(dims[dim_name], lo, hi)

            # 读取现有用户档案
            existing_meta = {}
            current_affection = 0
            existing_row = self.db.conn.execute(
                "SELECT metadata, affection FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
                (user_id, group_id, self.bot_db_id),
            ).fetchone()
            if existing_row:
                if existing_row[0]:
                    try:
                        existing_meta = json.loads(existing_row[0])
                    except Exception:
                        pass
                if existing_row[1] is not None:
                    current_affection = existing_row[1]

            # 态度等级与综合好感度：
            # 严格废黜纯代码私自修改 affection 的旧逻辑。
            # affection 只能由 LLM 显式调用 wave_memory_record_social_impression 工具裁决；
            # lifecycle 仅维护 dimensions 特征蓄水池和衰减，绝对不私自覆盖 affection！
            attitude = _get_attitude_level(current_affection)

            # 只更新 dimensions 相关字段，保留 MetaThinking/LLM 的 impression/tags/ledger
            existing_meta["dimensions"] = {k: round(v, 2) for k, v in dims.items()}
            existing_meta["last_decay_at"] = now
            existing_meta["attitude_level"] = attitude
            target_profile = self.target_profiles.get(user_id)
            if target_profile:
                existing_meta["target_type"] = "bot"
                existing_meta["target_bot_id"] = target_profile.get("db_id") or user_id
                existing_meta["target_name"] = target_profile.get("name") or target_profile.get("db_id") or user_id
            else:
                existing_meta.setdefault("target_type", "user")
            meta = existing_meta

            # 写入：affection 绝不在 ON CONFLICT 中被粗暴覆盖，保持当前 LLM 设定的正式分值
            self.db.conn.execute(
                """INSERT INTO user_profiles (user_id, group_id, nickname, affection, interaction_count, first_seen, last_seen, personality_tags, notes, metadata, bot_id)
                   VALUES (?, ?, ?, ?, 0, ?, ?, '', '', ?, ?)
                   ON CONFLICT(user_id, group_id, bot_id) DO UPDATE SET
                   last_seen = excluded.last_seen,
                   metadata = excluded.metadata,
                   interaction_count = interaction_count + 1""",
                (user_id, group_id, "", current_affection, now, now, json.dumps(meta, ensure_ascii=False), self.bot_db_id),
            )
            updated += 1

        # 顺便更新 person_registry（别名自动发现）
        for (user_id, group_id) in list(self._buffer.keys())[:50]:
            try:
                names = self.db.conn.execute(
                    "SELECT DISTINCT sender_name FROM memories WHERE sender_id=? AND sender_name != '' LIMIT 10",
                    (user_id,),
                ).fetchall()
                if not names:
                    continue
                all_names = [n[0] for n in names]
                # flush 缓冲只保留 legacy (user_id, group_id) 键，无法证明
                # Fact 的 Bot/canonical session 归属；不得从 legacy facts 猜测别名。
                # 正式 alias assertion/People 投影将在 Facts v2 阶段提供带 Scope 的来源。
                # 取最近使用的名字作为 display_name
                recent_name = self.db.conn.execute(
                    "SELECT sender_name FROM memories WHERE sender_id=? AND sender_name != '' ORDER BY timestamp DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if recent_name:
                    display_name = recent_name[0]
                aliases_json = json.dumps(all_names, ensure_ascii=False)
                msg_count = self.db.conn.execute("SELECT COUNT(*) FROM memories WHERE sender_id=?", (user_id,)).fetchone()[0]
                self.db.conn.execute(
                    """INSERT INTO person_registry (qq_id, display_name, aliases, message_count, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(qq_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       aliases = excluded.aliases,
                       message_count = excluded.message_count,
                       last_seen = excluded.last_seen""",
                    (user_id, display_name, aliases_json, msg_count, now, now),
                )
            except Exception:
                pass

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

    def __init__(
        self,
        db: WaveMemoryDB,
        bot_qq_id: str = "",
        bot_db_id: str = "yushu",
        mood_duration_hours: float = 2.0,
        mood_msg_threshold: int = 30,
        positive_emotion_threshold: float = 0.6,
        negative_emotion_threshold: float = 0.4,
        run_global_jobs: bool = True,
        target_profiles: dict[str, dict[str, str]] | None = None,
        bot_identities: Mapping[str, str] | None = None,
        relationship_service: Any | None = None,
        jargon_service: Any | None = None,
    ):
        self.db = db
        self.jargon_service = jargon_service
        identities = {
            str(identity).strip(): str(qq_id or "").strip()
            for identity, qq_id in dict(bot_identities or {}).items()
            if str(identity or "").strip()
        }
        if bot_db_id and bot_db_id not in identities:
            identities[str(bot_db_id)] = str(bot_qq_id or "")
        self._affinities: dict[str, AffinityEngine] = {
            identity: AffinityEngine(
                db,
                bot_qq_id=qq_id,
                bot_db_id=identity,
                target_profiles=target_profiles,
                relationship_service=relationship_service,
                jargon_service=jargon_service,
            )
            for identity, qq_id in identities.items()
        }
        # 兼容属性只允许精确命中配置的 Bot；禁止退回注册表中的第一个 Bot。
        self.affinity = self._affinities.get(str(bot_db_id))
        self.patterns = PatternAggregator(db)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_pattern_update: float = 0
        self._last_decay_run: float = 0
        # 情绪参数
        self.mood_duration_hours = mood_duration_hours
        self.mood_msg_threshold = mood_msg_threshold
        self.positive_emotion_threshold = positive_emotion_threshold
        self.negative_emotion_threshold = negative_emotion_threshold
        self.run_global_jobs = run_global_jobs
        self._scoped_mood_scopes: dict[tuple[str, str, str], RuntimeScope] = {}
        self._last_scoped_mood_update: dict[tuple[str, str, str], float] = {}

    def process_scoped_message(
        self,
        *,
        scope: RuntimeScope,
        content: str,
        emotion_tag_ids: list[int] | None = None,
        is_reply_to_bot: bool = False,
        is_at_bot: bool = False,
        conversation_depth: int = 0,
        hour: int = -1,
    ) -> bool:
        """Route an ingress-resolved message to its exact Bot affinity engine."""
        if not isinstance(scope, RuntimeScope):
            return False
        if scope.session is None or scope.visibility != "group":
            return False
        scope_key = (scope.bot_id, scope.session.id, scope.visibility)
        self._scoped_mood_scopes[scope_key] = scope
        affinity = self._affinities.get(scope.bot_id)
        if affinity is None:
            return False
        processed = affinity.process_message(
            content=content,
            emotion_tag_ids=emotion_tag_ids,
            is_reply_to_bot=is_reply_to_bot,
            is_at_bot=is_at_bot,
            conversation_depth=conversation_depth,
            hour=hour,
            scope=scope,
        )
        if processed:
            self._update_scoped_mood(scope, content=content)
        return processed

    def _update_scoped_mood(
        self,
        scope: RuntimeScope,
        *,
        content: str = "",
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        """根据正式 Scope 最近消息更新正式 Soul mood；绝不回写 Legacy mood。"""
        if not isinstance(scope, RuntimeScope) or scope.session is None:
            return False
        repository = getattr(self.db, "soul_repository", None)
        if repository is None or not callable(getattr(repository, "upsert_mood", None)):
            return False
        timestamp = float(now or time.time())
        key = (scope.bot_id, scope.session.id, scope.visibility)
        last_update = self._last_scoped_mood_update.get(key, 0.0)
        text = str(content or "")[:500]
        immediate_hits = sum(1 for word in POSITIVE_EMOTION_KW | FUN_EMOTION_KW | NEGATIVE_EMOTION_KW if word in text)
        if not force and timestamp - last_update < 15.0 and not immediate_hits:
            return False
        window = timestamp - 1800.0
        try:
            rows = self.db.conn.execute(
                """SELECT content FROM memories
                   WHERE bot_id=? AND session_id=? AND visibility=?
                     AND timestamp>? AND memory_type='message'
                     AND COALESCE(quarantine, 0)=0
                   ORDER BY timestamp DESC LIMIT 80""",
                (scope.bot_id, scope.session.id, scope.visibility, window),
            ).fetchall()
        except Exception:
            rows = []
        samples = [str(row[0] or "")[:500] for row in rows]
        if text and text not in samples:
            samples.insert(0, text)
        message_count = len(samples)
        positive_hits = sum(sum(1 for word in POSITIVE_EMOTION_KW if word in sample) for sample in samples)
        negative_hits = sum(sum(1 for word in NEGATIVE_EMOTION_KW if word in sample) for sample in samples)
        fun_hits = sum(sum(1 for word in FUN_EMOTION_KW if word in sample) for sample in samples)
        total_emotion = positive_hits + negative_hits + fun_hits
        if message_count == 0 and total_emotion == 0:
            return False
        if message_count >= self.mood_msg_threshold:
            cause = "当前群聊互动很密集"
        elif negative_hits and negative_hits / max(total_emotion, 1) >= self.negative_emotion_threshold:
            cause = "最近对话中负面情绪较多"
        elif positive_hits + fun_hits and (positive_hits + fun_hits) / max(total_emotion, 1) >= self.positive_emotion_threshold:
            cause = "最近对话氛围较积极"
        else:
            cause = "最近对话保持平稳"
        valence = _clamp((positive_hits + fun_hits - negative_hits) / max(total_emotion, 1), -1.0, 1.0)
        arousal = _clamp(
            0.2 + min(message_count / max(self.mood_msg_threshold, 1), 1.0) * 0.45
            + min(fun_hits / max(message_count, 1), 1.0) * 0.2
            + min(negative_hits / max(message_count, 1), 1.0) * 0.2,
            0.0,
            1.0,
        )
        try:
            repository.upsert_mood(
                scope,
                valence=round(valence, 3),
                arousal=round(arousal, 3),
                cause=cause,
                evidence=[{
                    "source": "formal_scoped_memories",
                    "window_seconds": 1800,
                    "message_count": message_count,
                    "positive_hits": positive_hits,
                    "negative_hits": negative_hits,
                    "fun_hits": fun_hits,
                }],
                policy_version="scoped-mood/v2",
                observed_at=timestamp,
            )
            self._last_scoped_mood_update[key] = timestamp
            return True
        except Exception as exc:
            logger.debug("[WaveMemory] formal scoped mood update skipped: %s", exc)
            return False

    def start(self, supervisor=None):
        if self._running:
            return
        self._running = True
        if supervisor is None:
            self._task = asyncio.create_task(self._loop())
        else:
            self._task = supervisor.start(
                "wave-memory:lifecycle", self._loop(), owner="lifecycle"
            )
        logger.info(
            "[WaveMemory] LifecycleService started bots=%s global_jobs=%s",
            sorted(self._affinities),
            self.run_global_jobs,
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        # 停止前持久化每个 Bot 的残余缓冲。
        for affinity in self._affinities.values():
            try:
                affinity.flush()
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
        """一次 tick：好感度持久化 + 模式更新 + 衰减 + 情绪。"""
        now = time.time()

        # 1. 好感度 flush（将每个 Bot 的消息缓冲维度增量持久化到 DB）
        #    MetaThinking 只在@bot 时更新好感度，日常聊天的 familiarity/depth 靠这里
        for bot_id, affinity in self._affinities.items():
            flushed = affinity.flush()
            if flushed > 0:
                logger.info(f"[WaveMemory] Affinity flushed bot={bot_id}: {flushed} users")

        if not self.run_global_jobs:
            return

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

        # 4. Bot 情绪更新（每次 tick）
        try:
            self._update_mood(now)
        except Exception as e:
            logger.debug(f"[WaveMemory] Mood update failed: {e}")

    def _run_decay(self) -> int:
        """标记过期记忆为 archived 并且对 user_profiles 执行多维情感衰减。"""
        now = time.time()

        # 旧实现按全库条件直接归档，无法证明 Bot/session Scope，已退出正式写面。
        # scoped EvictionService 负责按 RuntimeScope 分组提交 archive/delete/evict 命令。
        archived_count = 0

        try:
            rows = self.db.conn.execute(
                "SELECT user_id, group_id, bot_id, affection, metadata, last_seen FROM user_profiles"
            ).fetchall()
            
            ATTITUDE_ORDER = {
                "intimate": 4,
                "friendly": 3,
                "neutral": 2,
                "cold": 1,
                "hostile": 0
            }
            
            decay_dims = ["trust", "familiarity", "fun", "depth"]
            
            for user_id, group_id, bot_id, old_affection, meta_str, last_seen in rows:
                meta = {}
                if meta_str:
                    try:
                        meta = json.loads(meta_str)
                    except Exception:
                        pass
                
                dims = meta.get("dimensions", {"familiarity": 0.0, "trust": 0.0, "fun": 0.0, "hostility": 0.0, "depth": 0.0})
                
                # 如果没有 valid 的 last_seen，取当前时间
                last_seen_val = last_seen or now
                days_passed = max(0.0, (now - last_seen_val) / 86400.0)
                decay_factor = min(0.01, (0.01 / 225.0) * (days_passed ** 2))
                
                # 获取原有的态度等级
                old_attitude = meta.get("attitude_level") or _get_attitude_level(old_affection)
                
                # 衰减指定的维度（如果存在）
                dims_changed = False
                for d in decay_dims:
                    if d in dims:
                        old_v = dims[d]
                        new_v = max(0.0, old_v - old_v * decay_factor - decay_factor)
                        dims[d] = round(new_v, 2)
                        dims_changed = True
                
                if dims_changed:
                    # 正式 affection 只能由社交工具裁决；衰减只维护 dimensions 蓄水池。
                    current_attitude = _get_attitude_level(int(old_affection or 0))
                    old_order = ATTITUDE_ORDER.get(old_attitude, 2)
                    new_order = ATTITUDE_ORDER.get(current_attitude, 2)
                    if new_order < old_order:
                        meta["decay_downgrade_noted"] = True
                        meta["last_attitude_before_decay"] = old_attitude
                    meta["dimensions"] = dims
                    meta["attitude_level"] = current_attitude
                    meta["last_decay_at"] = now
                    self.db.conn.execute(
                        """UPDATE user_profiles
                           SET metadata = ?
                           WHERE user_id = ? AND group_id = ? AND bot_id = ?""",
                        (json.dumps(meta, ensure_ascii=False), user_id, group_id, bot_id)
                    )
            
            self.db.conn.commit()
        except Exception as e:
            logger.warning(f"[WaveMemory] User profile decay processing failed: {e}")
            
        return archived_count

    def get_user_affinity(self, user_id: str, group_id: str, bot_id: str | None = None) -> dict | None:
        """读取精确 Bot affinity；未知 Bot 或未记录关系返回 ``None``。"""
        db_bot_id = str(bot_id or "").strip()
        if not db_bot_id:
            return None
        affinity = self._affinities.get(db_bot_id)
        if affinity is None:
            return None
        row = self.db.conn.execute(
            "SELECT affection, metadata FROM user_profiles WHERE user_id = ? AND group_id = ? AND bot_id = ?",
            (user_id, group_id, db_bot_id),
        ).fetchone()

        if not row:
            return None

        meta = json.loads(row[1]) if row[1] else {}
        dims = meta.get("dimensions", {})

        # 缓冲只记录触达，不再把关键词增量并入正式五维。
        key = (user_id, group_id)
        if affinity is not None and key in affinity._buffer:
            for dim, delta in affinity._buffer[key].items():
                if dim.startswith("_"):
                    continue
                dims[dim] = dims.get(dim, 0) + delta

        return {
            "affection": row[0],
            "attitude": meta.get("attitude_level", "neutral"),
            "dimensions": dims,
        }

    def _update_mood(self, now: float):
        """周期性刷新已经见过的正式 Scope 情绪；不再写 Legacy mood 表。"""
        for scope in tuple(self._scoped_mood_scopes.values()):
            self._update_scoped_mood(scope, now=now, force=True)
