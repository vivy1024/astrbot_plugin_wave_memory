"""启动接线：关系小传服务随记账与学习一起创建；总开关默认关闭，关闭时不起后台循环、注入不用小传；
统计接入学习统计；模型客户端经 _portrait_llm() 取（目前与事后记账共用 learning_llm）。"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.database import WaveMemoryDB
from services.relationship_portrait import (
    RelationshipPortraitService,
    configure_portrait_injection,
    portrait_injection_enabled,
)
from webui.container import ServiceContainer, get_container

STARTUP = Path(__file__).resolve().parents[1] / "app" / "startup.py"


def _methods(*names: str) -> dict:
    """编译 app/startup.py 里的真实方法，只把包内相对导入改成顶层导入。"""
    tree = ast.parse(STARTUP.read_text(encoding="utf-8"))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]

    class Imports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            node.level = 0
            return node

    module = ast.fix_missing_locations(ast.Module(body=[Imports().visit(n) for n in nodes], type_ignores=[]))
    env = {
        "logger": logging.getLogger(__name__), "json": json, "os": os, "re": re,
        "_record_err": lambda *_a, **_k: None,
        "LLMFallbackClient": lambda **_kwargs: None,
    }
    exec(compile(module, str(STARTUP), "exec"), env)
    return {name: env[name] for name in names}


class _Plugin:
    pass


_METHODS = _methods("_init_learning_services", "_portrait_llm")
_Plugin._init_learning_services = _METHODS["_init_learning_services"]
_Plugin._portrait_llm = _METHODS["_portrait_llm"]


@pytest.fixture
def plugin(tmp_path):
    from services import auto_review, fact_policy, impression_timeline

    ServiceContainer.reset()
    saved = portrait_injection_enabled()
    # 学习服务初始化会改几处模块级全局（自动审核、事实策略、结算提示开关）：测后还原，不影响其他用例
    saved_globals = (auto_review.get_auto_reviewer(), fact_policy.get_fact_policy(), impression_timeline.impression_prompting())
    db = WaveMemoryDB(str(tmp_path / "startup.sqlite"), dimension=3)
    item = _Plugin()
    item.db = db
    item.tool_registry = object()
    item.writer = None
    item.context = None
    item.write_gateway = None
    item.meta_thinking = SimpleNamespace(llm=SimpleNamespace(text_chat=None))
    # 模型分工：关系小传走 portrait 任务，默认跟 MetaThinking 一族（default_model）
    from services.llm_routing import LLMRouter

    item.llm_routing = LLMRouter(
        {"MetaThinking_Settings": {"default_model": "meta-model"}},
        profile_lookup=lambda _db_id: None,
    )
    item.spawned = []

    def spawn(coro, *, name=None, owner="plugin"):
        item.spawned.append((name, owner))
        coro.close()

    item._spawn = spawn
    item._concern_tidy_loop = lambda: _noop()
    item._llm_chain = lambda: []
    item._bot_display_name = lambda bot_id: "羽书"
    item._get_group_name = lambda bot_id, group_id: group_id
    item.bot_registry = SimpleNamespace(all=lambda: [SimpleNamespace(db_id="yushu")])
    try:
        yield item
    finally:
        configure_portrait_injection(enabled=saved)
        auto_review.set_auto_reviewer(saved_globals[0])
        fact_policy.set_fact_policy(saved_globals[1])
        impression_timeline._PROMPTING.update(saved_globals[2])
        ServiceContainer.reset()
        db.close()


async def _noop():
    return None


def test_portrait_is_off_by_default_but_available_for_admin(plugin):
    plugin.config = {"Learning_Settings": {}}
    plugin._init_learning_services()
    service = plugin.relationship_portrait
    assert isinstance(service, RelationshipPortraitService) and service.enabled is False
    routing = plugin.llm_routing
    assert service.llm is routing.client("portrait"), "走模型分工的 portrait 任务"
    assert routing.chain("portrait") == ["meta-model"], "留空时跟 MetaThinking 默认模型一族"
    per_bot = plugin._portrait_llm("yushu")
    assert per_bot.bot_id == "yushu" and per_bot.provider_ids == ["meta-model"]
    assert "wave-memory:learning:relationship-portrait" not in [name for name, _owner in plugin.spawned]
    assert portrait_injection_enabled() is False, "关闭时注入完全沿用规则摘要"
    assert get_container().relationship_portrait is service
    stats = get_container().learning_stats_getter()
    assert stats["relationship_portrait"] == {"enabled": False}


def test_enabled_portrait_starts_loop_under_learning_owner(plugin):
    plugin.config = {"Learning_Settings": {"relationship_portrait_enabled": True, "portrait_daily_limit": 5}}
    plugin._init_learning_services()
    service = plugin.relationship_portrait
    assert service.enabled is True and service.settings.daily_limit == 5
    assert ("wave-memory:learning:relationship-portrait", "learning") in plugin.spawned
    assert portrait_injection_enabled() is True
    assert service._bot_ids() == ["yushu"]


def test_enabled_without_llm_keeps_rule_summary(plugin):
    from services.llm_routing import LLMRouter

    plugin.meta_thinking = None
    plugin.llm_routing = LLMRouter({}, profile_lookup=lambda _db_id: None)
    plugin.llm_routing.set_meta_thinking_active(False)  # 启动流程按 MetaThinking 是否创建来设置
    plugin.config = {"Learning_Settings": {"relationship_portrait_enabled": True}}
    plugin._init_learning_services()
    assert plugin.relationship_portrait is None
    assert portrait_injection_enabled() is False
    assert get_container().learning_stats_getter()["relationship_portrait"] is None
