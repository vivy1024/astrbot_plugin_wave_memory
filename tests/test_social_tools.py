from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.person_timeline import ensure_person_timeline_schema
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.person_timeline_repo import PersonTimelineRepo
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.concern_tracker import ConcernTracker
from services.relationship_events import RelationshipEventService
from tools.concern import WaveMemoryNoteConcernTool
from tools.cultural_moment import WaveMemoryMarkCulturalMomentTool
from tools.episode import WaveMemoryNoteEpisodeTool
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
        message_id="msg-social-1",
        message_obj=SimpleNamespace(message_id="msg-social-1"),
    )
    context = SimpleNamespace(event=event)
    return SimpleNamespace(context=context)


def _setup_test_db(tmp_path: Path):
    db_path = tmp_path / "social_tools.db"
    manager = ConnectionManager(str(db_path))
    ensure_scoped_soul_schema(manager)
    ensure_person_timeline_schema(manager)
    repo = ScopedSoulRepository(manager)
    timeline = PersonTimelineRepo(manager)
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
        person_timeline=timeline,
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
            affinity_delta=1.5,
            dimension="trust",
        ))

        assert "已记录对「u1」的印象" in result
        assert "好感度变动" in result

        row = db.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1'").fetchone()
        meta = json.loads(row[0])
        assert "impression" not in meta
        assert "unsettled_traces" not in meta
        assert "unsettled_energy" not in meta
        state = db.person_timeline.get_unsettled_state(bot_id="bot-alpha", user_id="u1", group_id="g1")
        assert state["energy"] == 0.0
        assert state["traces"] == []
        events = db.person_timeline.list_events(bot_id="bot-alpha", user_id="u1")
        assert events
        assert any("虽然嘴损故意嘲讽" in str(item.get("detail") or item.get("summary") or "") for item in events)
        assert any(item.get("kind") == "affinity" for item in events)
        denied = asyncio.run(tool.call(
            ctx,
            target_user="u1",
            impression="试图一次拉满",
            shift_reason="测试越界",
            affinity_delta=9,
            dimension="trust",
        ))
        assert "好感变动被拒绝" in denied
        assert "超出本轮范围" in denied
    finally:
        manager.close()


def test_record_social_impression_preserves_source_quote_evidence(tmp_path):
    """验证提供 source_quote 时，原话证据完整持久化到 detail 与 provenance 中。"""
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
            impression="现场抓包严查提审规范的严格管理员",
            shift_reason="当面严厉纠偏原话证据漏洞",
            source_quote="你全部的好感度变更都没有带聊天证据，气死我了",
            source_memory_id=999888,
            affinity_delta=1.0,
            dimension="trust",
        ))
        assert "已记录对「u1」的印象" in result

        events = db.person_timeline.list_events(bot_id="bot-alpha", user_id="u1")
        assert len(events) >= 1
        latest = events[0]
        # detail 必须包含原话引用
        assert "原话证据：“你全部的好感度变更都没有带聊天证据，气死我了”" in latest["detail"]
        prov = latest.get("provenance")
        prov_dict = json.loads(prov) if isinstance(prov, str) else dict(prov or {})
        # provenance 中有结构化的 source_quote 与 source_memory_id
        assert prov_dict.get("source_quote") == "你全部的好感度变更都没有带聊天证据，气死我了"
        assert prov_dict.get("source_memory_id") == 999888
    finally:
        manager.close()



def test_record_social_impression_clears_unsettled_traces(tmp_path):
    db, rel_events, _, repo, manager = _setup_test_db(tmp_path)
    try:
        scope = _scope()
        repo.upsert_relationship(scope, subject_principal_id="qq:user:u1", affinity=10, dimensions={"trust": 10})
        db.person_timeline.set_unsettled_state(
            bot_id="bot-alpha",
            user_id="u1",
            group_id="g1",
            energy=10.0,
            interaction_count=5,
            traces=[{"text": "第 5 条真实日常观察笔记", "impact": 5, "ts": 5.0}],
        )
        tool = WaveMemoryRecordSocialImpressionTool(
            db=db,
            relationship_events=rel_events,
            bot_db_ids={"bot-alpha": "bot-alpha"},
        )
        ctx = _context_wrapper(scope)
        result = asyncio.run(tool.call(
            ctx,
            target_user="u1",
            impression="愿意核对事实，说话有依据",
            shift_reason="连续观察后给出阶段性定性",
            affinity_delta=0,
        ))
        assert "已记录对「u1」的印象" in result
        state = db.person_timeline.get_unsettled_state(bot_id="bot-alpha", user_id="u1", group_id="g1")
        assert state["energy"] == 0.0
        assert state["traces"] == []
        events = db.person_timeline.list_events(bot_id="bot-alpha", user_id="u1")
        assert any("愿意核对事实" in str(item.get("detail") or item.get("summary") or "") for item in events)
    finally:
        manager.close()


