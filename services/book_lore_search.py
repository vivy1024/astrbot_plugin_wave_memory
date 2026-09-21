"""BookLore 统一检索服务：供 AstrBot 工具与 Cortico Runtime API 共用。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from ..domain.scope import CatalogScope, validate_formal_command_scope
    from ..services.identity_safety import is_identity_contamination
except ImportError:
    from domain.scope import CatalogScope, validate_formal_command_scope
    from services.identity_safety import is_identity_contamination


@dataclass(frozen=True)
class BookLoreSearchItem:
    id: str
    resource: str
    title: str
    content: str
    score: float
    rank: int
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource": self.resource,
            "title": self.title,
            "content": self.content,
            "scores": {"score": round(self.score, 4), "rank": self.rank},
            "provenance": self.provenance,
        }


class BookLoreSearchService:
    """提供统一的 semantic / keyword / hybrid 书设检索与有界图遍历。"""

    def __init__(
        self,
        *,
        lore_db_path: str | Path,
        book_lore_index: Any = None,
        embedding_service: Any = None,
    ):
        raw = str(lore_db_path or "").strip()
        self.db_path = Path(raw).expanduser().resolve() if raw else None
        self.index = book_lore_index
        self.embedding = embedding_service

    def _connect(self):
        if not self.db_path or not self.db_path.is_file():
            raise RuntimeError("book_lore_database_unavailable")
        uri = self.db_path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    async def search(
        self,
        *,
        query: str,
        scope: CatalogScope,
        mode: str = "hybrid",
        resources: Sequence[str] = ("entities", "communities", "notes"),
        top_k: int = 5,
        min_score: float = 0.3,
        entity_types: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        decision = validate_formal_command_scope("catalog.read", scope)
        if not decision.allowed:
            raise PermissionError(decision.reason_code or "catalog_scope_required")

        q = str(query or "").strip()
        if not q:
            return {"items": [], "mode": mode, "degraded": False, "warnings": []}

        norm_mode = mode.lower().strip()
        top_k = max(1, min(int(top_k or 5), 20))
        allowed_res = set(resources or ("entities", "communities", "notes"))
        allowed_types = {str(t).strip() for t in (entity_types or []) if str(t).strip()}

        warnings: list[str] = []
        effective_mode = norm_mode
        vector = None

        if norm_mode in {"semantic", "hybrid"}:
            if self.embedding is not None and self.index is not None:
                try:
                    vector = await self.embedding.get_embedding(q)
                except Exception as exc:
                    warnings.append(f"embedding_error: {str(exc)[:80]}")
            if vector is None:
                if norm_mode == "semantic":
                    return {"items": [], "mode": "semantic", "degraded": True, "warnings": warnings + ["embedding_unavailable"]}
                effective_mode = "keyword"
                warnings.append("degraded_to_keyword")

        conn = self._connect()
        try:
            candidates: list[BookLoreSearchItem] = []

            # 1. Semantic 候选采集
            if vector is not None and self.index is not None:
                if "entities" in allowed_res and hasattr(self.index, "search_entities"):
                    for eid, score in (self.index.search_entities(vector, k=top_k * 2) or []):
                        if score < min_score:
                            continue
                        row = conn.execute(
                            "SELECT id, title, type, description, book_name FROM book_entities WHERE id = ?",
                            (str(eid),),
                        ).fetchone()
                        if not row:
                            continue
                        etype = str(row["type"] or "")
                        if allowed_types and etype not in allowed_types:
                            continue
                        desc = str(row["description"] or "")[:1000]
                        if is_identity_contamination(f"{row['title']} {desc}"):
                            continue
                        candidates.append(BookLoreSearchItem(
                            id=str(row["id"]),
                            resource="entities",
                            title=f"{row['title']}({etype})" if etype else str(row["title"]),
                            content=desc,
                            score=float(score),
                            rank=1,
                            provenance={"book_name": row["book_name"], "entity_type": etype},
                        ))

                if "communities" in allowed_res and hasattr(self.index, "search_communities"):
                    for cid, score in (self.index.search_communities(vector, k=top_k * 2) or []):
                        if score < min_score:
                            continue
                        row = conn.execute(
                            "SELECT id, title, summary, book_name, level, rank FROM book_communities WHERE id = ?",
                            (str(cid),),
                        ).fetchone()
                        if not row:
                            continue
                        summary = str(row["summary"] or "")[:1000]
                        if is_identity_contamination(f"{row['title']} {summary}"):
                            continue
                        candidates.append(BookLoreSearchItem(
                            id=str(row["id"]),
                            resource="communities",
                            title=str(row["title"] or ""),
                            content=summary,
                            score=float(score),
                            rank=1,
                            provenance={"book_name": row["book_name"], "level": row["level"]},
                        ))

                if "notes" in allowed_res and hasattr(self.index, "search_notes"):
                    for nid, score in (self.index.search_notes(vector, k=top_k * 2) or []):
                        if score < min_score:
                            continue
                        row = conn.execute(
                            "SELECT id, title, content, book_name, arc, category, source_file FROM book_notes WHERE id = ?",
                            (str(nid),),
                        ).fetchone()
                        if not row:
                            continue
                        content = str(row["content"] or "")[:1000]
                        if is_identity_contamination(f"{row['title']} {content}"):
                            continue
                        candidates.append(BookLoreSearchItem(
                            id=str(row["id"]),
                            resource="notes",
                            title=str(row["title"] or ""),
                            content=content,
                            score=float(score),
                            rank=1,
                            provenance={"book_name": row["book_name"], "arc": row["arc"], "source_file": row["source_file"]},
                        ))

            # 2. Keyword 补充采集
            if effective_mode in {"keyword", "hybrid"} and len(candidates) < top_k:
                like_pat = f"%{q}%"
                if "entities" in allowed_res:
                    rows = conn.execute(
                        "SELECT id, title, type, description, book_name FROM book_entities WHERE title LIKE ? OR description LIKE ? LIMIT ?",
                        (like_pat, like_pat, top_k),
                    ).fetchall()
                    for row in rows:
                        etype = str(row["type"] or "")
                        if allowed_types and etype not in allowed_types:
                            continue
                        desc = str(row["description"] or "")[:1000]
                        if is_identity_contamination(f"{row['title']} {desc}"):
                            continue
                        candidates.append(BookLoreSearchItem(
                            id=str(row["id"]),
                            resource="entities",
                            title=f"{row['title']}({etype})" if etype else str(row["title"]),
                            content=desc,
                            score=0.5,
                            rank=2,
                            provenance={"book_name": row["book_name"], "entity_type": etype, "match": "keyword"},
                        ))

                if "communities" in allowed_res and len(candidates) < top_k:
                    rows = conn.execute(
                        "SELECT id, title, summary, book_name, level FROM book_communities WHERE title LIKE ? OR summary LIKE ? LIMIT ?",
                        (like_pat, like_pat, top_k),
                    ).fetchall()
                    for row in rows:
                        summary = str(row["summary"] or "")[:1000]
                        if is_identity_contamination(f"{row['title']} {summary}"):
                            continue
                        candidates.append(BookLoreSearchItem(
                            id=str(row["id"]),
                            resource="communities",
                            title=str(row["title"] or ""),
                            content=summary,
                            score=0.45,
                            rank=2,
                            provenance={"book_name": row["book_name"], "level": row["level"], "match": "keyword"},
                        ))

            # 3. 去重与截断
            deduped: list[BookLoreSearchItem] = []
            seen: set[str] = set()
            for item in sorted(candidates, key=lambda x: (x.rank, -x.score)):
                if item.id in seen:
                    continue
                seen.add(item.id)
                deduped.append(item)
                if len(deduped) >= top_k:
                    break

            return {
                "items": [it.to_dict() for it in deduped],
                "mode": effective_mode,
                "degraded": effective_mode != norm_mode,
                "warnings": warnings,
            }
        finally:
            conn.close()

    async def graph_traverse(
        self,
        *,
        entity_name: str,
        scope: CatalogScope,
        depth: int = 1,
        max_edges: int = 20,
    ) -> dict[str, Any]:
        decision = validate_formal_command_scope("catalog.read", scope)
        if not decision.allowed:
            raise PermissionError(decision.reason_code or "catalog_scope_required")

        name = str(entity_name or "").strip()
        if not name:
            return {"entity": None, "relations": []}

        depth = max(1, min(int(depth or 1), 2))
        max_edges = max(1, min(int(max_edges or 20), 50))

        conn = self._connect()
        try:
            root = conn.execute(
                "SELECT id, title, type, description FROM book_entities WHERE title = ? OR title LIKE ? LIMIT 1",
                (name, f"%{name}%"),
            ).fetchone()
            if not root:
                return {"entity": None, "relations": []}

            current_titles = {str(root["title"])}
            edges: list[dict[str, Any]] = []

            for _ in range(depth):
                if not current_titles or len(edges) >= max_edges:
                    break
                next_titles = set()
                placeholders = ",".join("?" for _ in current_titles)
                rows = conn.execute(
                    f"SELECT id, source_title, target_title, description, weight FROM book_relations "
                    f"WHERE source_title IN ({placeholders}) OR target_title IN ({placeholders}) LIMIT ?",
                    (*tuple(current_titles), *tuple(current_titles), max_edges - len(edges)),
                ).fetchall()
                for r in rows:
                    edges.append({
                        "id": str(r["id"]),
                        "source": str(r["source_title"]),
                        "target": str(r["target_title"]),
                        "description": str(r["description"] or "")[:300],
                        "weight": float(r["weight"] or 1.0),
                    })
                    next_titles.add(str(r["source_title"]))
                    next_titles.add(str(r["target_title"]))
                current_titles = next_titles - current_titles

            return {
                "entity": {
                    "id": str(root["id"]),
                    "title": str(root["title"]),
                    "type": str(root["type"] or ""),
                    "description": str(root["description"] or "")[:500],
                },
                "relations": edges,
            }
        finally:
            conn.close()
