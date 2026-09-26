"""纯增量、可重复的 scoped derived knowledge schema 迁移。

本迁移只创建或 ALTER ``scoped_*`` 表和索引：不会 ALTER、UPDATE、回填或以任何
方式读取后改写 legacy 的 jargon/facts/tags/memory_tags/tag_relations/beliefs/kv_store。
旧数据没有可验证的完整 RuntimeScope，因此只能由显式审核迁移流程处理。
"""

from __future__ import annotations

import time
import unicodedata

from ..connection import ConnectionManager


_SCOPED_DERIVED_KNOWLEDGE_SCHEMA = """
/* tag_catalog is a global semantic vocabulary only. It never stores memory content
   and is joined back to scoped_tags before any memory/query operation. */
CREATE TABLE IF NOT EXISTS tag_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    tag_type TEXT NOT NULL DEFAULT 'keyword',
    description TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    embedding_model TEXT,
    embedding_dim INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (normalized_name, tag_type)
);

CREATE TABLE IF NOT EXISTS scoped_jargon (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    word TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    is_jargon INTEGER,
    frequency INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.0,
    contexts TEXT NOT NULL DEFAULT '[]',
    source_memory_id INTEGER,
    source_context TEXT,
    provenance TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (bot_id, session_id, visibility, word)
);

CREATE TABLE IF NOT EXISTS scoped_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending',
    source_memory_id INTEGER,
    provenance TEXT NOT NULL DEFAULT '{}',
    valid_from REAL,
    valid_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE (bot_id, session_id, visibility, subject, predicate, object)
);

CREATE TABLE IF NOT EXISTS scoped_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id INTEGER,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    name TEXT NOT NULL,
    tag_type TEXT NOT NULL DEFAULT 'keyword',
    description TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (bot_id, session_id, visibility, name)
);

CREATE TABLE IF NOT EXISTS scoped_memory_tags (
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    memory_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    relevance REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    PRIMARY KEY (bot_id, session_id, visibility, memory_id, tag_id),
    FOREIGN KEY (tag_id) REFERENCES scoped_tags(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scoped_tag_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    source_tag_id INTEGER NOT NULL,
    target_tag_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    valid_until REAL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (bot_id, session_id, visibility, source_tag_id, target_tag_id, relation_type),
    FOREIGN KEY (source_tag_id) REFERENCES scoped_tags(id) ON DELETE CASCADE,
    FOREIGN KEY (target_tag_id) REFERENCES scoped_tags(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scoped_beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    belief_key TEXT NOT NULL,
    content TEXT NOT NULL,
    belief_type TEXT NOT NULL DEFAULT 'world_view',
    strength REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending',
    source_memory_id INTEGER,
    provenance TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (bot_id, session_id, visibility, belief_key)
);

CREATE TABLE IF NOT EXISTS scoped_belief_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    belief_id INTEGER NOT NULL,
    window_key TEXT NOT NULL,
    polarity TEXT NOT NULL CHECK (polarity IN ('support', 'challenge')),
    memory_ids TEXT NOT NULL DEFAULT '[]',
    participants TEXT NOT NULL DEFAULT '[]',
    source_tags TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    window_started_at REAL,
    window_ended_at REAL,
    observed_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (bot_id, session_id, visibility, belief_id, window_key, polarity),
    FOREIGN KEY (belief_id) REFERENCES scoped_beliefs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scoped_consolidation_cursors (
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'group'),
    cursor_name TEXT NOT NULL,
    cursor_value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, session_id, visibility, cursor_name)
);

CREATE INDEX IF NOT EXISTS idx_tag_catalog_status
    ON tag_catalog (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_scoped_jargon_scope_status
    ON scoped_jargon (bot_id, session_id, visibility, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_scoped_facts_scope_subject
    ON scoped_facts (bot_id, session_id, visibility, subject, updated_at);
-- v5.1：scoped_tags 的 UNIQUE(bot_id, session_id, visibility, name) 与 scoped_memory_tags 的主键
-- (bot_id, session_id, visibility, memory_id, tag_id) 已覆盖这两组前缀，单独的索引只是重复存储
DROP INDEX IF EXISTS idx_scoped_tags_scope_name;
DROP INDEX IF EXISTS idx_scoped_memory_tags_scope_memory;
CREATE INDEX IF NOT EXISTS idx_scoped_memory_tags_scope_tag
    ON scoped_memory_tags (bot_id, session_id, visibility, tag_id, memory_id);
CREATE INDEX IF NOT EXISTS idx_scoped_memory_tags_tag
    ON scoped_memory_tags (tag_id, memory_id);
CREATE INDEX IF NOT EXISTS idx_scoped_tag_relations_scope_source
    ON scoped_tag_relations (bot_id, session_id, visibility, source_tag_id);
CREATE INDEX IF NOT EXISTS idx_scoped_beliefs_scope_status
    ON scoped_beliefs (bot_id, session_id, visibility, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_scoped_belief_observations_scope_belief
    ON scoped_belief_observations (bot_id, session_id, visibility, belief_id, observed_at);
"""


