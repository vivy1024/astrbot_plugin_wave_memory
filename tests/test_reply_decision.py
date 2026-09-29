"""Offline rules and real mixin integration with fictional scopes and SQLite."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import socket
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from services.concern_followup import ConcernFollowupService
from services.proactive_policy import proactive_event_allowed
from services.reply_decision import ReplyDecisionService, ReplySignals


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Windows creates an internal loopback socketpair when constructing a loop.
    # Construct it first; all calls made by the test itself remain prohibited.
    loop = asyncio.new_event_loop()
    def denied(*args, **kwargs):
        raise AssertionError("reply decision tests must stay offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    yield loop
    loop.close()


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def signals(**kwargs):
    baseline = ReplySignals("bot-sample", "testnet:group:room-a", "group", "请帮我检查纸桥结构？", candidate=True, relevance=1.0)
    return replace(baseline, **kwargs)


@pytest.mark.parametrize("mode", [None, "", "bad", "off"])
def test_off_keeps_existing_candidate_and_does_not_create_wake(mode):
    svc = ReplyDecisionService({"mode": mode})
    assert svc.decide(signals(message="哈哈哈"))["allow"] is True
    assert svc.decide(signals(candidate=False))["allow"] is False


def test_shadow_records_counterfactual_but_does_not_suppress():
    svc = ReplyDecisionService({"mode": "shadow"})
    decision = svc.decide(signals(message="哈哈哈"))
    assert decision["decision"] == "suppress"
    assert decision["would_reply"] is False and decision["allow"] is True
    assert decision["enforced"] is False and "pure_noise" in decision["reasons"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("change,reason", [({"message": "  "}, "no_message"), ({"platform_allowed": False}, "platform_denied"), ({"stopped": True}, "event_stopped"), ({"self_message": True}, "self_message"), ({"session_id": ""}, "scope_missing")])
def test_rules_never_grant_permission(mode, change, reason):
    decision = ReplyDecisionService({"mode": mode}).decide(signals(**change))
    assert not decision["allow"] and reason in decision["reasons"]


@pytest.mark.parametrize("visibility", ["group", "private"])
def test_direct_requests_keep_priority_over_soft_rules(visibility):
    svc = ReplyDecisionService({"mode": "enforce"})
    for _ in range(8):
        svc.note_reply("bot-sample", "testnet:group:room-a", visibility)
    result = svc.decide(signals(visibility=visibility, direct=True, message="嗯", backlog=99, cooldown_remaining=300))
    assert result["allow"] and result["reasons"] == ["direct_priority"]


def test_relevance_question_noise_and_unknown_feedback_are_explainable():
    svc = ReplyDecisionService({"mode": "enforce", "backoff_base_seconds": 0})
    result = svc.decide(signals())
    assert result["allow"] and result["score"] == 65
    assert result["factors"]["feedback"] == 0 and "relationship" not in result["factors"]
    assert {"feedback_unknown", "question_request"} <= set(result["reasons"])
    assert not any(reason.startswith("relationship") for reason in result["reasons"])
    assert not svc.decide(signals(message="!!!"))["allow"]
    assert not svc.decide(signals(candidate=False, concern=True))["allow"]
    assert svc.decide(signals(message="我的纸桥已经搭好了", relevance=None, concern=True))["allow"]
    assert not svc.decide(signals(relevance=0, backlog=3))["allow"]


def test_only_explicit_feedback_changes_score():
    svc = ReplyDecisionService({"mode": "shadow", "backoff_base_seconds": 0})
    for unknown in (None, "unknown", "no_response", "timeout"):
        assert svc.decide(signals(feedback=unknown))["factors"]["feedback"] == 0
    assert svc.decide(signals(feedback="negative"))["factors"]["feedback"] == -15
    assert svc.decide(signals(feedback="positive"))["factors"]["feedback"] == 10


def test_affinity_scores_are_not_a_reply_signal():
    """好感分数只给群友玩：开口判断没有关系分数项，也不按分数拦截。"""
    assert "relationship" not in ReplySignals.__dataclass_fields__
    svc = ReplyDecisionService({"mode": "enforce", "backoff_base_seconds": 0})
    result = svc.decide(signals())
    assert result["allow"] and result["score"] == 65
    assert set(result["factors"]) == {
        "relevance", "question_request", "noise", "concern", "feedback", "backlog", "recent_replies",
    }


def test_reply_frequency_uses_sent_history_and_full_scope():
    clock = Clock()
    svc = ReplyDecisionService({"mode": "enforce"}, clock=clock)
    for _ in range(5):
        assert svc.decide(signals())["allow"]  # decisions aren't sends
    for _ in range(3):
        svc.note_reply("bot-sample", "testnet:group:room-a", "group")
    denied = svc.decide(signals())
    assert denied["recent_replies"] == 3 and not denied["allow"]
    assert svc.decide(signals(bot_id="bot-other"))["allow"]
    assert svc.decide(signals(session_id="othernet:group:room-a"))["allow"]
    clock.now += 61
    assert svc.decide(signals())["allow"]


def test_bounded_backoff_does_not_slide_and_recovers():
    clock = Clock()
    svc = ReplyDecisionService({"mode": "enforce", "backoff_base_seconds": 5, "backoff_max_seconds": 12}, clock=clock)
    for expected in (5, 10, 12, 12):
        result = svc.decide(signals(message="哈哈"))
        assert result["retry_after_seconds"] == expected
        clock.now += 1
        waiting = svc.decide(signals())
        assert waiting["retry_after_seconds"] == expected - 1
        clock.now += expected - 1
    assert svc.decide(signals())["allow"]
    clock.now -= 100
    assert svc.decide(signals())["allow"]  # wall-clock rollback cannot lock it indefinitely


def test_scoped_overrides_and_invalid_numeric_values():
    svc = ReplyDecisionService({
        "mode": "off", "score_threshold": "NaN", "backoff_max_seconds": "inf",
        "bots": {"bot-sample": {"mode": "shadow", "groups": {"testnet:group:room-a": {"mode": "enforce"}}}},
        "groups": {"testnet:group:room-b": {"mode": "enforce", "score_threshold": 90}},
    })
    assert svc.settings("bot-sample", "testnet:group:room-a")["mode"] == "enforce"
    assert svc.settings("bot-sample", "testnet:group:room-c")["mode"] == "shadow"
    assert svc.settings("bot-other", "testnet:group:room-b")["score_threshold"] == 90
    assert svc.settings("bot-other", "testnet:group:room-c")["mode"] == "off"
    assert svc.settings("bot-other", "testnet:group:room-c")["score_threshold"] == 45


def test_diagnostics_and_history_are_bounded_without_message_content():
    svc = ReplyDecisionService({"mode": "enforce"}, clock=Clock())
    for i in range(1100):
        svc.decide(signals(session_id=f"testnet:group:room-{i}", message="fictional private message", candidate=False))
    assert len(svc.diagnostics) == 128 and len(svc._history) <= 1024
    assert "fictional private message" not in json.dumps(list(svc.diagnostics))
    assert {"mode", "decision", "score", "reasons", "timestamp"} <= svc.diagnostics[-1].keys()


def scope(bot="bot-sample", room="room-a"):
    return RuntimeScope(bot_id=bot, visibility="group", subject_principal_id="testnet:user:visitor",
                        session=SessionRef(id=f"testnet:group:{room}", platform_id="testnet", kind="group", conversation_id=room))


@pytest.fixture
def followup(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE scoped_soul_concerns (
        id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
        topic TEXT, intensity REAL, status TEXT, evidence TEXT,
        created_at REAL, last_triggered REAL, last_progress_at REAL)""")
    for cid, bot in [(1, "bot-sample"), (2, "bot-other")]:
        conn.execute("INSERT INTO scoped_soul_concerns VALUES (?,?,?,'group',?,0.8,'active',?,1,NULL,NULL)",
                     (cid, bot, "testnet:group:room-a", "纸桥搭建进度", json.dumps([{"kind": "subject", "user_id": "visitor"}])))
    clock = Clock()
    service = ConcernFollowupService(SimpleNamespace(conn=conn), enabled=True, quiet_hours=(0, 0),
                                     state_path=str(tmp_path / "followups.json"), clock=clock)
    yield service
    conn.close()


