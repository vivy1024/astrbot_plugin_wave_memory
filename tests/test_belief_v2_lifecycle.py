"""信念 v2 生命周期：转移规则、衰减、数量上限、否决/恢复与旧数据归档（全部保留历史）。"""

from __future__ import annotations

import json

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.bot_belief_repo import BeliefRevisionConflict, BeliefWrite
from engine.db.migrations.bot_beliefs import archive_legacy_scoped_beliefs
from services import belief_lifecycle as lc
from services.belief_lifecycle import BeliefAdminError, BeliefLifecycleService, BeliefPolicy

DAY = 86400.0
NOW = 1_790_000_000.0
BOT = "bot-demo"
POLICY = BeliefPolicy()


def ev(date: str, diary_id: int = 1, session: str = "qq:group:g1") -> dict:
    return {"diary_id": diary_id, "date": date, "session_id": session, "group_name": "摸鱼测试群", "title": "日记"}


def state(**fields) -> dict:
    base = {"id": 1, "status": "sprout", "support_dates": ["2026-09-20"], "confirm_runs": 1, "challenge_count": 0,
            "challenge_streak": 0, "first_seen_at": NOW, "last_confirmed_at": NOW, "revision": 1, "strength": 0.5}
    base.update(fields)
    return base


def test_policy_defaults_and_blank_config():
    assert BeliefPolicy.from_config({}) == BeliefPolicy(form_min_days=2, decay_days=30, max_active=20)
    assert BeliefPolicy.from_config({"belief_form_min_days": "", "belief_decay_days": None, "belief_max_active": "abc"}) == BeliefPolicy()
    policy = BeliefPolicy.from_config({"belief_form_min_days": 3, "belief_decay_days": 2, "belief_max_active": 999})
    assert (policy.form_min_days, policy.decay_days, policy.max_active, policy.stable_min_days) == (3, 3, 200, 6)


def test_sprout_forms_only_after_different_days_and_a_later_confirmation():
    sprout = lc.new_sprout(content="我讲冷笑话群里大多不接", category="group", triggers=["冷笑话"],
                           evidence=[ev("2026-09-20")], reason="", now=NOW, policy=POLICY)
    assert sprout.fields["status"] == "sprout" and sprout.event["event_type"] == "sprouted"
    same_day = lc.confirm(state(), evidence=[ev("2026-09-20", 2)], reason="", now=NOW, policy=POLICY)
    assert same_day.fields["status"] == "sprout", "同一天的经历不算在不同日子里被印证"
    formed = lc.confirm(state(), evidence=[ev("2026-09-21", 2)], reason="", now=NOW + DAY, policy=POLICY)
    assert formed.fields["status"] == "formed" and formed.fields["support_dates"] == ["2026-09-20", "2026-09-21"]
    # 萌芽时就有两天的依据，也要等后来的反思再印证一次
    born_with_two_days = lc.new_sprout(content="x" * 8, category="self", triggers=[], reason="", now=NOW, policy=POLICY,
                                       evidence=[ev("2026-09-19"), ev("2026-09-20", 2)])
    assert born_with_two_days.fields["status"] == "sprout"


def test_stable_needs_sustained_confirmation():
    formed = state(status="formed", support_dates=["2026-09-20", "2026-09-21", "2026-09-22"], confirm_runs=2)
    change = lc.confirm(formed, evidence=[ev("2026-09-23")], reason="", now=NOW, policy=POLICY)
    assert change.fields["status"] == "stable"
    too_few_runs = lc.confirm(state(status="formed", support_dates=["2026-09-20", "2026-09-21", "2026-09-22"], confirm_runs=1),
                              evidence=[ev("2026-09-23")], reason="", now=NOW, policy=POLICY)
    assert too_few_runs.fields["status"] == "formed"


@pytest.mark.parametrize(("status", "target"), [("sprout", "faded"), ("formed", "shaken"), ("stable", "shaken"), ("shaken", "faded")])
def test_counterexamples_shake_then_refute(status, target):
    change = lc.challenge(state(status=status), evidence=[ev("2026-09-22")], reason="反例", now=NOW, policy=POLICY)
    assert change.fields["status"] == target
    assert change.event["event_type"] == "challenged" and change.event["evidence"][0]["date"] == "2026-09-22"


def test_shaken_view_confirmed_again_goes_back_to_formed():
    change = lc.confirm(state(status="shaken", challenge_streak=1, support_dates=["2026-09-20", "2026-09-21"], confirm_runs=2),
                        evidence=[ev("2026-09-25")], reason="", now=NOW, policy=POLICY)
    assert change.fields["status"] == "formed" and change.fields["challenge_streak"] == 0


