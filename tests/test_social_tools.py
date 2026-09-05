from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.concern_tracker import ConcernTracker
from services.relationship_events import RelationshipEventService
from tools.cultural_moment import WaveMemoryMarkCulturalMomentTool
from tools.social_anchor import WaveMemoryNoteSocialAnchorTool
from tools.social_impression import WaveMemoryRecordSocialImpressionTool


def _scope(group: str = "g1", subject: str = "qq:user:u1") -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef(f"qq:group:{group}", "qq", "group", group),
        subject_principal_id=subject,
    )


def _context_wrapper(scope: RuntimeScope) -> SimpleNamespace:
    event = SimpleNamespace(
        _wave_memory_runtime_scope=scope,
        get_sender_id=lambda: "u1",
        get_group_id=lambda: "g1",
        get_self_id=lambda: "bot-alpha",
    )
    context = SimpleNamespace(event=event)
    return SimpleNamespace(context=context)


def _setup_test_db(tmp_path: Path):
    db_path = tmp_path / "social_tools.db"
    manager = ConnectionManager(str(db_path))
    ensure_scoped_soul_schema(manager)
    repo = ScopedSoulRepository(manager)
    conn = manager.conn
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_profiles (
               user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT,
               affection REAL DEFAULT 0, interaction_count INTEGER DEFAULT 0,
               first_seen REAL, last_seen REAL, personality_tags TEXT,
               notes TEXT, metadata TEXT,
               PRIMARY KEY (user_id, group_id, bot_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS relationship_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               bot_id TEXT, group_id TEXT, user_id TEXT, event_type TEXT,
               dimension TEXT, delta REAL, reason TEXT, created_at REAL
           )"""
    )
    conn.execute("INSERT INTO user_profiles (user_id, group_id, bot_id, metadata) VALUES ('u1', 'g1', 'bot-alpha', '{}')")
    conn.commit()

    rel_events = RelationshipEventService(conn=conn, repository=repo)
    concerns = ConcernTracker(db=SimpleNamespace(conn=conn), bot_id="bot-alpha", repository=repo)

    db_wrapper = SimpleNamespace(
        conn=conn,
        closed=False,
        reopen=lambda: None,
        soul_repository=repo,
        scoped_knowledge=None,
    )
    return db_wrapper, rel_events, concerns, repo, manager


def test_record_social_impression_tool_updates_impression_and_affinity(tmp_path):
    db, rel_events, _, repo, manager = _setup_test_db(tmp_path)
    try:
        scope = _scope()
        repo.upsert_relationship(scope, subject_principal_id="qq:user:u1", affinity=10, dimensions={"trust": 10})
        tool = WaveMemoryRecordSocialImpressionTool(
            db=db,
            relationship_events=rel_events,
            bot_db_ids={"bot-alpha": "bot-alpha"},
        )
        ctx = _context_wrapper(scope)

        result = asyncio.run(tool.call(
            ctx,
            target_user="u1",
            impression="虽然嘴损故意嘲讽，但其实是在帮我圆场，属于自己人的反串犯贱",
            shift_reason="熟练接通群黑话反串",
            shift_level="subtle_increase",
        ))

        assert "已记录对「u1」的印象" in result
        assert "好感度变动" in result

        row = db.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()
        meta = json.loads(row[0])
        assert "虽然嘴损故意嘲讽" in meta["impression"]
        assert len(meta["impression_history"]) >= 1
        assert len(meta["impression_ledger"]) == 1
        assert meta["impression_ledger"][0]["event_type"] == "direct_reply"
        assert meta["impression_ledger"][0]["delta"] == 1.5
    finally:
        manager.close()


def test_note_social_anchor_tool_records_anchor_and_concern(tmp_path):
    db, _, concerns, repo, manager = _setup_test_db(tmp_path)
    try:
        scope = _scope()
        tool = WaveMemoryNoteSocialAnchorTool(
            db=db,
            concern_tracker=concerns,
            repository=repo,
        )
        ctx = _context_wrapper(scope)

        result = asyncio.run(tool.call(
            ctx,
            target_user="u1",
            anchor_type="bot_helped_user",
            summary="通宵帮他排查了毕业设计代码死锁问题",
            is_active_concern=True,
        ))

        assert "已记录与「u1」的人情备忘" in result
        assert "已挂载为活跃关切" in result

        row = db.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()
        meta = json.loads(row[0])
        assert len(meta["social_anchors"]) == 1
        assert meta["social_anchors"][0]["anchor_type"] == "bot_helped_user"
        assert meta["social_anchors"][0]["is_active_concern"] is True

        # 检查 ConcernTracker 确实挂上了
        active_concerns = concerns._concerns_for(scope)
        assert any("毕业设计" in c.topic for c in active_concerns)
    finally:
        manager.close()


def test_mark_cultural_moment_tool_records_exemplar_reply(tmp_path):
    db, _, _, _, manager = _setup_test_db(tmp_path)
    try:
        scope = _scope()
        tool = WaveMemoryMarkCulturalMomentTool(db=db)
        ctx = _context_wrapper(scope)

        result = asyncio.run(tool.call(
            ctx,
            moment_type="exemplar_reply",
            target_phrase_or_snippet="面对道德绑架时，用事实因果冷峻剖析，不道歉也不说教",
            context_note="实事求是学者风骨示范",
        ))

        assert "已标记高光回复范式" in result

        rows = db.conn.execute("SELECT snippet, context_note FROM exemplar_reply_candidates").fetchall()
        assert len(rows) == 1
        assert "道德绑架" in rows[0][0]
        assert rows[0][1] == "实事求是学者风骨示范"
    finally:
        manager.close()