def test_note_social_anchor_tool_records_anchor_and_concern(tmp_path):
    db, _, concerns, repo, manager = _setup_test_db(tmp_path)
    try:
        scope = _scope()
        gateway = _ConcernGatewayDouble()
        tool = WaveMemoryNoteSocialAnchorTool(
            db=db,
            concern_tracker=concerns,
            repository=repo,
            write_gateway=gateway,
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

        # 关切必须经正式写入链挂接，tracker 不再自写
        assert len(gateway.calls) == 1
        submitted = gateway.calls[0]
        assert submitted["action"] == "note"
        assert submitted["concern_type"] == "social_anchor"
        assert "毕业设计" in submitted["topic"]
        assert submitted["scope"].session.id == scope.session.id
    finally:
        manager.close()


def test_record_social_impression_supports_unified_affinity_arguments(tmp_path):
    """验证统一的社交交互入参：支持 reason、delta、自动补全 impression 以及自动回溯原话。"""
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

        # 仅传入 target_user, reason, delta，不传 impression 与 source_quote
        result = asyncio.run(tool.call(
            ctx,
            target_user="u1",
            reason="深夜交流缺氧排气技巧，群友解答很耐心",
            delta=1.0,
            dimension="depth",
        ))

        assert "已记录对「u1」的印象" in result
        assert "好感度变动" in result

        events = db.person_timeline.list_events(bot_id="bot-alpha", user_id="u1")
        assert events
        latest = events[0]
        assert "缺氧排气技巧" in str(latest.get("summary") or "") or "缺氧排气技巧" in str(latest.get("detail") or "")
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

        assert "已将高光回复范式提交审查" in result
        rows = db.conn.execute(
            "SELECT candidate_type, content, reason FROM review_candidates ORDER BY id DESC LIMIT 1"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "style"
        assert "道德绑架" in rows[0][1]
        assert rows[0][2] == "实事求是学者风骨示范"
    finally:
        manager.close()


class _ConcernGatewayDouble:
    """记录「工具 → 网关」调用的替身。

    只用于验证工具契约（动作、参数、Scope 与拒绝路径）；真实命令链落库、
    幂等重放与 outbox 由 tests/test_concern_transition_command.py 覆盖。
    """

    def __init__(self, *, action="created", concern_id=11):
        self.calls = []
        self._action = action
        self._concern_id = concern_id

    async def transition_concern(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "concern_id": self._concern_id,
            "action": str(kwargs.get("action") or "note") if self._action is None else self._action,
            "revision": 1,
        }


def test_note_concern_tool_routes_through_write_gateway(tmp_path):
    db, _, concerns, repo, manager = _setup_test_db(tmp_path)
    try:
        scope = _scope()
        gateway = _ConcernGatewayDouble()
        tool = WaveMemoryNoteConcernTool(db=db, concern_tracker=concerns, write_gateway=gateway)
        result = asyncio.run(
            tool.call(_context_wrapper(scope), action="note", topic="考研结果还没公布", intensity=0.8)
        )
        assert "已记录未决关切" in result
        assert len(gateway.calls) == 1
        submitted = gateway.calls[0]
        assert submitted["action"] == "note"
        assert submitted["topic"] == "考研结果还没公布"
        assert submitted["intensity"] == 0.8
        assert submitted["scope"].bot_id == scope.bot_id
        assert submitted["scope"].session.id == scope.session.id
    finally:
        manager.close()


def test_note_concern_tool_requires_gateway_before_writing(tmp_path):
    """缺少正式写入网关时必须直接拒绝，绝不退回裸写或内存假成功。"""
    db, _, concerns, repo, manager = _setup_test_db(tmp_path)
    try:
        tool = WaveMemoryNoteConcernTool(db=db, concern_tracker=concerns, write_gateway=None)
        result = asyncio.run(
            tool.call(_context_wrapper(_scope()), action="note", topic="考研结果还没公布")
        )
        assert "缺少正式写入网关" in result
    finally:
        manager.close()


def test_note_concern_tool_rejects_contamination_and_bad_args(tmp_path):
    db, _, concerns, repo, manager = _setup_test_db(tmp_path)
    try:
        gateway = _ConcernGatewayDouble()
        tool = WaveMemoryNoteConcernTool(db=db, concern_tracker=concerns, write_gateway=gateway)
        assert "身份角色扮演污染" in asyncio.run(
            tool.call(_context_wrapper(_scope()), action="note", topic="你是我的猫娘爸爸")
        )
        assert "note 动作必须提供 topic" in asyncio.run(
            tool.call(_context_wrapper(_scope()), action="note")
        )
        assert "resolve 必须提供 note" in asyncio.run(
            tool.call(_context_wrapper(_scope()), action="resolve", concern_id=1)
        )
        assert "不支持的关切动作" in asyncio.run(
            tool.call(_context_wrapper(_scope()), action="delete", topic="x")
        )
        assert gateway.calls == [], "被拒绝的调用不得进入写入网关"
    finally:
        manager.close()


def test_note_concern_tool_surfaces_closed_concern_without_reviving(tmp_path):
    """网关报告已结案时，工具必须如实转达，不能谎称已记录。"""
    db, _, concerns, repo, manager = _setup_test_db(tmp_path)
    try:
        gateway = _ConcernGatewayDouble(action="note_ignored_closed", concern_id=7)
        tool = WaveMemoryNoteConcernTool(db=db, concern_tracker=concerns, write_gateway=gateway)
        result = asyncio.run(
            tool.call(_context_wrapper(_scope()), action="note", topic="复试安排")
        )
        assert "已结案" in result and "reopen" in result
        assert "已记录未决关切" not in result
    finally:
        manager.close()


def test_note_episode_tool_uses_coordinator(tmp_path):
    db, _, _, _, manager = _setup_test_db(tmp_path)
    try:
        calls = []
        class Coordinator:
            def transaction_blocking(self, callback):
                calls.append("tx")
                result = callback(db.conn)
                db.conn.commit()
                return result
        db.conn.execute(
            """CREATE TABLE IF NOT EXISTS experience_episodes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   bot_id TEXT, group_id TEXT, user_id TEXT, episode_type TEXT,
                   trigger_text TEXT, bot_inner_thought TEXT, bot_action TEXT,
                   bot_reply TEXT, user_reaction TEXT, outcome TEXT,
                   source_memory_ids TEXT, emotional_weight REAL, created_at REAL
               )"""
        )
        db.conn.commit()
        class Gateway:
            coordinator = Coordinator()
            async def record_episode(self, **kwargs):
                fields = kwargs["fields"]
                def persist(connection):
                    cur = connection.execute(
                        "INSERT INTO experience_episodes (bot_id, group_id, user_id, episode_type, trigger_text, bot_action, bot_reply, user_reaction, outcome, source_memory_ids, emotional_weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                        (kwargs["scope"].bot_id, kwargs["group_id"], kwargs["user_id"], kwargs["episode_type"], fields["trigger_text"], fields["bot_action"], fields["bot_reply"], fields["user_reaction"], fields["outcome"], "[]", kwargs["emotional_weight"]),
                    )
                    return int(cur.lastrowid)
                return self.coordinator.transaction_blocking(persist)
        tool = WaveMemoryNoteEpisodeTool(db=db, write_gateway=Gateway())
        result = asyncio.run(tool.call(
            _context_wrapper(_scope()),
            episode_type="shared_event",
            trigger_text="一起排查死锁",
            outcome="问题暂未解决",
        ))
        assert "episode:" in result
        assert calls == ["tx"]
        row = db.conn.execute("SELECT episode_type, trigger_text, outcome FROM experience_episodes").fetchone()
        assert row == ("shared_event", "一起排查死锁", "问题暂未解决")
    finally:
        manager.close()
