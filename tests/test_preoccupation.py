"""心事 v1 与关切两段式占用：说出口才算跟进，沉默只留一小段「考虑过」。"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from services.concern_followup import ConcernFollowupService
from services.preoccupation import (
    build_heart_prompt,
    hesitation_line,
    select_preoccupations,
)

NOON = datetime(2026, 10, 20, 12, 0).timestamp()


def _scope(group: str = "g1") -> RuntimeScope:
    return RuntimeScope("yushu", "group", SessionRef(f"qq:group:{group}", "qq", "group", group))


def _db(items):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE scoped_soul_concerns (
           id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT, concern_type TEXT, evidence TEXT,
           created_at REAL, last_triggered REAL, last_progress_at REAL)"""
    )
    for item in items:
        evidence = [{"kind": "subject", "user_id": item["user_id"]}]
        if item.get("due"):
            evidence.append({"kind": "due", "date": item["due"]})
        conn.execute(
            "INSERT INTO scoped_soul_concerns VALUES (?, 'yushu', 'qq:group:g1', 'group', ?, ?, 'active',"
            " ?, ?, ?, NULL, NULL)",
            (
                item["id"], item["topic"], item.get("intensity", 0.8),
                item.get("concern_type", "wellbeing"), json.dumps(evidence),
                item.get("created_at", NOON - 2 * 86400),
            ),
        )
    return SimpleNamespace(conn=conn)


def _service(tmp_path, items, **kwargs):
    return ConcernFollowupService(
        _db(items), enabled=True, quiet_hours=(0, 0), clock=lambda: NOON,
        state_path=str(tmp_path / "concern_followups.json"), **kwargs,
    )


def _event(scope, nickname="小明", claim=None):
    event = SimpleNamespace(
        message_obj=SimpleNamespace(sender=SimpleNamespace(nickname=nickname)),
        _wave_memory_runtime_scope=scope,
    )
    if claim is not None:
        event._wave_memory_concern_followup = claim
    return event


def _method(filename, name, **namespace):
    path = Path(__file__).resolve().parents[1] / "app" / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )

    class Imports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            node.level = 0
            return node

    module = ast.fix_missing_locations(ast.Module(body=[Imports().visit(node)], type_ignores=[]))
    env = {"RuntimeScope": RuntimeScope, "logger": logging.getLogger(__name__), **namespace}
    exec(compile(module, str(path), "exec"), env)
    return env[name]


# ─── 占用与结算 ───


def test_claim_only_reserves_until_she_speaks(tmp_path):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    chosen = service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)
    assert chosen["id"] == 1
    assert service.is_pending(1, now=NOON)
    assert not service.recently_followed(1, now=NOON)
    assert not Path(service.state_path).exists()

    assert service.confirm_followup(1, spoke=True, now=NOON)
    assert service.recently_followed(1, now=NOON)
    assert Path(service.state_path).exists()

    reasons = []
    assert service.claim_followup(
        _scope(), "u1", "小明", "在吗", now=NOON + 60,
        on_skip=lambda reason, _remaining: reasons.append(reason),
    ) is None
    assert reasons == ["concern_cooldown"]


def test_silence_keeps_short_considered_cooldown_without_spending_group_rate(tmp_path):
    service = _service(tmp_path, [
        {"id": 1, "topic": "面试结果", "user_id": "u1"},
        {"id": 2, "topic": "搬家", "user_id": "u2"},
    ])
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)["id"] == 1
    assert service.confirm_followup(1, spoke=False, now=NOON)
    assert service.recently_followed(1, now=NOON)
    assert 0 < service.cooldown_remaining(1, now=NOON) <= service.considered_cooldown_seconds

    # 沉默不算一次主动开口，不该占掉同一群的主动间隔
    immediate = service.claim_followup(_scope(), "u2", "小红", "搬完了", now=NOON + 1)
    assert immediate and immediate["id"] == 2
    assert not service.recently_followed(1, now=NOON + service.considered_cooldown_seconds + 1)


def test_release_and_pending_expiry_allow_a_retry(tmp_path):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)
    assert service.release_followup(1)
    assert not service.is_pending(1, now=NOON)
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)

    # 占用超时：上一轮没有走到发送，这一轮可以重新考虑
    later = NOON + service.pending_ttl_seconds + 1
    assert not service.is_pending(1, now=later)
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=later)


def test_duplicate_claim_while_pending_is_blocked(tmp_path):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)
    reasons = []
    assert service.claim_followup(
        _scope(), "u1", "小明", "在吗", now=NOON + 1,
        on_skip=lambda reason, _remaining: reasons.append(reason),
    ) is None
    assert reasons == ["concern_pending"]


# ─── 心事组合 ───


