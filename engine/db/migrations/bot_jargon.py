"""增量创建 Bot 级广域黑话表。

与 `scoped_jargon` 的根本区别：本表**不含** session/visibility 维度，只按
`bot_id` 归属，因而可被该 Bot 的所有群共享。它不替代群级黑话，而是承载
「手动提升为广域」与「从内置广域资产导入」两类跨群词条。
"""
from __future__ import annotations
from ..connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_jargon (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 bot_id TEXT NOT NULL,
 word TEXT NOT NULL,
 meaning TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
 is_jargon INTEGER,
 confidence REAL NOT NULL DEFAULT 0.0,
 source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','promoted','holyman_import','manual_deleted')),
 origin_scope TEXT,
 reference_key TEXT,
 provenance TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE (bot_id, word)
);
CREATE INDEX IF NOT EXISTS idx_bot_jargon_bot_status ON bot_jargon(bot_id, status, updated_at);
"""

def ensure_bot_jargon_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    with cm.migration_transaction() as tx:
        for statement in _SCHEMA.split(';'):
            if statement.strip():
                tx.execute(statement)

__all__ = ['ensure_bot_jargon_schema']
