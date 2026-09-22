"""群分析插件 (astrbot_plugin_qq_group_daily_analysis) 成果与群聊日记桥接器。

每日复用群分析插件对 3200 条消息总结产出的 SummaryTopic、GoldenQuote 与 QualityReview，
直接转化为羽书的第一人称生活日记并入库 experience_episodes 与 scoped_soul_timeline。
无需羽书在聊天会话中重复抓取海量上下文，实现零额外开销的认知闭环。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = logging.getLogger(__name__)


def find_daily_analysis_db() -> Path | None:
    """自动发现群分析插件持久化数据库 traces.db。"""
    candidates = [
        Path("/AstrBot/data/plugin_data/astrbot_plugin_qq_group_daily_analysis/traces.db"),
        Path("AstrBot-master/data/plugin_data/astrbot_plugin_qq_group_daily_analysis/traces.db"),
        Path("../data/plugin_data/astrbot_plugin_qq_group_daily_analysis/traces.db"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def fetch_latest_group_analysis(
    group_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """从群分析 traces.db 的 stage_checkpoints 表中提取最新完成的分析成果。"""
    path = db_path or find_daily_analysis_db()
    if not path or not path.exists():
        return None

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT checkpoint_id, group_id, date_str, stage_name, data_json, created_at
              FROM stage_checkpoints
             WHERE stage_name = 'LLM_ANALYSIS'
        """
        params: list[Any] = []
        if group_id:
            sql += " AND group_id = ?"
            params.append(str(group_id).strip())
        sql += " ORDER BY created_at DESC LIMIT 1"

        row = conn.execute(sql, params).fetchone()
        conn.close()
        if not row:
            return None

        data_raw = row["data_json"]
        if not data_raw:
            return None
        payload = json.loads(data_raw)
        return {
            "checkpoint_id": row["checkpoint_id"],
            "group_id": row["group_id"],
            "date_str": row["date_str"],
            "created_at": float(row["created_at"] or time.time()),
            "topics": payload.get("topics") or [],
            "golden_quotes": (payload.get("statistics") or {}).get("golden_quotes") or [],
            "quality_review": payload.get("chat_quality_review") or (payload.get("statistics") or {}).get("chat_quality_review") or {},
            "statistics": payload.get("statistics") or {},
        }
    except Exception as exc:
        logger.debug(f"[DailyDiaryBridge] fetch analysis failed: {exc}")
        return None


def convert_analysis_to_diary(analysis: dict[str, Any], bot_name: str = "羽书") -> dict[str, str]:
    """将群分析的结构化数据转换为一篇第一人称群聊日记。"""
    date_str = analysis.get("date_str") or time.strftime("%Y-%m-%d")
    topics = analysis.get("topics") or []
    quotes = analysis.get("golden_quotes") or []
    review = analysis.get("quality_review") or {}
    stats = analysis.get("statistics") or {}

    msg_count = stats.get("message_count", 0)
    user_count = stats.get("participant_count", 0)

    # 1. 日记大标题
    main_topic = topics[0].get("topic") if topics else "群内闲笔杂谈"
    review_title = review.get("title") if isinstance(review, dict) else ""
    if review_title:
        title = f"《{review_title} · {main_topic}》"
    else:
        title = f"《{date_str} 群生活随笔：{main_topic}》"

    # 2. 一句话主线摘要
    topic_names = "、".join(t.get("topic") for t in topics[:3] if t.get("topic"))
    summary = f"今日全群畅聊 {msg_count} 条，{user_count} 位群友参与。核心围绕「{topic_names}」展开。"
    if review and isinstance(review, dict) and review.get("summary"):
        summary += f" 氛围锐评：{review['summary']}"

    # 3. 亲笔正文排版 (Markdown)
    body_lines = [
        f"### {date_str} 群生活实录",
        f"今天群里热热闹闹聊了 {msg_count} 条消息，抓到了很多生草又真实的场面：",
        "",
        "#### 一、今日焦点事件",
    ]
    for idx, t in enumerate(topics[:4], 1):
        t_name = t.get("topic", "话题")
        t_detail = t.get("detail", "")
        contributors = "、".join(t.get("contributors") or [])
        body_lines.append(f"{idx}. **{t_name}**：{t_detail}")
        if contributors:
            body_lines.append(f"   *(主要参与：{contributors})*")

    if quotes:
        body_lines.extend([
            "",
            "#### 二、群聊高光与名场面金句",
        ])
        for q in quotes[:3]:
            q_content = q.get("content", "")
            q_sender = q.get("sender", "群友")
            q_reason = q.get("reason", "")
            body_lines.append(f"- **「{q_content}」** —— *{q_sender}*")
            if q_reason:
                body_lines.append(f"  > {q_reason}")

    if review and isinstance(review, dict):
        body_lines.extend([
            "",
            f"#### 三、心智锐评与自省",
            f"{review.get('summary', '')}",
        ])

    return {
        "title": title,
        "summary": summary[:240],
        "content": "\n".join(body_lines),
    }


