"""Offline lifecycle tests using fictional people, bots and local SQLite only."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef, ScopeCodec
from engine.db.connection import ConnectionManager
from engine.db.fact_policy_repo import FactPolicyRepo, source_memory, resolve_subject
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.migrations.scoped_fact_history import ensure_scoped_fact_history_schema
from engine.db.migrations.scoped_fact_review import ensure_scoped_fact_review_schema
from engine.db.outbox_repo import OutboxRepository
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.auto_review import AutoReviewer
from services.fact_policy import FactPolicy, is_effective, ineffective_reason
from services.facts_conflict import FactConflictClassifier
from services.injection.channels.facts import FactsChannel
from services.scoped_knowledge_mutations import (
    ScopedKnowledgeMutationGateway, ScopedKnowledgeMutationTarget,
    ScopedKnowledgeNotFound, ScopedKnowledgeRevisionConflict, ScopedKnowledgeIdempotencyConflict,
)
from tools.fact_proposal import WaveMemoryProposeFactTool


def ts(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-09-29T12:00:00")


def scope(bot="bot-cedar", kind="group", conversation="room-a"):
    return RuntimeScope(bot, kind, SessionRef(f"demo:{kind}:{conversation}", "demo", kind, conversation))


class Coordinator:
    def __init__(self, cm):
        self.cm = cm
        self._consumer_names = ("projection",)

    async def transaction(self, callback, *, actor=None):
        with self.cm.write_transaction() as conn:
            return callback(conn)


@pytest.fixture
def env(tmp_path):
    cm = ConnectionManager(str(tmp_path / "fictional-facts.sqlite3"))
    cm.executescript("""CREATE TABLE memories (
        id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
        resolution_state TEXT, quarantine INTEGER DEFAULT 0, content TEXT,
        sender_id TEXT, sender_name TEXT, timestamp REAL, source TEXT DEFAULT 'live',
        memory_type TEXT DEFAULT 'message', group_id TEXT DEFAULT 'room-a');""")
    ensure_scoped_derived_knowledge_schema(cm)
    ensure_scoped_fact_history_schema(cm)
    ensure_scoped_fact_review_schema(cm)
    OutboxRepository.migrate(cm._write_conn)
    cm.commit()
    repo = ScopedKnowledgeRepo(cm)
    writer = SimpleNamespace(coordinator=Coordinator(cm))
    result = SimpleNamespace(cm=cm, repo=repo, facts=FactPolicyRepo(cm), writer=writer,
                             db=SimpleNamespace(scoped_knowledge=repo, conn=cm.conn))
    result.gateway = ScopedKnowledgeMutationGateway(writer, clock=SimpleNamespace(now=lambda: NOW))
    yield result
    cm.close()


def memory(env, *, target=None, content="我住在北港", sender="73001", name="林舟", timestamp=NOW, **extra):
    target = target or scope()
    cursor = env.cm.execute_write(
        """INSERT INTO memories(bot_id,session_id,visibility,resolution_state,quarantine,content,
           sender_id,sender_name,timestamp,source,memory_type) VALUES (?,?,?,'resolved',?,?,?,?,?,?,?)""",
        (target.bot_id, target.session.id, target.visibility, extra.get("quarantine", 0), content,
         sender, name, timestamp, extra.get("source", "live"), extra.get("memory_type", "message")),
    )
    env.cm.commit()
    return cursor.lastrowid


def fact(env, *, target=None, subject="林舟", predicate="住在", object="北港", **kwargs):
    return env.repo.upsert_scoped_fact(target or scope(), subject=subject, predicate=predicate, object=object, **kwargs)


def review(env, fact_id, *, target=None, revision=1, **kwargs):
    return asyncio.run(env.gateway.review_fact(scope=target or scope(),
        target=ScopedKnowledgeMutationTarget("fact", fact_id, revision), **kwargs))


@pytest.mark.parametrize("word,offset", [("今天", 0), ("今晚", 0), ("明天", 1), ("昨天", -1), ("tonight", 0)])
def test_relative_time_uses_source_day_not_processing_day(word, offset):
    anchor = ts("2021-04-03T05:00:00")
    validity = FactPolicy().validity(predicate="计划", object="参加会议", source_quote=f"{word}参加会议", source_timestamp=anchor)
    assert validity.valid_from == ts("2021-04-03T00:00:00") + offset * 86400
    assert validity.valid_until == validity.valid_from + 86400


def test_configurable_timezone_handles_local_day_and_dst():
    policy = FactPolicy(timezone="America/New_York")
    validity = policy.validity(predicate="计划", object="聚会", source_quote="今天聚会", source_timestamp=ts("2026-03-08T12:00:00"))
    assert validity.valid_from == ts("2026-03-08T05:00:00")
    assert validity.valid_until == ts("2026-03-09T04:00:00")
    assert validity.valid_until - validity.valid_from == 23 * 3600


@pytest.mark.parametrize("quote", ["今天我认识到林舟擅长编程", "今天我才了解到林舟擅长编程", "今天发现林舟擅长编程"])
def test_discovery_date_does_not_expire_a_long_term_fact(quote):
    validity = FactPolicy().validity(predicate="擅长", object="编程", source_quote=quote, source_timestamp=NOW)
    assert validity.valid_from is None and validity.valid_until is None
    assert not validity.needs_review


@pytest.mark.parametrize("quote,reason", [("今晚开会", "source_time_required"), ("最近开会", "unclear_time"), ("2026-09-30和2026-10-01开会", "ambiguous_time_range")])
def test_unknown_time_is_not_invented(quote, reason):
    validity = FactPolicy().validity(predicate="参加", object="开会", source_quote=quote)
    assert validity.needs_review and validity.reason == reason
    assert validity.valid_until is None


def test_explicit_date_takes_priority_and_date_end_is_inclusive():
    policy = FactPolicy(timezone="Asia/Tokyo")
    validity = policy.validity(predicate="参加", object="会议", source_quote="明天开会", source_timestamp=NOW,
                               valid_from="2026-10-04", valid_until="2026-10-05")
    assert validity.valid_from == ts("2026-10-03T15:00:00")
    assert validity.valid_until == ts("2026-10-05T15:00:00")
    inferred = policy.validity(predicate="参加", object="会议", source_quote="2026年10月4日参加会议")
    assert inferred.valid_from == validity.valid_from
    assert inferred.valid_until == ts("2026-10-04T15:00:00")


def test_birthday_date_is_content_not_an_expiry():
    validity = FactPolicy().validity(predicate="出生日期", object="2001-04-02", source_quote="林舟出生于2001-04-02")
    assert validity.valid_until is None and not validity.needs_review


@pytest.mark.parametrize("quote,start,end", [
    ("明天14:00-16:00参加会议", "2026-09-30T14:00:00", "2026-09-30T16:00:00"),
    ("今天截至18:00有空", "2026-09-29T00:00:00", "2026-09-29T18:00:00"),
    ("今晚在家", "2026-09-29T00:00:00", "2026-09-30T00:00:00"),
])
def test_explicit_clock_and_temporary_state(quote, start, end):
    result = FactPolicy().validity(predicate="安排", object="空闲", source_quote=quote, source_timestamp=NOW)
    assert (result.valid_from, result.valid_until) == (ts(start), ts(end))


@pytest.mark.parametrize("start,end", [(20, 10), (10, 10), (float("inf"), None), (True, None), (None, "invalid")])
def test_invalid_explicit_intervals_are_rejected(start, end):
    with pytest.raises(ValueError):
        FactPolicy().validity(predicate="住在", object="北港", valid_from=start, valid_until=end)


def test_validity_boundaries_are_half_open():
    row = {"status": "active", "valid_from": 10, "valid_until": 20}
    assert not is_effective(row, 9.999)
    assert is_effective(row, 10) and is_effective(row, 19.999)
    assert not is_effective(row, 20)
    assert ineffective_reason(row, 10) is None
    assert ineffective_reason(row, 9) == "not_yet_valid"
    assert ineffective_reason(row, 20) == "expired"


def test_classifier_checks_all_rows_and_configures_single_value_slots():
    c = FactConflictClassifier()
    candidate = {"subject": "林舟", "predicate": "住在", "object": "南湾", "valid_from": 20}
    rows = [{**candidate, "id": 1}, {**candidate, "id": 2, "object": "北港"}]
    assert c.classify(candidate, rows).existing_id == 2
    assert c.classify(candidate, [{**rows[1], "valid_until": 20, "valid_from": 10}]).relation == "scoped"
    assert c.classify({**candidate, "predicate": "喜欢"}, [{**rows[1], "predicate": "喜欢"}]).relation == "compatible"
    custom = FactConflictClassifier(policy=FactPolicy(single_value_predicates=("喜欢",)))
    assert custom.classify({**candidate, "predicate": "喜欢"}, [{**rows[1], "predicate": "喜欢"}]).relation == "conflicts"


def test_provenance_does_not_authorize_replacement(env):
    old = fact(env, status="active")
    new = fact(env, object="南湾", provenance={"supersedes": True, "supersedes_fact_id": old})
    assert review(env, new, action="approve").status == "conflict"
    assert env.facts.get(scope(), old)["status"] == "active"


def test_explicit_replace_is_atomic_audited_and_idempotent(env):
    old = fact(env, status="active", provenance={"source_quote": "旧的来源原话"})
    new = fact(env, object="南湾")
    kwargs = dict(action="replace", supersedes_fact_id=old, supersedes_revision=1, idempotency_key="replace-fiction")
    result = review(env, new, **kwargs)
    assert result.status == "active"
    previous = env.facts.get(scope(), old)
    assert previous["status"] == "superseded" and previous["valid_until"] == NOW
    assert previous["provenance"]["source_quote"] == "旧的来源原话"
    assert env.cm.execute_read("SELECT COUNT(*) FROM scoped_fact_reviews").fetchone()[0] == 2
    assert env.cm.execute_read("SELECT COUNT(*) FROM domain_outbox").fetchone()[0] == 2
    assert review(env, new, **kwargs).operation_id == result.operation_id
    assert env.cm.execute_read("SELECT COUNT(*) FROM domain_outbox").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["replace", "coexist"])
def test_auto_review_has_no_conflict_resolution_authority(env, action):
    old = fact(env, status="active")
    new = fact(env, object="南湾")
    with pytest.raises(ValueError, match="human_review"):
        review(env, new, action=action, actor="auto_review", **(
            {"supersedes_fact_id": old, "supersedes_revision": 1} if action == "replace" else {}))
    assert env.facts.get(scope(), old)["status"] == "active"
    assert env.facts.get(scope(), new)["status"] == "pending"


def test_coexist_and_multi_value_relations_do_not_retire_facts(env):
    old = fact(env, status="active")
    new = fact(env, object="南湾")
    assert review(env, new, action="coexist").status == "active"
    assert env.facts.get(scope(), old)["status"] == "active"
    first = fact(env, predicate="喜欢", object="茶", status="active")
    second = fact(env, predicate="喜欢", object="咖啡")
    assert review(env, second, action="approve").status == "active"
    assert env.facts.get(scope(), first)["status"] == "active"


def test_replace_rejects_stale_target_and_rolls_back(env):
    old = fact(env, status="active")
    new = fact(env, object="南湾")
    with pytest.raises(ScopedKnowledgeRevisionConflict):
        review(env, new, action="replace", supersedes_fact_id=old, supersedes_revision=2)
    assert env.facts.get(scope(), old)["status"] == "active"
    assert env.facts.get(scope(), new)["revision"] == 1
    assert env.cm.execute_read("SELECT COUNT(*) FROM write_operations").fetchone()[0] == 0


def test_audit_failure_rolls_back_both_facts_and_outbox(env):
    old = fact(env, status="active")
    new = fact(env, object="南湾")
    env.cm.executescript("""CREATE TRIGGER fail_review BEFORE INSERT ON scoped_fact_reviews
                           BEGIN SELECT RAISE(ABORT, 'fictional audit failure'); END;""")
    with pytest.raises(Exception, match="fictional audit failure"):
        review(env, new, action="replace", supersedes_fact_id=old, supersedes_revision=1)
    assert env.facts.get(scope(), old)["status"] == "active"
    assert env.facts.get(scope(), new)["status"] == "pending"
    assert env.cm.execute_read("SELECT COUNT(*) FROM domain_outbox").fetchone()[0] == 0


def test_future_candidate_cannot_retire_a_current_fact(env):
    old = fact(env, status="active")
    new = fact(env, object="南湾", valid_from=NOW + 3600)
    with pytest.raises(ValueError, match="replacement_not_effective"):
        review(env, new, action="replace", supersedes_fact_id=old, supersedes_revision=1)
    assert env.facts.get(scope(), old)["status"] == "active"


def test_expired_old_fact_does_not_block_new_value(env):
    old = fact(env, status="active", valid_until=NOW)
    new = fact(env, object="南湾")
    assert review(env, new, action="approve").status == "active"
    assert env.facts.get(scope(), old)["status"] == "active"  # Preserve history; eligibility handles expiry.


@pytest.mark.parametrize("foreign", [scope("bot-maple"), scope(kind="private", conversation="73001"), scope(conversation="room-b")])
def test_review_and_replace_cannot_cross_scope(env, foreign):
    old = fact(env, target=foreign, status="active")
    new = fact(env, object="南湾")
    assert review(env, new, action="approve").status == "active"
    other = fact(env, object="东城")
    with pytest.raises(ScopedKnowledgeNotFound):
        review(env, other, action="replace", supersedes_fact_id=old, supersedes_revision=1)


@pytest.mark.parametrize("subject", ["73001", "林舟", "demo:user:73001"])
def test_auto_review_accepts_resolved_real_accounts_and_named_subjects(env, subject):
    mid = memory(env)
    fid = fact(env, subject=subject, source_memory_id=mid, provenance={"source_quote": "我住在北港"})
    reviewer = AutoReviewer(env.db, env.writer, enabled=True)
    assert asyncio.run(reviewer.review_fact(scope(), fid)) == "active"
    assert env.cm.execute_read("SELECT actor FROM scoped_fact_reviews").fetchone()[0] == "auto_review"


def test_auto_review_accepts_explicit_third_party(env):
    mid = memory(env, content="柯岚住在北港", name="林舟")
    fid = fact(env, subject="柯岚", source_memory_id=mid, provenance={"source_quote": "柯岚住在北港"})
    assert asyncio.run(AutoReviewer(env.db, env.writer, enabled=True).review_fact(scope(), fid)) == "active"


@pytest.mark.parametrize("subject,quote,obj", [("未知", "我住在北港", "北港"), ("---", "我住在北港", "北港"),
    ("999999", "我住在北港", "北港"), ("林舟", "我住在南湾", "南湾"), ("林舟", "我住在北港", "南湾")])
def test_auto_review_keeps_invalid_or_unsupported_facts_pending(env, subject, quote, obj):
    mid = memory(env)
    fid = fact(env, subject=subject, object=obj, source_memory_id=mid, provenance={"source_quote": quote})
    assert asyncio.run(AutoReviewer(env.db, env.writer, enabled=True).review_fact(scope(), fid)) is None
    assert env.facts.get(scope(), fid)["status"] == "pending"


@pytest.mark.parametrize("quote", ["听说林舟住在北港", "林舟最近住在北港", "林舟可能住在北港", "林舟住在北港？"])
def test_uncertain_source_or_time_stays_pending(env, quote):
    mid = memory(env, content=quote)
    fid = fact(env, source_memory_id=mid, provenance={"source_quote": quote})
    assert asyncio.run(AutoReviewer(env.db, env.writer, enabled=True).review_fact(scope(), fid)) is None


def test_gateway_revalidates_source_in_transaction(env):
    mid = memory(env)
    fid = fact(env, source_memory_id=mid, provenance={"source_quote": "我住在北港"})
    env.cm.execute_write("UPDATE memories SET quarantine=1 WHERE id=?", (mid,))
    env.cm.commit()
    with pytest.raises(ValueError, match="no_evidence"):
        review(env, fid, action="approve", actor="auto_review")
    assert env.facts.get(scope(), fid)["status"] == "pending"


def test_cross_bot_source_forged_after_proposal_is_not_trusted(env):
    mid = memory(env, target=scope("bot-maple"))
    fid = fact(env, provenance={"source_quote": "我住在北港"})
    env.cm.execute_write("UPDATE scoped_facts SET source_memory_id=? WHERE id=?", (mid, fid))
    env.cm.commit()
    assert asyncio.run(AutoReviewer(env.db, env.writer, enabled=True).review_fact(scope(), fid)) is None
    with pytest.raises(ValueError, match="no_evidence"):
        review(env, fid, action="approve", actor="auto_review")


def test_quarantine_cannot_be_automatically_approved(env):
    mid = memory(env)
    fid = fact(env, status="quarantined", source_memory_id=mid, provenance={"source_quote": "我住在北港"})
    with pytest.raises(ValueError, match="auto_review_requires_pending"):
        review(env, fid, action="approve", actor="auto_review")


def test_automatic_approval_never_honors_provenance_supersedes(env):
    old = fact(env, status="active")
    mid = memory(env, content="我住在南湾")
    new = fact(env, object="南湾", source_memory_id=mid,
               provenance={"source_quote": "我住在南湾", "supersedes": True, "supersedes_fact_id": old})
    assert review(env, new, action="approve", actor="auto_review").status == "conflict"
    assert env.facts.get(scope(), old)["status"] == "active"


def test_duplicate_proposal_preserves_source_validity_and_history(env):
    old = fact(env, status="active", valid_from=10, valid_until=20, provenance={"source_quote": "旧的明确原话"})
    before = env.facts.get(scope(), old)
    assert env.facts.propose(scope(), subject="林舟", predicate="住在", object="北港",
                             provenance={"source_quote": "新的原话"}) == old
    assert env.facts.get(scope(), old) == before
    row = env.cm.execute_read("SELECT candidate_snapshot,existing_snapshot FROM scoped_fact_history").fetchone()
    assert json.loads(row[0])["provenance"]["source_quote"] == "新的原话"
    assert json.loads(row[1])["valid_until"] == 20


def test_proposal_sends_single_value_conflict_to_review_immediately(env):
    old = fact(env, status="active")
    fid = env.facts.propose(scope(), subject="林舟", predicate="住在", object="南湾")
    assert env.facts.get(scope(), fid)["status"] == "conflict"
    assert env.facts.get(scope(), old)["status"] == "active"


def test_tool_anchors_tonight_to_source_and_does_not_invent_evidence(env):
    mid = memory(env, content="今晚参加会议", timestamp=ts("2020-05-02T23:50:00"))
    ctx = SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope())))
    tool = WaveMemoryProposeFactTool(db=env.db)
    result = asyncio.run(tool.call(ctx, subject="73001", predicate="参加", object="会议",
                                  source_quote="今晚参加会议", source_memory_id=mid, context_evidence="当事人原话"))
    assert "已成功提审事实" in result
    row = env.repo.list_scoped_facts(scope())[0]
    assert row["valid_until"] == ts("2020-05-03T00:00:00")
    assert row["provenance"]["resolved_subject"]["account_id"] == "73001"
    asyncio.run(tool.call(ctx, subject="林舟", predicate="住在", object="南湾", source_quote="我住在南湾",
                          source_memory_id=mid, context_evidence="无法核对的摘录"))
    assert env.repo.list_scoped_facts(scope())[0]["source_memory_id"] is None


@pytest.mark.asyncio
async def test_tool_proposal_uses_real_writer_and_preserves_evidence(env, monkeypatch):
    from engine.write_coordinator import WriteCoordinator
    from services import auto_review

    quote = "这是会议的背景说明。" * 30 + "今晚参加会议"
    context = "当事人明确说明当晚安排。" * 20
    mid = memory(env, content=quote)
    coordinator = WriteCoordinator(env.cm.db_path, command_handlers={}, consumer_names=("projection",),
                                   clock=SimpleNamespace(now=lambda: NOW))
    writer = SimpleNamespace(coordinator=coordinator)
    # Startup already publishes this dependency even with auto-approval disabled.
    monkeypatch.setattr(auto_review, "_REVIEWER", AutoReviewer(env.db, writer, enabled=False))

    def no_legacy_transaction(*args, **kwargs):
        pytest.fail("production proposal bypassed the writer")

    monkeypatch.setattr(env.cm, "write_transaction", no_legacy_transaction)
    ctx = SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope())))
    try:
        result = await WaveMemoryProposeFactTool(db=env.db).call(
            ctx, subject="73001", predicate="参加", object="会议", source_memory_id=mid,
            source_quote=quote, context_evidence=context)
        assert "已成功提审事实" in result
        row = env.repo.list_scoped_facts(scope())[0]
        assert row["valid_until"] == ts("2026-09-30T00:00:00")
        history = env.cm.execute_read("SELECT evidence,source_memory_id FROM scoped_fact_history").fetchone()
        assert history[1] == mid
        assert json.loads(history[0])["context_evidence"] == context
        assert json.loads(history[0])["source_quote"] == quote
        assert row["provenance"]["evidence"] == context
        event = env.cm.execute_read("SELECT event_type,payload_json FROM domain_outbox").fetchone()
        assert event[0] == "scoped_fact.proposed"
        assert json.loads(event[1])["observation_id"] > 0
        assert json.loads(event[1])["evidence"]["source_memory_id"] == mid
        assert env.cm.execute_read("SELECT COUNT(*) FROM outbox_deliveries").fetchone()[0] == 1
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["scoped_fact_history", "domain_outbox", "outbox_deliveries"])
async def test_proposal_failure_rolls_back_fact_observation_and_outbox(env, table):
    env.cm.executescript(f"""CREATE TRIGGER fail_proposal BEFORE INSERT ON {table}
                             BEGIN SELECT RAISE(ABORT, 'proposal persistence failed'); END;""")
    with pytest.raises(Exception, match="proposal persistence failed"):
        await env.gateway.propose_fact(scope=scope(), subject="林舟", predicate="住在", object="北港")
    for name in ("scoped_facts", "scoped_fact_history", "write_operations", "domain_outbox", "outbox_deliveries"):
        assert env.cm.execute_read(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "active", "rejected", "deleted", "superseded"])
async def test_writer_duplicate_preserves_fact_and_appends_idempotent_observation(env, status):
    old = fact(env, status=status, valid_until=NOW - 1, provenance={"source_quote": "原来的证据"})
    before = env.facts.get(scope(), old)
    mid = memory(env)
    kwargs = dict(scope=scope(), subject="林舟", predicate="住在", object="北港", source_memory_id=mid,
                  provenance={"source_quote": "我住在北港", "context_evidence": "新的独立观察"},
                  idempotency_key="new-observation")
    result = await env.gateway.propose_fact(**kwargs)
    assert await env.gateway.propose_fact(**kwargs) == result
    assert result.locator == old and result.revision == 1 and result.status == status
    assert env.facts.get(scope(), old) == before
    assert env.cm.execute_read("SELECT COUNT(*) FROM scoped_fact_history").fetchone()[0] == 1
    event = env.cm.execute_read("SELECT event_type,payload_json FROM domain_outbox").fetchone()
    assert event[0] == "scoped_fact.observed"
    assert json.loads(event[1])["evidence"]["context_evidence"] == "新的独立观察"
    with pytest.raises(ScopedKnowledgeIdempotencyConflict):
        await env.gateway.propose_fact(**{**kwargs, "object": "南湾"})
    assert env.cm.execute_read("SELECT COUNT(*) FROM scoped_fact_history").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_proposal_rechecks_source_after_entering_writer_transaction(env):
    mid = memory(env, content="今晚参加会议")

    class ChangedSourceCoordinator(Coordinator):
        async def transaction(self, callback, *, actor=None):
            with self.cm.write_transaction() as conn:
                conn.execute("UPDATE memories SET quarantine=1 WHERE id=?", (mid,))
                return callback(conn)

    gateway = ScopedKnowledgeMutationGateway(SimpleNamespace(coordinator=ChangedSourceCoordinator(env.cm)))
    result = await gateway.propose_fact(scope=scope(), subject="73001", predicate="参加", object="会议",
        source_memory_id=mid, provenance={"source_quote": "今晚参加会议", "source_timestamp": NOW,
                                        "resolved_subject": {"account_id": "73001"}})
    row = env.facts.get(scope(), result.locator)
    assert row["status"] == "pending" and row["source_memory_id"] is None
    assert row["valid_until"] is None
    assert row["provenance"]["resolved_subject"] is None
    assert row["provenance"]["temporal_policy"]["needs_review"]


@pytest.mark.asyncio
async def test_tool_does_not_fall_back_when_runtime_writer_rejects(env, monkeypatch):
    class StoppedCoordinator:
        async def transaction(self, callback, *, actor=None):
            raise RuntimeError("writer stopped")

    from tools import fact_proposal
    monkeypatch.setattr(fact_proposal, "_auto_reviewer", lambda: None)
    tool = WaveMemoryProposeFactTool(db=env.db, write_gateway=SimpleNamespace(coordinator=StoppedCoordinator()))
    ctx = SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope())))
    result = await tool.call(ctx, subject="林舟", predicate="住在", object="北港",
                             source_quote="我住在北港", context_evidence="原话")
    assert "writer stopped" in result
    assert env.cm.execute_read("SELECT COUNT(*) FROM scoped_facts").fetchone()[0] == 0


def test_injection_filters_both_paths_time_status_and_private_scope(env):
    good = fact(env, status="active", valid_from=NOW, valid_until=NOW + 1)
    for obj, extra in [("未来城", {"valid_from": NOW + 1}), ("过去城", {"valid_until": NOW}),
                       ("旧城", {"status": "superseded"}), ("待审城", {"status": "pending"})]:
        fact(env, object=obj, **{"status": "active", **extra})
    fact(env, target=scope("bot-maple"), object="另一机器的城", status="active")
    fact(env, target=scope(kind="private", conversation="other-person"), object="私密城", status="active")
    # One-hop joins to the good object's entity must use the same time guard.
    fact(env, subject="北港", predicate="位于", object="未来地区", status="active", valid_from=NOW + 1)
    ctx = SimpleNamespace(scope=scope(), now=NOW, message="林舟", mode="full", config={})
    channel = FactsChannel(db=env.db, facts_decay_rate=0)
    result = asyncio.run(channel.build(ctx))
    assert [item["rowid"] for item in result.items] == [good]
    ctx.now = NOW + 1
    assert not is_effective(env.facts.get(scope(), good), ctx.now)


def test_private_fact_only_in_its_own_chat(env):
    private = scope(kind="private", conversation="person-one")
    fid = fact(env, target=private, status="active")
    channel = FactsChannel(db=env.db)
    for target, expected in [(private, [fid]), (scope(kind="private", conversation="person-two"), []),
                             (scope(), []), (scope("bot-maple", "private", "person-one"), [])]:
        result = asyncio.run(channel.build(SimpleNamespace(scope=target, now=NOW, message="林舟", mode="full", config={})))
        assert [item["rowid"] for item in result.items] == expected


def test_entity_cache_never_uses_another_private_chats_subjects(env):
    target = scope(kind="private", conversation="person-one")
    fact(env, target=target, subject="虚构保密实体", status="active")
    channel = FactsChannel(db=env.db)
    assert "虚构保密实体" in channel._known_entities(target)
    assert "虚构保密实体" not in channel._known_entities(scope())
    assert "虚构保密实体" not in channel._known_entities(scope(kind="private", conversation="person-two"))



def test_review_idempotency_key_cannot_change_actor_or_resolution(env):
    fid = fact(env)
    review(env, fid, action="approve", idempotency_key="same-command")
    with pytest.raises(ScopedKnowledgeIdempotencyConflict):
        review(env, fid, action="approve", actor="auto_review", idempotency_key="same-command")


@pytest.mark.asyncio
async def test_real_fact_routes_return_lifecycle_and_accept_replace(env, monkeypatch):
    from quart import Quart
    from webui.blueprints import facts as routes
    from webui.middleware import auth
    from webui.api_contract import ObjectRefRegistry
    container = SimpleNamespace(db=env.db, write_gateway=env.writer, scoped_knowledge_mutations=env.gateway, password="")
    monkeypatch.setattr(routes, "get_container", lambda: container)
    monkeypatch.setattr(auth, "get_container", lambda: container)
    app = Quart(__name__)
    app.extensions["wave_api_contract"] = {"object_refs": ObjectRefRegistry()}
    app.register_blueprint(routes.facts_bp)
    old = fact(env, status="active")
    new = fact(env, object="南湾", status="conflict")
    expired = fact(env, object="过去城", status="active", valid_until=1)
    client = app.test_client()
    response = await client.get("/api/facts?bot_id=bot-cedar&session_id=demo:group:room-a&visibility=group")
    assert response.status_code == 200
    payload = await response.get_json()
    items = {row["id"]: row for row in payload["items"]}
    assert items[expired]["ineffective_reason"] == "expired"
    assert items[new]["review_actions"] == ["approve", "reject", "coexist", "replace"]
    assert [row["id"] for row in items[new]["conflict_candidates"]] == [old]
    response = await client.post(f"/api/facts/{new}/replace", json={
        "scope": ScopeCodec.to_dict(scope()), "object_ref": items[new]["object_ref"], "revision": 1,
        "supersedes_fact_id": old, "supersedes_revision": 1,
    })
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["item"]["revision"] == 2
    assert payload["item"]["effective"] is True
    assert payload["item"]["conflict_candidates"] == []
    # Old batch approval/rejection remains compatible, and coexist uses the
    # same item envelope without inventing a second review API.
    batch_id = fact(env, object="西岭")
    response = await client.get("/api/facts?bot_id=bot-cedar&session_id=demo:group:room-a&visibility=group")
    batch_row = next(row for row in (await response.get_json())["items"] if row["id"] == batch_id)
    response = await client.post("/api/facts/commands/batch-review", json={
        "scope": ScopeCodec.to_dict(scope()), "action": "coexist",
        "items": [{"id": batch_id, "revision": 1, "object_ref": batch_row["object_ref"]}],
    })
    assert response.status_code == 200
    assert (await response.get_json())["succeeded"] == [{"id": batch_id, "status": "active", "revision": 2}]
