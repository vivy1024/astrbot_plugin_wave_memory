from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.outbox_repo import OutboxEvent
from services.belief_engine import BeliefEngine
from services.belief_lifecycle import BeliefLifecycleService


CONTENT = "小明对照顾动物一直很有责任感"


def group_scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
    )


def add_message(db: WaveMemoryDB, scope: RuntimeScope, content: str) -> int:
    return db.add_memory(
        "g1",
        content,
        sender_id="u1",
        sender_name="小明",
        timestamp=1000 + db.get_memory_count(),
        scope=scope,
    )


def prepare_relationship(db: WaveMemoryDB, scope: RuntimeScope, *, trust: float, hostility: float) -> None:
    subject_scope = RuntimeScope(
        bot_id=scope.bot_id,
        visibility=scope.visibility,
        session=scope.session,
        subject_principal_id="qq:user:u1",
    )
    db.soul_repository.upsert_relationship(
        subject_scope,
        subject_principal_id="qq:user:u1",
        affinity=50,
        dimensions={"familiarity": 50, "trust": trust, "fun": 20, "hostility": hostility, "depth": 20},
    )


def add_tag(db: WaveMemoryDB, scope: RuntimeScope, memory_id: int) -> None:
    tag_id = db.upsert_scoped_tag(scope, name=f"evidence-{memory_id}", tag_type="keyword", confidence=0.9)
    db.link_scoped_memory_tag(scope, memory_id=memory_id, tag_id=tag_id)


def record_window(
    db: WaveMemoryDB,
    scope: RuntimeScope,
    belief_id: int,
    memory_id: int,
    *,
    polarity: str = "support",
) -> None:
    db.record_scoped_belief_observation(
        scope,
        belief_id=belief_id,
        window_key=f"window-{memory_id}",
        polarity=polarity,
        memory_ids=[memory_id],
        participants=["u1"],
        window_started_at=1000.0 + memory_id,
        window_ended_at=1000.0 + memory_id,
        observed_at=1000.0 + memory_id,
    )


def _revision(row: dict) -> int:
    return max(1, int(float(row.get("updated_at") or row.get("created_at") or 1) * 1000))


def attach_approved_facts(db: WaveMemoryDB, scope: RuntimeScope, belief_id: int, memory_ids: list[int]) -> list[int]:
    fact_ids: list[int] = []
    for index, memory_id in enumerate(list(memory_ids)[:2]):
        fact_ids.append(db.scoped_knowledge.upsert_scoped_fact(
            scope,
            subject="小明",
            predicate=f"照顾{index}",
            object="动物",
            status="approved",
            source_memory_id=memory_id,
        ))
    if len(fact_ids) == 1:
        fact_ids.append(db.scoped_knowledge.upsert_scoped_fact(
            scope,
            subject="小明",
            predicate="复证",
            object="动物",
            status="approved",
            source_memory_id=memory_ids[0],
        ))
    row = db.get_scoped_belief(scope, belief_id)
    provenance = dict(row.get("provenance") or {})
    provenance["source_fact_ids"] = fact_ids
    db.upsert_scoped_belief(
        scope,
        belief_key=row["belief_key"],
        content=row["content"],
        belief_type=row["belief_type"],
        strength=float(row.get("strength") or 0.0),
        status=row["status"],
        source_memory_id=row.get("source_memory_id"),
        provenance=provenance,
    )
    return fact_ids


def seed_belief(
    db: WaveMemoryDB,
    scope: RuntimeScope,
    *,
    key: str,
    memory_ids: list[int],
    status: str = "pending",
    gating: dict | None = None,
    candidate: dict | None = None,
    tagged: bool = True,
) -> int:
    provenance: dict = {"anchor_sentence": CONTENT}
    if gating:
        provenance["gating"] = gating
    if candidate:
        provenance["candidate"] = candidate
    belief_id = db.upsert_scoped_belief(
        scope,
        belief_key=key,
        content=CONTENT,
        belief_type="person_judgment",
        strength=0.0,
        status=status,
        source_memory_id=memory_ids[0],
        provenance=provenance,
    )
    for memory_id in memory_ids:
        record_window(db, scope, belief_id, memory_id)
        if tagged:
            add_tag(db, scope, memory_id)
    return belief_id


