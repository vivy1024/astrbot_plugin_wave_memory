import pytest
import sqlite3
from pathlib import Path
from domain.scope import CatalogScope
from services.book_lore_search import BookLoreSearchService

@pytest.fixture
def temp_lore_db(tmp_path: Path):
    db_file = tmp_path / "book_lore.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE book_entities (
            id TEXT PRIMARY KEY, title TEXT, type TEXT, description TEXT, book_name TEXT, frequency INT, degree INT, vector BLOB
        )
    """)
    conn.execute("""
        CREATE TABLE book_communities (
            id TEXT PRIMARY KEY, title TEXT, summary TEXT, full_content TEXT, level INT, rank REAL, book_name TEXT, vector BLOB
        )
    """)
    conn.execute("""
        CREATE TABLE book_notes (
            id TEXT PRIMARY KEY, book_name TEXT, arc TEXT, category TEXT, title TEXT, content TEXT, source_file TEXT, vector BLOB
        )
    """)
    conn.execute("""
        CREATE TABLE book_relations (
            id TEXT PRIMARY KEY, source_title TEXT, target_title TEXT, description TEXT, weight REAL, book_name TEXT
        )
    """)
    conn.execute("INSERT INTO book_entities VALUES ('e1', '磁极神君', '人物', '万法大学资深修士与研究领袖，张羽的师尊。', '没钱修什么仙', 10, 5, NULL)")
    conn.execute("INSERT INTO book_entities VALUES ('e2', '张羽', '人物', '没钱修什么仙主角，拥有羽书系统。', '没钱修什么仙', 20, 10, NULL)")
    conn.execute("INSERT INTO book_relations VALUES ('r1', '磁极神君', '张羽', '师徒关系，曾传授道种复制条件与法海试作方案。', 0.9, '没钱修什么仙')")
    conn.commit()
    conn.close()
    return db_file

@pytest.mark.asyncio
async def test_book_lore_keyword_and_graph(temp_lore_db: Path):
    service = BookLoreSearchService(lore_db_path=temp_lore_db)
    scope = CatalogScope(catalog_id="book-lore", corpus_id="default", version="current")
    
    # 关键词检索测试
    res = await service.search(query="磁极神君", scope=scope, mode="keyword")
    assert len(res["items"]) >= 1
    assert "磁极神君" in res["items"][0]["title"]
    assert res["degraded"] is False

    # 图谱遍历测试
    graph = await service.graph_traverse(entity_name="磁极神君", scope=scope)
    assert graph["entity"] is not None
    assert graph["entity"]["title"] == "磁极神君"
    assert len(graph["relations"]) == 1
    assert graph["relations"][0]["target"] == "张羽"
