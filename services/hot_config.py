"""Wave Memory 热配置 — 运行时可调参数管理"""

from __future__ import annotations

import threading
from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:  # 单测与独立运行时没有 AstrBot
    import logging

    logger = logging.getLogger("astrbot")


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
        _register_specs(self)

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
            {"key": "query.group_weight_current", "type": "float", "min": 1.0, "max": 3.0, "default": 1.5,
             "description": "当前群记忆权重（越高越优先当前群）"},
            {"key": "query.group_weight_cross", "type": "float", "min": 0.1, "max": 1.0, "default": 0.8,
             "description": "跨群记忆权重（越低越降权跨群内容）"},
            {"key": "geodesic.energy_weight", "type": "float", "min": 0.0, "max": 1.0, "default": 0.3,
             "description": "测地线能量权重"},
            {"key": "residual.boost_range", "type": "float", "min": 0.0, "max": 1.0, "default": 0.6,
             "description": "残差增益范围"},
            {"key": "social.abuse_trigger_count", "type": "int", "min": 1, "max": 10, "default": 3,
             "description": "辱骂触发冷却次数"},
            {"key": "social.abuse_cooldown_base", "type": "int", "min": 60, "max": 3600, "default": 600,
             "description": "辱骂冷却起步秒数"},
            {"key": "social.abuse_cooldown_max", "type": "int", "min": 600, "max": 7200, "default": 3600,
             "description": "辱骂冷却上限秒数"},
            {"key": "social.aba_window_seconds", "type": "int", "min": 10, "max": 120, "default": 30,
             "description": "ABA连续对话窗口（秒）"},
            # v6：原来写死在代码里的常量
            {"key": "ingress.debounce_seconds", "type": "float", "min": 0.5, "max": 15.0, "default": 4.0,
             "description": "同一人连发消息的合并等待（秒）：这段时间内没有新消息就合并成一条处理"},
            {"key": "ingress.debounce_max_seconds", "type": "float", "min": 2.0, "max": 60.0, "default": 12.0,
             "description": "消息合并最长等待（秒）：从第一条算起，到点强制截断"},
            {"key": "injection.slow_warning_ms", "type": "int", "min": 200, "max": 20000, "default": 2000,
             "description": "注入总耗时超过这个值时记警告日志（毫秒）"},
            {"key": "tags.admission_neighbor_limit", "type": "int", "min": 0, "max": 512, "default": 64,
             "description": "打标签判重时比对的目录邻居条数（0 = 不比对）"},
            {"key": "tags.admission_cache_seconds", "type": "int", "min": 0, "max": 1800, "default": 60,
             "description": "目录邻居缓存秒数（0 = 不缓存）"},
            {"key": "storage.active_count_cache_seconds", "type": "int", "min": 60, "max": 86400, "default": 1800,
             "description": "活跃记忆计数缓存秒数：数一次要读约 1.5GB，越长越省磁盘"},
            {"key": "jargon.person_alias_cache_seconds", "type": "int", "min": 0, "max": 3600, "default": 300,
             "description": "群友名单缓存秒数（黑话判重不再每次扫全群）"},
            {"key": "people.nickname_cache_seconds", "type": "int", "min": 0, "max": 3600, "default": 300,
             "description": "按昵称找人的结果缓存秒数（含未命中的名字）"},
            {"key": "webui.stats_cache_seconds", "type": "int", "min": 0, "max": 3600, "default": 15,
             "description": "管理台总览统计缓存秒数"},
            {"key": "webui.vector_count_cache_seconds", "type": "int", "min": 0, "max": 86400, "default": 600,
             "description": "管理台带向量记忆数缓存秒数（读取时约 3 秒）"},
            {"key": "webui.coverage_cache_seconds", "type": "int", "min": 0, "max": 86400, "default": 300,
             "description": "标签覆盖率报告缓存秒数（冷读约 12 秒）"},
            {"key": "webui.legacy_quality_cache_seconds", "type": "int", "min": 0, "max": 86400, "default": 300,
             "description": "旧口径标签质量统计缓存秒数（只在覆盖率报告失败时用到）"},
            {"key": "webui.list_count_cache_seconds", "type": "int", "min": 0, "max": 86400, "default": 300,
             "description": "记忆列表总数缓存秒数"},
            {"key": "webui.list_count_wait_seconds", "type": "float", "min": 0.0, "max": 30.0, "default": 1.5,
             "description": "记忆列表等待总数的上限（秒）：超过就先出列表，总数后台算完后补上"},
        ]

    def tunable_keys(self) -> set[str]:
        return {item["key"] for item in self.get_tunable_params()}

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


def hot_value(key: str, default: Any = None) -> Any:
    """读热参数，按注册表的类型与上下限钳制；没有实例时返回默认值。

    深层模块（注入通道、工具、管理台接口）用这个，不要自己去 new 一个实例。
    """
    inst = HotConfig._instance
    if inst is None:
        return default
    spec = _SPEC_BY_KEY.get(key)
    value = inst.get(key, default)
    if spec is None or value is None:
        return value
    try:
        if spec["type"] == "int":
            value = int(value)
        elif spec["type"] == "float":
            value = float(value)
        elif spec["type"] == "bool":
            value = bool(value)
        else:
            value = str(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
    return value


_SPEC_BY_KEY: dict[str, dict] = {}


def _register_specs(instance: "HotConfig") -> None:
    _SPEC_BY_KEY.clear()
    for item in instance.get_tunable_params():
        _SPEC_BY_KEY[item["key"]] = item
