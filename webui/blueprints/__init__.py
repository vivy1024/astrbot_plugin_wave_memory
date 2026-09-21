"""Blueprint 注册表

导入失败必须可见：此前外层 `except Exception` 把**所有**核心蓝图导入错误静默吞成
`None`，只要有一个文件缺失或语法错误，`get_blueprints()` 就返回空列表 —— 表现是
WebUI 页面骨架能加载、但全部 /api/* 路由 404，且日志里没有任何线索。实测曾因缺少
一个 `webui/facts_evidence.py` 导致 21 个蓝图全部失效而无声。

现在的策略：
- 缺少 quart（本地单测未安装）→ 静默走轻量兜底，保持原有行为；
- 其他任何导入异常 → 记录 error 日志（含模块名与异常），并把失败模块名保留在
  `BLUEPRINT_IMPORT_FAILURES`，让启动日志能直接指出问题所在。
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger("astrbot")

# 导入失败的蓝图 (模块名, 异常)；供启动诊断与测试断言。
BLUEPRINT_IMPORT_FAILURES: list[tuple[str, BaseException]] = []

# 核心蓝图：缺失即视为部署不完整（不再静默降级）。
_CORE = (
    ("auth", "auth_bp"),
    ("pages", "pages_bp"),
    ("explore", "explore_bp"),
    ("memories", "memories_bp"),
    ("tags", "tags_bp"),
    ("tag_graph", "tag_graph_bp"),
    ("config", "config_bp"),
    ("system", "system_bp"),
    ("beliefs", "beliefs_bp"),
    ("soul", "soul_bp"),
    ("jargon", "jargon_bp"),
    ("kg", "kg_bp"),
    ("knowledge", "knowledge_bp"),
    ("facts", "facts_bp"),
    ("options", "options_bp"),
    ("people", "people_bp"),
    ("maintenance", "maintenance_bp"),
)
# 可选蓝图：历史上新增，旧部署可能没有对应文件，缺失只降级不报错。
_OPTIONAL = (
    ("injection_observatory", "injection_observatory_bp"),
    ("channel_config", "channel_config_bp"),
    ("agent_feedback", "agent_feedback_bp"),
    ("compatibility", "compatibility_bp"),
    ("runtime", "runtime_bp"),
)

_NAMES = [n for _, n in _CORE] + [n for _, n in _OPTIONAL]

try:
    from quart import Blueprint  # type: ignore[assignment]
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时只导入 helper
    class Blueprint:  # type: ignore[no-redef]
        pass

    globals().update({name: None for name in _NAMES})
else:
    def _load(module: str, attr: str, *, required: bool) -> object:
        try:
            mod = __import__(f"{__name__}.{module}", fromlist=[attr])
            return getattr(mod, attr)
        except Exception as exc:
            BLUEPRINT_IMPORT_FAILURES.append((module, exc))
            level = logging.ERROR if required else logging.WARNING
            logger.log(
                level,
                "[WaveMemory WebUI] 蓝图导入失败 module=%s: %s: %s",
                module, type(exc).__name__, exc,
            )
            return None

    for _module, _attr in _CORE:
        globals()[_attr] = _load(_module, _attr, required=True)
    for _module, _attr in _OPTIONAL:
        globals()[_attr] = _load(_module, _attr, required=False)


def get_blueprints() -> List[Blueprint]:
    """返回所有要注册的 Blueprint。"""
    return [bp for bp in (globals().get(name) for name in _NAMES) if bp is not None]


def missing_core_blueprints() -> tuple[str, ...]:
    """返回导入失败的核心蓝图模块名；非空说明部署不完整。"""
    failed = {module for module, _ in BLUEPRINT_IMPORT_FAILURES}
    return tuple(module for module, attr in _CORE if globals().get(attr) is None and module in failed)