def test_high_trust_requires_two_windows_before_manual_activation(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "belief-gating.db"), dimension=4)
    try:
        scope = group_scope()
        prepare_relationship(db, scope, trust=80, hostility=10)
        first_id = add_message(db, scope, "小明主动照顾了流浪猫。")
        second_id = add_message(db, scope, "小明又安排了流浪猫的领养。")
        add_tag(db, scope, first_id)
        add_tag(db, scope, second_id)
        engine = BeliefEngine(db, None, bot_id="bot-alpha")
        belief_id = seed_belief(db, scope, key="person:care", memory_ids=[first_id], tagged=False)

        first = engine.refresh_evidence_after_tags(scope, belief_id)
        assert first["status"] == "pending"
        assert db.list_scoped_beliefs(scope)[0]["status"] == "pending"
        assert not first["provenance"]["activation_eligible"]

        record_window(db, scope, belief_id, second_id)
        second = engine.refresh_evidence_after_tags(scope, belief_id)
        assert second["status"] == "pending"
        assert second["provenance"]["activation_eligible"]
        try:
            BeliefLifecycleService(db.scoped_knowledge).transition(scope, belief_id, "approve")
            assert False, "should fail without approved facts"
        except ValueError as exc:
            assert "belief_facts_required" in str(exc)
        attach_approved_facts(db, scope, belief_id, [first_id, second_id])
        approved = BeliefLifecycleService(db.scoped_knowledge).transition(scope, belief_id, "approve")
        assert approved["status"] == "active"
    finally:
        db.close()


def test_pending_reinforce_candidate_is_idempotent_and_guarded_merge(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "candidate-merge.db"), dimension=4)
    try:
        scope = group_scope()
        prepare_relationship(db, scope, trust=80, hostility=10)
        first_id = add_message(db, scope, "小明照顾了流浪猫。")
        second_id = add_message(db, scope, "小明安排了领养。")
        third_id = add_message(db, scope, "小明继续跟进领养。")
        engine = BeliefEngine(db, None, bot_id="bot-alpha")
        target_id = seed_belief(
            db,
            scope,
            key="person:care",
            memory_ids=[first_id, second_id],
            gating={"decision": "direct", "reason_code": "relationship_direct"},
        )
        engine.refresh_evidence_after_tags(scope, target_id)
        attach_approved_facts(db, scope, target_id, [first_id, second_id])
        BeliefLifecycleService(db.scoped_knowledge).transition(scope, target_id, "approve")
        assert db.get_scoped_belief(scope, target_id)["status"] == "active"

        target = db.get_scoped_belief(scope, target_id)
        candidate_id = seed_belief(
            db,
            scope,
            key="person:care-reinforce",
            memory_ids=[third_id],
            candidate={
                "relation": "reinforce",
                "target_belief_id": target_id,
                "target_revision_at_capture": _revision(target),
            },
        )
        refreshed_candidate = engine.refresh_evidence_after_tags(scope, candidate_id)
        assert refreshed_candidate["status"] == "pending"
        assert candidate_id != target_id
        assert len(db.list_scoped_beliefs(scope)) == 2

        record_window(db, scope, candidate_id, third_id)
        engine.refresh_evidence_after_tags(scope, candidate_id)
        assert len(db.list_scoped_belief_observations(scope, belief_id=candidate_id)) == 1

        try:
            BeliefLifecycleService(db.scoped_knowledge).transition(scope, candidate_id, "approve")
        except ValueError as exc:
            assert str(exc) == "belief_evidence_incomplete"
        else:
            raise AssertionError("candidate without evidence-v1 support must not be approved")
        assert db.get_scoped_belief(scope, target_id)["status"] == "active"

        fourth_id = add_message(db, scope, "小明继续负责后续照顾。")
        add_tag(db, scope, fourth_id)
        record_window(db, scope, candidate_id, fourth_id)
        engine.refresh_evidence_after_tags(scope, candidate_id)
        merged = BeliefLifecycleService(db.scoped_knowledge).transition(scope, candidate_id, "approve")
        assert merged["resolution"] == "merged"
        assert merged["target_id"] == target_id
        assert db.get_scoped_belief(scope, candidate_id)["status"] == "archived"
        assert db.get_scoped_belief(scope, target_id)["status"] == "active"
        assert len(db.list_scoped_belief_observations(scope, belief_id=target_id)) == 4

        fifth_id = add_message(db, scope, "小明继续负责后续照顾并完成了复盘。")
        add_tag(db, scope, fifth_id)
        record_window(db, scope, target_id, fifth_id)
        active_rows = [row for row in db.list_scoped_beliefs(scope) if row["status"] == "active"]
        assert [row["id"] for row in active_rows] == [target_id]
    finally:
        db.close()


