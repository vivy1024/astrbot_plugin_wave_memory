from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.belief_lifecycle import BeliefLifecycleService
from tools.fact_proposal import WaveMemoryProposeFactTool
from tools.belief_proposal import WaveMemoryProposeBeliefTool


def _group_scope(group: str = "group-100", subject: str = "qq:user:u1") -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef(f"qq:group:{group}", "qq", "group", group),
        subject_principal_id=subject,
    )


def _private_scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "private",
        SessionRef("qq:private:user-1", "qq", "private", "user-1"),
        subject_principal_id="qq:user:user-1",
    )


def _context_wrapper(scope: RuntimeScope) -> SimpleNamespace:
    event = SimpleNamespace(
        _wave_memory_runtime_scope=scope,
        get_sender_id=lambda: "u1",
        get_group_id=lambda: "group-100",
        get_self_id=lambda: "bot-alpha",
    )
    context = SimpleNamespace(event=event)
    return SimpleNamespace(context=context)


class _FakeHolyman:
    def __init__(self, matches=None):
        self._matches = matches or []

    def match_text(self, text: str, max_items: int = 8):
        return [m for m in self._matches if m.get("term") in text]


class _FakeJargonService:
    def __init__(self, holyman=None):
        self._holyman = holyman


def test_fact_proposal_tool_requires_source_quote(tmp_path: Path):
    db_path = tmp_path / "fact_quote.db"
    db = WaveMemoryDB(str(db_path))
    tool = WaveMemoryProposeFactTool(db=db)
    ctx = _context_wrapper(_group_scope())

    res = asyncio.run(tool.call(
        ctx,
        subject="小明",
        predicate="住在",
        object="上海",
        source_quote="",
        context_evidence="日常聊天",
    ))
    assert "subject、predicate、object、source_quote 与 context_evidence 均为必填项" in res


def test_fact_proposal_tool_rejects_explicit_irony_jargon(tmp_path: Path):
    db_path = tmp_path / "fact_irony.db"
    db = WaveMemoryDB(str(db_path))
    scope = _group_scope()
    db.scoped_knowledge.upsert_scoped_jargon(
        scope,
        word="动了谁的蛋糕",
        meaning="反串式阴谋化归因",
        status="active",
        provenance={"irony": True},
    )
    tool = WaveMemoryProposeFactTool(db=db)
    ctx = _context_wrapper(scope)

    res = asyncio.run(tool.call(
        ctx,
        subject="小明",
        predicate="动了",
        object="蛋糕",
        source_quote="笑死，看来是动了谁的蛋糕了",
        context_evidence="群友随口反串",
    ))
    assert "已标记为反串/阴阳，不能升格为认真事实" in res


def test_fact_proposal_tool_writes_pending_with_jargon_hits(tmp_path: Path):
    db_path = tmp_path / "fact_hits.db"
    db = WaveMemoryDB(str(db_path))
    scope = _group_scope()
    db.scoped_knowledge.upsert_scoped_jargon(
        scope,
        word="考研",
        meaning="全国硕士研究生统一招生考试",
        status="active",
        provenance={"irony": False},
    )
    tool = WaveMemoryProposeFactTool(db=db)
    ctx = _context_wrapper(scope)

    res = asyncio.run(tool.call(
        ctx,
        subject="小明",
        predicate="正在备考",
        object="计算机考研",
        source_quote="我今年一定要考研上岸！",
        context_evidence="当面表态",
    ))
    assert "已成功提审事实" in res

    facts = db.scoped_knowledge.list_scoped_facts(scope)
    assert len(facts) == 1
    f = facts[0]
    assert f["status"] == "pending"
    assert f["provenance"]["source_quote"] == "我今年一定要考研上岸！"
    assert len(f["provenance"]["jargon_hits"]) == 1
    assert f["provenance"]["jargon_hits"][0]["word"] == "考研"


def test_fact_proposal_tool_rejects_private_scope(tmp_path: Path):
    db_path = tmp_path / "fact_private.db"
    db = WaveMemoryDB(str(db_path))
    tool = WaveMemoryProposeFactTool(db=db)
    ctx = _context_wrapper(_private_scope())

    res = asyncio.run(tool.call(
        ctx,
        subject="小明",
        predicate="住在",
        object="上海",
        source_quote="我住上海",
        context_evidence="私聊对话",
    ))
    assert "scope_required" in res


