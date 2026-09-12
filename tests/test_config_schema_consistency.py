"""schema ↔ 代码 双向一致性。

历史上配置在两个方向上持续漂移，且没有任何测试拦住：

- 方向一：代码读取的配置键不在 `_conf_schema.json` → 配置项在界面上看不到，
  更糟的是这类章节会被 AstrBot 的 `check_config_integrity` 当作「参考配置中
  没有的项」在每次启动时删除并写回磁盘，用户改了也留不住。
- 方向二：schema 定义了键但没有任何代码读取 → 界面上是可调旋钮，实际是死的。

本测试把两个方向都锁住。新增配置键时必须同时满足：有 schema 定义、有代码读取
（或显式登记为「仅兼容保留」）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "_conf_schema.json"

# 允许「只定义、代码不读」的键。当前为空：已废弃的键必须从 schema 删除，
# 而不是留在 schema 里靠前端隐藏（AstrBot 不会因 config.json 多出未知键而拒绝启动）。
LEGACY_ONLY_KEYS: set[tuple[str, str]] = set()

# 纯说明项，不是配置。
DOC_ONLY_KEYS = {"_system_status"}


def _schema_object_sections() -> dict[str, dict]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {
        key: value
        for key, value in schema.items()
        if isinstance(value, dict) and "items" in value
    }


def _all_python_source() -> str:
    chunks: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or path.parts[0] == "tests":
            continue
        chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def test_every_schema_key_is_read_by_code_or_declared_legacy():
    """方向二：schema 每个键都要被代码读取，或显式登记为兼容保留。"""
    source = _all_python_source()
    orphans: list[str] = []
    for section, meta in _schema_object_sections().items():
        for item_key in meta["items"]:
            if item_key in DOC_ONLY_KEYS:
                continue
            if (section, item_key) in LEGACY_ONLY_KEYS:
                continue
            if f'"{item_key}"' not in source and f"'{item_key}'" not in source:
                orphans.append(f"{section}.{item_key}")
    assert orphans == [], (
        "以下配置项在界面上可调但代码从不读取（假旋钮），"
        f"要么接上配置、要么登记进 LEGACY_ONLY_KEYS：{orphans}"
    )


def test_legacy_only_keys_are_still_declared():
    """登记为兼容保留的键必须真的还在 schema 里，避免清单过期后失效。"""
    sections = _schema_object_sections()
    for section, item_key in LEGACY_ONLY_KEYS:
        assert section in sections, f"{section} 章节缺失"
        assert item_key in sections[section]["items"], f"{section}.{item_key} 已不存在"


def test_inject_settings_exposes_behavior_switches():
    """注入编排开关与条数必须可配：它们是会影响回复内容的行为参数。"""
    items = _schema_object_sections()["Inject_Settings"]["items"]
    for key in (
        "orchestrator_active_enabled",
        "orchestrator_shadow_enabled",
        "skip_recent_minutes",
        "facts_max",
    ):
        assert key in items, f"Inject_Settings.{key} 必须在 schema 中定义"


def test_relationship_delta_caps_are_wired_to_config():
    """关系变化上限必须从配置读取，而不是只定义在 schema 里当摆设。"""
    source = _all_python_source()
    # 构造 RelationshipEventService 时要把三个上限传进去
    for key in (
        "relationship_single_delta_cap",
        "relationship_daily_delta_cap",
        "relationship_hostility_delta_cap",
    ):
        assert f'"{key}"' in source or f"'{key}'" in source, f"{key} 未被任何代码读取"


def test_config_sections_read_by_code_are_declared_in_schema():
    """方向一：代码读取的配置章节必须在 schema 中定义。

    未定义的章节会被 AstrBot 的 check_config_integrity 在启动时删除，
    用户在该章节里的设置无法存活到下一次重启。
    """
    declared = set(_schema_object_sections())
    # 已知的兼容/运行时章节，不走 schema 渲染（由专门的接口管理）
    runtime_managed = {
        "Channel_Settings",   # 由 /api/channels 专用接口与页面管理
        "Performance_Settings",
        "BookLore_Settings",
        "Affinity_Settings",
        "TagWorker_Settings",
        "Social_Settings",
    }
    source = _all_python_source()
    read_sections = set(
        re.findall(r'\.get\(\s*["\']([A-Z][a-zA-Z0-9_]*_Settings)["\']', source)
    )
    undeclared = sorted(read_sections - declared - runtime_managed)
    assert undeclared == [], (
        "以下配置章节被代码读取但 schema 未定义，会被 AstrBot 启动时删除："
        f"{undeclared}"
    )
