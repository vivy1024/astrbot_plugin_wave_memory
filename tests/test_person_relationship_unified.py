"""一个生命对一个人只有一份好感：群聊、私聊、关联账号全部计入，所有读取方同一口径。"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

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

BOT = "bot-0001"
SUBJECT = "qq:user:u1"


def _group(gid: str) -> RuntimeScope:
    return RuntimeScope(BOT, "group", SessionRef(f"qq:group:{gid}", "qq", "group", gid), subject_principal_id=SUBJECT)


def _private() -> RuntimeScope:
    return RuntimeScope(BOT, "private", SessionRef("qq:private:u1", "qq", "private", "u1"), subject_principal_id=SUBJECT)


@pytest.fixture
def repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "soul.sqlite3"))
    ensure_scoped_soul_schema(cm)
    try:
        yield ScopedSoulRepository(cm)
    finally:
        cm.close()


def _row(repo, scope, dims: str, updated_at: float = 1000.0) -> None:
    repo.cm.execute_write(
        """INSERT INTO scoped_soul_relationships(
               bot_id, session_id, visibility, subject_principal_id, affinity, state,
               dimensions, revision, evidence, updated_at)
           VALUES (?, ?, ?, ?, 0, 'neutral', ?, 1, '[]', ?)""",
        (scope.bot_id, scope.session.id, scope.visibility, SUBJECT, dims, updated_at),
    )
    repo.cm.commit()


def test_private_rows_count_toward_the_single_score(repo):
    _row(repo, _group("g1"), '{"familiarity":30.0}')
    _row(repo, _private(), '{"familiarity":20.0,"trust":10.0}', 1500.0)
    data = repo.person_relationship(BOT, SUBJECT)
    assert data["available"] is True
    assert data["dimensions"] == {"familiarity": 50.0, "trust": 10.0}
    assert data["scene_count"] == 2
    assert data["affinity"] == int(50 * 0.25 + 10 * 0.30)


def test_score_is_identical_from_every_scene(repo):
    _row(repo, _group("g1"), '{"familiarity":30.0}')
    _row(repo, _group("g2"), '{"trust":12.0}')
    _row(repo, _private(), '{"fun":8.0}')
    first = repo.person_relationship(BOT, SUBJECT)
    # 不依赖调用场合：同一 bot + 人永远拿到同一份
    assert repo.person_relationship(BOT, SUBJECT) == first
    assert first["dimensions"] == {"familiarity": 30.0, "trust": 12.0, "fun": 8.0}


def test_dimensions_are_clamped_after_merge(repo):
    _row(repo, _group("g1"), '{"familiarity":80.0}')
    _row(repo, _group("g2"), '{"familiarity":80.0}')
    assert repo.person_relationship(BOT, SUBJECT)["dimensions"]["familiarity"] == 100.0


def test_unknown_person_is_unavailable(repo):
    data = repo.person_relationship(BOT, "qq:user:nobody")
    assert data["available"] is False and data["affinity"] is None


def test_injection_uses_unified_score_including_private(repo):
    """群里注入的分数包含私聊攒下的数值，但不出现私聊证据。"""
    from services.injection.channels.relationship import RelationshipChannel

    _row(repo, _group("g1"), '{"familiarity":30.0}')
    _row(repo, _private(), '{"trust":40.0}')
    scope = _group("g1")
    totals = RelationshipChannel(repository=repo)._relationship_totals(scope)
    assert totals["merged_dimensions"] == {"familiarity": 30.0, "trust": 40.0}
    assert totals["merged_affinity"] == repo.person_relationship(BOT, SUBJECT)["affinity"]


def test_query_tool_single_mode_reads_unified_score(repo):
    from tools.affinity_update import WaveMemoryAffinityTool

    _row(repo, _group("g1"), '{"familiarity":30.0}')
    _row(repo, _private(), '{"trust":40.0}')
    tool = WaveMemoryAffinityTool.__new__(WaveMemoryAffinityTool)
    tool.db = SimpleNamespace(soul_repository=repo)
    unified = tool._unified(BOT, SUBJECT)
    assert unified and unified["affinity"] == repo.person_relationship(BOT, SUBJECT)["affinity"]
