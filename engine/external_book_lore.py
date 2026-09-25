"""真实外部 BookLore SQLite 的只读访问边界。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from ..domain.scope import CatalogScope, validate_formal_command_scope
except ImportError:  # pragma: no cover - 独立导入兼容
    from domain.scope import CatalogScope, validate_formal_command_scope


BOOK_LORE_TABLES = {
    "entities": "book_entities",
    "relations": "book_relations",
    "communities": "book_communities",
    "notes": "book_notes",
}


class ExternalBookLoreError(RuntimeError):
    """外部 BookLore 读取失败。"""


class ExternalBookLoreScopeError(ExternalBookLoreError):
    """读取没有携带有效的显式 CatalogScope。"""


class ExternalBookLoreSchemaError(ExternalBookLoreError):
    """外部库 schema 不满足读取要求。"""


class ExternalBookLoreStore:
    """仅以 SQLite ``mode=ro`` 打开真实 BookLore 数据库。

    Store 不创建文件、不迁移 schema、不回退 WaveMemory 主库。每次公开读取都要求调用方
    显式提供 ``CatalogScope``，避免把 raw catalog 猜测成 Bot/会话学习作用域。
    """

    def __init__(self, db_path: str | Path):
        raw_path = str(db_path or "").strip()
        if not raw_path:
            raise ExternalBookLoreError("book_lore_db_path_required")
        self.db_path = Path(raw_path).expanduser().resolve()

    def _require_scope(self, scope: CatalogScope | None) -> CatalogScope:
        if not isinstance(scope, CatalogScope):
            raise ExternalBookLoreScopeError("catalog_scope_required")
        decision = validate_formal_command_scope("catalog.read", scope)
        if not decision.allowed:
            raise ExternalBookLoreScopeError(decision.reason_code or "catalog_scope_required")
        return scope

    @contextmanager
    def connect(self, *, scope: CatalogScope):
        """打开生命周期受控的只读连接；文件缺失时不会被 SQLite 自动创建。"""
        self._require_scope(scope)
        if not self.db_path.is_file():
            raise ExternalBookLoreError("book_lore_database_unavailable")
        uri = self.db_path.as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise ExternalBookLoreError("book_lore_database_unavailable") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            yield conn
        finally:
            conn.close()

    def schema_info(self, *, scope: CatalogScope) -> dict[str, Any]:
        """返回基于表、列、类型、非空和主键声明的稳定 schema fingerprint。"""
        with self.connect(scope=scope) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?, ?) ORDER BY name",
                tuple(sorted(BOOK_LORE_TABLES.values())),
            ).fetchall()
            tables: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                table = str(row[0])
                columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                tables[table] = [
                    {
                        "name": str(column[1]),
                        "type": str(column[2] or "").upper(),
                        "notnull": bool(column[3]),
                        "pk": int(column[5] or 0),
                    }
                    for column in columns
                ]
        canonical = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "fingerprint": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "tables": tables,
            "missing_tables": sorted(set(BOOK_LORE_TABLES.values()) - set(tables)),
        }

    def schema_fingerprint(self, *, scope: CatalogScope) -> str:
        return str(self.schema_info(scope=scope)["fingerprint"])

    def counts(self, *, scope: CatalogScope) -> dict[str, int]:
        with self.connect(scope=scope) as conn:
            existing = self._existing_tables(conn)
            return {
                resource: self._count(conn, table) if table in existing else 0
                for resource, table in BOOK_LORE_TABLES.items()
            }

    def list_entities(self, *, scope: CatalogScope, **options: Any) -> dict[str, Any]:
        return self._list_resource("entities", scope=scope, search_columns=("title", "description", "book_name"), **options)

    def list_relations(self, *, scope: CatalogScope, **options: Any) -> dict[str, Any]:
        return self._list_resource("relations", scope=scope, search_columns=("source_title", "target_title", "description"), **options)

    def list_communities(self, *, scope: CatalogScope, **options: Any) -> dict[str, Any]:
        return self._list_resource("communities", scope=scope, search_columns=("title", "summary", "book_name"), **options)

    def list_notes(self, *, scope: CatalogScope, **options: Any) -> dict[str, Any]:
        return self._list_resource("notes", scope=scope, search_columns=("title", "content", "book_name", "arc", "category"), **options)

    def communities_by_ids(self, ids: Sequence[Any], *, scope: CatalogScope) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(ids))
        if not normalized:
            self._require_scope(scope)
            return []
        with self.connect(scope=scope) as conn:
            columns = self._columns(conn, BOOK_LORE_TABLES["communities"])
            self._require_columns("book_communities", columns, ("id", "title", "summary"))
            placeholders = ",".join("?" for _ in normalized)
            rows = conn.execute(
                f"SELECT id, title, summary FROM book_communities WHERE id IN ({placeholders})",
                tuple(normalized),
            ).fetchall()
        by_id = {str(row["id"]): self._row_dict(row) for row in rows}
        return [by_id[str(item)] for item in normalized if str(item) in by_id]

    def notes_by_ids(self, ids: Sequence[Any], *, scope: CatalogScope) -> list[dict[str, Any]]:
        """按 id 取笔记（章节事件、人物、世界观等），顺序同 ids。"""
        normalized = list(dict.fromkeys(str(item) for item in ids))
        if not normalized:
            self._require_scope(scope)
            return []
        with self.connect(scope=scope) as conn:
            columns = self._columns(conn, BOOK_LORE_TABLES["notes"])
            self._require_columns("book_notes", columns, ("id", "title", "content"))
            category = "category" if "category" in columns else "'' AS category"
            placeholders = ",".join("?" for _ in normalized)
            rows = conn.execute(
                f"SELECT id, title, content, {category} FROM book_notes WHERE id IN ({placeholders})",
                tuple(normalized),
            ).fetchall()
        by_id = {str(row["id"]): self._row_dict(row) for row in rows}
        return [by_id[item] for item in normalized if item in by_id]

    def sample_communities(
        self,
        *,
        scope: CatalogScope,
        candidate_limit: int,
        min_rank: float = 7.0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(candidate_limit), 500))
        with self.connect(scope=scope) as conn:
            columns = self._columns(conn, "book_communities")
            self._require_columns("book_communities", columns, ("id", "title", "summary"))
            rank_expr = "rank" if "rank" in columns else "0.0 AS rank"
            where_rank = " AND rank >= ?" if "rank" in columns else ""
            params: tuple[Any, ...] = (float(min_rank), limit) if where_rank else (limit,)
            rows = conn.execute(
                f"SELECT id, title, summary, {rank_expr} FROM book_communities "
                f"WHERE summary IS NOT NULL AND TRIM(summary) != ''{where_rank} "
                f"ORDER BY {('rank DESC, ' if 'rank' in columns else '')}id ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def _list_resource(
        self,
        resource: str,
        *,
        scope: CatalogScope,
        limit: int = 50,
        offset: int = 0,
        search: str = "",
        sort: str = "id",
        filter: str = "",
        search_columns: Iterable[str] = (),
    ) -> dict[str, Any]:
        table = BOOK_LORE_TABLES[resource]
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self.connect(scope=scope) as conn:
            columns = self._columns(conn, table)
            self._require_columns(table, columns, ("id",))
            clauses: list[str] = []
            params: list[Any] = []
            usable_search = [column for column in search_columns if column in columns]
            if search and usable_search:
                clauses.append("(" + " OR ".join(f'"{column}" LIKE ?' for column in usable_search) + ")")
                params.extend([f"%{search}%"] * len(usable_search))
            if filter:
                filter_column = next((name for name in ("book_name", "category", "type") if name in columns), None)
                if filter_column:
                    clauses.append(f'"{filter_column}" = ?')
                    params.append(filter)
            where = " AND ".join(clauses)
            order_column = str(sort or "id").lstrip("-")
            if order_column not in columns:
                order_column = "id"
            direction = "DESC" if str(sort).startswith("-") else "ASC"
            total = self._count(conn, table, where, tuple(params))
            rows = conn.execute(
                f'SELECT * FROM "{table}"' + (f" WHERE {where}" if where else "")
                + f' ORDER BY "{order_column}" {direction} LIMIT ? OFFSET ?',
                tuple(params) + (limit, offset),
            ).fetchall()
        return {
            "items": [self._row_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search,
            "sort": sort,
            "filter": filter,
        }

    @staticmethod
    def _existing_tables(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        if table not in ExternalBookLoreStore._existing_tables(conn):
            return set()
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}

    @staticmethod
    def _require_columns(table: str, columns: set[str], required: Sequence[str]) -> None:
        missing = sorted(set(required) - columns)
        if missing:
            raise ExternalBookLoreSchemaError(f"book_lore_schema_mismatch:{table}:{','.join(missing)}")

    @staticmethod
    def _count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table}"' + (f" WHERE {where}" if where else ""),
            params,
        ).fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: (f"<bytes:{len(row[key])}>" if isinstance(row[key], bytes) else row[key])
            for key in row.keys()
        }


__all__ = [
    "BOOK_LORE_TABLES",
    "ExternalBookLoreError",
    "ExternalBookLoreSchemaError",
    "ExternalBookLoreScopeError",
    "ExternalBookLoreStore",
]
