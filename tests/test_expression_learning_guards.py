"""表达学习的约束：人设检查、可配门槛、待审过期、按人限流、防复读落盘。"""

from __future__ import annotations

import sqlite3

import pytest

from services.expression_policy import ExpressionPolicy, persona_consistent
from services.few_shot.service import FewShotService
from services.reply_feedback import ReplyFeedbackService
from tests.test_reply_feedback_learning import add_message, scope, setup_store


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    setup_store(connection)
    yield connection
    connection.close()


def _policy(conn, *, now=100.0, **kwargs):
    clock = {"now": now}
    feedback = ReplyFeedbackService(conn, enabled=True, now=lambda: clock["now"])
    policy = ExpressionPolicy(conn, enabled=True, feedback_service=feedback, now=lambda: clock["now"], **kwargs)
    feedback.expression_policy = policy
    return feedback, policy, clock


def _observe(feedback, ident, text, *, speaker="visitor-a", content=None, situation="encourage", conn=None):
    add_message(conn, ident, text, speaker=speaker)
    feedback.record_bookkeeping(scope(), {"expressions": [{
        "source_memory_id": ident, "speaker_id": speaker, "content": content or text,
        "situation": situation, "approved": True,
    }]}, source_message_ids=(ident,))


def test_persona_check_blocks_aggressive_and_configured_words():
    assert persona_consistent("慢慢来，一步一步走") is True
    assert persona_consistent("关你屁事") is False
    assert persona_consistent("直接怼回去") is False
    assert persona_consistent("本座说了算", ["本座"]) is False


def test_auto_review_leaves_persona_inconsistent_expression_pending(conn):
    feedback, policy, _ = _policy(conn, persona_blocklist=["主人"])
    _observe(feedback, 10, "这事你懂个锤子", speaker="visitor-a", content="你懂个锤子", conn=conn)
    _observe(feedback, 11, "你懂个锤子啊", speaker="visitor-b", content="你懂个锤子", conn=conn)
    _observe(feedback, 12, "关你屁事真的", speaker="visitor-a", content="关你屁事", conn=conn)
    _observe(feedback, 13, "关你屁事吧", speaker="visitor-b", content="关你屁事", conn=conn)
    stats = policy.auto_review_qualified_all()
    statuses = dict(conn.execute("SELECT content, status FROM reply_expression_candidates").fetchall())
    assert statuses["你懂个锤子"] == "approved"
    assert statuses["关你屁事"] == "pending", "攻击性话术不自动转正，留给人工"
    assert stats["persona_rejected"] == 1


def test_min_occurrence_is_configurable(conn):
    feedback, policy, _ = _policy(conn, auto_min_occurrence=2)
    _observe(feedback, 20, "慢慢来嘛", content="慢慢来嘛", conn=conn)
    _observe(feedback, 21, "慢慢来嘛真的", content="慢慢来嘛", conn=conn)
    stats = policy.auto_review_qualified_all()
    assert stats["approved"] == 1
    assert ExpressionPolicy(conn, auto_min_occurrence=0).auto_min_occurrence == 2, "下限 2：一次性说法不自动转正"


def test_stale_unqualified_pending_expires_but_fresh_or_qualified_stays(conn):
    feedback, policy, clock = _policy(conn, now=1000.0, pending_ttl_days=30)
    _observe(feedback, 30, "冲冲冲别怕", content="冲冲冲别怕", conn=conn)
    clock["now"] = 1000.0 + 31 * 86400
    _observe(feedback, 31, "稳住别慌", content="稳住别慌", conn=conn)
    stats = policy.auto_review_qualified_all()
    rows = dict(conn.execute("SELECT content, status FROM reply_expression_candidates").fetchall())
    assert rows["冲冲冲别怕"] == "rejected" and rows["稳住别慌"] == "pending"
    assert stats["expired"] == 1
    note = conn.execute("SELECT review_note FROM reply_expression_candidates WHERE content='冲冲冲别怕'").fetchone()[0]
    assert note.startswith("auto:")
    assert ExpressionPolicy(conn, enabled=True, pending_ttl_days=0).expire_stale_pending() == 0, "0 表示不过期"


def test_recall_limits_expressions_per_speaker(conn):
    feedback, policy, _ = _policy(conn, auto_min_occurrence=2, max_per_speaker=1)
    for base, text in ((40, "别怕慢慢来"), (42, "撑住别放弃")):
        _observe(feedback, base, text, speaker="visitor-a", conn=conn)
        _observe(feedback, base + 1, text + "啊", speaker="visitor-a", content=text, conn=conn)
    policy.auto_review_qualified_all()
    recalled = policy.recall(scope(), message="好难过，感觉要失败了", max_items=2)
    assert len(recalled) == 1, "同一个群友只贡献一句"
    unlimited = ExpressionPolicy(conn, enabled=True, max_per_speaker=0, feedback_service=policy.feedback_service)
    assert len(unlimited.recall(scope(), message="好难过，感觉要失败了", max_items=2)) == 2


