"""标签覆盖率：把「没有标签」拆成按规则不需要、模型判定无标签、失败、标签丢失、待处理几类。

v5 的三个页面各算各的：首页用「正式标签数 / 全部记忆（含噪声、归档）」，维护页只数旧的
``memory_tags``，两边都把按规则不提取的短消息和模型判定无标签的记为缺失，显示的缺失率远高于实际。
这里只算活跃记忆（非噪声、非归档、未隔离），正式 ``scoped_memory_tags`` 与旧 ``memory_tags``
任一有标签即算已标。

分类（互斥）：
- ``tagged``         有标签
- ``too_short``      正文不足 ``MIN_CONTENT_LENGTH`` 字，TagWorker 按规则不提取
- ``skipped``        提取过，模型判定没有可用标签
- ``failed``         提取失败；``attempts`` 达到上限后不再自动重试
- ``lost``           状态是 done 但标签不在了（历史清理删掉了关联），需要重新提取
- ``pending``        TagWorker 会处理、还没轮到
- ``not_eligible``   TagWorker 不处理的记忆（私聊，或缺少归属的旧行）

``effective_coverage`` = tagged / (tagged + failed + lost + pending)：去掉按设计不打标签的部分，
是「该打的打了多少」。
"""

from __future__ import annotations

import time
from typing import Any

MIN_CONTENT_LENGTH = 10
MAX_ATTEMPTS = 5
CATEGORIES = ("tagged", "too_short", "skipped", "failed", "lost", "pending", "not_eligible")
REQUEUEABLE = ("lost", "skipped", "failed")

_LABELS = {
    "tagged": "已有标签",
    "too_short": "太短（按规则不提取）",
    "skipped": "模型判定无标签",
    "failed": "提取失败",
    "lost": "标签丢失（需重新提取）",
    "pending": "排队中",
    "not_eligible": "不在提取范围（私聊/旧行）",
}


