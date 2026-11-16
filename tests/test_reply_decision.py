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
def test_real_ingress_filters_before_claiming_concern(followup, mode, wake, stopped, claimed):
    p = plugin(followup, mode)
    event = Event("哈哈哈")
    apply(p, event)
    assert event.is_at_or_wake_command is wake and event.stopped is stopped
    # 先占用、发送阶段才结算；开口判断本身不写跟进冷却
    assert followup.is_pending(1, now=followup.clock()) is claimed
    assert followup.recently_followed(1, now=followup.clock()) is False
    assert Path(followup.state_path).exists() is False
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
    assert followup.confirm_followup(1, spoke=True)
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
    release = mixin_method("injection.py", "_release_concern_claim")
    p._release_concern_claim = lambda ev: release(p, ev)
    guard = mixin_method("injection.py", "_reply_request_suppressed")
    assert guard(p, event) and event.stopped
    assert event._wave_memory_reply_decision["decision"] == "suppress"
    assert "platform_denied_before_request" in p.reply_decision.diagnostics[-1]["reasons"]
    # 平台在请求阶段才否决：临时占用要放掉，不能算一次已经发生的跟进
    assert not followup.is_pending(1, now=followup.clock())


def test_zero_clock_does_not_make_unseen_concern_recent(followup):
    followup.clock.now = 0
    assert not followup.recently_followed(1)
    assert followup.claim_followup(scope(), "visitor", "访客甲", "纸桥进展顺利")
    assert followup.is_pending(1, now=0)
    assert followup.confirm_followup(1, spoke=True, now=0)
    assert followup.recently_followed(1, now=0)


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
    assert event.is_at_or_wake_command and followup.is_pending(1, now=followup.clock())


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


def test_proactive_topic_match_beats_global_zero_and_followup_continues(followup):
    from domain.bot_profile import BotProfile, ProactiveSettings

    followup.enabled = False
    clock = followup.clock
    p = plugin(followup, "enforce")
    p.meta_thinking = SimpleNamespace(is_interesting=lambda _: False)
    profile = BotProfile(
        db_id="baizz",
        name="白真真",
        qq_id="1336495069",
        interest_keywords=["灵币", "打工"],
        proactive=ProactiveSettings(
            enabled=True,
            mode="enforce",
            min_gap_seconds=300.0,
            followup_window_seconds=120.0,
            max_per_hour=3,
            score_threshold=55.0,
            preferred_topics=["上班", "打工"],
        ),
    )
    p.bot_registry = SimpleNamespace(get=lambda bid: profile if bid == "baizz" else None)
    p._reply_tracker = {}
    baizz_scope = RuntimeScope(
        bot_id="baizz",
        visibility="group",
        subject_principal_id="testnet:user:visitor",
        session=SessionRef(id="testnet:group:room-a", platform_id="testnet", kind="group", conversation_id="room-a"),
    )

    # 1. 全局 meta_thinking 返回 False(0.0)，但命中白真真擅长话题「打工」+「上班」→ topic_match 顶起 relevance
    e1 = Event("打工上班太苦了")
    e1._wave_memory_runtime_scope = baizz_scope
    method = mixin_method("ingress.py", "_apply_reply_decision")
    method(p, e1, baizz_scope, "visitor", "访客甲", e1.message_str)
    assert e1._wave_memory_reply_decision["factors"]["relevance"] > 0
    assert e1._wave_memory_reply_decision["allow"] is True
    assert e1._wave_memory_reply_decision["proactive_wake"] is True

    # 2. 白真真回复后 56s，同一群友继续回一句不带关键词的话：虽在 min_gap=300s 内，但命中 120s 接话窗口仍放行
    p.reply_decision.note_reply("baizz", "testnet:group:room-a", "group", proactive=True)
    p._reply_tracker["baizz:room-a:visitor"] = clock()
    clock.now += 56.0
    e2 = Event("今天真有点顶不住")
    e2._wave_memory_runtime_scope = baizz_scope
    method(p, e2, baizz_scope, "visitor", "访客甲", e2.message_str)
    assert e2._wave_memory_reply_decision["allow"] is True
    assert e2._wave_memory_reply_decision["proactive_wake"] is True
    assert "followup_window" in e2._wave_memory_reply_decision["reasons"]