def test_low_trust_candidate_is_quarantined_without_touching_active_target(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "quarantine.db"), dimension=4)
    try:
        scope = group_scope()
        prepare_relationship(db, scope, trust=80, hostility=10)
        first_id = add_message(db, scope, "小明持续照顾动物，并在多次后续安排中体现了稳定而明确的责任感。")
        engine = BeliefEngine(db, None, bot_id="bot-alpha")
        target_id = seed_belief(db, scope, key="person:care", memory_ids=[first_id])
        engine.refresh_evidence_after_tags(scope, target_id)
        second_id = add_message(db, scope, "小明提出了新的看法。")
        prepare_relationship(db, scope, trust=20, hostility=10)
        candidate_id = seed_belief(
            db,
            scope,
            key="person:care-low-trust",
            memory_ids=[second_id],
            status="quarantined",
            gating={"decision": "quarantine", "reason_code": "relationship_low_trust"},
            candidate={"relation": "reinforce", "target_belief_id": target_id},
        )
        result = engine.refresh_evidence_after_tags(scope, candidate_id)
        assert result["status"] == "quarantined"
        assert db.get_scoped_belief(scope, target_id)["status"] == "pending"
        assert db.get_scoped_belief(scope, candidate_id)["status"] == "quarantined"
        assert db.get_scoped_belief(scope, candidate_id)["provenance"]["gating"]["reason_code"] == "relationship_low_trust"
    finally:
        db.close()


def test_refresh_after_tags_promotes_direct_pending_and_keeps_quarantine(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "belief-tag-refresh.db"), dimension=4)
    try:
        scope = group_scope()
        prepare_relationship(db, scope, trust=80, hostility=10)
        first_id = add_message(db, scope, "小明主动照顾了流浪猫。")
        second_id = add_message(db, scope, "小明又安排了流浪猫的领养。")
        engine = BeliefEngine(db, None, bot_id="bot-alpha")
        belief_id = seed_belief(
            db,
            scope,
            key="person:care",
            memory_ids=[first_id, second_id],
            gating={"decision": "direct", "reason_code": "relationship_direct"},
            tagged=False,
        )
        pending = engine.refresh_evidence_after_tags(scope, belief_id)
        assert pending["status"] == "pending"
        assert pending["provenance"]["tag_chain_status"] == "empty"
        assert db.list_scoped_belief_ids_citing_memory(scope, first_id) == [belief_id]
        assert db.list_scoped_belief_ids_citing_memory(scope, second_id) == [belief_id]

        add_tag(db, scope, first_id)
        first_refresh = engine.refresh_evidence_after_tags(scope, belief_id)
        assert first_refresh["status"] == "pending"
        assert first_refresh["provenance"]["tag_chain_status"] == "empty"

        add_tag(db, scope, second_id)
        second_refresh = engine.refresh_evidence_after_tags(scope, belief_id)
        assert second_refresh["status"] == "pending"
        assert second_refresh["provenance"]["tag_chain_status"] == "complete"
        assert db.get_scoped_belief(scope, belief_id)["status"] == "pending"
        attach_approved_facts(db, scope, belief_id, [first_id, second_id])
        approved = BeliefLifecycleService(db.scoped_knowledge).transition(scope, belief_id, "approve")
        assert approved["status"] == "active"

        hostile_id = add_message(db, scope, "小明继续跟进领养。")
        prepare_relationship(db, scope, trust=20, hostility=10)
        candidate_id = seed_belief(
            db,
            scope,
            key="person:care-hostile",
            memory_ids=[hostile_id],
            status="quarantined",
            gating={"decision": "quarantine", "reason_code": "relationship_low_trust"},
            tagged=False,
        )
        add_tag(db, scope, hostile_id)
        quarantined = engine.refresh_evidence_after_tags(scope, candidate_id)
        assert quarantined["status"] == "quarantined"
    finally:
        db.close()


