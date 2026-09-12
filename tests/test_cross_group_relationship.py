"""好感度不分群：同一用户在全部群的关系积累合并为一份态度。

真人不会因为换了个群就忘记交情。原本由 PersonaEvolution 按裸 QQ 号合并
（`cross_group_persona_merge`），2026-08-02 scope 治理时被隔离并最终移除。
本模块以**读时合并**恢复该能力：各群维度累加后钳制，再推导综合值与态度；
存储仍按 `(bot_id, session_id, visibility, subject_principal_id)` 保留每群原始增量，
聚合只在读取侧进行，不写库、不改主键。
"""

from __future__ import annotations

import sys
import types

import pytest

if "astrbot.api" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass

    _api.logger = _Logger()
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository


SUBJECT = "qq:user:u1"


def _scope(bot: str = "bot-0001", session: str = "qq:group:g1") -> RuntimeScope:
    return RuntimeScope(
        bot, "group",
        SessionRef(session, "qq", "group", session.split(":")[-1]),
        subject_principal_id=SUBJECT,
    )


@pytest.fixture
def repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "soul.sqlite3"))
    ensure_scoped_soul_schema(cm)
    try:
        yield ScopedSoulRepository(cm)
    finally:
        cm.close()


def _upsert(repo, scope, affinity, state, updated_at):
    """直接落一行关系，模拟该群已有的关系状态。"""
    repo.cm.execute_write(
        """INSERT INTO scoped_soul_relationships(
               bot_id, session_id, visibility, subject_principal_id, affinity, state,
               dimensions, revision, evidence, updated_at)
           VALUES (?, ?, 'group', ?, ?, ?, '{}', 1, '[]', ?)""",
        (scope.bot_id, scope.session.id, SUBJECT, affinity, state, updated_at),
    )
    repo.cm.commit()


def test_aggregate_merges_same_subject_across_groups(repo):
    """同一个 principal 在两个群的关系行必须被聚合出来。"""
    _upsert(repo, _scope(session="qq:group:g1"), 42, "friendly", 1000.0)
    _upsert(repo, _scope(session="qq:group:g2"), 7, "wary", 2000.0)

    data = repo.summarize_cross_group_relationship(_scope(session="qq:group:g1"), subject_principal_id=SUBJECT)

    assert data["available"] is True
    assert data["group_count"] == 2
    assert data["current_group_id"] == "qq:group:g1"
    assert data["updated_at"] == 2000.0
    session_ids = {g["session_id"] for g in data["groups"]}
    assert session_ids == {"qq:group:g1", "qq:group:g2"}


def test_aggregate_is_read_only(repo):
    """聚合绝不能写入任何行。"""
    _upsert(repo, _scope(), 10, "neutral", 1000.0)
    before = repo.cm.execute_read("SELECT COUNT(*) FROM scoped_soul_relationships").fetchone()[0]

    repo.summarize_cross_group_relationship(_scope(), subject_principal_id=SUBJECT)

    after = repo.cm.execute_read("SELECT COUNT(*) FROM scoped_soul_relationships").fetchone()[0]
    assert before == after == 1


def test_aggregate_scoped_to_bot(repo):
    """另一个 Bot 的关系不得混入。"""
    _upsert(repo, _scope(bot="bot-0001"), 42, "friendly", 1000.0)
    _upsert(repo, _scope(bot="bot-0002"), 99, "hostile", 1500.0)

    data = repo.summarize_cross_group_relationship(_scope(bot="bot-0001"), subject_principal_id=SUBJECT)

    assert data["group_count"] == 1
    assert data["groups"][0]["affinity"] == 42


def test_aggregate_unavailable_when_no_rows(repo):
    data = repo.summarize_cross_group_relationship(_scope(), subject_principal_id="qq:user:nobody")

    assert data["available"] is False
    assert data["group_count"] == 0


