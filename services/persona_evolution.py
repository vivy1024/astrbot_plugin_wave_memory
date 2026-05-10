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

    def __init__(self, db: WaveMemoryDB):
        self.db = db

    def get_persona_injection(self, sender_id: str, group_id: str) -> str:
        """为指定用户生成人格态度注入文本。

        返回空字符串表示无需注入（新用户或数据不足）。
        """
        if not sender_id:
            return ""

        # 读取 user_profile
        row = self.db.conn.execute(
            "SELECT nickname, affection, metadata FROM user_profiles WHERE user_id = ? AND group_id = ?",
            (sender_id, group_id),
        ).fetchone()

        if not row:
            return ""

        nickname, affection, meta_json = row
        if not meta_json:
            return ""

        meta = json.loads(meta_json)
        dims = meta.get("dimensions", {})
        attitude = meta.get("attitude_level", "neutral")

        # 好感度太低（新用户）不注入
        if affection == 0 and all(v == 0 for v in dims.values()):
            return ""

        # 构建注入文本
        parts = []
        parts.append("[对话者画像]")
        parts.append(f"- 昵称: {nickname or sender_id}")
        parts.append(f"- 好感度: {affection}/100 ({attitude})")

        # 表达模式摘要
        pattern_row = self.db.conn.execute(
            "SELECT expression FROM expression_patterns WHERE group_id = ? AND situation = ?",
            (group_id, f"user:{sender_id}"),
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
                    parts.append(f"- 特征: {', '.join(traits)}")
            except (json.JSONDecodeError, TypeError):
                pass

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

        return "\n".join(parts)

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