def _bind_tag_refresh_methods(host):
    source = Path(__file__).resolve().parents[1] / "main.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    class_node = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "WaveMemoryPlugin")
    wanted = {
        "_on_memory_projection_refresh",
        "_schedule_belief_tag_refresh",
        "_run_belief_tag_refresh",
    }
    namespace = {
        "asyncio": asyncio,
        "logger": types.SimpleNamespace(debug=lambda *args, **kwargs: None),
        "RuntimeScope": RuntimeScope,
    }
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            extracted = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(extracted)
            exec(compile(extracted, str(source), "exec"), namespace)
            setattr(host, node.name, types.MethodType(namespace[node.name], host))
    return host


class _TagRefreshHost:
    def __init__(self, db: WaveMemoryDB, engine: BeliefEngine, delay: float = 0.05):
        self.db = db
        self.belief_engine = engine
        self.pair_sim_service = types.SimpleNamespace(cleared=0, clear_cache=lambda: setattr(self.pair_sim_service, "cleared", self.pair_sim_service.cleared + 1))
        self._belief_tag_refresh_delay_seconds = delay
        self._pending_belief_tag_refresh = {}
        self._spawn = lambda coro, *args, **kwargs: asyncio.create_task(coro)
        _bind_tag_refresh_methods(self)


def _tag_event(scope: RuntimeScope, memory_id: int, event_type: str = "memory.tags_applied") -> OutboxEvent:
    return OutboxEvent(
        event_id=f"event-{event_type}-{memory_id}",
        operation_id="operation-1",
        write_sequence=1,
        aggregate_kind="memory",
        aggregate_id=str(memory_id),
        aggregate_version=2,
        event_type=event_type,
        payload_version=1,
        payload={"memory_id": memory_id, "tag_ids": [1], "scope": scope.to_dict()},
        consumer_name="runtime_refresh",
        attempt=1,
    )


def test_tag_applied_event_debounces_and_promotes_pending_belief(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "belief-tag-event.db"), dimension=4)
    try:
        scope = group_scope()
        prepare_relationship(db, scope, trust=80, hostility=10)
        first_id = add_message(db, scope, "小明主动照顾了流浪猫。")
        second_id = add_message(db, scope, "小明又安排了流浪猫的领养。")
        engine = BeliefEngine(db, None, bot_id="bot-alpha")
        belief_id = seed_belief(
            db,
            scope,
            key="person:care",
            memory_ids=[first_id, second_id],
            gating={"decision": "direct", "reason_code": "relationship_direct"},
            tagged=False,
        )
        engine.refresh_evidence_after_tags(scope, belief_id)
        belief = db.get_scoped_belief(scope, belief_id)
        assert belief["status"] == "pending"
        add_tag(db, scope, first_id)
        add_tag(db, scope, second_id)

        async def _run():
            host = _TagRefreshHost(db, engine, delay=0.05)
            await host._on_memory_projection_refresh(_tag_event(scope, first_id))
            first_task = next(iter(host._pending_belief_tag_refresh.values()))
            await host._on_memory_projection_refresh(_tag_event(scope, second_id))
            remaining = [task for task in host._pending_belief_tag_refresh.values() if task is not first_task]
            assert remaining == list(host._pending_belief_tag_refresh.values())
            assert len(remaining) == 1
            await remaining[0]
            await asyncio.gather(first_task, return_exceptions=True)
            assert first_task.cancelled() or first_task.done()
            return host

        host = asyncio.run(_run())
        refreshed = db.get_scoped_belief(scope, belief["id"])
        assert refreshed["status"] == "pending"
        assert refreshed["provenance"]["tag_chain_status"] == "complete"
        assert host.pair_sim_service.cleared >= 1
    finally:
        db.close()
