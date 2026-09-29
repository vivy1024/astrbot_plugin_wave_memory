from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from tools.fact_proposal import WaveMemoryProposeFactTool


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


def test_fact_proposal_in_private_chat_stays_in_that_private_chat(tmp_path: Path):
    """私聊里得知的事实可以提审，按原会话保存，只在该私聊可见。"""
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
    assert "已成功提审事实" in res
    rows = db.list_scoped_facts(_private_scope())
    assert [(row["subject"], row["status"]) for row in rows] == [("小明", "pending")]
    assert db.list_scoped_facts(_group_scope()) == []
    other_private = RuntimeScope(
        "bot-alpha", "private", SessionRef("qq:private:user-2", "qq", "private", "user-2"),
        subject_principal_id="qq:user:user-2",
    )
    assert db.list_scoped_facts(other_private) == []
    assert db.list_bot_scoped_facts(_group_scope(), keywords=["小明"]) == [], "私聊事实不进入 Bot 级公开事实"
    db.close()