def test_fewshot_rotation_state_survives_restart(conn, tmp_path):

    path = str(tmp_path / "fewshot_rotation.json")
    service = FewShotService(conn, enabled=True, rotation_state_path=path)
    key = ("bot-aurora", "test:group:room-a", "group")
    service._last_injected_by_scope[key] = [7, 9]
    service._save_rotation_state()

    restarted = FewShotService(conn, enabled=True, rotation_state_path=path)
    assert restarted._last_injected_by_scope[key] == [7, 9]

    (tmp_path / "fewshot_rotation.json").write_text("{broken", encoding="utf-8")
    assert FewShotService(conn, enabled=True, rotation_state_path=path)._last_injected_by_scope == {}


def test_reusable_phrase_filter_blocks_bot_queries_and_long_fragments(conn):
    from services.expression_policy import expression_is_reusable

    assert expression_is_reusable("有一说一") is True
    assert expression_is_reusable("慢慢来，一步一步走") is True
    # 线上真实脏样本：问 Bot 的话、超 12 字叙事残片、含 Bot 名字
    assert expression_is_reusable("为啥你去回复快一个小时前的对话") is False
    assert expression_is_reusable("把LCK整条山脉一座一座推平") is False
    assert expression_is_reusable("你为什么一直回复我") is False
    assert expression_is_reusable("有没有人玩原神啊") is False
    assert expression_is_reusable("羽书今天怎么不说话", bot_terms=("羽书", "白真真")) is False
    assert expression_is_reusable("（这句没写完") is False

    feedback, policy, _ = _policy(conn, bot_terms_for=lambda _: ("羽书", "白真真"))
    _observe(feedback, 60, "为啥你去回复快一个小时前的对话", content="为啥你去回复快一个小时前的对话", situation="clarify", conn=conn)
    _observe(feedback, 61, "你为什么老回我", content="你为什么老回我", situation="clarify", conn=conn)
    assert policy.list_candidates(scope())[1] == 0


def test_per_bot_blocklist_and_same_group_cross_bot_frequency_sharing(conn):
    from tests.test_reply_feedback_learning import scope as make_scope

    yushu_scope = make_scope(bot="yushu", room="room-shared")
    baizz_scope = make_scope(bot="baizz", room="room-shared")
    blocklists = {"yushu": [], "baizz": ["锤子", "爷"]}
    feedback = ReplyFeedbackService(conn, enabled=True, now=lambda: 100.0)
    policy = ExpressionPolicy(
        conn,
        enabled=True,
        feedback_service=feedback,
        now=lambda: 100.0,
        persona_blocklist=["全局违禁"],
        bot_blocklist_for=lambda bid: blocklists.get(bid, []),
    )
    feedback.expression_policy = policy

    # 同一群 room-shared：访客甲在 yushu 视角说了一句，访客乙在 baizz 视角说了同一句（各自单行 occurrence=1）
    for idx, owner, spk in ((70, yushu_scope, "visitor-a"), (71, baizz_scope, "visitor-b")):
        add_message(conn, idx, "真有你的呀", speaker=spk, owner=owner)
        feedback.record_bookkeeping(owner, {"expressions": [{
            "source_memory_id": idx, "speaker_id": spk, "content": "真有你的呀", "situation": "humor",
        }]}, source_message_ids=(idx,))

    # 粗俗词『懂个锤子』同样两人说过：yushu 允许转正，baizz 被专属 blocklist 拦在待审
    for idx, owner, spk in ((72, yushu_scope, "visitor-a"), (73, baizz_scope, "visitor-b")):
        add_message(conn, idx, "懂个锤子哦", speaker=spk, owner=owner)
        feedback.record_bookkeeping(owner, {"expressions": [{
            "source_memory_id": idx, "speaker_id": spk, "content": "懂个锤子哦", "situation": "humor",
        }]}, source_message_ids=(idx,))

    stats = policy.auto_review_qualified_all()
    assert stats["approved"] == 3  # yushu 2 条 + baizz 『真有你的呀』 1 条
    assert stats["persona_rejected"] == 1  # baizz 『懂个锤子哦』被专属禁用词拦截
    baizz_rows = dict(conn.execute(
        "SELECT content, status FROM reply_expression_candidates WHERE bot_id='baizz'"
    ).fetchall())
    assert baizz_rows == {"真有你的呀": "approved", "懂个锤子哦": "pending"}


def test_cleanup_and_auto_review_purges_legacy_noisy_pending(conn):
    feedback, policy, _ = _policy(conn)
    # 模拟旧库遗留一条 15 字长问句待审行
    add_message(conn, 80, "为啥你去回复快一个小时前的对话", speaker="visitor-a")
    conn.execute(
        """INSERT INTO reply_expression_candidates
           (bot_id, session_id, visibility, speaker_id, source_memory_id, content, situation, source_hash, artifact_json, created_at)
           VALUES ('bot-aurora', 'test:group:room-a', 'group', 'visitor-a', 80, '为啥你去回复快一个小时前的对话', 'clarify', 'h', '{}', 90.0)"""
    )
    conn.commit()
    res = policy.cleanup_and_auto_review(scope())
    assert res["cleaned"] == 1
    status, reviewer = conn.execute(
        "SELECT status, reviewer FROM reply_expression_candidates WHERE source_memory_id=80"
    ).fetchone()
    assert status == "rejected" and reviewer == "auto_hygiene"

