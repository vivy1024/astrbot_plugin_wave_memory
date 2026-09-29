"""Expired facts must not re-enter learning through bookkeeping or belief evidence."""

from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.belief_engine import approved_source_fact_ids
from services.post_reply_bookkeeping import PostReplyBookkeeper


def test_learning_sources_respect_current_fact_validity(monkeypatch):
    monkeypatch.setattr("services.belief_engine.time.time", lambda: 1000.0)
    scope = RuntimeScope("bot-demo", "group", SessionRef("demo:group:g1", "demo", "group", "g1"))
    base = {"subject": "测试用户甲", "predicate": "状态", "status": "active"}
    facts = [
        {**base, "id": 1, "object": "持续事实"},
        {**base, "id": 2, "object": "有效事实", "valid_from": 1000, "valid_until": 1200},
        {**base, "id": 3, "object": "过期事实", "valid_until": 1000},
        {**base, "id": 4, "object": "未来事实", "valid_from": 1001},
        {**base, "id": 5, "object": "被替代事实", "status": "superseded"},
    ]
    repo = SimpleNamespace(list_scoped_facts=lambda owner, limit: facts if owner == scope else [])
    assert approved_source_fact_ids(repo, scope, [1, 2, 3, 4, 5]) == [1, 2]
    keeper = PostReplyBookkeeper(SimpleNamespace(scoped_knowledge=repo), object(), object())
    block = keeper._fact_block(scope)
    assert "持续事实" in block and "有效事实" in block
    assert "过期事实" not in block and "未来事实" not in block and "被替代事实" not in block