def test_aggregate_counts_relationship_events_across_groups(repo):
    scope = _scope(session="qq:group:g1")
    _upsert(repo, scope, 10, "neutral", 1000.0)
    _upsert(repo, _scope(session="qq:group:g2"), 20, "friendly", 1200.0)
    repo.cm.execute_write(
        """INSERT INTO scoped_soul_relationship_events(
               bot_id, session_id, visibility, subject_principal_id, event_type, dimension,
               delta, reason, revision, created_at)
           VALUES (?, 'qq:group:g2', 'group', ?, 'interaction', 'trust', 1.0, 'r', 1, 1200.0)""",
        (scope.bot_id, SUBJECT),
    )
    repo.cm.commit()

    data = repo.summarize_cross_group_relationship(scope, subject_principal_id=SUBJECT)

    assert data["total_events"] == 1


def _upsert_dims(repo, scope, affinity, state, dimensions, updated_at):
    """落一行关系并带真实 dimensions（用于验证取最高印象的维度快照）。"""
    repo.cm.execute_write(
        """INSERT INTO scoped_soul_relationships(
               bot_id, session_id, visibility, subject_principal_id, affinity, state,
               dimensions, revision, evidence, updated_at)
           VALUES (?, ?, 'group', ?, ?, ?, ?, 1, '[]', ?)""",
        (scope.bot_id, scope.session.id, SUBJECT, affinity, state, dimensions, updated_at),
    )
    repo.cm.commit()


def test_aggregate_sums_all_groups(repo):
    """好感度不分群：某人在 A 群的羁绊，在 B 群也必须算数。

    真实场景：同一人在老群积累 27（救命之恩），在新群刚有 0；
    只读当前群会让 bot 在新群完全不知道这段关系。
    """
    _upsert_dims(repo, _scope(session="qq:group:g2"), 27, "neutral",
                 '{"depth":10.0,"familiarity":79.5,"fun":5.0,"trust":15.0}', 1000.0)
    _upsert_dims(repo, _scope(session="qq:group:g1"), 0, "neutral", '{"trust":2.2}', 5000.0)

    data = repo.summarize_cross_group_relationship(_scope(session="qq:group:g1"), subject_principal_id=SUBJECT)

    assert data["current_group_id"] == "qq:group:g1"
    assert data["merged_affinity"] > 0, "其他群的羁绊必须在当前群同样生效"
    assert data["group_count"] == 2


def test_aggregate_merged_state_recomputed_from_best_value(repo):
    """态度基于累加后的好感度重新推导：单群都是 neutral，合起来才是 friendly。"""
    dims = '{"familiarity":80.0,"trust":15.0,"depth":20.0}'  # 单群 29 分
    _upsert_dims(repo, _scope(session="qq:group:g1"), 29, "neutral", dims, 1000.0)
    _upsert_dims(repo, _scope(session="qq:group:g2"), 29, "neutral", dims, 1200.0)

    data = repo.summarize_cross_group_relationship(_scope(session="qq:group:g1"), subject_principal_id=SUBJECT)

    assert data["merged_affinity"] == 44, "信任/深度累加后跨过 friendly 门槛"
    assert data["merged_state"] == "friendly", "累加后态度必须重算而非沿用单群 neutral"


def test_aggregate_dimension_snapshot_comes_from_best_group(repo):
    """维度在各群之间累加，且各自钳制到合法上限（熟悉度会饱和到 100）。"""
    _upsert_dims(repo, _scope(session="qq:group:g2"), 27, "neutral",
                 '{"depth":10.0,"familiarity":79.5,"fun":5.0,"trust":15.0}', 1000.0)
    _upsert_dims(repo, _scope(session="qq:group:g1"), 0, "neutral", '{"trust":2.2}', 5000.0)

    data = repo.summarize_cross_group_relationship(_scope(session="qq:group:g1"), subject_principal_id=SUBJECT)

    assert data["merged_dimensions"]["trust"] == 17.2, "trust 应在两群间累加"
    assert data["merged_dimensions"]["familiarity"] == 79.5
    assert data["merged_affinity"] > 0


