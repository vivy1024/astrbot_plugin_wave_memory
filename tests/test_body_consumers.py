"""身体两个派生输出（proactive_drive、reply_length_factor）都有真实的消费方。"""

from __future__ import annotations

from domain import body_policy as bp
from services.reply_decision import ReplyDecisionService, ReplySignals


def _decision_service(clock_now=1_800_000_000.0):
    return ReplyDecisionService(
        config={
            "mode": "enforce",
            "default_mode": "enforce",
            "score_threshold": 55.0,
            "bots": {"yushu": {"mode": "enforce", "score_threshold": 55.0}},
        },
        clock=lambda: clock_now,
    )


def test_proactive_drive_nudges_reply_decision_without_overpowering_topics():
    svc = _decision_service()
    base = dict(
        bot_id="yushu", session_id="qq:group:g1", visibility="group",
        message="随便聊聊", direct=False, platform_allowed=True, proactive_enabled=True,
        topic_match=0.8, followup_eligible=True,
    )
    high = svc.decide(ReplySignals(**base, body_drive=0.45))
    neutral = svc.decide(ReplySignals(**base, body_drive=0.15))
    low = svc.decide(ReplySignals(**base, body_drive=0.0))
    off = svc.decide(ReplySignals(**base, body_drive=None))

    assert high["factors"]["body_drive"] > 0 and "body_drive_up" in high["reasons"]
    assert low["factors"]["body_drive"] < 0 and "body_drive_down" in low["reasons"]
    assert "body_drive" not in neutral["factors"] and "body_drive" not in off["factors"]
    assert low["score"] < neutral["score"] < high["score"]
    # 夹在 ±15，不会盖过话题与硬边界
    extreme = svc.decide(ReplySignals(**base, body_drive=1.0, mood_influence=2.0))
    assert extreme["factors"]["body_drive"] <= 15.0


def test_reply_length_factor_emits_terse_or_chatty_without_duplicating_mood_flags():
    # 多巴胺/血清素/催产素都略高、但没触发 wired 时，给出顺口多聊两句的边沿提示
    chatty_state = {**bp.default_state(14), "dopamine": 80.0, "serotonin": 85.0, "oxytocin": 75.0}
    r_chat = bp.read(chatty_state, hour=14)
    assert r_chat.reply_length_factor >= 1.25
    assert r_chat.flags["chatty"] is True and r_chat.flags["wired"] is False
    lines, _ = bp.edge_notes(r_chat.flags, {}, now=100.0)
    assert any("多聊" in line for line in lines)

    # 已经触发 wired 时，wired 本身就说「话容易多」，不重复出 chatty
    wired_state = {**bp.default_state(14), "dopamine": 95.0, "serotonin": 85.0}
    r_wired = bp.read(wired_state, hour=14)
    assert r_wired.flags["wired"] is True and r_wired.flags["chatty"] is False

    # 低血清素 + 高褪黑素还没过 0.6 阈值时，length 被拉低，给出短回复提示
    terse_state = {**bp.default_state(14), "serotonin": 43.0, "melatonin": 58.0, "cortisol": 60.0}
    r_terse = bp.read(terse_state, hour=14)
    assert r_terse.reply_length_factor <= 0.85
    assert r_terse.flags["terse"] is True
    t_lines, _ = bp.edge_notes(r_terse.flags, {}, now=100.0)
    assert any("短" in line for line in t_lines)
