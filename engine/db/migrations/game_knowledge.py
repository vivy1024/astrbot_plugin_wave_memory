"""游戏知识库的纯增量 schema：按游戏分库的攻略常识（所有 Bot 共用），加上每个 Bot 自己玩出来的经验卡。

- ``game_libraries``：一个游戏一行，``game_id`` 为小写标识（如 minecraft、oni）；``lessons_enabled`` 是
  这个游戏允不允许 Bot 写经验卡，``state_keys`` 记下直播请求里见过的状态键（写经验卡的条件时用）。
- ``game_knowledge_cards``：知识卡——标题、阶段、要点、坑、触发关键词与数值条件、出处、状态、版本号、来源类型。
  ``bot_id`` 为空 = 所有 Bot 共用的攻略；不为空 = 这个 Bot 从自己的经历里写下的经验卡（source_type=experience）。
- ``game_knowledge_terms``：词条——术语、别名、解释、相关条目。
- ``fts_game_cards`` / ``fts_game_terms``：知识卡与词条的中文检索索引，与 ``fts_memories_cjk`` 相同的
  「相邻两字」切词（``engine/db/fts_cjk.py``）；rowid 就是卡片 / 词条 id，由仓储在同一个写事务里维护。
- ``game_lesson_runs``：经验反思的进度，每次一行；``to_memory_id`` 是读到的记忆表位置（按 Bot 算游标）。

游玩经历本身由 Cortico 写回记忆表（source=game_event），存档状态留在 Cortico。本迁移不读取、不改写任何旧表；
全部 ``IF NOT EXISTS``，缺列只补列，重复执行无副作用。
"""

from __future__ import annotations

from ..connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_libraries (
    game_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    lessons_enabled INTEGER NOT NULL DEFAULT 1,
    state_keys TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS game_knowledge_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    card_key TEXT NOT NULL,
    title TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    points TEXT NOT NULL DEFAULT '[]',
    pitfalls TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
    conditions TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'archived')),
    version INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    bot_id TEXT NOT NULL DEFAULT '',
    UNIQUE (game_id, card_key)
);
CREATE TABLE IF NOT EXISTS game_knowledge_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    term_key TEXT NOT NULL,
    term TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    explanation TEXT NOT NULL DEFAULT '',
    related TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'archived')),
    version INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (game_id, term_key)
);
CREATE TABLE IF NOT EXISTS game_lesson_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    game_id TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
    started_at REAL NOT NULL,
    finished_at REAL,
    from_memory_id INTEGER NOT NULL DEFAULT 0,
    to_memory_id INTEGER NOT NULL DEFAULT 0,
    events INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    counts TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
)
"""
# 索引与检索表在补列之后建：更早版本留下的表缺列时，先补列再建索引
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_game_knowledge_cards_game_status
    ON game_knowledge_cards (game_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_game_knowledge_terms_game_status
    ON game_knowledge_terms (game_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_game_knowledge_cards_owner
    ON game_knowledge_cards (game_id, bot_id, status);
CREATE INDEX IF NOT EXISTS idx_game_lesson_runs_bot
    ON game_lesson_runs (bot_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_game_cards USING fts5(tokens, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_game_terms USING fts5(tokens, tokenize='unicode61')
"""

# 之后新增的列在这里登记：旧库缺列时补上（ALTER TABLE ... ADD COLUMN），不改已有数据。
_COLUMNS: dict[str, dict[str, str]] = {
    "game_libraries": {
        "aliases": "TEXT NOT NULL DEFAULT '[]'",
        "description": "TEXT NOT NULL DEFAULT ''",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "lessons_enabled": "INTEGER NOT NULL DEFAULT 1",
        "state_keys": "TEXT NOT NULL DEFAULT '{}'",
    },
    "game_knowledge_cards": {
        "status": "TEXT NOT NULL DEFAULT 'draft'",
        "stage": "TEXT NOT NULL DEFAULT ''",
        "topic": "TEXT NOT NULL DEFAULT ''",
        "points": "TEXT NOT NULL DEFAULT '[]'",
        "pitfalls": "TEXT NOT NULL DEFAULT '[]'",
        "keywords": "TEXT NOT NULL DEFAULT '[]'",
        "conditions": "TEXT NOT NULL DEFAULT '[]'",
        "source": "TEXT NOT NULL DEFAULT ''",
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "source_type": "TEXT NOT NULL DEFAULT 'manual'",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "note": "TEXT NOT NULL DEFAULT ''",
        "meta": "TEXT NOT NULL DEFAULT '{}'",
        "bot_id": "TEXT NOT NULL DEFAULT ''",
    },
    "game_knowledge_terms": {
        "status": "TEXT NOT NULL DEFAULT 'draft'",
        "aliases": "TEXT NOT NULL DEFAULT '[]'",
        "explanation": "TEXT NOT NULL DEFAULT ''",
        "related": "TEXT NOT NULL DEFAULT '[]'",
        "source": "TEXT NOT NULL DEFAULT ''",
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "source_type": "TEXT NOT NULL DEFAULT 'manual'",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "note": "TEXT NOT NULL DEFAULT ''",
        "meta": "TEXT NOT NULL DEFAULT '{}'",
    },
}

GAME_KNOWLEDGE_TABLES = (
    "game_libraries",
    "game_knowledge_cards",
    "game_knowledge_terms",
    "fts_game_cards",
    "fts_game_terms",
    "game_lesson_runs",
)


def ensure_game_knowledge_schema(cm: ConnectionManager) -> None:
    if not hasattr(cm, "migration_transaction"):
        raise TypeError("cm must provide migration_transaction")
    tables = tuple(s.strip() for s in _SCHEMA.split(";") if s.strip())
    indexes = tuple(s.strip() for s in _INDEXES.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in tables:
            tx.execute(statement)
        for table, columns in _COLUMNS.items():
            existing = {str(row[1]) for row in tx.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    tx.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        for statement in indexes:
            tx.execute(statement)


__all__ = ["GAME_KNOWLEDGE_TABLES", "ensure_game_knowledge_schema"]