def test_select_preoccupations_excludes_claimed_and_dedupes():
    concerns = [
        {"id": 1, "topic": "小明：面试结果", "intensity": 0.9, "created_at": NOON - 3 * 86400},
        {"id": 2, "topic": "搬家", "intensity": 0.8, "created_at": NOON - 3 * 86400},
        {"id": 3, "topic": "搬家", "intensity": 0.7, "created_at": NOON - 1 * 86400},
        {"id": 4, "topic": "考试", "intensity": 0.6, "created_at": NOON - 2 * 86400},
    ]
    items = select_preoccupations(
        concerns, exclude_ids={1}, sender_name="小明", limit=2, now=NOON,
    )
    assert [item.concern_id for item in items] == [2, 4]
    assert items[0].topic == "搬家" and items[0].days == 3


def test_heart_prompt_is_inner_voice_and_permits_silence():
    items = select_preoccupations(
        [{"id": 2, "topic": "搬家", "intensity": 0.8, "created_at": NOON - 2 * 86400}],
        now=NOON,
    )
    prompt = build_heart_prompt(items, sender_name="小明", hesitation="你有点犹豫。")
    assert "不是待办" in prompt and "说不说都由你" in prompt
    assert "必须" not in prompt and "搬家" in prompt
    assert build_heart_prompt([], sender_name="小明") == ""


def test_hesitation_line_comes_from_reactions_and_fatigue():
    assert "没太接你的话" in hesitation_line(counts={"rejected": 2})
    assert "摸 ta 的脾气" in hesitation_line(counts={"rejected": 1, "accepted": 2})
    assert "纠正过你" in hesitation_line(counts={"corrected": 1})
    assert "有点累" in hesitation_line(fatigue=0.8)
    assert "踏实" in hesitation_line(counts={"accepted": 1})
    assert hesitation_line() == ""


# ─── 注入链路 ───


def test_non_proactive_prompt_reserves_without_committing_followup(tmp_path):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    method = _method("injection.py", "_concern_followup_prompt")
    plugin = SimpleNamespace(concern_followup=service)
    event = _event(_scope())

    prompt = method(plugin, event, "u1")
    assert prompt and "你正在回复" in prompt
    assert event._wave_memory_concern_followup["proactive"] is False
    assert service.is_pending(1, now=NOON)
    assert not service.recently_followed(1, now=NOON)

    assert service.confirm_followup(1, spoke=False, now=NOON)
    assert method(plugin, _event(_scope()), "u1") == ""


def test_preoccupation_prompt_uses_other_concerns_and_her_fatigue(tmp_path):
    service = _service(tmp_path, [
        {"id": 1, "topic": "小明：面试结果", "user_id": "u1", "intensity": 0.9},
        {"id": 2, "topic": "搬家", "user_id": "u1", "intensity": 0.7},
    ])
    method = _method(
        "injection.py", "_preoccupation_prompt",
        select_preoccupations=select_preoccupations,
        hesitation_line=hesitation_line,
        build_heart_prompt=build_heart_prompt,
    )
    plugin = SimpleNamespace(
        concern_followup=service,
        mood_service=SimpleNamespace(current=lambda bot_id, viewer_scope=None: {"fatigue": 0.8}),
    )
    event = _event(_scope(), claim={"concern": {"id": 1, "topic": "面试结果"}, "proactive": True})

    prompt = method(plugin, event, "u1")
    assert "搬家" in prompt and "面试结果" not in prompt
    assert "有点累" in prompt
    assert event._wave_memory_preoccupation["concern_ids"] == [2]


def test_preoccupation_prompt_stays_silent_without_a_claim(tmp_path):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    method = _method(
        "injection.py", "_preoccupation_prompt",
        select_preoccupations=select_preoccupations,
        hesitation_line=hesitation_line,
        build_heart_prompt=build_heart_prompt,
    )
    assert method(SimpleNamespace(concern_followup=service), _event(_scope()), "u1") == ""


@pytest.mark.parametrize("spoke", [True, False])
def test_confirm_without_claim_is_a_noop(tmp_path, spoke):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    assert service.confirm_followup(1, spoke=spoke, now=NOON) is False
    assert not service.recently_followed(1, now=NOON)


@pytest.mark.parametrize("spoke", [True, False])
def test_settle_helper_commits_the_model_decision(tmp_path, spoke):
    service = _service(tmp_path, [{"id": 1, "topic": "面试结果", "user_id": "u1"}])
    assert service.claim_followup(_scope(), "u1", "小明", "我回来了", now=NOON)
    settle = _method("injection.py", "_settle_concern_claim")
    event = _event(
        _scope(), claim={"concern": {"id": 1, "topic": "面试结果"}, "proactive": True},
    )
    settle(SimpleNamespace(concern_followup=service), event, spoke=spoke)
    assert not service.is_pending(1, now=NOON)
    assert service.recently_followed(1, now=NOON)
    remaining = service.cooldown_remaining(1, now=NOON)
    if spoke:
        assert remaining > service.considered_cooldown_seconds
    else:
        assert remaining <= service.considered_cooldown_seconds