@pytest.mark.parametrize("hour,window,blocked", [
    (0, (0, 6), True), (5, (0, 6), True), (6, (0, 6), False), (7, (0, 6), False),
    (23, (23, 6), True), (0, (23, 6), True), (6, (23, 6), False), (12, (23, 6), False),
])
@pytest.mark.parametrize("path", ["topic", "followup", "concern", "direct"])
def test_service_quiet_union_blocks_non_direct_paths(followup, hour, window, blocked, path):
    from datetime import datetime
    from domain.bot_profile import BodySettings, BotProfile, ProactiveSettings

    # body.enabled=False 且不挂 body_service 仓时，仍应严格遵守服务静默窗口（[start, end) 左闭右开）。
    followup.clock.now = datetime(2026, 11, 15, hour).timestamp()
    followup.enabled = path == "concern"
    p = plugin(followup, "enforce")
    profile = BotProfile(
        db_id="bot-sample", name="样例", qq_id="self-sample",
        body=BodySettings(enabled=False, service_quiet_start=window[0], service_quiet_end=window[1]),
        proactive=ProactiveSettings(enabled=True, mode="enforce", quiet_hours_start=0, quiet_hours_end=0,
                                    preferred_topics=["纸桥", "搭建"], score_threshold=45),
    )
    p.bot_registry = SimpleNamespace(get=lambda _: profile)
    p.meta_thinking = SimpleNamespace(is_interesting=lambda _: True)
    if path == "followup":
        p._reply_tracker = {"bot-sample:room-a:visitor": followup.clock() - 10}
    event = Event("纸桥搭建如何了？", direct=path == "direct")
    apply(p, event)
    decision = event._wave_memory_reply_decision
    if blocked and path != "direct":
        assert decision["allow"] is False
        assert decision["proactive_wake"] is False
        assert "proactive_quiet_hours" in decision["reasons"]
        assert not event.is_at_or_wake_command
        assert not followup.is_pending(1, now=followup.clock())
    else:
        assert decision["allow"] is True