def test_single_group_has_no_merged_override(repo):
    """只有一个群时结果等于该群自身，不引入额外偏移。"""
    _upsert_dims(repo, _scope(session="qq:group:g1"), 8, "neutral", '{"familiarity":32.0}', 1000.0)

    data = repo.summarize_cross_group_relationship(_scope(session="qq:group:g1"), subject_principal_id=SUBJECT)

    assert data["group_count"] == 1
    assert data["merged_affinity"] == 8


def test_row_without_affinity_derives_from_dimensions(repo):
    """affinity 为 NULL 的写入被 schema 拒绝，聚合无需依赖空值兜底。"""
    with pytest.raises(Exception):
        repo.cm.execute_write(
            """INSERT INTO scoped_soul_relationships(
                   bot_id, session_id, visibility, subject_principal_id, affinity, state,
                   dimensions, revision, evidence, updated_at)
               VALUES ('bot-0001', 'qq:group:g2', 'group', ?, NULL, 'neutral',
                       '{"familiarity":100.0}', 1, '[]', 1000.0)""",
            (SUBJECT,),
        )


class _CrossGroupRepo:
    """最小仓储替身：当前群给低分，跨群汇总给高分（复现线上懒惰芝麻场景）。"""

    def __init__(self, merged_affinity=27):
        self._merged_affinity = merged_affinity
        self.summarize_calls = 0

    def get_state(self, scope, subject_principal_id=None, limit=25, offset=0):
        return {
            "relationship": {
                "affinity": 0,
                "state": "neutral",
                "dimensions": {"trust": 2.2},
                "values": {},
                "revision": 1,
                "evidence": [{"relationship_event_id": 1}],
            },
            "relationship_history": {"items": []},
            "timeline": {"items": []},
            "revision": 1,
        }

    def summarize_cross_group_relationship(self, scope, *, subject_principal_id):
        self.summarize_calls += 1
        return {
            "available": True,
            "group_count": 2,
            "current_group_id": "qq:group:g1",
            "groups": [
                {"session_id": "qq:group:g1", "affinity": 0, "state": "neutral"},
                {"session_id": "qq:group:g2", "affinity": self._merged_affinity, "state": "neutral"},
            ],
            "merged_affinity": self._merged_affinity,
            "merged_state": "neutral",
            "merged_dimensions": {"depth": 10.0, "familiarity": 79.5, "fun": 5.0, "trust": 15.0},
            "updated_at": 1000.0,
            "total_events": 2,
        }


def _channel_scope() -> RuntimeScope:
    """注入路径要求 session/conversation/subject 三者自洽。"""
    return RuntimeScope(
        "bot-0001", "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:1",
    )


def _channel_ctx(scope, *, cross_group_enabled=True, sender_id="1"):
    from types import SimpleNamespace

    config = {"channels": {"affinity": {"enabled": True}}}
    if cross_group_enabled is not None:
        config["cross_group_enabled"] = cross_group_enabled
    return SimpleNamespace(mode="full", config=config, scope=scope, sender_id=sender_id)


def _run_channel(repo, ctx):
    import asyncio

    from services.injection.channels.relationship import RelationshipChannel

    return asyncio.run(RelationshipChannel(repository=repo).build(ctx))


def test_injection_uses_cross_group_affinity_not_current_group():
    """核心回归：当前群好感度为 0，注入必须给出该用户全部群累加后的态度。"""
    repo = _CrossGroupRepo()
    result = _run_channel(repo, _channel_ctx(_channel_scope()))

    assert result.status == "hit"
    assert "综合值=27" in result.text, "不得把当前群的 0 当成对该用户的真实态度"
    assert "综合值=0" not in result.text
    assert "familiarity=79.5" in result.text
    assert "跨群关系" not in result.text, "好感度不分群，不应再按群拆分展示"


def test_injection_always_aggregates_regardless_of_cross_group_switch():
    """好感度不分群，不存在「关掉跨群」的开关。"""
    repo = _CrossGroupRepo()
    result = _run_channel(repo, _channel_ctx(_channel_scope(), cross_group_enabled=False))

    assert "综合值=27" in result.text, "好感度合并是默认行为，不受开关影响"
    assert repo.summarize_calls == 1
