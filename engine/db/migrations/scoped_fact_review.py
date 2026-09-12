"""增量创建 scoped fact 人工审核审计表。

与 `scoped_fact_history` 的分工：后者记录「候选事实 vs 既有事实」的关系观察
（relation: compatible/scoped/conflicts/supersedes），本表只记录人工对某条正式
事实做出的状态裁决与其冲突判定结果，供 WebUI 审计流水查询。
"""
from __future__ import annotations
from ..connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_fact_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 bot_id TEXT NOT NULL, session_id TEXT NOT NULL, visibility TEXT NOT NULL CHECK (visibility='group'),
 fact_id INTEGER NOT NULL,
 action TEXT NOT NULL CHECK (action IN ('approve','reject')),
 actor TEXT NOT NULL DEFAULT 'webui',
 reason TEXT NOT NULL DEFAULT '',
 from_status TEXT NOT NULL, to_status TEXT NOT NULL,
 relation TEXT NOT NULL DEFAULT '' ,
 conflict_with_fact_id INTEGER,
 idempotency_key TEXT NOT NULL,
 reviewed_at REAL NOT NULL,
 UNIQUE(bot_id,session_id,visibility,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_scoped_fact_reviews_scope_fact ON scoped_fact_reviews(bot_id,session_id,visibility,fact_id,reviewed_at);
CREATE INDEX IF NOT EXISTS idx_scoped_fact_reviews_scope_time ON scoped_fact_reviews(bot_id,session_id,visibility,reviewed_at);
"""

def ensure_scoped_fact_review_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    with cm.migration_transaction() as tx:
        for statement in _SCHEMA.split(';'):
            if statement.strip():
                tx.execute(statement)

__all__ = ['ensure_scoped_fact_review_schema']
