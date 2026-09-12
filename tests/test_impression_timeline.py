from services.impression_timeline import (
    affinity_shift_range,
    append_impression,
    clear_impression,
    current_impression_text,
    injection_lines,
    meaningful_event_anchor,
    normalize_timeline_half_life,
    propose_affinity_shift,
    snapshot_from_relationship,
    timeline_decay_weight,
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


def test_injection_lines_include_decayed_trajectory_summaries():
    metadata = append_impression(
        {},
        "刚进群挺活跃",
        now=1.0,
        event={"before_affinity": 0, "after_affinity": 5, "reason": "直接回复"},
    )
    metadata = append_impression(
        metadata,
        "聊了考研其实挺有想法",
        now=2.0,
        event={"before_affinity": 5, "after_affinity": 12, "reason": "深夜长谈"},
        actor="social_verdict",
    )
    lines = injection_lines(metadata, now=3.0)

    assert any(line.startswith("你对这个人的印象：聊了考研其实挺有想法") for line in lines)
    assert any(line.startswith("印象时间线") for line in lines)
    assert all("好感 5→12" not in line for line in lines)
    assert injection_lines({}, events=[{"kind": "impression", "detail": "只有详情没有摘要", "occurred_at": 1.0}]) == []


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
    assert any(line.startswith("你对这个人的印象：愿意核对事实") for line in lines)
    assert any(line.startswith("印象时间线") for line in lines)
    assert any("愿意核对事实" in line for line in lines[1:])


def test_snapshot_from_relationship_prefers_effective_values():
    snapshot = snapshot_from_relationship({
        "affinity": 8,
        "dimensions": {"trust": 1},
        "values": {"trust": {"effective_value": 9.16}, "depth": {"effective_value": 4}},
    })
    assert snapshot == {"trust": 9.2, "depth": 4.0, "affinity": 8.0}


def test_unsettled_energy_and_threshold_transition():
    from services.impression_timeline import (
        parse_impression_mark,
        should_trigger_affinity_transition,
        unsettled_energy,
    )

    assert parse_impression_mark("无新看法") == ("", 0.0)
    body, impact = parse_impression_mark("文史功底扎实 | impact: 4")
    assert body == "文史功底扎实"
    assert impact == 4.0

    # 未结算能量由 person_unsettled_state 提供；这里直接构造等价 metadata 验证阈值。
    meta = {}
    assert not should_trigger_affinity_transition(meta)
    meta = {"unsettled_traces": [{"impact": 4}, {"impact": 4}]}
    assert unsettled_energy(meta) == 8.0
    assert not should_trigger_affinity_transition(meta)
    meta = {"unsettled_traces": [{"impact": 4}, {"impact": 4}, {"impact": 3}]}
    assert unsettled_energy(meta) == 11.0
    assert should_trigger_affinity_transition(meta)
    assert should_trigger_affinity_transition({}, {"hostility": 12.0})
    assert not should_trigger_affinity_transition({})


def test_history_is_not_truncated_at_twenty():
    events = [{"kind": "impression", "summary": f"印象节点{i:02d}足够长", "detail": f"印象节点{i:02d}足够长", "occurred_at": float(i + 1)} for i in range(25)]
    lines = injection_lines({}, events=events, now=30.0)
    body = "\n".join(lines)
    for i in range(25):
        assert f"印象节点{i:02d}足够长" in body
    assert current_impression_text(list(reversed(events))) == "印象节点24足够长"


def test_affinity_shift_range_rejects_big_jumps():
    bounds = affinity_shift_range({}, dimension="trust")
    assert bounds["min"] == -2.0
    assert bounds["max"] == 2.0
    ok = propose_affinity_shift({}, dimension="trust", requested_delta=1.5)
    assert ok["ok"] is True
    assert ok["delta"] == 1.5
    denied = propose_affinity_shift({}, dimension="trust", requested_delta=6)
    assert denied["ok"] is False
    assert denied["error"] == "affinity_delta_out_of_range"


def test_match_timeline_cue_requires_overlap_and_stays_read_only():
    from services.impression_timeline import match_timeline_cue, timeline_cue_prompt

    events = [
        {"kind": "impression", "summary": "通宵帮排查毕业设计死锁", "detail": "一起看锁等待"},
        {"kind": "message_seen", "summary": "看见一条群友消息"},
        {"kind": "impression", "summary": "刚进群挺活跃"},
    ]
    hit = match_timeline_cue(events, "还记得毕业设计那个死锁吗")
    assert hit is not None
    assert "死锁" in hit["summary"]
    prompt = timeline_cue_prompt(hit)
    assert "印象线索" in prompt
    assert "不是必须回复的指令" in prompt

    assert match_timeline_cue(events, "午饭吃什么") is None
    assert match_timeline_cue(events, "短") is None
    assert match_timeline_cue(events, "今天天气不错啊") is None


def test_timeline_injection_is_full_and_labels_age():
    from services.impression_timeline import impression_timeline_lines

    now = 1_780_000_000.0  # 2026-06-02-ish unix
    may_ts = now - 90 * 86400
    july_ts = now - 3 * 86400
    events = [
        {"id": 11, "kind": "person_fact", "summary": "介绍 健身软件开发", "detail": "完整软件开发详情", "occurred_at": may_ts, "group_id": "g1"},
        {"id": 12, "kind": "person_fact", "summary": "计划切换使用 城3.1", "detail": "计划切换使用 城3.1", "occurred_at": july_ts, "group_id": "g1"},
        {"id": 13, "kind": "person_fact", "summary": "说了 喜欢伊芙", "detail": "说了 喜欢伊芙", "occurred_at": now - 86400, "group_id": "g1"},
        {"id": 14, "kind": "person_fact", "summary": "时间缺失的旧记录", "detail": "不该被当成今天", "occurred_at": "invalid"},
    ]
    lines = impression_timeline_lines({}, events=events, now=now, half_life_days=21)
    body = "\n".join(lines)
    assert "介绍 健身软件开发" in body
    assert "计划切换使用 城3.1" in body
    assert "说了 喜欢伊芙" in body
    assert "完整软件开发详情" not in body
    assert body.index("介绍 健身软件开发") < body.index("计划切换使用 城3.1") < body.index("说了 喜欢伊芙")
    assert "事件#11" in body
    assert "来源群=g1" in body
    assert "时间权重=0.5" in "\n".join(impression_timeline_lines({}, events=[{"id": 1, "summary": "半衰期", "occurred_at": now - 21 * 86400}], now=now))
    may_weight = timeline_decay_weight(events[0], now=now, half_life_days=21)
    july_weight = timeline_decay_weight(events[1], now=now, half_life_days=21)
    assert may_weight is not None and july_weight is not None
    assert may_weight < july_weight
    assert f"时间权重={may_weight:.6g}" in body
    assert "时间未知" in body
    assert "昨天" in body
    assert "3天前" in body
    assert normalize_timeline_half_life(None) == 21.0
    assert normalize_timeline_half_life(True) == 21.0
    assert normalize_timeline_half_life(-3) == 21.0


def test_timeline_injection_keeps_old_fact_with_date_when_query_matches():
    from services.impression_timeline import impression_timeline_lines

    now = 1_780_000_000.0
    events = [
        {"kind": "person_fact", "summary": "介绍 健身软件开发", "detail": "介绍 健身软件开发", "occurred_at": now - 90 * 86400},
        {"kind": "person_fact", "summary": "计划切换使用 城3.1", "detail": "计划切换使用 城3.1", "occurred_at": now - 3 * 86400},
    ]
    lines = impression_timeline_lines({}, events=events, now=now, query="软件开发")
    body = "\n".join(lines)
    assert "介绍 健身软件开发" in body
    assert "计划切换使用 城3.1" in body
    assert "2025-" in body or "2026-" in body

