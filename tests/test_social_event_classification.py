from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services import lifecycle
from services.lifecycle import AffinityEngine


def _group_scope() -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:u1",
    )


def test_keyword_classifier_removed_from_formal_path():
    assert not hasattr(lifecycle, "classify_social_event")


def test_process_message_only_marks_touch_and_skips_keyword_deltas():
    class _Conn:
        def execute(self, *args, **kwargs):
            raise AssertionError("process_message must not query last_seen or classify events")

    engine = AffinityEngine(
        db=SimpleNamespace(conn=_Conn()),
        bot_qq_id="bot-qq",
        bot_db_id="bot-alpha",
    )
    assert engine.process_message(content="你真厉害", is_at_bot=True, scope=_group_scope()) is True
    buf = engine._buffer[("u1", "g1")]
    assert buf["_touched"] == 1.0
    assert "trust" not in buf
    assert "hostility" not in buf
    assert "familiarity" not in buf
    assert "fun" not in buf

