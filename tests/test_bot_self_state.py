"""Bot 自身状态属于 Bot 本人，不按群分裂。

- 心情只有一个：任何场合最近一次的心情带到所有场合；
- 私聊里发生的具体内容（心情原因、关切、经历）只在同一私聊中可见；
- 信念是 Bot 自己的：在别的群形成的信念处处可用，依据在形成它的群里校验；
- Soul 表迁移只放宽 visibility 约束，不改动任何已有数据。
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.migrations.scoped_soul_private_visibility import ensure_scoped_soul_private_visibility_connection
from services.belief_engine import BeliefEngine
from services.injection.channels.soul_state import SoulStateChannel


def _group(group_id: str, bot_id: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(bot_id, "group", SessionRef(f"qq:group:{group_id}", "qq", "group", group_id))


def _private(user_id: str = "u1", bot_id: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(
        bot_id, "private", SessionRef(f"qq:private:{user_id}", "qq", "private", user_id),
        subject_principal_id=f"qq:user:{user_id}",
    )


def _soul_text(repo, scope) -> str:
    ctx = SimpleNamespace(mode="full", config={"channels": {"soul_state": {"enabled": True}}}, scope=scope)
    return asyncio.run(SoulStateChannel(repository=repo).build(ctx)).text or ""


def test_mood_is_one_continuous_state_with_private_causes_kept_private(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "soul.sqlite"), dimension=3)
    try:
        repo = db.soul_repository
        repo.upsert_mood(_group("g1"), valence=0.6, arousal=0.4, cause="群里被夸", observed_at=1000.0)
        assert "群里被夸" in _soul_text(repo, _private("u1"))
        assert "valence=0.6" in _soul_text(repo, _group("g2"))

        repo.upsert_mood(_private("u1"), valence=-0.4, arousal=0.3, cause="他私下说要离开", observed_at=2000.0)
        in_group = _soul_text(repo, _group("g1"))
        assert "valence=-0.4" in in_group, "心情是同一个，私聊里的低落带到群里"
        assert "他私下说要离开" not in in_group, "私聊里的原因不外流"
        assert "他私下说要离开" not in _soul_text(repo, _private("u2"))
        assert "他私下说要离开" in _soul_text(repo, _private("u1"))
        assert "valence" not in _soul_text(db.soul_repository, _group("g1", bot_id="bot-b"))
    finally:
        db.close()


def test_self_timeline_follows_the_bot_but_private_entries_stay_private(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "timeline.sqlite"), dimension=3)
    try:
        repo = db.soul_repository
        repo.add_timeline_event(_group("g1"), event_summary="和大家一起通关了", emotional_weight=0.8, timestamp=1000.0)
        repo.add_timeline_event(_private("u1"), event_summary="陪他聊到深夜", emotional_weight=0.9, timestamp=2000.0)

        assert "和大家一起通关了" in _soul_text(repo, _group("g2"))
        assert "陪他聊到深夜" not in _soul_text(repo, _group("g2"))
        assert "和大家一起通关了" in _soul_text(repo, _private("u1"))
        assert "陪他聊到深夜" in _soul_text(repo, _private("u1"))
        assert "陪他聊到深夜" not in _soul_text(repo, _private("u2"))
    finally:
        db.close()


def test_beliefs_formed_in_one_group_are_the_bots_everywhere(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "beliefs.sqlite"), dimension=3)
    try:
        origin = _group("g2")
        f1 = db.upsert_scoped_fact(origin, subject="羽书", predicate="喜欢", object="下雨天", confidence=0.9, status="approved")
        f2 = db.upsert_scoped_fact(origin, subject="羽书", predicate="常在", object="雨天写诗", confidence=0.9, status="approved")
        db.scoped_knowledge.upsert_scoped_belief(
            origin, belief_key="self:rain", content="我是个喜欢雨天的人", belief_type="self_identity",
            strength=0.8, status="active", provenance={"source_fact_ids": [f1, f2]},
        )
        engine = BeliefEngine(db, llm_client=None, bot_id="bot-a")

        for scope in (_group("g1"), _private("u1")):
            details = engine.get_injection_details(scope)
            assert "我是个喜欢雨天的人" in details["text"]
        other_bot = BeliefEngine(db, llm_client=None, bot_id="bot-b")
        assert "喜欢雨天" not in other_bot.get_injection_details(_group("g1", bot_id="bot-b"))["text"]
    finally:
        db.close()


def test_private_visibility_migration_preserves_rows_indexes_and_sequence(tmp_path):
    path = tmp_path / "legacy-soul.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE scoped_soul_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility = 'group'),
            event_summary TEXT NOT NULL
        );
        CREATE INDEX idx_timeline_scope ON scoped_soul_timeline (bot_id, session_id, visibility);
        INSERT INTO scoped_soul_timeline(bot_id, session_id, visibility, event_summary)
            VALUES ('bot-a', 'qq:group:g1', 'group', '一'), ('bot-a', 'qq:group:g1', 'group', '二'),
                   ('bot-a', 'qq:group:g1', 'group', '三');
        DELETE FROM scoped_soul_timeline WHERE id=3;
        """
    )
    conn.commit()

    assert ensure_scoped_soul_private_visibility_connection(conn) == ["scoped_soul_timeline"]
    assert ensure_scoped_soul_private_visibility_connection(conn) == [], "迁移必须幂等"
    rows = conn.execute("SELECT id, event_summary FROM scoped_soul_timeline ORDER BY id").fetchall()
    assert rows == [(1, "一"), (2, "二")]
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_timeline_scope'"
    ).fetchone()
    new_id = conn.execute(
        "INSERT INTO scoped_soul_timeline(bot_id, session_id, visibility, event_summary)"
        " VALUES ('bot-a', 'qq:private:u1', 'private', '私聊')"
    ).lastrowid
    assert new_id == 4, "AUTOINCREMENT 序列必须保留，已删除的 id 不得复用"
    conn.close()


def test_private_episode_is_stored_under_private_scene_key(tmp_path):
    """私聊经历按 private:<会话ID> 保存，不会与同号的群混在一起。"""
    from services.experience_episodes import fetch_recent_episodes
    from services.system_convergence_runtime import ProductionWriteGateway

    from engine.db.migrations.v2_2_experience_rework import run_migration

    path = str(tmp_path / "episodes.sqlite")
    WaveMemoryDB(path, dimension=3).close()
    run_migration(path)

    async def main():
        gateway = ProductionWriteGateway(path)
        try:
            return await gateway.record_episode(
                scope=_private("12345678"),
                group_id="private:12345678",
                user_id="12345678",
                episode_type="shared_event",
                fields={"trigger_text": "他说考上了", "bot_action": "", "bot_reply": "恭喜",
                        "user_reaction": "开心", "outcome": "一起庆祝"},
                source_memory_ids=[],
                emotional_weight=3.0,
                idempotency_hint="p-ep-1",
            )
        finally:
            await gateway.shutdown()

    episode_id = asyncio.run(main())
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT group_id FROM experience_episodes WHERE id=?", (episode_id,)).fetchone()[0] == "private:12345678"
        assert fetch_recent_episodes(conn, bot_id="bot-a", group_id="12345678") == [], "同号的群看不到私聊经历"
        assert [row["id"] for row in fetch_recent_episodes(conn, bot_id="bot-a", group_id="private:12345678")] == [episode_id]
    finally:
        conn.close()