def sync_analysis_report_to_diary(
    wave_db: Any,
    bot_id: str,
    group_id: str,
    analysis_db_path: Path | None = None,
) -> int | None:
    """幂等将最新群分析结果同步为该群该 Bot 的群聊日记。若今日已同步则跳过。"""
    analysis = fetch_latest_group_analysis(group_id=group_id, db_path=analysis_db_path)
    if not analysis:
        return None

    date_str = analysis["date_str"]
    conn = getattr(wave_db, "conn", None) or getattr(wave_db, "_conn", None) or wave_db
    if conn is None:
        return None

    # 检查是否已存在该日期的 daily_diary
    try:
        existing = conn.execute(
            """SELECT id FROM experience_episodes
               WHERE bot_id = ? AND group_id = ? AND episode_type = 'daily_diary'
                 AND trigger_text LIKE ? LIMIT 1""",
            (bot_id, group_id, f"%{date_str}%"),
        ).fetchone()
        if existing:
            return int(existing[0])
    except Exception:
        pass

    diary = convert_analysis_to_diary(analysis, bot_name="羽书")
    now = analysis.get("created_at") or time.time()

    try:
        # 1. 写入 experience_episodes
        cur = conn.execute(
            """INSERT INTO experience_episodes (
                   bot_id, group_id, user_id, episode_type, trigger_text,
                   bot_inner_thought, bot_action, bot_reply, user_reaction,
                   outcome, source_memory_ids, emotional_weight, created_at
               ) VALUES (?, ?, NULL, 'daily_diary', ?, ?, 'wrote_daily_diary', ?, 'daily_analysis_sync', 'crystallized', '[]', 6.0, ?)""",
            (
                bot_id,
                group_id,
                diary["title"],
                diary["summary"],
                diary["content"],
                now,
            ),
        )
        episode_id = int(getattr(cur, "lastrowid", 0) or 0)

        # 2. 写入 scoped_soul_timeline
        evidence_json = json.dumps({
            "episode_id": episode_id,
            "title": diary["title"],
            "source": "daily_analysis_bridge",
            "date_str": date_str,
        }, ensure_ascii=False)
        conn.execute(
            """INSERT INTO scoped_soul_timeline (
                   bot_id, session_id, visibility, subject_principal_id,
                   event_summary, event_type, emotional_weight, occurred_at,
                   revision, evidence, created_at
               ) VALUES (?, ?, 'group', NULL, ?, 'episode', 6.0, ?, 1, ?, ?)""",
            (
                bot_id,
                f"羽书:group:{group_id}",
                f"【日记】{diary['title']}：{diary['summary']}",
                now,
                evidence_json,
                now,
            ),
        )
        if hasattr(conn, "commit"):
            conn.commit()
        logger.info(f"[DailyDiaryBridge] 成功将群 {group_id} 的群分析同步为日记 Episode #{episode_id}")
        return episode_id
    except Exception as exc:
        logger.warning(f"[DailyDiaryBridge] 日记同步失败: {exc}")
        return None