def _has_table(conn: Any, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _classify_sql(conn: Any) -> str:
    """每条活跃记忆一行 (id, category, bot_id, visibility)。所有子查询都走索引。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    has_scope = {"bot_id", "session_id", "visibility"} <= columns
    scoped = (
        "EXISTS (SELECT 1 FROM scoped_memory_tags t WHERE t.bot_id=m.bot_id AND t.session_id=m.session_id "
        "AND t.visibility=m.visibility AND t.memory_id=m.id)"
        if has_scope and _has_table(conn, "scoped_memory_tags") else "0"
    )
    legacy = "EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id=m.id)" if _has_table(conn, "memory_tags") else "0"
    status = "s.status" if _has_table(conn, "tag_extraction_status") else "NULL"
    attempts = "COALESCE(s.attempts, 0)" if status != "NULL" else "0"
    join = "LEFT JOIN tag_extraction_status s ON s.memory_id = m.id" if status != "NULL" else ""
    # 与 TagWorker._fetch_untagged_batch 的两条通道一致：正式群聊行、无归属的旧群聊行
    if has_scope:
        eligible = (
            "((COALESCE(m.bot_id,'')!='' AND COALESCE(m.session_id,'')!='' AND m.visibility='group') "
            "OR (COALESCE(m.bot_id,'')='' AND COALESCE(m.session_id,'')='' AND COALESCE(m.visibility,'')='' "
            "AND COALESCE(m.group_id,'')!=''))"
        )
        bot, visibility = "COALESCE(m.bot_id,'')", "COALESCE(m.visibility,'')"
    else:
        eligible, bot, visibility = "COALESCE(m.group_id,'')!=''", "''", "''"
    active = ["COALESCE(m.memory_type,'message') NOT IN ('noise','archived','evicted','deleted')"]
    if "quarantine" in columns:
        active.append("COALESCE(m.quarantine,0)=0")
    if "source" in columns:
        active.append("COALESCE(m.source,'')!='noise'")
    return f"""
        SELECT m.id AS id, {bot} AS bot_id, {visibility} AS visibility,
               CASE
                 WHEN {scoped} OR {legacy} THEN 'tagged'
                 WHEN LENGTH(COALESCE(m.content,'')) < {MIN_CONTENT_LENGTH} THEN 'too_short'
                 WHEN {status} = 'skipped' THEN 'skipped'
                 WHEN {status} = 'failed' THEN 'failed'
                 WHEN {status} = 'done' THEN 'lost'
                 WHEN NOT {eligible} THEN 'not_eligible'
                 ELSE 'pending'
               END AS category,
               {attempts} AS attempts
          FROM memories m {join}
         WHERE {' AND '.join(active)}
    """


def build_tag_coverage(conn: Any, *, worker_status: dict[str, Any] | None = None, sample_limit: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    conn.execute("DROP TABLE IF EXISTS temp.tag_coverage_rows")
    conn.execute(f"CREATE TEMP TABLE tag_coverage_rows AS {_classify_sql(conn)}")
    try:
        counts = dict.fromkeys(CATEGORIES, 0)
        for category, count in conn.execute("SELECT category, COUNT(*) FROM temp.tag_coverage_rows GROUP BY category"):
            counts[str(category)] = int(count)
        by_bot: dict[str, dict[str, int]] = {}
        for bot_id, category, count in conn.execute(
            "SELECT bot_id, category, COUNT(*) FROM temp.tag_coverage_rows GROUP BY bot_id, category"
        ):
            by_bot.setdefault(str(bot_id or "(无归属旧行)"), dict.fromkeys(CATEGORIES, 0))[str(category)] = int(count)
        poisoned = conn.execute(
            "SELECT COUNT(*) FROM temp.tag_coverage_rows WHERE category='failed' AND attempts >= ?", (MAX_ATTEMPTS,)
        ).fetchone()[0]
        samples: dict[str, list[dict[str, Any]]] = {}
        for category in ("lost", "skipped", "failed", "pending", "not_eligible"):
            rows = conn.execute(
                """SELECT m.id, substr(m.content, 1, 80), m.timestamp, r.bot_id, r.visibility
                     FROM temp.tag_coverage_rows r JOIN memories m ON m.id = r.id
                    WHERE r.category = ? ORDER BY r.id DESC LIMIT ?""",
                (category, int(sample_limit)),
            ).fetchall()
            samples[category] = [
                {"id": int(r[0]), "preview": r[1] or "", "timestamp": r[2], "bot_id": r[3], "visibility": r[4]} for r in rows
            ]
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.tag_coverage_rows")

    throughput: dict[str, int] = {}
    if _has_table(conn, "tag_extraction_status"):
        now = time.time()
        for label, seconds in (("last_24h", 86400), ("last_7d", 7 * 86400)):
            throughput[label] = int(conn.execute(
                "SELECT COUNT(*) FROM tag_extraction_status WHERE updated_at > ? AND status IN ('done','skipped')",
                (now - seconds,),
            ).fetchone()[0])

    active = sum(counts.values())
    needs = counts["tagged"] + counts["failed"] + counts["lost"] + counts["pending"]
    backlog = counts["pending"] + counts["lost"] + (counts["failed"] - int(poisoned))
    eta_hours = None
    status = dict(worker_status or {})
    per_hour = 0.0
    if status.get("batch_size") and status.get("interval_seconds"):
        per_hour = float(status["batch_size"]) * 3600.0 / max(1.0, float(status["interval_seconds"]))
        eta_hours = round(backlog / per_hour, 1) if per_hour else None
    return {
        "active_memories": active,
        "counts": counts,
        "labels": dict(_LABELS),
        "coverage": round(counts["tagged"] / active, 4) if active else 1.0,
        "effective_coverage": round(counts["tagged"] / needs, 4) if needs else 1.0,
        "backlog": max(0, backlog),
        "failed_exhausted": int(poisoned),
        "by_bot": by_bot,
        "samples": samples,
        "throughput": throughput,
        "worker": status,
        "worker_capacity_per_hour": round(per_hour, 1),
        "backlog_eta_hours": eta_hours,
        "requeueable": list(REQUEUEABLE),
        "min_content_length": MIN_CONTENT_LENGTH,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "generated_at": time.time(),
    }


def requeue_ids(conn: Any, category: str, *, limit: int = 5000) -> list[int]:
    """某一类（lost/skipped/failed）里要重新提取的记忆 id，按新到旧。"""
    if category not in REQUEUEABLE:
        raise ValueError(f"只能重新提取 {', '.join(REQUEUEABLE)}")
    sql = f"SELECT id FROM ({_classify_sql(conn)}) WHERE category = ? ORDER BY id DESC LIMIT ?"
    return [int(row[0]) for row in conn.execute(sql, (category, int(limit))).fetchall()]


__all__ = ["CATEGORIES", "MIN_CONTENT_LENGTH", "REQUEUEABLE", "build_tag_coverage", "requeue_ids"]