@pytest.mark.parametrize("hour,blocked", [(0, True), (5, True), (6, True), (7, False), (22, False)])
def test_service_quiet_and_proactive_quiet_form_union(followup, hour, blocked):
    from datetime import datetime
    from domain.bot_profile import BodySettings, BotProfile, ProactiveSettings

    # 服务静默 0–6 与主动免打扰 1–7 并集为 0–7：0/5/6 点皆禁，7 点恢复。
    followup.clock.now = datetime(2026, 11, 15, hour).timestamp()
    followup.enabled = False
    p = plugin(followup, "enforce")
    profile = BotProfile(
        db_id="bot-sample", name="样例", qq_id="self-sample",
        body=BodySettings(enabled=False, service_quiet_start=0, service_quiet_end=6),
        proactive=ProactiveSettings(enabled=True, mode="enforce", quiet_hours_start=1, quiet_hours_end=7,
                                    preferred_topics=["纸桥", "搭建"], score_threshold=45),
    )
    p.bot_registry = SimpleNamespace(get=lambda _: profile)
    event = Event("纸桥搭建如何了？")
    apply(p, event)
    decision = event._wave_memory_reply_decision
    assert decision["allow"] is (not blocked)
    if blocked:
        assert "proactive_quiet_hours" in decision["reasons"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_quiet_hours_also_blocks_concern_when_air_reading_disabled(mode):
    decision = ReplyDecisionService({"mode": mode}).decide(signals(
        direct=False, candidate=True, concern=True,
        proactive_enabled=False, quiet_hours_active=True,
    ))
    assert decision["allow"] is False
    assert "proactive_quiet_hours" in decision["reasons"]


def test_unanchored_proactive_question_cannot_break_threshold_by_padding():
    """复现线上 case：『为啥没人逆向LIbTv』无话题/无关切/非接话窗口，光靠问号+心智+身体+沉默不得破门。"""
    svc = ReplyDecisionService({"bots": {"yushu": {"mode": "enforce", "score_threshold": 55.0}}})
    d = svc.decide(ReplySignals(
        bot_id="yushu",
        session_id="qq:group:138118443",
        visibility="group",
        message="为啥没人逆向LIbTv？",
        direct=False,
        proactive_enabled=True,
        relevance=0.0,
        topic_match=0.0,
        concern=False,
        followup_eligible=False,
        mind_drive=0.95,
        mind_energy=0.95,
        body_drive=0.45,
        silence_messages=15,
    ))
    assert d["allow"] is False
    assert d["proactive_wake"] is False
    assert d["score"] < 55.0
    assert "no_topic_anchor" in d["reasons"]


def test_addressed_other_and_system_notice_are_hard_blocked_even_with_high_factors():
    """复现线上 case：『@傅四霁 ...』或『😢 所有图片均下载失败』即使叠满高分因子也硬拦截。"""
    from services.reply_decision import detect_message_air_flags

    svc = ReplyDecisionService({"bots": {"yushu": {"mode": "enforce", "score_threshold": 55.0}}})
    d_other = svc.decide(ReplySignals(
        bot_id="yushu",
        session_id="qq:group:138118443",
        visibility="group",
        message="[At:3193472] 缺氧自动化怎么搭？",
        direct=False,
        candidate=True,
        concern=True,
        proactive_enabled=True,
        addressed_other=True,
        relevance=1.0,
        topic_match=1.0,
        mind_drive=0.95,
        mind_energy=0.95,
        body_drive=0.45,
        silence_messages=12,
        feedback="positive",
    ))
    assert d_other["allow"] is False
    assert "addressed_other" in d_other["reasons"]

    flags = detect_message_air_flags("😢 所有图片均下载失败", bot_terms=("羽书",))
    assert flags["is_system_notice"] is True
    d_err = svc.decide(ReplySignals(
        bot_id="yushu",
        session_id="qq:group:398291136",
        visibility="group",
        message="😢 所有图片均下载失败",
        direct=False,
        proactive_enabled=True,
        is_system_notice=True,
        relevance=1.0,
        topic_match=1.0,
        followup_eligible=True,
    ))
    assert d_err["allow"] is False
    assert "system_or_error_notice" in d_err["reasons"]


def test_unrelated_group_chatter_settles_snubbed_while_followup_clears_it(followup):
    """群友聊别的不清零主动冷场计数，超 120s 且 >=4 条无关消息触发 snubbed；点名或接话窗口才清零。"""
    from domain.bot_profile import BodySettings, BotProfile, ProactiveSettings

    followup.enabled = False
    clock = followup.clock
    p = plugin(followup, "enforce")
    body_events = []
    mood_ignored = []
    p.body_service = SimpleNamespace(
        active_for=lambda _: True,
        reading=lambda _: None,
        in_service_quiet=lambda *a, **kw: False,
        record_event=lambda bot_id, kind, **kw: body_events.append((bot_id, kind)),
    )
    p.mood_service = SimpleNamespace(
        mind_state=lambda *a, **kw: None,
        note_silence_or_ignored=lambda bot_id, sid, **kw: mood_ignored.append((bot_id, sid, kw.get("reason"))),
        note_bot_engaged_in_session=lambda *a, **kw: None,
    )
    profile = BotProfile(
        db_id="bot-sample",
        name="羽书",
        qq_id="self-sample",
        body=BodySettings(enabled=True),
        proactive=ProactiveSettings(enabled=True, mode="enforce", min_gap_seconds=300.0, preferred_topics=["缺氧"]),
    )
    p.bot_registry = SimpleNamespace(get=lambda _: profile)
    p._reply_tracker = {}

    # Bot 刚主动插了一句嘴
    p.reply_decision.note_reply("bot-sample", "testnet:group:room-a", "group", proactive=True)
    clock.now += 150.0
    # 群里其他人聊了 4 句无关闲聊：不应清零，第 4 句应触发一次冷场结算与身体 snubbed
    for i in range(4):
        ev = Event(f"大家中午吃什么 {i}")
        apply(p, ev)
    state = p.reply_decision._state(("bot-sample", "group", "testnet:group:room-a"))
    assert state.unanswered_proactive == 1
    assert p.reply_decision.proactive_backoff_seconds(
        bot_id="bot-sample", session_id="testnet:group:room-a", visibility="group", base_gap_seconds=300.0
    ) > 0
    assert body_events == [("bot-sample", "snubbed")]
    assert mood_ignored == [("bot-sample", "testnet:group:room-a", "ignored")]

    # 群友直接 @ Bot：正式清零冷场退避
    direct_ev = Event("羽书在吗", direct=True)
    apply(p, direct_ev)
    assert state.unanswered_proactive == 0


def test_proactive_stale_abort_cancels_delayed_wake_before_llm(followup):
    """复现线上 case：主动唤醒后排队超过 stale_abort_seconds 或群内刷过多条新消息时放弃回复。"""
    from domain.bot_profile import BotProfile, ProactiveSettings

    followup.enabled = False
    clock = followup.clock
    p = plugin(followup, "enforce")
    profile = BotProfile(
        db_id="bot-sample",
        name="羽书",
        qq_id="self-sample",
        proactive=ProactiveSettings(
            enabled=True,
            mode="enforce",
            preferred_topics=["缺氧"],
            score_threshold=45.0,
            stale_abort_seconds=60.0,
            stale_max_new_messages=3,
        ),
    )
    p.bot_registry = SimpleNamespace(get=lambda _: profile)
    p.meta_thinking = SimpleNamespace(is_interesting=lambda _: True)

    woke = Event("缺氧电解制氧怎么摆？")
    apply(p, woke)
    assert woke.is_at_or_wake_command is True
    assert isinstance(woke._wave_memory_proactive_freshness, dict)

    # 模拟上游排队卡了 75 秒才进入 LLM 请求守卫
    clock.now += 75.0
    guard = mixin_method("injection.py", "_reply_request_suppressed")
    assert guard(p, woke) is True
    assert woke.stopped is True
    assert "proactive_stale_aborted" in woke._wave_memory_reply_decision["reasons"]