class Event:
    def __init__(self, message="纸桥已经搭好了", *, direct=False):
        self.message_str = message
        self.is_at_or_wake_command = direct
        self.is_wake = True  # ALL handler activation alone is not a direct wake
        self.call_llm = False
        self.stopped = False
        self._extras = {}
        self._wave_memory_runtime_scope = scope()
        self.message_obj = SimpleNamespace(sender=SimpleNamespace(nickname="访客甲"))

    def get_extra(self, key):
        return self._extras.get(key)

    def set_extra(self, key, value):
        self._extras[key] = value

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stopped = True

    def get_message_str(self):
        return self.message_str

    def get_sender_id(self):
        return "visitor"

    def get_group_id(self):
        return "room-a"

    def get_self_id(self):
        return "self-sample"

    def get_result(self):
        return SimpleNamespace(chain=[SimpleNamespace(text="已收到")])


def mixin_method(filename, name, **namespace):
    """Compile the actual method, only replacing package-relative imports.

    Matches repository's existing AST integration tests; no installed AstrBot
    or live plugin bootstrap/configuration/database is needed.
    """
    path = Path(__file__).resolve().parents[1] / "app" / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)

    class Imports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            node.level = 0
            return node

    module = ast.fix_missing_locations(ast.Module(body=[Imports().visit(method)], type_ignores=[]))
    env = {"RuntimeScope": RuntimeScope, "AstrMessageEvent": Event, "logger": logging.getLogger(__name__), **namespace}
    exec(compile(module, str(path), "exec"), env)
    return env[name]


