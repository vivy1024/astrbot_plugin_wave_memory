from __future__ import annotations

from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from webui.blueprints import people as people_bp_module


class _Repo:
    def list_legacy_relationship_audit_summary(self, scope, **_kwargs):
        assert scope.subject_principal_id == "羽书:user:u1"
        return {
            "available": True,
            "total": 3,
            "by_type": [{"event_type": "direct_reply", "count": 3}],
            "recent": [
                {
                    "event_type": "direct_reply",
                    "dimension": "familiarity",
                    "delta": 0.5,
                    "reason": "看见一条群友消息",
                }
            ],
        }


def test_historical_audit_summary_helper_is_readonly_side_channel():
    scope = RuntimeScope(
        "yushu",
        "group",
        SessionRef("羽书:group:398291136", "羽书", "group", "398291136"),
    )
    summary = people_bp_module._historical_audit_summary_for_subject(
        _Repo(),
        scope,
        "羽书:user:u1",
    )
    assert summary["available"] is True
    assert summary["total"] == 3
    assert summary["readonly"] is True
    assert summary["affects_affinity"] is False
    assert summary["source_table"] == "scoped_soul_relationship_legacy_events"


def test_historical_audit_summary_helper_falls_back_to_formal_relationship_history():
    class _Repo:
        def list_legacy_relationship_audit_summary(self, scope, **_kwargs):
            return {"available": False, "total": 0, "by_type": [], "recent": []}

        def list_relationship_history(self, scope, **_kwargs):
            return {
                "total": 1,
                "items": [{
                    "id": "relationship-event:1",
                    "event_type": "direct_reply",
                    "kind": "automatic",
                    "dimension": "trust",
                    "delta": 2,
                    "reason": "真实回复",
                    "timestamp": 123,
                }],
            }

    scope = RuntimeScope(
        "yushu",
        "group",
        SessionRef("羽书:group:398291136", "羽书", "group", "398291136"),
    )
    summary = people_bp_module._historical_audit_summary_for_subject(
        _Repo(),
        scope,
        "羽书:user:u1",
    )
    assert summary["available"] is True
    assert summary["source_table"] == "scoped_soul_relationship_events"
    assert summary["recent"][0]["reason"] == "真实回复"


def test_historical_audit_summary_helper_fail_closed_without_repo_api():
    scope = RuntimeScope(
        "yushu",
        "group",
        SessionRef("羽书:group:398291136", "羽书", "group", "398291136"),
    )
    summary = people_bp_module._historical_audit_summary_for_subject(
        SimpleNamespace(),
        scope,
        "羽书:user:u1",
    )
    assert summary["available"] is False
    assert summary["total"] == 0
    assert summary["affects_affinity"] is False


def test_relationship_alias_rows_are_readonly_and_do_not_override_current_session():
    class _Repo:
        def list_relationships(self, scope):
            session_id = scope.session.id
            if session_id.endswith(":g1") and session_id.startswith("qq:"):
                return [{
                    "subject_principal_id": "qq:user:u1",
                    "affinity": 12,
                    "revision": 3,
                    "values": {"trust": {"effective_value": 12}},
                    "calibration": {"available": True, "reason_code": None},
                }]
            return [{
                "subject_principal_id": "qq2:user:u2",
                "affinity": 8,
                "revision": 1,
                "values": {"trust": {"effective_value": 8}},
                "calibration": {"available": True, "reason_code": None},
            }]

    scope = RuntimeScope(
        "yushu",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
    )
    rows = people_bp_module._relationship_rows_for_scope(_Repo(), scope, ["qq:group:g1", "qq2:group:g1"])
    assert rows["qq:user:u1"]["calibration"]["available"] is True
    assert rows["qq:user:u2"]["calibration"] == {"available": False, "reason_code": "alias_session_readonly"}
    assert rows["qq:user:u2"]["affinity"] == 8


def test_table_rows_can_filter_user_profiles_by_bot_and_group():
    class _Cursor:
        description = (("user_id",), ("bot_id",), ("group_id",))

        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self):
            self.sql = ""
            self.params = None

        def execute(self, sql, params=()):
            self.sql = sql
            self.params = params
            return _Cursor([("u1", "yushu", "398291136")])

    conn = _Conn()
    rows = people_bp_module._table_rows(
        conn,
        "user_profiles",
        "bot_id=? AND group_id=?",
        ("yushu", "398291136"),
    )
    assert "WHERE bot_id=? AND group_id=?" in conn.sql
    assert conn.params == ("yushu", "398291136")
    assert rows == [{"user_id": "u1", "bot_id": "yushu", "group_id": "398291136"}]


def test_formal_evidence_summaries_extracts_historical_audit_summary():
    texts = people_bp_module._formal_evidence_summaries(
        {
            "evidence": [
                {"relationship_event_id": 1},
                {
                    "kind": "historical_audit_summary",
                    "summary": "历史审计事件 12 条；类型：direct_reply×12",
                    "affects_affinity": False,
                },
            ]
        }
    )
    assert texts == ["历史审计事件 12 条；类型：direct_reply×12"]
    assert people_bp_module._formal_evidence_summaries({"evidence": []}) == []
    assert people_bp_module._formal_evidence_summaries(None) == []
