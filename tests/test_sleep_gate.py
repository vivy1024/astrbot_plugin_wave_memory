"""睡眠门禁：睡着时被叫到不调模型；醒不醒按窗口内第几次叫决定；没醒只回一句固定短语。"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass

    _api.logger = _Logger()
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.bot_body_repo import BotBodyRepository
from engine.db.connection import ConnectionManager
from services.bot_body import BotBodyService

BOT = "yushu"
T0 = 1_800_000_000.0


def _gate_method():
    path = Path(__file__).resolve().parents[1] / "app" / "injection.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_sleep_gate")

    class Imports(ast.NodeTransformer):
        def visit_ImportFrom(self, n):
            n.level = 0
            return n

    module = ast.fix_missing_locations(ast.Module(body=[Imports().visit(node)], type_ignores=[]))
    namespace = {
        "RuntimeScope": RuntimeScope,
        "logger": SimpleNamespace(debug=lambda *a, **k: None),
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_sleep_gate"]


class _Event:
    def __init__(self, scope):
        self._wave_memory_runtime_scope = scope
        self.result = None
        self.llm = True
        self.stopped = False

    def get_sender_id(self):
        return "u1"

    def plain_result(self, text):
        return ("plain", text)

    def set_result(self, value):
        self.result = value

    def should_call_llm(self, flag):
        self.llm = flag

    def stop_event(self):
        self.stopped = True


def _scope():
    return RuntimeScope(
        BOT, "group", SessionRef("qq:group:g1", "qq", "group", "g1"), subject_principal_id="qq:user:u1",
    )


def _plugin(tmp_path, *, hour: float, enabled=True):
    cm = ConnectionManager(str(tmp_path / "wave.sqlite3"))
    db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
    profile = SimpleNamespace(body=SimpleNamespace(
        enabled=enabled, sleep_start=23, sleep_end=6, sleep_replies=["……zzz"],
        sleep_reply_cooldown_seconds=1800.0, hormone_gain=1.0,
    ))
    clock = {"now": T0}
    body = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: clock["now"], hour_of=lambda _t: hour)
    return cm, SimpleNamespace(body_service=body), clock


def test_awake_bot_is_not_gated(tmp_path):
    cm, plugin, _ = _plugin(tmp_path, hour=14.0)
    try:
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is False
        assert event.llm is True and event.stopped is False
    finally:
        cm.close()


def test_sleeping_bot_replies_with_a_fixed_phrase_and_skips_the_model(tmp_path, monkeypatch):
    cm, plugin, _ = _plugin(tmp_path, hour=2.0)
    try:
        import random
        monkeypatch.setattr(random, "random", lambda: 0.99)  # 掷骰不醒
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is True
        assert event.result == ("plain", "……zzz")
        assert event.llm is False and event.stopped is True
    finally:
        cm.close()


def test_same_person_gets_the_sleep_phrase_only_once_per_cooldown(tmp_path, monkeypatch):
    cm, plugin, clock = _plugin(tmp_path, hour=2.0)
    try:
        import random
        monkeypatch.setattr(random, "random", lambda: 0.99)
        gate = _gate_method()
        first, second = _Event(_scope()), _Event(_scope())
        assert gate(plugin, first) is True and first.result is not None
        clock["now"] += 30
        assert gate(plugin, second) is True
        assert second.result is None, "冷却期内保持沉默，不重复回"
        assert second.llm is False and second.stopped is True
    finally:
        cm.close()


def test_a_sixth_call_always_wakes_her(tmp_path, monkeypatch):
    cm, plugin, clock = _plugin(tmp_path, hour=2.0)
    try:
        import random
        monkeypatch.setattr(random, "random", lambda: 0.99)
        gate = _gate_method()
        outcomes = []
        for i in range(6):
            clock["now"] = T0 + i * 20
            outcomes.append(gate(plugin, _Event(_scope())))
        assert outcomes[:5] == [True] * 5 and outcomes[5] is False
        # 醒了之后同一波对话里保持清醒
        clock["now"] = T0 + 200
        assert gate(plugin, _Event(_scope())) is False
    finally:
        cm.close()


def test_body_disabled_for_this_bot_never_gates(tmp_path):
    cm, plugin, _ = _plugin(tmp_path, hour=2.0, enabled=False)
    try:
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is False and event.llm is True
    finally:
        cm.close()


def test_no_body_service_is_a_noop():
    event = _Event(_scope())
    assert _gate_method()(SimpleNamespace(body_service=None), event) is False


# ─── 模型服务静默（如 antigravity 夜间停服）：窗口内直接回话术，不依赖 body.enabled、不掷骰唤醒 ───


def _quiet_plugin(tmp_path, *, hour: float, enabled=False, start=23, end=6, replies=None):
    cm = ConnectionManager(str(tmp_path / "quiet.sqlite3"))
    db = type("DB", (), {"bot_body": BotBodyRepository(cm)})()
    profile = SimpleNamespace(body=SimpleNamespace(
        enabled=enabled, sleep_start=None, sleep_end=None, sleep_replies=["……zzz"],
        sleep_reply_cooldown_seconds=1800.0, hormone_gain=1.0,
        service_quiet_start=start, service_quiet_end=end,
        quiet_replies=replies if replies is not None else ["夜深了，{end}见。"],
    ))
    clock = {"now": T0}
    body = BotBodyService(db, profile_for=lambda _b: profile, clock=lambda: clock["now"], hour_of=lambda _t: hour)
    return cm, SimpleNamespace(body_service=body), clock


def test_quiet_window_replies_even_when_the_body_is_disabled(tmp_path):
    cm, plugin, _ = _quiet_plugin(tmp_path, hour=1.0, enabled=False)
    try:
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is True
        assert event.result == ("plain", "夜深了，6 点见。"), "{end} 换成几点回来"
        assert event.llm is False and event.stopped is True
    finally:
        cm.close()


def test_quiet_window_never_rolls_to_wake_her(tmp_path, monkeypatch):
    """静默窗口里模型停服，叫醒也答不上：连着叫很多次也不放行。"""
    cm, plugin, clock = _quiet_plugin(tmp_path, hour=2.0)
    try:
        import random
        monkeypatch.setattr(random, "random", lambda: 0.0)  # 掷骰必醒也不应该醒
        gate = _gate_method()
        for i in range(8):
            clock["now"] = T0 + i * 20
            event = _Event(_scope())
            assert gate(plugin, event) is True and event.llm is False
    finally:
        cm.close()


def test_outside_the_quiet_window_nothing_happens(tmp_path):
    cm, plugin, _ = _quiet_plugin(tmp_path, hour=14.0)
    try:
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is False and event.llm is True
    finally:
        cm.close()


def test_quiet_window_wraps_midnight_and_covers_both_sides(tmp_path):
    from domain import body_policy as bp

    for hour, expected in ((22.9, False), (23.0, True), (0.0, True), (5.9, True), (6.0, False), (14.0, False)):
        assert bp.in_quiet_window(hour, 23, 6) is expected, hour
    assert bp.in_quiet_window(3.0, None, None) is False and bp.in_quiet_window(3.0, 5, 5) is False
    assert bp.quiet_end_label(6) == "6 点" and bp.quiet_end_label(None) == "天亮"


def test_same_person_gets_the_quiet_phrase_only_once_per_cooldown(tmp_path):
    cm, plugin, clock = _quiet_plugin(tmp_path, hour=1.0)
    try:
        gate = _gate_method()
        first, second = _Event(_scope()), _Event(_scope())
        assert gate(plugin, first) is True and first.result is not None
        clock["now"] += 30
        assert gate(plugin, second) is True
        assert second.result is None, "冷却期内保持沉默，不重复回"
        assert second.llm is False and second.stopped is True
    finally:
        cm.close()


def test_quiet_phrases_are_customizable_and_picked_by_roll(tmp_path):
    cm, plugin, _ = _quiet_plugin(tmp_path, hour=1.0, end=7, replies=["甲 {end}", "乙，{end}回来", "丙"])
    try:
        body = plugin.body_service
        assert body.quiet_reply(BOT, "p1", pick=0) == "甲 7 点"
        assert body.quiet_reply(BOT, "p2", pick=1) == "乙，7 点回来"
        assert body.quiet_reply(BOT, "p3", pick=2) == "丙"
        assert body.quiet_reply(BOT, "p4", pick=3) == "甲 7 点", "超过话术数量时循环取"
    finally:
        cm.close()


def test_empty_quiet_phrases_stay_silent_but_still_skip_the_model(tmp_path):
    cm, plugin, _ = _quiet_plugin(tmp_path, hour=1.0, replies=[])
    try:
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is True
        assert event.result is None and event.llm is False and event.stopped is True
    finally:
        cm.close()


def test_quiet_and_sleep_phrases_do_not_eat_each_others_cooldown(tmp_path, monkeypatch):
    """静默话术与睡眠短语各自记冷却：同一个人先收到静默话术，之后进入睡眠态仍能收到睡眠短语。"""
    cm, plugin, clock = _quiet_plugin(tmp_path, hour=1.0)
    try:
        body = plugin.body_service
        assert body.quiet_reply(BOT, "p1") != ""
        assert body.sleep_reply(BOT, "p1") == "……zzz"
    finally:
        cm.close()


def test_no_quiet_window_configured_means_no_quiet(tmp_path):
    cm, plugin, _ = _quiet_plugin(tmp_path, hour=1.0, start=None, end=None)
    try:
        assert plugin.body_service.in_service_quiet(BOT) is False
        event = _Event(_scope())
        assert _gate_method()(plugin, event) is False
    finally:
        cm.close()