def plugin(followup, mode):
    svc = ReplyDecisionService({"mode": mode}, clock=followup.clock)
    return SimpleNamespace(reply_decision=svc, concern_followup=followup, meta_thinking=None, db=followup.db)


def apply(p, event):
    method = mixin_method("ingress.py", "_apply_reply_decision")
    method(p, event, event._wave_memory_runtime_scope, "visitor", "访客甲", event.message_str)


@pytest.mark.parametrize("mode,wake,stopped,claimed", [("off", True, False, True), ("shadow", True, False, True), ("enforce", False, True, False)])
def test_real_ingress_filters_before_claiming_cooldown(followup, mode, wake, stopped, claimed):
    p = plugin(followup, mode)
    event = Event("哈哈哈")
    apply(p, event)
    assert event.is_at_or_wake_command is wake and event.stopped is stopped
    assert followup.recently_followed(1) is claimed
    assert Path(followup.state_path).exists() is claimed
    assert event.get_extra("wave_memory_reply_decision") == event._wave_memory_reply_decision
    assert p.reply_decision.diagnostics[-1]["decision"] == event._wave_memory_reply_decision["decision"]


def test_real_ingress_never_wakes_ordinary_questions(followup):
    followup.enabled = False
    event = Event("谁能帮忙检查纸桥？")
    p = plugin(followup, "enforce")
    p.meta_thinking = SimpleNamespace(is_interesting=lambda _: True)
    apply(p, event)
    assert not event.is_at_or_wake_command and not event.stopped
    assert "no_existing_candidate" in event._wave_memory_reply_decision["reasons"]


