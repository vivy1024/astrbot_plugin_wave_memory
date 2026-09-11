"""运行时包路径导入兼容性测试。"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _astrbot_package_import_context():
    """模拟 AstrBot 从插件父目录导入 astrbot_plugin_wave_memory 包。"""
    plugin_root = Path(__file__).resolve().parents[1]
    workspace_root = plugin_root.parent
    old_path = list(sys.path)
    old_modules = dict(sys.modules)
    try:
        filtered = []
        for item in old_path:
            if item == "":
                continue
            try:
                resolved = Path(item).resolve()
            except (OSError, RuntimeError):
                filtered.append(item)
                continue
            if resolved == plugin_root:
                continue
            filtered.append(item)
        sys.path[:] = [str(workspace_root)] + filtered
        for name in list(sys.modules):
            if name == "services" or name.startswith("services."):
                sys.modules.pop(name, None)
            if name == "tools" or name.startswith("tools."):
                sys.modules.pop(name, None)
            if name == "astrbot_plugin_wave_memory" or name.startswith("astrbot_plugin_wave_memory.services.injection"):
                sys.modules.pop(name, None)
        yield
    finally:
        sys.path[:] = old_path
        sys.modules.clear()
        sys.modules.update(old_modules)


def test_injection_shadow_channels_import_under_plugin_package_name():
    """AstrBot 运行时没有顶层 services 包时，注入通道仍应可导入。"""
    modules = [
        "astrbot_plugin_wave_memory.services.injection.channels.safety",
        "astrbot_plugin_wave_memory.services.injection.channels.memory_recall",
        "astrbot_plugin_wave_memory.services.injection.channels.facts",
        "astrbot_plugin_wave_memory.services.injection.channels.persona",
        "astrbot_plugin_wave_memory.services.injection.channels.belief",
        "astrbot_plugin_wave_memory.services.injection.channels.jargon",
        "astrbot_plugin_wave_memory.services.injection.channels.fewshot",
        "astrbot_plugin_wave_memory.services.injection.channels.book_lore",
        "astrbot_plugin_wave_memory.services.injection.channels.fts5",
        "astrbot_plugin_wave_memory.engine.db.migrations.scoped_learning_projections",
        "astrbot_plugin_wave_memory.engine.db.migrations.scoped_soul",
        "astrbot_plugin_wave_memory.engine.db.scoped_learning_projection_repo",
        "astrbot_plugin_wave_memory.engine.db.scoped_soul_repo",
    ]

    with _astrbot_package_import_context():
        for module_name in modules:
            importlib.import_module(module_name)
