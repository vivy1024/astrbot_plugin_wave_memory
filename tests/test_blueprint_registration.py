"""蓝图注册必须完整且失败可见。

背景：webui/blueprints/__init__.py 曾用外层 `except Exception` 把所有核心蓝图导入
失败静默吞成 None。实测因缺少一个 webui/facts_evidence.py，21 个蓝图全部失效，
表现为 WebUI 骨架能加载但所有 /api/* 404，且日志无任何线索。

本测试锁死两点：
1. 正常情况下全部核心蓝图都能导入（部署完整性）；
2. 导入失败时必须被记录在案，而不是无声降级。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui.blueprints import (  # noqa: E402
    BLUEPRINT_IMPORT_FAILURES,
    get_blueprints,
    missing_core_blueprints,
)

CORE_MODULES = (
    "auth", "pages", "explore", "memories", "tags", "tag_graph", "config",
    "system", "beliefs", "soul", "jargon", "kg", "knowledge", "facts",
    "options", "people", "maintenance",
)


@pytest.fixture(scope="module")
def registered():
    return get_blueprints()


def test_no_blueprint_import_failures(registered):
    """当前代码树下不应有任何蓝图导入失败。"""
    assert not BLUEPRINT_IMPORT_FAILURES, (
        "蓝图导入失败（缺文件或语法错误）：\n  "
        + "\n  ".join(f"{m}: {type(e).__name__}: {e}" for m, e in BLUEPRINT_IMPORT_FAILURES)
    )
    assert missing_core_blueprints() == ()


def test_all_core_blueprints_registered(registered):
    """17 个核心蓝图必须全部注册 —— 少一个就说明部署不完整。"""
    names = {bp.name for bp in registered}
    expected = {m.split(".")[-1] for m in CORE_MODULES}
    # 模块名与蓝图 name 不一定同名，用数量与关键项兜底校验
    assert len(registered) >= len(CORE_MODULES), f"注册蓝图过少: {sorted(names)}"
    for key in ("auth", "pages", "facts", "knowledge", "memories", "maintenance"):
        assert key in names, f"核心蓝图缺失: {key}（现有 {sorted(names)}）"


def test_auth_route_is_reachable():
    """auth 蓝图必须带 /api 前缀 —— 前端启动即调 /api/auth/check。"""
    auth = next((bp for bp in get_blueprints() if bp.name == "auth"), None)
    assert auth is not None, "auth 蓝图未注册；前端 /api/auth/check 会 404"
    assert auth.url_prefix == "/api"


def test_auth_blueprint_declares_check_endpoint():
    """直接读源码确认 auth/check 仍存在（避免拼写漂移）。"""
    src = (ROOT / "webui" / "blueprints" / "auth.py").read_text(encoding="utf-8")
    assert '"/auth/check"' in src
    assert '"/login"' in src


def test_import_failure_is_recorded_not_silent(monkeypatch):
    """导入失败必须被记录，而不是静默返回空列表。"""
    module = importlib.import_module("webui.blueprints")
    original = module.BLUEPRINT_IMPORT_FAILURES
    try:
        module.BLUEPRINT_IMPORT_FAILURES = [("facts_evidence_test", ModuleNotFoundError("boom"))]
        # missing_core_blueprints 只报告核心模块，未知模块名不应被当成核心
        assert module.missing_core_blueprints() == ()
    finally:
        module.BLUEPRINT_IMPORT_FAILURES = original


def test_blueprints_list_is_not_empty():
    """守住本次故障的核心症状：注册表为空 = 全站 404。"""
    assert get_blueprints(), "蓝图列表为空会导致所有 /api/* 返回 404"


def test_no_duplicate_api_routes():
    """同一 (URL, HTTP方法) 绝不得被不同端点争夺。

    Flask/Quart 对重复规则按注册顺序取先到者，后注册的新实现会被静默遮蔽。
    实测曾发生：knowledge.list_facts_compatibility（旧兼容别名）与
    facts.list_facts（新实现，带审核能力）都注册 GET /api/facts，旧端点先注册
    故生效，导致事实页显示「审核能力不可用：服务端未提供事实变更网关」。

    注意：同一个端点注册 GET+POST（如 /api/beliefs/ 查列表又做创建）是合法的；
    本断言守护的是**不同端点撞同方法同路径**的真遮蔽。
    """
    from collections import defaultdict
    from webui.app import create_app

    by_route = defaultdict(list)
    for rule in create_app().url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            by_route[(str(rule), method)].append(rule.endpoint)

    conflicts = {
        f"{method} {path}": endpoints
        for (path, method), endpoints in by_route.items()
        if len(set(endpoints)) > 1
    }
    assert not conflicts, ("同路径同方法的端点冲突（后者被遮蔽）：" + str(conflicts))


def test_facts_endpoint_serves_review_capabilities():
    """/api/facts 必须由带审核能力的新实现提供。"""
    from webui.app import create_app

    endpoints = [
        rule.endpoint for rule in create_app().url_map.iter_rules()
        if str(rule) == "/api/facts"
    ]
    assert endpoints == ["facts.list_facts"], (
        f"/api/facts 被旧兼容别名遮蔽，实际端点: {endpoints}"
    )
