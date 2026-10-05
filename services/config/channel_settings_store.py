"""9876 通道配置的持久化：存进 WaveMemory 数据库，而不是只靠 AstrBot 静态配置。

``Channel_Settings`` 里真正的内容（各通道的开关与参数、分层覆盖）在 ``_conf_schema.json``
里没有逐项声明，AstrBot 加载插件做完整性检查时会把未声明的键删掉
（启动日志 ``Config key removed: Channel_Settings.channels``）——9876 上改的系统级
通道配置一重启就丢。这里把整份 ``Channel_Settings`` 存进 ``config_overrides`` 表
（独立的键，不进热参数），启动时叠回内存里的插件配置；静态配置照旧写一份，兼容旧版本读取。
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

STORE_KEY = "__channel_settings__"


def save_channel_settings(db: Any, settings: Mapping[str, Any], *, updated_by: str = "webui.channels") -> bool:
    repo = getattr(db, "config_overrides", None) if db is not None else None
    if repo is None:
        return False
    repo.set_many({STORE_KEY: copy.deepcopy(dict(settings))}, updated_by=updated_by)
    return True


def load_channel_settings(db: Any) -> dict[str, Any] | None:
    repo = getattr(db, "config_overrides", None) if db is not None else None
    if repo is None:
        return None
    value = repo.values().get(STORE_KEY)
    return copy.deepcopy(value) if isinstance(value, dict) else None


def restore_channel_settings(plugin_config: Any, db: Any) -> Any | None:
    """启动时把数据库里的通道配置叠回 ``plugin_config``，返回据此构建的通道配置；没存过返回 None。

    存的值无效时抛出 ValueError，插件配置保持不动。
    """
    stored = load_channel_settings(db)
    if stored is None:
        return None
    try:
        from .channel_config import build_channel_config_from_plugin_config
    except ImportError:  # pragma: no cover
        from services.config.channel_config import build_channel_config_from_plugin_config
    config = build_channel_config_from_plugin_config({**dict(plugin_config), "Channel_Settings": stored})
    plugin_config["Channel_Settings"] = stored
    return config


__all__ = ["STORE_KEY", "load_channel_settings", "restore_channel_settings", "save_channel_settings"]
