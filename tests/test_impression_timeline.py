from services.impression_timeline import (
    append_impression,
    append_ledger_entry,
    clear_impression,
    injection_lines,
    ledger_entries,
    meaningful_event_anchor,
    relationship_context,
    snapshot_from_relationship,
)


def test_append_impression_keeps_history_and_latest_pointer():
    first = append_impression({}, "说话谨慎", now=1000.0)
    second = append_impression(first, "愿意核对事实", now=2000.0)

    assert second["impression"] == "愿意核对事实"
    assert second["impression_history"][0]["text"] == "说话谨慎"
    assert [item["text"] for item in second["impression_history"]] == ["说话谨慎"]


def test_clear_impression_archives_current_pointer():
    metadata = append_impression({}, "说话谨慎", now=1000.0)
    cleared = clear_impression(metadata, reason="印象过期需要重看", now=2000.0)

    assert cleared["impression"] == ""
    assert cleared["impression_cleared_reason"] == "印象过期需要重看"
    assert cleared["impression_history"][-1]["text"] == "说话谨慎"


def test_injection_lines_include_bounded_trajectory():
    metadata = append_impression({}, "旧印象", now=1.0)
    metadata = append_impression(metadata, "新印象", now=2.0)
    lines = injection_lines(metadata)

    assert lines[0] == "你对这个人的印象：新印象"
    assert "印象演变：旧印象 → 新印象" in lines[1]


def test_append_impression_stores_snapshot_and_event_on_archive():
    first = append_impression({}, "说话谨慎", now=1000.0, snapshot={"trust": 6, "affinity": 12})
    second = append_impression(
        first,
        "愿意核对事实",
        now=2000.0,
        snapshot={"trust": 18, "affinity": 20},
        event={"event_type": "deep_talk", "reason": "深夜长谈", "dimension": "depth", "delta": 4.2},
    )

    archived = second["impression_history"][0]
    assert archived["snapshot"]["affinity"] == 20
    assert archived["event"]["event_type"] == "deep_talk"
    assert second["impression_event"]["reason"] == "深夜长谈"


def test_meaningful_event_anchor_skips_message_seen_noise():
    anchor = meaningful_event_anchor([
        {"event_type": "message_seen", "reason": "看见一条群友消息", "delta": 0.05},
        {"event_type": "deep_talk", "reason": "深夜陪聊", "dimension": "depth", "delta": 3.1, "event_id": 9},
    ])
    assert anchor == {
        "event_type": "deep_talk",
        "reason": "深夜陪聊",
        "dimension": "depth",
        "delta": 3.1,
        "event_id": 9,
    }


def test_injection_lines_prefer_stored_event_then_history_anchor():
    metadata = append_impression(
        {},
        "愿意核对事实",
        now=2.0,
        event={"event_type": "bot_praised", "reason": "被感谢"},
    )
    lines = injection_lines(metadata, history=[{"event_type": "joke", "reason": "接梗"}])
    assert lines[0] == "你对这个人的印象：愿意核对事实"
    assert lines[1] == "最近关系线索：bot_praised：被感谢"


def test_relationship_context_reads_repository_snapshot():
    class _Repo:
        def get_state(self, scope, subject_principal_id=None, limit=25, offset=0):
            return {
                "relationship": {
                    "affinity": 12,
                    "values": {"trust": {"effective_value": 6.44}},
                    "dimensions": {"fun": 2},
                },
                "relationship_history": {
                    "items": [
                        {"event_type": "message_seen", "reason": "看见一条群友消息"},
                        {"event_type": "joke", "reason": "接探活梗", "dimension": "fun", "delta": 1.2},
                    ]
                },
            }

    snapshot, event = relationship_context(_Repo(), object())
    assert snapshot["affinity"] == 12
    assert snapshot["trust"] == 6.4
    assert event["event_type"] == "joke"


def test_snapshot_from_relationship_prefers_effective_values():
    snapshot = snapshot_from_relationship({
        "affinity": 8,
        "dimensions": {"trust": 1},
        "values": {"trust": {"effective_value": 9.16}, "depth": {"effective_value": 4}},
    })
    assert snapshot == {"trust": 9.2, "depth": 4.0, "affinity": 8.0}


def test_ledger_skips_passby_and_keeps_scored_rows():
    first = append_ledger_entry({}, event_type="message_seen", dimension="familiarity", delta=0.05, reason="看见一条群友消息", at=1)
    assert first.get("impression_ledger") in (None, [])
    second = append_ledger_entry(first, event_type="bot_praised", dimension="trust", delta=3, reason="正面评价 bot", at=2, event_id=9)
    third = append_ledger_entry(second, event_type="direct_reply", dimension="trust", delta=1.5, reason="回复 bot 消息", at=3, event_id=10)
    rows = ledger_entries(third, limit=5)
    assert [item["event_type"] for item in rows] == ["bot_praised", "direct_reply"]
    lines = injection_lines(third)
    assert any(line.startswith("最近关系账本：") for line in lines)
