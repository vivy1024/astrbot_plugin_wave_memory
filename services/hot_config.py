"""Wave Memory 热配置 — 运行时可调参数管理"""

from __future__ import annotations

import threading
from typing import Any, Callable

from astrbot.api import logger


class HotConfig:
    """热配置单例：支持运行时修改参数并通知订阅者。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, initial_config: dict = None):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._config: dict = {}
        self._callbacks: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()

        if initial_config:
            self._config.update(initial_config)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值。支持点号分隔的嵌套键。"""
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def update(self, updates: dict) -> dict:
        """批量更新配置并通知订阅者。返回更新后的完整配置。"""
        with self._lock:
            changed = {}
            for key, value in updates.items():
                old = self.get(key)
                if old != value:
                    self._set_nested(key, value)
                    changed[key] = {"old": old, "new": value}

        if changed:
            logger.info(f"[WaveMemory] HotConfig updated: {list(changed.keys())}")
            self._notify(changed)

        return self._config.copy()

    def on_change(self, callback: Callable[[dict], None]):
        """注册变更回调。callback 接收 {key: {"old": ..., "new": ...}}。"""
        self._callbacks.append(callback)

    def get_all(self) -> dict:
        """获取所有配置。"""
        return self._config.copy()

    def get_tunable_params(self) -> list[dict]:
        """返回可调参数列表（含范围信息）。"""
        return [
            {"key": "spike.firing_threshold", "type": "float", "min": 0.01, "max": 0.5, "default": 0.10,
             "description": "脉冲发射阈值"},
            {"key": "spike.base_decay", "type": "float", "min": 0.1, "max": 0.5, "default": 0.25,
             "description": "常规传播衰减"},
            {"key": "spike.wormhole_decay", "type": "float", "min": 0.3, "max": 0.9, "default": 0.70,
             "description": "虫洞传播衰减"},
            {"key": "spike.tension_threshold", "type": "float", "min": 0.3, "max": 2.0, "default": 1.0,
             "description": "虫洞张力阈值"},
            {"key": "spike.max_hops", "type": "int", "min": 1, "max": 8, "default": 4,
             "description": "最大传播跳数"},
            {"key": "query.min_similarity", "type": "float", "min": 0.1, "max": 0.8, "default": 0.35,
             "description": "最低相似度阈值"},
            {"key": "query.boost_alpha_base", "type": "float", "min": 0.1, "max": 0.6, "default": 0.3,
             "description": "浪潮增强基础因子"},
            {"key": "geodesic.energy_weight", "type": "float", "min": 0.0, "max": 1.0, "default": 0.3,
             "description": "测地线能量权重"},
            {"key": "residual.boost_range", "type": "float", "min": 0.0, "max": 1.0, "default": 0.6,
             "description": "残差增益范围"},
        ]

    def _set_nested(self, key: str, value: Any):
        """设置嵌套键值。"""
        keys = key.split(".")
        d = self._config
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    def _notify(self, changed: dict):
        """通知所有订阅者。"""
        for callback in self._callbacks:
            try:
                callback(changed)
            except Exception as e:
                logger.warning(f"[WaveMemory] HotConfig callback error: {e}")

    @classmethod
    def reset(cls):
        """重置单例（仅用于测试）。"""
        cls._instance = None
