"""日上限按人跨场合、正负分别累计；私聊关系同样随时间变淡，闲置天数按人计算。"""

from __future__ import annotations

import sys
import types

import pytest

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

from domain.relationship_policy import DAILY_DELTA_CAP, cap_automatic_delta
from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository

BOT = "bot-0001"
SUBJECT = "qq:user:u1"
NOW = 1_800_000_000.0
DAY = 86400.0


def _group(gid: str) -> RuntimeScope:
    return RuntimeScope(BOT, "group", SessionRef(f"qq:group:{gid}", "qq", "group", gid), subject_principal_id=SUBJECT)


def _private() -> RuntimeScope:
    return RuntimeScope(BOT, "private", SessionRef("qq:private:u1", "qq", "private", "u1"), subject_principal_id=SUBJECT)


@pytest.fixture
def repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "soul.sqlite3"))
    ensure_scoped_soul_schema(cm)
    try:
        yield ScopedSoulRepository(cm)
    finally:
        cm.close()


def _event(repo, scope, delta: float, at: float, dimension: str = "trust", event_type: str = "bot_praised"):
    return repo.record_relationship_event(
        scope, event_type=event_type, dimension=dimension, delta=delta,
        reason="测试互动", created_at=at,
    )


def test_policy_counts_positive_and_negative_separately():
    # 正负交替：净值为 0，但正向已用满，不能再加
    assert cap_automatic_delta(dimension="trust", requested_delta=5, daily_total=0,
                               daily_positive=DAILY_DELTA_CAP, daily_negative=DAILY_DELTA_CAP) == 0.0
    assert cap_automatic_delta(dimension="trust", requested_delta=-5, daily_total=0,
                               daily_positive=0.0, daily_negative=DAILY_DELTA_CAP) == 0.0
    # 旧调用方式（只给净值）保持兼容
    assert cap_automatic_delta(dimension="trust", requested_delta=5, daily_total=12) == 3.0


def test_daily_cap_is_shared_across_groups_and_private(repo):
    applied = []
    for scope in (_group("g1"), _group("g1"), _group("g1"), _group("g2"), _private()):
        applied.append(_event(repo, scope, 5.0, NOW)["applied_delta"])
    # 15 分额度在 g1 用完；换群、换私聊都不能再加
    assert applied == [5.0, 5.0, 5.0, 0.0, 0.0]


def test_alternating_signs_cannot_bypass_cap(repo):
    total_positive = 0.0
    for i in range(8):
        delta = 5.0 if i % 2 == 0 else -5.0
        result = _event(repo, _group("g1"), delta, NOW + i)
        if result["applied_delta"] > 0:
            total_positive += result["applied_delta"]
    assert total_positive == DAILY_DELTA_CAP


def test_cap_window_rolls_after_24_hours(repo):
    for _ in range(3):
        _event(repo, _group("g1"), 5.0, NOW)
    assert _event(repo, _group("g2"), 5.0, NOW + DAY + 1)["applied_delta"] == 5.0


def _trust(repo, scope) -> float:
    rows = repo.list_relationships(scope)
    return float(rows[0]["values"]["trust"]["automatic_value"])


def test_private_relationship_decays_when_person_is_idle(repo):
    _event(repo, _private(), 5.0, NOW - 40 * DAY, event_type="deep_talk")
    before = _trust(repo, _private())
    stats = repo.apply_time_decay(now=NOW)
    assert stats["decayed_subjects"] >= 1
    assert _trust(repo, _private()) < before


def test_recent_group_activity_keeps_private_row_warm(repo):
    _event(repo, _private(), 5.0, NOW - 40 * DAY, event_type="deep_talk")
    _event(repo, _group("g1"), 1.0, NOW - 60, event_type="joke", dimension="fun")
    before = _trust(repo, _private())
    repo.apply_time_decay(now=NOW)
    # 这个人刚在群里聊过：私聊那份关系不算冷落
    assert _trust(repo, _private()) == before