def test_belief_proposal_requires_at_least_two_approved_facts(tmp_path: Path):
    db_path = tmp_path / "belief_facts.db"
    db = WaveMemoryDB(str(db_path))
    scope = _group_scope()
    tool = WaveMemoryProposeBeliefTool(db=db)
    ctx = _context_wrapper(scope)

    # 1. 传少于 2 条直接拒绝
    res = asyncio.run(tool.call(
        ctx,
        belief_type="person_judgment",
        content="小明很可靠",
        grounding_evidence="根据观察",
        source_fact_ids=[1],
    ))
    assert "至少两条已审核事实" in res

    # 2. 插入 2 条事实但为 pending 状态，仍然拒绝
    f1 = db.scoped_knowledge.upsert_scoped_fact(scope, subject="小明", predicate="协助调试", object="代码", status="pending")
    f2 = db.scoped_knowledge.upsert_scoped_fact(scope, subject="小明", predicate="通宵排查", object="死锁", status="pending")
    res_pending = asyncio.run(tool.call(
        ctx,
        belief_type="person_judgment",
        content="小明很可靠",
        grounding_evidence="根据观察",
        source_fact_ids=[f1, f2],
    ))
    assert "必须是当前群内至少两条已批准事实" in res_pending

    # 3. 事实批准为 approved 后，提审成功
    db.conn.execute("UPDATE scoped_facts SET status='approved' WHERE id IN (?, ?)", (f1, f2))
    db.conn.commit()

    res_ok = asyncio.run(tool.call(
        ctx,
        belief_type="person_judgment",
        content="小明表面言辞犀利，但在朋友遇到技术难关时从不推脱",
        grounding_evidence="已由两条技术协助事实证实",
        source_fact_ids=[f1, f2],
    ))
    assert "已成功提审信念" in res_ok

    beliefs = db.scoped_knowledge.list_scoped_beliefs(scope)
    assert len(beliefs) == 1
    b = beliefs[0]
    assert b["status"] == "pending"
    assert b["provenance"]["source_fact_ids"] == [f1, f2]


def test_belief_lifecycle_transition_approves_when_backed_by_approved_facts(tmp_path: Path):
    db_path = tmp_path / "belief_trans.db"
    db = WaveMemoryDB(str(db_path))
    scope = _group_scope()

    # 写入一条支撑记忆
    mem_id = db.add_memory(scope.session.conversation_id, "昨晚小明帮我通宵调代码", sender_id="u1", scope=scope)
    f1 = db.scoped_knowledge.upsert_scoped_fact(scope, subject="小明", predicate="协助调试", object="代码", status="approved", source_memory_id=mem_id)
    f2 = db.scoped_knowledge.upsert_scoped_fact(scope, subject="小明", predicate="通宵排查", object="死锁", status="approved", source_memory_id=mem_id)

    tool = WaveMemoryProposeBeliefTool(db=db)
    ctx = _context_wrapper(scope)
    asyncio.run(tool.call(
        ctx,
        belief_type="person_judgment",
        content="小明在技术求助时极度热心",
        grounding_evidence="两条已批准事实",
        source_fact_ids=[f1, f2],
    ))

    b_id = db.scoped_knowledge.list_scoped_beliefs(scope)[0]["id"]
    lifecycle = BeliefLifecycleService(db.scoped_knowledge)

    # 1. 事实批准状态被废止（如 superseded），则拒绝审核通过
    db.conn.execute("UPDATE scoped_facts SET status='superseded' WHERE id=?", (f1,))
    db.conn.commit()
    try:
        lifecycle.transition(scope, b_id, "approve")
        assert False, "should fail when approved facts < 2"
    except ValueError as exc:
        assert "belief_facts_required" in str(exc)

    # 2. 事实恢复为 approved，审核顺利通过为 active
    db.conn.execute("UPDATE scoped_facts SET status='approved' WHERE id=?", (f1,))
    db.conn.commit()
    res = lifecycle.transition(scope, b_id, "approve")
    assert res["status"] == "active"
