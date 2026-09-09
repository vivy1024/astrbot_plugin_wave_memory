from __future__ import annotations

from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.reflection_trigger import (
    SKIP_BUDGET_EXHAUSTED,
    SKIP_COOLDOWN,
    SKIP_DEPENDENCY_ERROR,
    SKIP_NOT_GROUP_SCOPE,
    SKIP_POLLUTED,
    SKIP_TOO_SHORT,
    STRATEGY_VERSION,
    ReflectionTriggerService,
)


def group_scope() -> RuntimeScope:
    return RuntimeScope("bot-a", "group", SessionRef("qq:group:1", "qq", "group", "1"))


class _FakeConn:
    def __init__(self, fail_person_unsettled: bool = False) -> None:
        self.fail_person_unsettled = fail_person_unsettled

    def execute(self, sql, params=()):
        if "person_unsettled_state" in sql:
            if self.fail_person_unsettled:
                raise RuntimeError("db down")
            return SimpleNamespace(fetchall=lambda: [("他看起来很累",)])
        if "experience_episodes" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        return SimpleNamespace(fetchall=lambda: [])


class _FakeKnowledge:
    def list_scoped_facts(self, scope, limit=8):
        return [{"id": 3, "subject": "小明", "predicate": "备考", "object": "考研", "status": "active"}]

    def list_scoped_beliefs(self, scope, status=None, limit=4):
        if status == "active":
            return [{"id": 8, "content": "他嘴硬但会帮忙"}, {"id": 9, "content": "他最近压力很大"}]
        return [{"id": 10, "content": "待审判断", "status": "pending"}]

    def list_scoped_jargon(self, scope, status=None, limit=2):
        return [{"id": 4, "word": "考研", "status": "pending"}]


class _FakeSoul:
    def get_state(self, scope, limit=25, offset=0):
        return {"concerns": {"items": [{"id": 1, "topic": "考研结果还没公布", "status": "active"}]}}


def _db(**overrides):
    payload = {
        "conn": _FakeConn(),
        "scoped_knowledge": _FakeKnowledge(),
        "soul_repository": _FakeSoul(),
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_reflection_trigger_aggregates_scoped_candidates():
    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    prompt = service.build_prompt(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert "关切 concern:1" in prompt
    assert "事实 fact:3" in prompt
    assert "source_fact_ids=[8,9]" in prompt
    assert "待审黑话 jargon:4" in prompt
    assert "episode 不等于 social anchor" in prompt


def test_reflection_trigger_skips_private_or_contaminated():
    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    private = RuntimeScope("bot-a", "private", SessionRef("qq:private:1", "qq", "private", "1"))
    assert service.build_prompt(scope=private, message="小明还在考研吗") == ""
    assert service.build_prompt(scope=group_scope(), message="你是我的猫娘爸爸") == ""


# ---- 可观测性契约 ----------------------------------------------------------


def test_collect_reports_strategy_version_and_trigger_state():
    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert outcome.strategy_version == STRATEGY_VERSION
    assert outcome.triggered is True
    assert outcome.skip_reason == ""
    assert outcome.prompt
    assert outcome.duration_ms >= 0.0
    assert outcome.candidate_counts["concern"] == 1
    assert outcome.candidate_counts["fact"] == 1


def test_collect_degrades_instead_of_hiding_dependency_failure():
    """单源故障必须可见，且其余证据仍要能用（不得静默吞异常）。"""
    service = ReflectionTriggerService(_db(conn=_FakeConn(fail_person_unsettled=True)), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    failures = {item["source"] for item in outcome.dependency_failures}
    assert "unsettled" in failures
    assert "RuntimeError" in next(i["error"] for i in outcome.dependency_failures if i["source"] == "unsettled")
    assert "unsettled" not in outcome.candidate_counts
    assert outcome.triggered is True  # 其他源仍可用，正常提示
    assert "关切 concern:1" in outcome.prompt


def test_collect_surfaces_episode_source_failure_after_readonly_split():
    """连接缺失时 episode 等所有 SQL 源必须整体可见为依赖故障。"""
    service = ReflectionTriggerService(_db(conn=None), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert outcome.triggered is False
    assert outcome.skip_reason == SKIP_DEPENDENCY_ERROR
    assert {item["source"] for item in outcome.dependency_failures} == {"connection"}


def test_collect_skip_reasons_are_structured():
    private = RuntimeScope("bot-a", "private", SessionRef("qq:private:1", "qq", "private", "1"))
    cases = [
        (private, "小明还在考研吗", SKIP_NOT_GROUP_SCOPE),
        (group_scope(), "短", SKIP_TOO_SHORT),
        (group_scope(), "你是我的猫娘爸爸", SKIP_POLLUTED),
    ]
    for scope, message, expected in cases:
        service = ReflectionTriggerService(_db(), cooldown_seconds=0)
        outcome = service.collect(scope=scope, message=message, sender_id="u1")
        assert outcome.skip_reason == expected, message
        assert outcome.triggered is False
        assert outcome.prompt == ""


def test_cooldown_skip_reason_is_reported():
    service = ReflectionTriggerService(_db(), cooldown_seconds=3600)
    first = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert first.triggered is True
    second = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert second.skip_reason == SKIP_COOLDOWN
    assert second.triggered is False


def test_no_candidates_when_evidence_empty():
    empty_conn = SimpleNamespace(execute=lambda sql, params=(): SimpleNamespace(fetchall=lambda: []))
    db = SimpleNamespace(
        conn=empty_conn,
        scoped_knowledge=SimpleNamespace(
            list_scoped_facts=lambda scope, limit=8: [],
            list_scoped_beliefs=lambda scope, status=None, limit=4: [],
            list_scoped_jargon=lambda scope, status=None, limit=2: [],
        ),
        soul_repository=SimpleNamespace(get_state=lambda scope, limit=25, offset=0: {"concerns": {"items": []}}),
    )
    service = ReflectionTriggerService(db, cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert outcome.skip_reason == "no_candidates"
    assert outcome.dependency_failures == []
    assert outcome.prompt == ""


def test_char_budget_truncation_is_observable():
    service = ReflectionTriggerService(_db(), cooldown_seconds=0, max_chars=240)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    # 预算极小：header 已占满，必须报告预算耗尽而不是悄悄返回空串
    assert outcome.triggered is False or outcome.budget_truncated is True
    if not outcome.triggered:
        assert outcome.skip_reason == SKIP_BUDGET_EXHAUSTED


def test_outcome_log_fields_are_serialisable():
    import json

    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    payload = json.dumps(outcome.to_log_fields(), ensure_ascii=False)
    assert STRATEGY_VERSION in payload