@pytest.mark.parametrize("veto", ["stopped", "call_llm", "proactive_extra", "already_sent"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_real_ingress_cannot_bypass_framework_or_platform_veto(followup, mode, veto):
    event = Event()
    if veto == "proactive_extra":
        event.set_extra("wave_memory_proactive_allowed", False)
    else:
        setattr(event, "_has_send_oper" if veto == "already_sent" else veto, True)
    assert not proactive_event_allowed(event)
    apply(plugin(followup, mode), event)
    assert not event.is_at_or_wake_command and not followup.recently_followed(1)


def test_direct_priority_does_not_claim_proactive_cooldown(followup):
    event = Event("嗯", direct=True)
    event.set_extra("wave_memory_proactive_allowed", False)
    apply(plugin(followup, "enforce"), event)
    assert not event.stopped and event._wave_memory_reply_decision["allow"]
    assert not followup.recently_followed(1)


def test_followup_cooldown_survives_restart_with_injected_clock_and_is_diagnosed(followup):
    p = plugin(followup, "enforce")
    event = Event()
    apply(p, event)
    assert event.is_at_or_wake_command
    restarted = ConcernFollowupService(followup.db, enabled=True, quiet_hours=(0, 0),
                                       state_path=followup.state_path, clock=followup.clock)
    p.concern_followup = restarted
    another = Event()
    apply(p, another)
    assert not another.is_at_or_wake_command
    assert "concern_cooldown" in another._wave_memory_reply_decision["reasons"]
    assert another._wave_memory_reply_decision["retry_after_seconds"] > 0


def test_followup_group_gap_does_not_leak_between_bots(followup):
    assert followup.claim_followup(scope(), "visitor", "访客甲", "我的纸桥完工了")
    assert followup.claim_followup(scope(bot="bot-other"), "visitor", "访客甲", "我的纸桥完工了")


def test_real_send_hook_counts_once_even_when_bot_memory_disabled(followup, no_network):
    method = mixin_method("ingress.py", "_handle_bot_sent")
    p = plugin(followup, "enforce")
    p.ignore_bot_messages = True
    event = Event()
    no_network.run_until_complete(method(p, event))
    no_network.run_until_complete(method(p, event))
    assert p.reply_decision.decide(signals())["recent_replies"] == 1


def test_injection_guard_stops_before_memory_and_preserves_identity(followup, no_network):
    p = plugin(followup, "enforce")
    event = Event("哈哈哈哈")
    apply(p, event)
    event.stopped = False  # even an erroneously continued event gets the same veto
    guard = mixin_method("injection.py", "_reply_request_suppressed")
    p._reply_request_suppressed = lambda e: guard(p, e)
    p.enable_auto_inject = True
    method = mixin_method("injection.py", "_handle_inject_memory",
                          prepend_identity_safety_system_prompt=lambda text, *a, **kw: "identity-first:" + text)
    req = SimpleNamespace(system_prompt="original")
    no_network.run_until_complete(method(p, event, req))
    assert event.stopped and req.system_prompt.startswith("identity-first:")


def test_injection_rechecks_late_platform_veto(followup):
    p = plugin(followup, "enforce")
    event = Event()
    apply(p, event)
    event.set_extra("wave_memory_proactive_allowed", False)
    guard = mixin_method("injection.py", "_reply_request_suppressed")
    assert guard(p, event) and event.stopped
    assert event._wave_memory_reply_decision["decision"] == "suppress"
    assert "platform_denied_before_request" in p.reply_decision.diagnostics[-1]["reasons"]


def test_zero_clock_does_not_make_unseen_concern_recent(followup):
    followup.clock.now = 0
    assert not followup.recently_followed(1)
    assert followup.claim_followup(scope(), "visitor", "访客甲", "纸桥进展顺利")
    assert followup.recently_followed(1)


@pytest.mark.parametrize("direct", [False, True])
def test_ingress_pipeline_receives_original_wake_before_rules(followup, no_network, direct):
    p = plugin(followup, "enforce")
    event = Event("哈哈哈哈", direct=direct)
    event.message_obj.message = []
    event.message_id = "fictional-msg-01"
    observed = []

    async def process_message(**kwargs):
        observed.append((kwargs["message"], kwargs["event"].is_at_or_wake_command, kwargs["event"].is_stopped()))

    p.inbound_pipeline = SimpleNamespace(process_message=process_message)
    p.scope_resolver = SimpleNamespace(resolve_event=lambda e: SimpleNamespace(
        scope=scope(), sender_local_id="visitor", conversation_local_id="room-a", bot_self_id="self-sample"))
    p.min_message_length = 1
    p._bot_qq_ids = []
    p.group_whitelist = []
    p.group_blacklist = []
    p.hot_config = {"ingress.debounce_seconds": 0, "ingress.debounce_max_seconds": 0}
    apply_method = mixin_method("ingress.py", "_apply_reply_decision")
    p._apply_reply_decision = lambda *args: apply_method(p, *args)
    method = mixin_method("ingress.py", "_handle_message", asyncio=asyncio, time=time,
                          sanitize_display_name=lambda value: value)
    no_network.run_until_complete(method(p, event))
    assert observed == [("哈哈哈哈", direct, False)]
    assert event.stopped is not direct
    assert not followup.recently_followed(1)


def test_ingress_does_not_read_relationship_scores(followup):
    class _ScoreRepo:
        def __getattr__(self, name):
            raise AssertionError(f"开口判断不应读取关系仓储：{name}")

    p = plugin(followup, "enforce")
    p.db.soul_repository = _ScoreRepo()
    event = Event()
    apply(p, event)
    factors = event._wave_memory_reply_decision["factors"]
    assert "relevance" in factors and "relationship" not in factors


def test_missing_service_keeps_legacy_followup_but_platform_veto_still_applies(followup):
    p = plugin(followup, "off")
    p.reply_decision = None
    event = Event()
    event.call_llm = True
    apply(p, event)
    assert not event.is_at_or_wake_command
    event.call_llm = False
    apply(p, event)
    assert event.is_at_or_wake_command and followup.recently_followed(1)


@pytest.mark.parametrize("counts,expected", [
    ({"accepted": 0, "corrected": 0, "rejected": 0, "unknown": 8}, 0),
    ({"accepted": 0, "corrected": 8, "rejected": 0, "unknown": 0}, 0),
    ({"accepted": 0, "corrected": 0, "rejected": 2, "unknown": 0}, -15),
    ({"accepted": 2, "corrected": 0, "rejected": 0, "unknown": 0}, 10),
])
def test_candidate_feedback_uses_only_explicit_recent_reaction(followup, counts, expected):
    calls = []
    def feedback(owner, *, limit, since_ts):
        calls.append((owner, limit, since_ts))
        return counts
    p = plugin(followup, "shadow")
    p.reply_feedback_service = SimpleNamespace(enabled=True, decision_signals=feedback)
    event = Event()
    apply(p, event)
    assert calls == [(scope(), 8, p.reply_decision.clock() - 86400)]
    assert event._wave_memory_reply_decision["factors"]["feedback"] == expected
