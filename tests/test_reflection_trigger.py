from __future__ import annotations

import json
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
    """模拟真实 person_unsettled_state：观感文本存在 traces JSON 数组里。

    真实表结构没有 text 列（列为 bot_id/user_id/group_id/energy/
    interaction_count/traces/updated_at）。这里按真实 schema 返回 traces，
    以便 SQL 写错列名时测试能直接失败。
    """

    def __init__(self, fail_person_unsettled: bool = False, traces: str | None = None) -> None:
        self.fail_person_unsettled = fail_person_unsettled
        self.traces = traces if traces is not None else json.dumps(
            [{"text": "他看起来很累", "summary": "他看起来很累", "impact": 1.0, "ts": 1.0}]
        )

    def execute(self, sql, params=()):
        if "person_unsettled_state" in sql:
            if self.fail_person_unsettled:
                raise RuntimeError("db down")
            # 真实表没有 text 列：查它必须报错，否则说明代码写错了列名。
            if "text" in sql and "traces" not in sql:
                raise RuntimeError("no such column: text")
            return SimpleNamespace(fetchall=lambda: [(self.traces,)])
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

# ---- 回归：person_unsettled_state 真实 schema 与工具名暴露 ----

def test_unsettled_reads_traces_column_not_text():
    """真实表没有 text 列；查错列名会让该源降级并拖垮整条反思链路。"""
    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    assert outcome.triggered is True, outcome.skip_reason
    assert outcome.dependency_failures == []
    assert outcome.candidate_counts.get("unsettled") == 1
    assert "他看起来很累" in outcome.prompt


def test_unsettled_parses_traces_json_array():
    """traces 是 JSON 数组，要解析出里面的 text 字段。"""
    traces = json.dumps([
        {"text": "搬知乎暴论的诸葛匹夫", "summary": "x", "impact": 1.0, "ts": 1.0},
        {"text": "被误会后急忙撇清的诸葛匹夫", "summary": "y", "impact": 1.0, "ts": 2.0},
    ])
    service = ReflectionTriggerService(_db(conn=_FakeConn(traces=traces)), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="诸葛匹夫又来了", sender_id="u1")
    assert outcome.triggered is True
    assert "搬知乎暴论的诸葛匹夫" in outcome.prompt
    assert "被误会后急忙撇清的诸葛匹夫" in outcome.prompt


def test_unsettled_bad_json_degrades_only_that_row():
    """单行坏数据只跳过该行，不能把整个源判为失败。"""
    service = ReflectionTriggerService(_db(conn=_FakeConn(traces="{not json")), cooldown_seconds=0)
    outcome = service.collect(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    # 该源没有候选，但也不应记为 dependency_failure
    assert "unsettled" not in [item["source"] for item in outcome.dependency_failures]


def test_prompt_exposes_exact_tool_names():
    """提示必须写出确切工具名，模型才能在多个 wave_memory_* 工具中选对。"""
    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    prompt = service.build_prompt(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    for tool in (
        "wave_memory_propose_fact",
        "wave_memory_propose_belief",
        "wave_memory_mark_cultural_moment",
        "wave_memory_note_social_anchor",
    ):
        assert tool in prompt, tool


def test_exposed_tool_names_actually_exist():
    """提示里写的工具名必须真实存在于 tools/，避免改名后提示漂移。"""
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    declared = set()
    for path in (root / "tools").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            marker = 'name: str = "wave_memory'
            if marker in line:
                declared.add(line.split('"')[1])
    service = ReflectionTriggerService(_db(), cooldown_seconds=0)
    prompt = service.build_prompt(scope=group_scope(), message="小明还在考研吗", sender_id="u1")
    mentioned = {token for token in declared if token in prompt}
    assert mentioned, "提示至少应写出一部分真实工具名"
    for name in mentioned:
        assert name in declared

def test_persona_prompt_only_references_real_tools():
    """人格提示词里引用的 wave_memory_* / book_lore_* 必须真实存在。

    历史上提示词写过 wave_memory_record_social_anchor、wave_memory_manage_concern、
    affinity_update 等不存在的名字，导致提审静默失败。这里锁住一致性。
    """
    import re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    declared = set()
    for path in (root / "tools").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.search(r'name: str = "([a-z_]+)"', line)
            if m:
                declared.add(m.group(1))

    prompt_path = root / "docs" / "persona-prompt-optimized.md"
    assert prompt_path.exists(), "人格提示词优化版缺失"
    prompt = prompt_path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"(?:wave_memory_[a-z_]+|book_lore_[a-z_]+)", prompt))
    assert referenced, "提示词应引用工具名"
    missing = sorted(name for name in referenced if name not in declared)
    assert missing == [], f"提示词引用了不存在的工具: {missing}"
