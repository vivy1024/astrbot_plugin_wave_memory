"""群聊看图识梗缓存表的幂等增量迁移。

启动阶段由数据库层统一调用；此表只缓存图片哈希到短摘要的映射与频次统计，
不直接修改 memories 或其他业务表。
"""

from __future__ import annotations

from .scoped_learning_projections import _columns, _transaction


_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS image_digest_cache (
    image_hash TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    is_sticker INTEGER NOT NULL DEFAULT 0 CHECK(is_sticker IN (0, 1)),
    hit_count INTEGER NOT NULL DEFAULT 1 CHECK(hit_count >= 0),
    last_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'resolved', 'failed')),
    source TEXT NOT NULL DEFAULT '' CHECK(source IN ('', 'onebot_summary', 'vision_llm', 'manual')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)"""

_COLUMN_ADDITIONS = (
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("is_sticker", "INTEGER NOT NULL DEFAULT 0"),
    ("hit_count", "INTEGER NOT NULL DEFAULT 1"),
    ("last_url", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("source", "TEXT NOT NULL DEFAULT ''"),
    ("created_at", "REAL NOT NULL DEFAULT 0"),
    ("updated_at", "REAL NOT NULL DEFAULT 0"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_image_digest_status_updated "
    "ON image_digest_cache(status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_image_digest_source_updated "
    "ON image_digest_cache(source, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_image_digest_hits "
    "ON image_digest_cache(hit_count DESC, updated_at DESC)",
)


def _upgrade_columns(connection) -> None:
    existing = _columns(connection, "image_digest_cache")
    for name, definition in _COLUMN_ADDITIONS:
        if name not in existing:
            connection.execute(f"ALTER TABLE image_digest_cache ADD COLUMN {name} {definition}")


def ensure_image_digest_schema(conn) -> None:
    """幂等创建 image_digest_cache 表与索引；兼容 ConnectionManager、_ConnProxy 与 sqlite3.Connection。"""
    def apply(tx) -> None:
        tx.execute(_CREATE_TABLE)
        _upgrade_columns(tx)
        for statement in _INDEXES:
            tx.execute(statement)

    if bool(getattr(conn, "in_transaction", False)):
        apply(conn)
    else:
        migration_tx = getattr(conn, "migration_transaction", None)
        if callable(migration_tx):
            with migration_tx() as tx:
                apply(tx)
        else:
            with _transaction(conn) as tx:
                apply(tx)


__all__ = ["ensure_image_digest_schema"]
