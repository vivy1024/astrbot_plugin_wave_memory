from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.proactive_policy import evaluate_proactive_policy


def make_scope(subject="qq:user:u1"):
    return RuntimeScope(
        bot_id="yushu",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id=subject,
    )


def test_formal_repository_timeline_audit_uses_group_scope_and_evidence(tmp_path):
    manager = ConnectionManager(str(tmp_path / "proactive-audit.db"))
    try:
        ensure_scoped_soul_schema(manager)
        repo = ScopedSoulRepository(manager)
        scope = make_scope()
        policy = evaluate_proactive_policy(
            [
                __import__("services.belief_gating", fromlist=["RelationshipSnapshot"]).RelationshipSnapshot(
                    "qq:user:u1",
                    {"familiarity": 40, "trust": 70, "fun": 40, "hostility": 5, "depth": 55},
                    revision=7,
                )
            ],
            concern_score=0.4,
            is_interesting=True,
        )
        with manager.write_transaction() as connection:
            event_id = repo.add_timeline_event(
                RuntimeScope(
                    bot_id=scope.bot_id,
                    visibility=scope.visibility,
                    session=scope.session,
                    subject_principal_id=None,
                ),
                event_summary="主动补充一个公开话题",
                emotional_weight=policy["trigger_weight"],
                event_type="proactive.interjection",
                evidence=[
                    {"kind": "proactive_policy", "policy_version": policy["policy_version"]},
                    {"kind": "relationship_snapshot", "subject_principal_id": "qq:user:u1", "revision": 7},
                    {"kind": "source_memory", "memory_id": 12, "timestamp": 100.0, "role": "primary"},
                ],
                connection=connection,
            )
        assert event_id > 0
        row = manager.execute_read(
            "SELECT subject_principal_id, event_type, emotional_weight, evidence FROM scoped_soul_timeline WHERE id=?",
            (event_id,),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "proactive.interjection"
        assert row[2] == policy["trigger_weight"]
        assert '"policy_version":"relationship-proactive-v1"' in row[3]
        assert '"revision":7' in row[3]
    finally:
        manager.close()
