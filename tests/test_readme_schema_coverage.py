"""README 配置参考必须覆盖全部 schema 分组。

背景：v0.x 建的配置表到 v5.0 只剩 6% 覆盖 —— 因为纯手工维护的表必然随版本漂移。
本测试把「README 配置参考」变成契约：schema 增删字段后 README 不跟，CI 直接红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))


def _groups() -> dict[str, dict]:
    return {
        key: value
        for key, value in SCHEMA.items()
        if isinstance(value, dict) and "items" in value
    }


def test_readme_documents_every_schema_field():
    """每个 schema 配置字段都必须在 README 出现（字段名级）。"""
    missing: list[str] = []
    for group, meta in _groups().items():
        for field in meta["items"]:
            if not re.search(r"\b" + re.escape(field) + r"\b", README):
                missing.append(f"{group}.{field}")
    assert not missing, (
        "README 未记录的配置字段（新增配置项时请同步 README 配置参考）：\n  "
        + "\n  ".join(missing)
    )


def test_readme_lists_every_schema_group():
    """每个 schema 分组都要在 README 有对应小节标题。

    允许同构分组合并展示（如 MetaThinking_Bot1 / MetaThinking_Bot2 共用一个标题）。
    """
    missing = [g for g in _groups() if g not in README]
    assert not missing, f"README 缺少配置分组小节: {missing}"


def test_readme_documents_no_phantom_config_keys():
    """README 配置表里不得出现 schema 已不存在的键（防「文档教用户找不存在的开关」）。"""
    known = {f for meta in _groups().values() for f in meta["items"]}
    # 顶层标量字段（不属于任何分组）
    known |= {
        k for k, v in SCHEMA.items()
        if isinstance(v, dict) and "type" in v
    }
    # 只扫描「配置参考」章节内、形如 `| key | value | desc |` 且 key 为 snake_case 的行；
    # 工具表/通道表也在同一份 README 里，用已知非配置前缀排除。
    start = README.index("## 📋 配置参考")
    end = README.index("## 运维排查")
    section = README[start:end]
    row_key = re.compile(r"^\|\s*([a-z][a-z0-9_]{2,})\s*\|", re.M)
    phantom = sorted({k for k in row_key.findall(section) if k not in known})
    assert not phantom, (
        "README 配置表出现 schema 中不存在的键（疑似已废弃未清理）：\n  "
        + "\n  ".join(phantom)
    )


def test_readme_has_no_reference_to_deleted_modules():
    """README 不得再描述已被删除的模块。"""
    deleted = ["persona_evolution.py", "consolidation.py", "ConsolidationService", "bot_soul.py"]
    hits = [name for name in deleted if name in README]
    assert not hits, f"README 仍引用已删除的模块: {hits}"


def test_readme_channel_list_matches_actual_channels():
    """README 的通道清单必须与实际通道名集合一致。"""
    channels_dir = ROOT / "services" / "injection" / "channels"
    actual = set()
    for path in channels_dir.glob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        match = re.search(r'^\s{4}name\s*=\s*"([a-z0-9_]+)"', text, re.M)
        if match:
            actual.add(match.group(1))
    listed = set(re.findall(r"^├─ ([a-z0-9_]+)（", README, re.M))
    listed |= set(re.findall(r"^└─ ([a-z0-9_]+)（", README, re.M))
    assert actual, "未解析到任何通道名，请检查 name 属性格式"
    assert listed == actual, (
        f"README 通道清单与实际不一致\n  仅 README 有: {sorted(listed - actual)}\n"
        f"  仅代码有: {sorted(actual - listed)}"
    )
