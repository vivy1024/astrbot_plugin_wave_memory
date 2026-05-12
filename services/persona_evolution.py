"""Wave Memory 人格进化 — 基于好感度动态调整 bot 对不同用户的态度"""

from __future__ import annotations

import json
import time
from typing import Optional

from astrbot.api import logger

from ..engine.database import WaveMemoryDB


# ═══════════════════════════════════════════════════════════════
# 态度指令映射
# ═══════════════════════════════════════════════════════════════

ATTITUDE_INSTRUCTIONS = {
    "intimate": (
        "你和这个人很熟悉，关系亲近。可以开玩笑、主动关心、"
        "记得对方说过的事和喜好，用轻松随意的语气交流。"
    ),
    "friendly": (
        "你和这个人关系不错，友好正常交流。"
        "偶尔可以调侃，可以记住对方提过的事。"
    ),
    "neutral": (
        "你和这个人不太熟，礼貌但保持适当距离，正常回答问题。"
    ),
    "cold": (
        "你对这个人印象一般，简短回复，不主动延伸话题。"
    ),
    "hostile": (
        "你对这个人有戒备，必要时可以反击，不配合无理要求，"
        "但不要主动挑衅。"
    ),
}

# 好感度维度对行为的细化影响
DIMENSION_HINTS = {
    "high_fun": "这个人经常和你玩梗整活，你可以用更活泼的方式回应。",
    "high_depth": "这个人喜欢深入讨论，你可以给出更详细的回答。",
    "high_trust": "这个人信任你，你可以更坦诚地表达观点。",
    "high_hostility": "这个人曾经对你不友好，保持警惕但不要记仇。",
    "low_familiarity": "你们互动不多，不要表现得太熟。",
}


