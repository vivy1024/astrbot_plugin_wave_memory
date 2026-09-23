from __future__ import annotations

import numpy as np

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB


def _scope(group_id: str = "g1", bot_id: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(
            id=f"qq:group:{group_id}",
            platform_id="qq",
            kind="group",
            conversation_id=group_id,
        ),
    )


def _memory(db: WaveMemoryDB, scope: RuntimeScope, *, content: str, source: str = "chat") -> int:
    return db.add_memory(
        group_id=scope.session.conversation_id,
        content=content,
        vector=np.asarray([0.1, 0.2, 0.3], dtype=np.float32),
        sender_id="u1",
        sender_name="User",
        timestamp=1_800_000_000.0,
        importance=1.0,
        source=source,
        scope=scope,
        provenance={},
        origin_metadata={},
    )


def test_scoped_cold_candidates_use_effective_tags_and_never_cross_scope(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "cold.sqlite"), dimension=3)
    scope = _scope()
    other_scope = _scope("g2")
    try:
        cold_id = _memory(db, scope, content="当前 Scope 的冷记忆")
        tag_id = db.upsert_scoped_tag(scope, name="冷召回", tag_type="topic", confidence=0.9, metadata={})
        db.link_scoped_memory_tag(scope, memory_id=cold_id, tag_id=tag_id, relevance=0.8)

        other_id = _memory(db, other_scope, content="另一个 Scope 的相同标签")
        other_tag = db.upsert_scoped_tag(other_scope, name="冷召回", tag_type="topic", confidence=0.9, metadata={})
        db.link_scoped_memory_tag(other_scope, memory_id=other_id, tag_id=other_tag, relevance=1.0)

        noise_id = _memory(db, scope, content="不该出现的噪声", source="noise")
        db.link_scoped_memory_tag(scope, memory_id=noise_id, tag_id=tag_id, relevance=1.0)

        rows = db.list_scoped_cold_memory_candidates(scope, [tag_id], limit=8)

        assert [row["id"] for row in rows] == [cold_id]
        assert rows[0]["tag_score"] == 0.8
        assert rows[0]["vector"] is not None
        assert db.list_scoped_cold_memory_candidates(scope, [other_tag], limit=8) == []
    finally:
        db.close()


def test_scoped_cold_candidates_expand_across_groups_of_same_bot_only_with_explicit_policy(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "cross-cold.sqlite"), dimension=3)
    scope = _scope("g1", "bot-a")
    same_bot_scope = _scope("g3", "bot-a")
    other_bot_scope = _scope("g2", "bot-b")
    try:
        current_id = _memory(db, scope, content="当前 Scope 的 Catalog 冷记忆")
        same_bot_id = _memory(db, same_bot_scope, content="同一 Bot 在另一个群的 Catalog 冷记忆")
        other_bot_id = _memory(db, other_bot_scope, content="另一个 Bot 的 Catalog 冷记忆")
        current_tag = db.upsert_scoped_tag(scope, name="共享话题", tag_type="topic", confidence=0.9, metadata={})
        same_bot_tag = db.upsert_scoped_tag(same_bot_scope, name="共享话题", tag_type="topic", confidence=0.9, metadata={})
        other_bot_tag = db.upsert_scoped_tag(other_bot_scope, name="共享话题", tag_type="topic", confidence=0.9, metadata={})
        db.link_scoped_memory_tag(scope, memory_id=current_id, tag_id=current_tag, relevance=1.0)
        db.link_scoped_memory_tag(same_bot_scope, memory_id=same_bot_id, tag_id=same_bot_tag, relevance=1.0)
        db.link_scoped_memory_tag(other_bot_scope, memory_id=other_bot_id, tag_id=other_bot_tag, relevance=1.0)

        tags = [current_tag, same_bot_tag, other_bot_tag]
        exact = db.list_scoped_cold_memory_candidates(scope, tags, limit=8)
        expanded = db.list_scoped_cold_memory_candidates(
            scope,
            tags,
            limit=8,
            allow_cross_group_recall=True,
        )
        catalog_id = db.get_scoped_tag_catalog_id(scope, current_tag)
        assert catalog_id is not None
        exact_links = db.list_scoped_catalog_links(scope, [catalog_id], allow_cross_group_recall=False)
        expanded_links = db.list_scoped_catalog_links(scope, [catalog_id], allow_cross_group_recall=True)

        assert [row["id"] for row in exact] == [current_id]
        # 跨群只扩展到同一 Bot 的其他群；另一个 Bot 亲历的记忆不属于当前 Bot。
        assert {row["id"] for row in expanded} == {current_id, same_bot_id}
        assert len(exact_links) == 1
        assert {row["scoped_tag_id"] for row in expanded_links} == {current_tag, same_bot_tag}
    finally:
        db.close()
