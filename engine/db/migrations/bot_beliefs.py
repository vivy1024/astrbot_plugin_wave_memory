"""信念 v2 的纯增量 schema：信念属于 Bot 本人（跨群共享），由反思服务从她自己的经历里形成。

- ``bot_beliefs``：一个 Bot 的一条看法（萌芽 / 形成 / 稳固 / 动摇 / 已淡出 / 已否决），
  带依据日期、依据所在的群、反思时顺带产出的触发词与版本号（管理台写入做版本冲突检查）；
- ``bot_belief_events``：每一次状态变化与印证/反例的历史，依据链接到具体日记；
- ``bot_belief_reflection_runs``：反思进度（每个 Bot 每天最多一行），重启后不重复、不漏做。

``archive_legacy_scoped_beliefs`` 把旧版 ``scoped_beliefs``（由事后记账从事实归纳、实为人物事实）
统一改为 ``legacy_archived``：不删除、不注入，管理台只读可见；原状态记在 provenance 里，重复执行无副作用。
"""

from __future__ import annotations

import json
import time

from ..connection import ConnectionManager

LEGACY_ARCHIVED_STATUS = "legacy_archived"

_BOT_BELIEF_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'world',
    status TEXT NOT NULL DEFAULT 'sprout',
    strength REAL NOT NULL DEFAULT 0,
    triggers TEXT NOT NULL DEFAULT '[]',
    support_dates TEXT NOT NULL DEFAULT '[]',
    session_ids TEXT NOT NULL DEFAULT '[]',
    confirm_runs INTEGER NOT NULL DEFAULT 0,
    challenge_count INTEGER NOT NULL DEFAULT 0,
    challenge_streak INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_confirmed_at REAL,
    last_challenged_at REAL,
    last_decay_at REAL,
    status_changed_at REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_beliefs_bot_status
    ON bot_beliefs (bot_id, status, updated_at);

CREATE TABLE IF NOT EXISTS bot_belief_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    belief_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    strength_before REAL,
    strength_after REAL,
    reason TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    run_id INTEGER,
    actor TEXT NOT NULL DEFAULT 'reflection',
    occurred_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_belief_events_belief
    ON bot_belief_events (bot_id, belief_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_bot_belief_events_bot_time
    ON bot_belief_events (bot_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS bot_belief_reflection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    diary_cursor INTEGER NOT NULL DEFAULT 0,
    diary_watermark REAL NOT NULL DEFAULT 0,
    diary_watermark_id INTEGER NOT NULL DEFAULT 0,
    diary_ids TEXT NOT NULL DEFAULT '[]',
    counts TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE (bot_id, run_date)
)
"""

# 之后新增的列在这里登记：旧库缺列时补上（ALTER TABLE ... ADD COLUMN），不改已有数据。
_COLUMNS: dict[str, dict[str, str]] = {
    "bot_belief_reflection_runs": {
        # 已读日记的 ``created_at`` 水位。旧库补列后为 0，下一轮反思按
        # ``FIRST_RUN_LOOKBACK_DAYS`` 重建水位，代价是最近 7 天里已读过的日记会被再读一次
        # （线上实测：yushu 8 篇、baizz 5 篇）。重复读不会重复建看法 —— 提示词要求
        # 「意思一样的放进 confirmed」，``_apply`` 里与已有看法相似的也走 merge/resprout 分支；
        # 最坏是出现一条近义新芽，仍比「漏读」可接受。反之若把补列后的 0 当成
        # 「什么都读过了」，这一周的日记就永久丢了。
        "diary_watermark": "REAL NOT NULL DEFAULT 0",
        # 同一天多群日记共用同一个写入时刻（23:30），需要用 id 在同刻内定位边界。
        "diary_watermark_id": "INTEGER NOT NULL DEFAULT 0",
    },
}


def ensure_bot_beliefs_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    statements = tuple(s.strip() for s in _BOT_BELIEF_SCHEMA.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)
        for table, columns in _COLUMNS.items():
            existing = {str(row[1]) for row in tx.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    tx.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def archive_legacy_scoped_beliefs(cm: ConnectionManager, *, now: float | None = None) -> int:
    """旧版 scoped_beliefs 一律归档为 legacy_archived（幂等）；返回本次改动的行数。"""
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    stamp = float(now if now is not None else time.time())
    with cm.migration_transaction() as tx:
        exists = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_beliefs'"
        ).fetchone()
        if exists is None:
            return 0
        rows = tx.execute(
            "SELECT id, status, provenance FROM scoped_beliefs WHERE status != ?",
            (LEGACY_ARCHIVED_STATUS,),
        ).fetchall()
        for belief_id, status, raw in rows:
            try:
                provenance = json.loads(raw or "{}")
            except (TypeError, ValueError):
                provenance = {}
            if not isinstance(provenance, dict):
                provenance = {"legacy_provenance": provenance}
            provenance.setdefault("legacy_status", str(status or ""))
            provenance.setdefault("legacy_archived_at", stamp)
            provenance.setdefault("legacy_archived_reason", "belief_v2_reflection_only")
            tx.execute(
                "UPDATE scoped_beliefs SET status=?, provenance=? WHERE id=?",
                (
                    LEGACY_ARCHIVED_STATUS,
                    json.dumps(provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    int(belief_id),
                ),
            )
        return len(rows)


__all__ = [
    "LEGACY_ARCHIVED_STATUS",
    "archive_legacy_scoped_beliefs",
    "ensure_bot_beliefs_schema",
]