class PersonaEvolution:
    """人格进化引擎：根据用户好感度生成态度注入文本。"""

    def __init__(self, db: WaveMemoryDB, cross_group_merge: bool = True, affinity_cfg: dict = None):
        self.db = db
        self.cross_group_merge = cross_group_merge
        self.affinity_cfg = affinity_cfg or {}

    def get_persona_injection(self, sender_id: str, group_id: str) -> str:
        """为指定用户生成人格态度注入文本。

        跨群画像合并：聚合同一 user_id 在所有群的数据，
        以当前群 profile 为主，其他群数据补充。
        """
        if not sender_id:
            return ""

        # 读取 profile
        if self.cross_group_merge:
            # 跨群合并：读取所有群的 profile
            rows = self.db.conn.execute(
                "SELECT group_id, nickname, affection, interaction_count, personality_tags, metadata FROM user_profiles WHERE user_id = ?",
                (sender_id,),
            ).fetchall()
        else:
            # 仅当前群
            rows = self.db.conn.execute(
                "SELECT group_id, nickname, affection, interaction_count, personality_tags, metadata FROM user_profiles WHERE user_id = ? AND group_id = ?",
                (sender_id, group_id),
            ).fetchall()

        if not rows:
            return ""

        # 分离当前群 profile 和其他群 profile
        current_profile = None
        other_profiles = []
        for r in rows:
            profile = {
                "group_id": r[0],
                "nickname": r[1],
                "affection": r[2] or 0,
                "interaction_count": r[3] or 0,
                "personality_tags": r[4],
                "metadata": json.loads(r[5]) if r[5] else {},
            }
            if r[0] == group_id:
                current_profile = profile
            else:
                # 排除 QQ号_群号 格式的伪 group（私聊上下文）
                if not r[0].startswith(f"{sender_id}_"):
                    other_profiles.append(profile)

        # 如果当前群没有 profile，取最高好感度的那个
        if not current_profile:
            if other_profiles:
                current_profile = max(other_profiles, key=lambda p: p["affection"])
            else:
                return ""

        # 合并画像
        merged = self._merge_profiles(current_profile, other_profiles)

        nickname = merged["nickname"]
        affection = merged["affection"]
        dims = merged["dimensions"]
        attitude = merged["attitude"]

        # 好感度太低（新用户）不注入
        if affection == 0 and all(v == 0 for v in dims.values()):
            return ""

        # 构建注入文本
        parts = []
        parts.append("[对话者画像]")
        parts.append(f"- 昵称: {nickname or sender_id}")

        # person_registry 补充别名
        aliases = self._get_aliases(sender_id)
        if aliases and len(aliases) > 1:
            other_names = [a for a in aliases if a != nickname][:3]
            if other_names:
                parts.append(f"- 别名: {', '.join(other_names)}")

        parts.append(f"- 好感度: {affection}/100 ({attitude})")

        # 跨群活跃信息
        total_interactions = merged["total_interactions"]
        active_groups = merged["active_groups"]
        if active_groups > 1:
            parts.append(f"- 跨群活跃: {active_groups} 个群, 共 {total_interactions} 次互动")

        # 表达模式摘要（当前群优先，fallback 到其他群）
        traits = self._get_expression_traits(sender_id, group_id, other_profiles)
        if traits:
            parts.append(f"- 特征: {', '.join(traits)}")

        # 态度指令
        instruction = ATTITUDE_INSTRUCTIONS.get(attitude, ATTITUDE_INSTRUCTIONS["neutral"])
        parts.append(f"- 态度指令: {instruction}")

        # 维度细化提示
        hints = []
        if dims.get("fun", 0) > 40:
            hints.append(DIMENSION_HINTS["high_fun"])
        if dims.get("depth", 0) > 40:
            hints.append(DIMENSION_HINTS["high_depth"])
        if dims.get("trust", 0) > 50:
            hints.append(DIMENSION_HINTS["high_trust"])
        if dims.get("hostility", 0) > 30:
            hints.append(DIMENSION_HINTS["high_hostility"])
        if dims.get("familiarity", 0) < 15:
            hints.append(DIMENSION_HINTS["low_familiarity"])

        if hints:
            parts.append(f"- 补充: {' '.join(hints)}")

        # personality_tags（合并去重）
        all_tags = merged["personality_tags"]
        if all_tags:
            parts.append(f"- 个性标签: {', '.join(all_tags[:8])}")

        return "\n".join(parts)

    def _merge_profiles(self, current: dict, others: list[dict]) -> dict:
        """合并当前群和其他群的 profile 数据。"""
        # 好感度：取最高值（跨群认知应该取最好的印象）
        all_profiles = [current] + others
        max_affection = max(p["affection"] for p in all_profiles)

        # 维度：加权平均（按 interaction_count 加权）
        merged_dims = {}
        total_weight = 0
        for p in all_profiles:
            dims = p["metadata"].get("dimensions", {})
            weight = max(p["interaction_count"], 1)
            total_weight += weight
            for k, v in dims.items():
                merged_dims[k] = merged_dims.get(k, 0) + v * weight

        if total_weight > 0:
            merged_dims = {k: round(v / total_weight, 2) for k, v in merged_dims.items()}

        # 态度：基于合并后的好感度重新计算
        attitude = self._affection_to_attitude(max_affection)

        # 总互动次数
        total_interactions = sum(p["interaction_count"] for p in all_profiles)
        active_groups = len([p for p in all_profiles if p["interaction_count"] > 0])

        # personality_tags 合并去重
        all_tags = set()
        for p in all_profiles:
            if p["personality_tags"]:
                try:
                    tags = json.loads(p["personality_tags"])
                    all_tags.update(tags[:10])
                except (json.JSONDecodeError, TypeError):
                    pass

        return {
            "nickname": current["nickname"],
            "affection": max_affection,
            "dimensions": merged_dims,
            "attitude": attitude,
            "total_interactions": total_interactions,
            "active_groups": active_groups,
            "personality_tags": sorted(all_tags)[:12],
        }

    def _affection_to_attitude(self, affection: int) -> str:
        """好感度 → 态度等级。"""
        intimate_th = int(self.affinity_cfg.get("intimate_threshold", 60))
        friendly_th = int(self.affinity_cfg.get("friendly_threshold", 30))
        neutral_th = int(self.affinity_cfg.get("neutral_threshold", 10))

        if affection >= intimate_th:
            return "intimate"
        elif affection >= friendly_th:
            return "friendly"
        elif affection >= neutral_th:
            return "neutral"
        elif affection >= -10:
            return "cold"
        else:
            return "hostile"

    def _get_aliases(self, sender_id: str) -> list[str]:
        """从 person_registry 获取别名列表。"""
        try:
            row = self.db.conn.execute(
                "SELECT aliases FROM person_registry WHERE qq_id = ?",
                (sender_id,),
            ).fetchone()
            if row and row[0]:
                return json.loads(row[0])
        except Exception:
            pass
        return []

    def _get_expression_traits(self, sender_id: str, group_id: str, other_profiles: list[dict]) -> list[str]:
        """获取表达模式特征，当前群优先，fallback 到其他群。"""
        # 先查当前群
        groups_to_check = [group_id] + [p["group_id"] for p in other_profiles]

        for gid in groups_to_check:
            pattern_row = self.db.conn.execute(
                "SELECT expression FROM expression_patterns WHERE group_id = ? AND situation = ?",
                (gid, f"user:{sender_id}"),
            ).fetchone()

            if pattern_row and pattern_row[0]:
                try:
                    pattern = json.loads(pattern_row[0])
                    traits = []
                    avg_len = pattern.get("avg_msg_length", 0)
                    if avg_len > 0:
                        if avg_len < 20:
                            traits.append("消息极简短")
                        elif avg_len < 40:
                            traits.append("消息简短")
                        elif avg_len > 100:
                            traits.append("消息较长")

                    hours = pattern.get("active_hours", [])
                    if hours:
                        if any(h in hours for h in [0, 1, 2, 3, 4]):
                            traits.append("深夜活跃")
                        elif any(h in hours for h in [6, 7, 8, 9]):
                            traits.append("早起型")

                    sentiment = pattern.get("sentiment_bias", 0)
                    if sentiment > 0.3:
                        traits.append("情感偏正面")
                    elif sentiment < -0.3:
                        traits.append("情感偏负面")

                    emoji_rate = pattern.get("emoji_rate", 0)
                    if emoji_rate > 0.2:
                        traits.append("爱用表情")

                    question_rate = pattern.get("question_rate", 0)
                    if question_rate > 0.15:
                        traits.append("爱提问")

                    if traits:
                        return traits
                except (json.JSONDecodeError, TypeError):
                    continue
        return []

    def get_group_atmosphere(self, group_id: str) -> str:
        """获取群体氛围描述（可选注入）。"""
        # 统计该群的平均好感度和活跃度
        row = self.db.conn.execute(
            """SELECT AVG(affection), COUNT(*), MAX(last_seen)
               FROM user_profiles WHERE group_id = ? AND affection != 0""",
            (group_id,),
        ).fetchone()

        if not row or not row[1]:
            return ""

        avg_aff, active_users, last_active = row

        if active_users < 3:
            return ""

        parts = []
        if avg_aff > 30:
            parts.append("群体氛围友好活跃")
        elif avg_aff > 10:
            parts.append("群体氛围正常")
        elif avg_aff >= 0:
            parts.append("群体氛围平淡")
        else:
            parts.append("群体氛围偏冷")

        return f"[群体氛围] {'; '.join(parts)}, 活跃成员 {active_users} 人"
