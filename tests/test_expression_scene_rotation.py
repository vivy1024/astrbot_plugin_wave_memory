"""情景词表、问号策略与同情景轮换（离线、虚构数据）。"""

from __future__ import annotations

import sqlite3

import pytest

from services.expression_policy import situations_for
from tests.test_reply_feedback_learning import add_message, scope, services, setup_store


@pytest.mark.parametrize("message,expected", [
    ("真的假的", ("clarify",)),
    ("何意味", ("clarify",)),
    ("这是啥", ("clarify",)),
    ("咋回事", ("clarify",)),
    ("为啥呀", ("clarify",)),
    ("你的意思是必须要对话?", ("clarify",)),
    ("有一说一", ("acknowledge",)),
    ("好的", ("acknowledge",)),
    ("行吧", ("acknowledge",)),
    ("确实", ("acknowledge",)),
    ("ok", ("acknowledge",)),
    ("懂了", ("acknowledge",)),
    ("草", ("humor",)),
    ("草，这也行", ("humor",)),
    ("绷不住了", ("humor",)),
    ("xswl", ("humor",)),
    ("2333", ("humor",)),
    ("没事", ("encourage",)),
    ("别慌", ("encourage",)),
    ("稳了", ("encourage",)),
    ("冲冲冲", ("encourage",)),
    ("好耶", ("celebrate",)),
    ("太强了", ("celebrate",)),
    ("牛逼", ("celebrate",)),
    ("恭喜", ("celebrate",)),
    ("别理他", ("boundary",)),
    ("太过分了", ("boundary",)),
])
def test_expanded_scene_vocabulary_hits(message, expected):
    assert situations_for(message) == expected


@pytest.mark.parametrize("message", [
    "草莓好吃", "草死你", "行", "冲", "不爽", "不过分", "没事干", "最好的办法",
    "book", "12333", "快乐了", "what", "show me",
])
def test_ambiguous_words_do_not_misfire(message):
    assert situations_for(message) == ()


def test_mention_prefix_is_ignored_for_scene_matching():
    # 昵称里的字（「哈哈」「谢谢」）不能触发情景。
    assert situations_for("@哈哈谢谢(123456) 中午吃什么饭") == ()
    assert situations_for("@某人(123456) 真的假的") == ("clarify",)


def test_question_mark_alone_is_only_a_weak_fallback():
    # 普通问句不再因为有问号就被当成求解释。
    assert situations_for("能嫁给我吗?") == ()
    assert situations_for("今天几点开播？") == ()
    # 整句就是疑问反应时，作为兜底判 clarify。
    for text in ("?", "？？", "啊？", "蛤??", "@某人(123456) ?"):
        assert situations_for(text) == ("clarify",)
    # 其它情景命中时问号不额外加 clarify。
    assert situations_for("哈哈？") == ("humor",)


@pytest.fixture
def clarify_pool():
    conn = sqlite3.connect(":memory:")
    setup_store(conn)
    fb, policy = services(conn)
    lines = ["我去，不早说", "何意味", "真的假的", "啥情况"]
    for offset, text in enumerate(lines):
        ident = 20 + offset
        add_message(conn, ident, text, speaker=f"visitor-{offset}")
        fb.record_bookkeeping(scope(), {"expressions": [{
            "source_memory_id": ident, "speaker_id": f"visitor-{offset}",
            "content": text, "situation": "clarify",
        }]}, source_message_ids=(ident,))
    rows = conn.execute("SELECT id FROM reply_expression_candidates ORDER BY id").fetchall()
    for (example_id,) in rows:
        policy.review(scope(), example_id, approved=True, expected_revision=1, reviewer="test-reviewer",
                      idempotency_key=f"review:{example_id}", style_only=True)
    yield conn, policy, [row[0] for row in rows]
    conn.close()


def test_recall_is_deterministic_for_same_seed(clarify_pool):
    _, policy, _ = clarify_pool
    first = policy.recall(scope(), message="真的假的?", seed="trace-1")
    assert len(first) == 2
    for _ in range(5):
        assert policy.recall(scope(), message="真的假的?", seed="trace-1") == first
    # 缺省种子（消息 + 日期）同样稳定。
    assert policy.recall(scope(), message="真的假的?") == policy.recall(scope(), message="真的假的?")


def test_recall_rotates_within_same_scene_across_seeds(clarify_pool):
    _, policy, ids = clarify_pool
    chosen, pairs = set(), set()
    for n in range(40):
        picked = policy.recall(scope(), message="真的假的?", seed=f"trace-{n}")
        chosen.update(item["example_id"] for item in picked)
        pairs.add(tuple(sorted(item["example_id"] for item in picked)))
    # 最小 id 的句式也能轮到，不再永远是 id 最大的一对。
    assert f"expression:{min(ids)}" in chosen
    assert chosen == {f"expression:{ident}" for ident in ids}
    assert len(pairs) > 1


def test_recall_default_seed_changes_with_message(clarify_pool):
    _, policy, ids = clarify_pool
    chosen = set()
    for n in range(40):
        chosen.update(item["example_id"] for item in policy.recall(scope(), message=f"真的假的 第{n}次?"))
    assert f"expression:{min(ids)}" in chosen


def test_quality_still_outranks_rotation(clarify_pool):
    _, policy, ids = clarify_pool
    favored = min(ids)
    policy.quality_adjustment = lambda _scope, example_id: 0.1 if example_id == favored else 0.0
    for n in range(20):
        picked = policy.recall(scope(), message="真的假的?", seed=f"trace-{n}")
        assert picked[0]["example_id"] == f"expression:{favored}"
