"""Focused formal-path tests for scoped consolidation without belief extraction."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None)
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.consolidation import ConsolidationService


class _Completion:
    def __init__(self, completion_text: str):
        self.completion_text = completion_text


class _Provider:
    async def text_chat(self, **kwargs):
        return _Completion(json.dumps({
            "summary": "大家讨论了如何照顾群里的流浪猫，并决定安排领养。",
            "topics": ["流浪猫领养"],
            "facts": [{"subject": "小明", "predicate": "计划", "object": "领养流浪猫"}],
            "relations": [{"source": "流浪猫领养", "target": "小明计划领养流浪猫", "type": "decides"}],
            "social": [{"person_a": "小明", "person_b": "小红", "relation": "合作"}],
            "nicknames": [{"person": "小明", "called": "猫管家"}],
        }, ensure_ascii=False))


class _Context:
    def get_provider_by_id(self, provider_id):
        return _Provider() if provider_id == "provider" else None


def _scope(bot_id="bot-alpha", group_id="group-1"):
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(f"qq:group:{group_id}", "qq", "group", group_id),
    )


def _add_messages(db, scope, count, prefix):
    return [
        db.add_memory(
            scope.session.conversation_id,
            f"{prefix} 第 {index} 条关于领养流浪猫的消息",
            sender_id=f"user-{index}",
            sender_name=f"用户{index}",
            timestamp=1000 + index,
            scope=scope,
        )
        for index in range(count)
    ]


def _legacy_counts(db):
    return {
        table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("facts", "tags", "memory_tags", "tag_relations", "beliefs", "kv_store")
    }


def test_consolidation_uses_exact_scope_cursor_and_never_writes_legacy_tables(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        alpha, beta = _scope(), _scope(bot_id="bot-beta")
        alpha_ids = _add_messages(db, alpha, 5, "alpha")
        beta_ids = _add_messages(db, beta, 5, "beta")
        db.add_memory("group-1", "隔离消息", sender_id="q", scope=alpha, quarantine=True)
        before = _legacy_counts(db)
        service = ConsolidationService(
            db, context=_Context(), provider_id="provider",
        )

        result = asyncio.run(service.consolidate_once())

        assert result["messages"] == 10
        assert db.get_scoped_consolidation_cursor(alpha, cursor_name="messages_v2_id") == str(alpha_ids[-1])
        assert db.get_scoped_consolidation_cursor(beta, cursor_name="messages_v2_id") == str(beta_ids[-1])
        assert db.list_scoped_facts(alpha) == []
        assert db.list_scoped_facts(beta) == []
        assert db.list_scoped_beliefs(alpha) == []
        assert db.list_scoped_beliefs(beta) == []
        assert _legacy_counts(db) == before

        # Per-scope cursors prevent a second run from reprocessing the same messages.
        assert asyncio.run(service.consolidate_once())["messages"] == 0
    finally:
        db.close()


def test_belief_engine_no_longer_extracts_from_summary():
    import services.belief_engine as belief_engine
    assert not hasattr(belief_engine.BeliefEngine, "extract_from_summary")
    assert not hasattr(belief_engine, "EXTRACT_PROMPT")


class _FailingProvider:
    """总是抛出指定错误的 provider，用于验证链遍历。"""

    def __init__(self, error_text):
        self.error_text = error_text
        self.calls = 0

    async def text_chat(self, **kwargs):
        self.calls += 1
        raise RuntimeError(self.error_text)


class _ChainContext:
    """按 id 返回不同 provider，记录查询顺序。"""

    def __init__(self, providers):
        self._providers = providers
        self.lookups = []

    def get_provider_by_id(self, provider_id):
        self.lookups.append(provider_id)
        return self._providers.get(provider_id)


def test_consolidation_falls_back_when_primary_provider_is_unavailable(tmp_path):
    """主渠道 503 时必须回退到备用渠道，而不是让摘要停产。

    回归背景：grok/grok-4.5 持续 503 导致 summary 自 7-13 起零产出。
    """
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        alpha = _scope()
        alpha_ids = _add_messages(db, alpha, 5, "alpha")
        primary = _FailingProvider(
            "Error code: 503 - {'error': {'code': 'upstream_unavailable'}}"
        )
        context = _ChainContext({"primary": primary, "backup": _Provider()})
        service = ConsolidationService(
            db,
            context=context,
            provider_id="primary",
            provider_fallback_ids="backup",
        )

        assert service.provider_ids == ["primary", "backup"]

        result = asyncio.run(service.consolidate_once())

        assert result["messages"] == 5
        assert primary.calls == 1
        assert context.lookups[:2] == ["primary", "backup"]
        assert db.get_scoped_consolidation_cursor(
            alpha, cursor_name="messages_v2_id"
        ) == str(alpha_ids[-1])
    finally:
        db.close()


def test_consolidation_skips_unrecoverable_provider_and_uses_next(tmp_path):
    """402 余额不足属于不可自愈错误，应跳到下一个渠道继续产出。"""
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        alpha = _scope()
        _add_messages(db, alpha, 5, "alpha")
        broke = _FailingProvider(
            "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"
        )
        context = _ChainContext({"broke": broke, "good": _Provider()})
        service = ConsolidationService(
            db,
            context=context,
            provider_id="broke",
            provider_fallback_ids=["good"],
        )

        result = asyncio.run(service.consolidate_once())

        assert result["messages"] == 5
        assert broke.calls == 1
        assert db.list_scoped_facts(alpha) == []
    finally:
        db.close()


def test_consolidation_skipped_when_chain_is_empty(tmp_path):
    """未配置任何 provider 时应安全跳过，不抛异常。"""
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        _add_messages(db, _scope(), 5, "alpha")
        service = ConsolidationService(db, context=_ChainContext({}), provider_id="")

        assert service.provider_ids == []
        assert asyncio.run(service.consolidate_once()) == {"status": "skipped"}
    finally:
        db.close()


def test_health_snapshot_reports_off_without_provider(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        snapshot = ConsolidationService(db, context=_ChainContext({}), provider_id="").health_snapshot()
        assert snapshot["status"] == "off"
    finally:
        db.close()


def test_health_snapshot_pending_before_first_run(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        service = ConsolidationService(db, context=_Context(), provider_id="provider")
        assert service.health_snapshot()["status"] == "pending"
    finally:
        db.close()


def test_health_snapshot_ok_after_successful_run(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        _add_messages(db, _scope(), 5, "alpha")
        service = ConsolidationService(db, context=_Context(), provider_id="provider")

        asyncio.run(service.consolidate_once())
        snapshot = service.health_snapshot()

        assert snapshot["status"] == "ok"
        assert snapshot["last_success_ts"] > 0
    finally:
        db.close()


def test_health_snapshot_degraded_when_every_scope_fails(tmp_path):
    """回归核心：provider 全挂时不能再报 ok。"""
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        _add_messages(db, _scope(), 5, "alpha")
        broken = _FailingProvider(
            "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"
        )
        service = ConsolidationService(
            db, context=_ChainContext({"p": broken}), provider_id="p",
        )

        asyncio.run(service.consolidate_once())
        snapshot = service.health_snapshot()

        assert snapshot["status"] == "degraded"
        assert snapshot["last_error_unrecoverable"] is True
        assert "余额" in snapshot["detail"]
        assert snapshot["failed_scopes"] == 1
    finally:
        db.close()


def test_health_snapshot_degraded_when_summaries_go_stale(tmp_path):
    """长期不产出新摘要必须被标记为降级，而不是沿用旧的成功状态。"""
    db = WaveMemoryDB(str(tmp_path / "wave-memory.sqlite3"), dimension=4)
    try:
        _add_messages(db, _scope(), 5, "alpha")
        service = ConsolidationService(
            db, context=_Context(), provider_id="provider", interval_hours=4.0,
        )
        asyncio.run(service.consolidate_once())

        assert service.health_snapshot()["status"] == "ok"

        # 模拟最后一次成功发生在 20 天前（真实故障窗口长度）。
        service._last_success_ts -= 20 * 86400
        snapshot = service.health_snapshot()

        assert snapshot["status"] == "degraded"
        assert "未产出新摘要" in snapshot["detail"]
    finally:
        db.close()
