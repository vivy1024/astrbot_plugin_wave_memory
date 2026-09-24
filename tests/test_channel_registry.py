"""注入通道注册表：内置顺序、外部通道默认配置与开关。"""

from __future__ import annotations

import pytest

from services.config.channel_config import ChannelConfig, build_default_channel_config
from services.injection.channel_registry import ChannelRegistry, ChannelRegistryError, ChannelSpec


class _Channel:
    def __init__(self, name, dep=None):
        self.name = name
        self.dep = dep


def test_build_passes_earlier_instances_and_reports_failures():
    registry = ChannelRegistry()
    registry.register(ChannelSpec("safety", lambda d, b: _Channel("safety")))
    registry.register(ChannelSpec("memory", lambda d, b: _Channel("memory", b["safety"])))
    registry.register(ChannelSpec("facts", lambda d, b: 1 / 0))
    built = registry.build(None)
    assert [ch.name for ch in built] == ["safety", "memory"]
    assert built[1].dep is built[0]
    assert "facts" in registry.status()["build_errors"]


def test_external_channel_requires_default_and_can_be_toggled():
    registry = ChannelRegistry()
    with pytest.raises(ChannelRegistryError):
        registry.register(ChannelSpec("live_stage", lambda d, b: _Channel("live_stage")))
    default = ChannelConfig("live_stage", True, priority=40, token_budget=120, timeout_ms=200)
    registry.register(ChannelSpec("live_stage", lambda d, b: _Channel("live_stage"), default_config=default))
    registry.build(None)
    config = registry.with_external_defaults(build_default_channel_config())
    assert config.channels["live_stage"] is default
    registry.set_external_enabled("live_stage", False)
    assert registry.channels() == []
    assert "live_stage" not in registry.with_external_defaults(build_default_channel_config()).channels
    with pytest.raises(ChannelRegistryError):
        registry.set_external_enabled("memory", False)


def test_builtin_specs_cover_all_known_channels_except_retired():
    import ast
    from pathlib import Path

    from services.config.channel_config import KNOWN_CHANNELS

    # 通道模块依赖 AstrBot，这里只静态检查登记表覆盖了全部内置通道。
    tree = ast.parse(Path("services/injection/channel_registry.py").read_text(encoding="utf-8"))
    names = [
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ChannelSpec"
        and node.args and isinstance(node.args[0], ast.Constant)
    ]
    assert set(names) == set(KNOWN_CHANNELS)
    assert names[0] == "safety"