def _columns(tx, table: str) -> set[str]:
    return {str(row[1]) for row in tx.execute(f'PRAGMA table_info("{table}")').fetchall()}


def ensure_scoped_derived_knowledge_schema(cm: ConnectionManager) -> None:
    """建立或幂等升级 scoped 派生知识表，且绝不触碰 legacy 表。"""
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    # sqlite3.Connection.executescript() 会隐式提交未完成事务；逐句执行才能
    # 保持 ConnectionManager 提供的单一原子 migration_transaction 语义。
    statements = tuple(
        statement.strip()
        for statement in _SCOPED_DERIVED_KNOWLEDGE_SCHEMA.split(";")
        if statement.strip()
    )
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)

        # Older scoped databases predate the canonical semantic Tag catalog.  The
        # column is additive and nullable during recovery; formal writers fill it
        # whenever the catalog table is available.  Legacy tags/memory_tags are
        # deliberately not read here.
        tag_columns = _columns(tx, "scoped_tags")
        if "catalog_id" not in tag_columns:
            tx.execute("ALTER TABLE scoped_tags ADD COLUMN catalog_id INTEGER")

        now = time.time()
        scoped_rows = tx.execute(
            "SELECT id, name, tag_type, description FROM scoped_tags "
            "WHERE catalog_id IS NULL"
        ).fetchall()
        for row in scoped_rows:
            tag_id, name, tag_type, description = row
            normalized_name = unicodedata.normalize("NFKC", str(name or "")).strip()
            if not normalized_name:
                continue
            normalized_type = str(tag_type or "keyword").strip() or "keyword"
            tx.execute(
                """INSERT INTO tag_catalog(
                       normalized_name, display_name, tag_type, description,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                   ON CONFLICT(normalized_name, tag_type) DO UPDATE SET
                       display_name=CASE WHEN tag_catalog.display_name='' THEN excluded.display_name
                                         ELSE tag_catalog.display_name END,
                       description=CASE WHEN tag_catalog.description='' THEN excluded.description
                                        ELSE tag_catalog.description END,
                       updated_at=excluded.updated_at""",
                (normalized_name, normalized_name, normalized_type, str(description or ""), now, now),
            )
            catalog = tx.execute(
                "SELECT id FROM tag_catalog WHERE normalized_name=? AND tag_type=?",
                (normalized_name, normalized_type),
            ).fetchone()
            if catalog is not None:
                tx.execute("UPDATE scoped_tags SET catalog_id=? WHERE id=?", (int(catalog[0]), int(tag_id)))

        tx.execute(
            "CREATE INDEX IF NOT EXISTS idx_scoped_tags_scope_catalog "
            "ON scoped_tags (bot_id, session_id, visibility, catalog_id)"
        )

        # 旧版 scoped schema 允许原地纯增量升级；这里只 ALTER scoped_* 表，
        # 不读取、回填或修改任何 legacy facts/tag_relations 表。
        fact_columns = _columns(tx, "scoped_facts")
        if "revision" not in fact_columns:
            tx.execute("ALTER TABLE scoped_facts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")

        relation_columns = _columns(tx, "scoped_tag_relations")
        if "status" not in relation_columns:
            tx.execute(
                "ALTER TABLE scoped_tag_relations ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        if "valid_until" not in relation_columns:
            tx.execute("ALTER TABLE scoped_tag_relations ADD COLUMN valid_until REAL")
        if "revision" not in relation_columns:
            tx.execute(
                "ALTER TABLE scoped_tag_relations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )


__all__ = ["ensure_scoped_derived_knowledge_schema"]