def test_long_idle_views_weaken_step_by_step_until_faded():
    stable = state(status="stable", last_confirmed_at=NOW)
    assert lc.decay(stable, now=NOW + 29 * DAY, policy=POLICY) is None
    first = lc.decay(stable, now=NOW + 31 * DAY, policy=POLICY)
    assert first.fields["status"] == "formed" and first.event["event_type"] == "decayed"
    after_first = {**stable, **first.fields}
    assert lc.decay(after_first, now=NOW + 40 * DAY, policy=POLICY) is None, "每降一级要再等一个周期"
    second = lc.decay(after_first, now=NOW + 62 * DAY, policy=POLICY)
    assert second.fields["status"] == "shaken"
    third = lc.decay({**after_first, **second.fields}, now=NOW + 93 * DAY, policy=POLICY)
    assert third.fields["status"] == "faded"
    assert lc.decay(state(status="sprout"), now=NOW + 31 * DAY, policy=POLICY).fields["status"] == "faded"
    assert lc.decay(state(status="vetoed"), now=NOW + 400 * DAY, policy=POLICY) is None


def test_strength_grows_with_days_and_drops_with_counterexamples_and_idleness():
    one = lc.compute_strength(state(), now=NOW, policy=POLICY)
    three = lc.compute_strength(state(support_dates=["2026-09-20", "2026-09-21", "2026-09-22"], confirm_runs=3), now=NOW, policy=POLICY)
    challenged = lc.compute_strength(state(support_dates=["2026-09-20", "2026-09-21", "2026-09-22"], confirm_runs=3,
                                           challenge_count=1, challenge_streak=1), now=NOW, policy=POLICY)
    idle = lc.compute_strength(state(support_dates=["2026-09-20", "2026-09-21", "2026-09-22"], confirm_runs=3), now=NOW + 60 * DAY, policy=POLICY)
    assert one < three and challenged < three and idle < three


def test_cap_keeps_the_strongest_held_views():
    held = [state(id=i, status="formed", support_dates=[f"2026-09-{10 + d:02d}" for d in range(i)], confirm_runs=2) for i in range(1, 5)]
    evicted = lc.cap_evictions(held, policy=BeliefPolicy(max_active=2), now=NOW)
    assert sorted(b["id"] for b in evicted) == [1, 2]


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "lifecycle.sqlite"), dimension=3)
    yield database
    database.close()


def _insert(db, content: str, **fields) -> int:
    base = {"content": content, "category": "world", "status": "formed", "support_dates": ["2026-09-20", "2026-09-21"],
            "confirm_runs": 2, "last_confirmed_at": NOW, "first_seen_at": NOW}
    base.update(fields)
    return db.bot_beliefs.apply(BOT, [BeliefWrite(None, base)], now=NOW)[0]


def test_veto_and_restore_keep_history_and_check_revision(db):
    belief_id = _insert(db, "晚上大家更愿意陪我聊天", status="stable")
    service = BeliefLifecycleService(db.bot_beliefs, clock=lambda: NOW + 10)
    current = db.bot_beliefs.get_belief(BOT, belief_id)
    with pytest.raises(BeliefRevisionConflict):
        service.veto(BOT, belief_id, revision=current["revision"] + 1)
    vetoed = service.veto(BOT, belief_id, revision=current["revision"], reason="不像她")
    assert vetoed["status"] == "vetoed" and vetoed["revision"] == current["revision"] + 1
    with pytest.raises(BeliefAdminError) as again:
        service.veto(BOT, belief_id, revision=vetoed["revision"])
    assert again.value.code == "belief_already_vetoed"
    restored = service.restore(BOT, belief_id, revision=vetoed["revision"])
    assert restored["status"] == "stable", "恢复到否决前的状态"
    history = db.bot_beliefs.list_events(BOT, belief_ids=[belief_id])
    assert [(e["event_type"], e["from_status"], e["to_status"], e["actor"]) for e in history] == [
        ("vetoed", "stable", "vetoed", "webui"), ("restored", "vetoed", "stable", "webui"),
    ]
    assert history[0]["reason"] == "不像她"
    with pytest.raises(BeliefAdminError) as missing:
        service.veto(BOT, 99999, revision=1)
    assert missing.value.code == "belief_not_found"
    with pytest.raises(BeliefAdminError) as other_bot:
        BeliefLifecycleService(db.bot_beliefs).veto("bot-other", belief_id, revision=restored["revision"])
    assert other_bot.value.code == "belief_not_found", "按 Bot 隔离"


def test_service_decay_and_cap_are_idempotent(db):
    old = _insert(db, "很久以前的看法", status="stable", last_confirmed_at=NOW - 40 * DAY)
    service = BeliefLifecycleService(db.bot_beliefs, BeliefPolicy(max_active=2), clock=lambda: NOW)
    assert service.apply_decay(BOT) == 1
    assert service.apply_decay(BOT) == 0, "同一周期内不重复降级"
    assert db.bot_beliefs.get_belief(BOT, old)["status"] == "formed"
    for text, days in (("看法一号很强", 4), ("看法二号也强", 3), ("看法三号最弱", 1)):
        _insert(db, text, support_dates=[f"2026-09-{10 + d:02d}" for d in range(days)])
    assert service.enforce_cap(BOT) == 2
    remaining = {b["content"] for b in db.bot_beliefs.list_beliefs(BOT, statuses=["formed", "stable", "shaken"])}
    assert remaining == {"看法一号很强", "看法二号也强"}
    faded = db.bot_beliefs.list_beliefs(BOT, statuses=["faded"])
    assert {b["content"] for b in faded} == {"很久以前的看法", "看法三号最弱"}
    for belief in faded:
        assert db.bot_beliefs.list_events(BOT, belief_ids=[belief["id"]])[-1]["event_type"] == "cap_evicted"
    assert service.enforce_cap(BOT) == 0


def test_repository_apply_is_atomic_on_revision_conflict(db):
    first = _insert(db, "第一条看法内容")
    second = _insert(db, "第二条看法内容")
    rev1 = db.bot_beliefs.get_belief(BOT, first)["revision"]
    with pytest.raises(BeliefRevisionConflict):
        db.bot_beliefs.apply(BOT, [
            BeliefWrite(first, {"status": "shaken"}, expected_revision=rev1, events=[{"event_type": "challenged"}]),
            BeliefWrite(second, {"status": "shaken"}, expected_revision=999),
        ], now=NOW)
    assert db.bot_beliefs.get_belief(BOT, first)["status"] == "formed", "整体回滚"
    assert db.bot_beliefs.list_events(BOT, belief_ids=[first]) == []
    with pytest.raises(ValueError):
        db.bot_beliefs.apply(BOT, [BeliefWrite(first, {"bot_id": "hijack"})], now=NOW)


def test_legacy_scoped_beliefs_are_archived_read_only_and_idempotently(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    db = WaveMemoryDB(path, dimension=3)
    scope = RuntimeScope(BOT, "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
    for key, status, kind in (("a", "active", "person_judgment"), ("b", "pending", "preference"), ("c", "active", "person_judgment")):
        db.scoped_knowledge.upsert_scoped_belief(scope, belief_key=key, content=f"测试群友甲偏爱射击游戏{key}",
                                                 belief_type=kind, status=status, provenance={"source": "llm_reflection"})
    db.close()

    db = WaveMemoryDB(path, dimension=3)  # 启动时迁移
    try:
        rows = db.scoped_knowledge.list_bot_scoped_beliefs(BOT, status=None)
        assert {r["status"] for r in rows} == {"legacy_archived"}
        by_key = {r["belief_key"]: r for r in rows}
        assert by_key["b"]["provenance"]["legacy_status"] == "pending"
        assert by_key["a"]["provenance"]["legacy_status"] == "active"
        assert by_key["a"]["provenance"]["source"] == "llm_reflection", "原 provenance 保留"
        assert by_key["a"]["content"] == "测试群友甲偏爱射击游戏a", "不删除、不改内容"
        stamp = by_key["a"]["provenance"]["legacy_archived_at"]
        assert archive_legacy_scoped_beliefs(db._cm) == 0, "再跑一次没有改动"
        again = {r["belief_key"]: r for r in db.scoped_knowledge.list_bot_scoped_beliefs(BOT, status=None)}
        assert again["a"]["provenance"]["legacy_archived_at"] == stamp
        assert db.bot_beliefs.list_beliefs(BOT) == [], "旧数据不进入信念 v2"
    finally:
        db.close()


def test_bot_belief_schema_is_additive(tmp_path):
    path = str(tmp_path / "schema.sqlite")
    WaveMemoryDB(path, dimension=3).close()
    db = WaveMemoryDB(path, dimension=3)
    try:
        tables = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"bot_beliefs", "bot_belief_events", "bot_belief_reflection_runs"} <= tables
        run_id = db.bot_beliefs.claim_run(BOT, "2026-09-20", now=NOW)
        assert db.bot_beliefs.claim_run(BOT, "2026-09-20", now=NOW + 10) is None
        db.bot_beliefs.apply(BOT, [], finish_run={"run_id": run_id, "diary_cursor": 7, "diary_ids": [7], "counts": {"new": 0}}, now=NOW)
        assert db.bot_beliefs.claim_run(BOT, "2026-09-20", now=NOW + 99999) is None, "成功过的那天不再占用"
        assert db.bot_beliefs.diary_cursor(BOT) == 7
        assert json.loads(json.dumps(db.bot_beliefs.last_succeeded_run(BOT)))["counts"] == {"new": 0}
    finally:
        db.close()
