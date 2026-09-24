"""运行时 Bot 注册表：Bot 定义的唯一入口，支持热重载。

来源优先级：``bot_profiles`` 表 > AstrBot 静态配置里的旧槽位 ``MetaThinking_BotN``。
旧槽位只在两种情况下起作用：数据库还没接上时（插件构造阶段）作为临时值；
数据库里没有这个 db_id 时迁移写入一次。之后在 9876 上改的都以数据库为准。

``profiles`` 是按 AstrBot 主账号（qq_id）索引的字典，v5 的所有调用方都拿着它的
引用。重载时**原地**更新这个字典，已经拿到引用的模块自动看到新 Bot；需要重建
内部结构的模块（ScopeResolver、MetaThinking 等）通过 ``add_listener`` 订阅。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

try:
    from ..domain import bot_identity
    from ..domain.bot_profile import BotProfile, BotProfileError, profile_from_legacy_config
except ImportError:  # top-level import in isolated tests
    from domain import bot_identity
    from domain.bot_profile import BotProfile, BotProfileError, profile_from_legacy_config

logger = logging.getLogger("astrbot")

LEGACY_SLOT_PATTERN = re.compile(r"^MetaThinking_Bot\d+$")

Listener = Callable[["BotRegistry"], None]


def legacy_profiles_from_config(config: Mapping[str, Any] | None) -> list[BotProfile]:
    """解析 AstrBot 静态配置里全部 ``MetaThinking_BotN`` 槽位（按编号排序）。"""
    config = config or {}
    slots = sorted(
        (key for key in config if isinstance(key, str) and LEGACY_SLOT_PATTERN.match(key)),
        key=lambda key: int(key.rsplit("Bot", 1)[1]),
    )
    profiles: list[BotProfile] = []
    for key in slots:
        cfg = config.get(key) or {}
        if not isinstance(cfg, Mapping) or not str(cfg.get("qq_id") or "").strip():
            continue
        try:
            profiles.append(profile_from_legacy_config(dict(cfg)))
        except BotProfileError as exc:
            logger.error("[WaveMemory] ignored incomplete BotProfile %s: %s", key, exc)
    return profiles


class BotConflictError(BotProfileError):
    pass


class BotRegistry:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.profiles: dict[str, BotProfile] = {}
        # 已启用 Bot 按 db_id 索引，同样原地更新；没有 QQ 账号的 Bot（如只接 Cortico）只出现在这里。
        self.by_db_id: dict[str, BotProfile] = {}
        self._all: dict[str, BotProfile] = {}
        self._repo: Any = None
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._source = "config"
        self._legacy = legacy_profiles_from_config(config)
        self._migration_report: dict[str, Any] = {}
        self._apply(self._legacy)

    @classmethod
    def from_profiles(cls, profiles: Iterable[BotProfile]) -> "BotRegistry":
        """不接数据库的注册表（单测、离线工具用）。"""
        registry = cls(None)
        registry._legacy = list(profiles)
        registry._apply(registry._legacy)
        return registry

    # ------------------------------------------------------------------ 加载

    def attach(self, repo: Any, *, connection: Any = None) -> dict[str, Any]:
        """接上数据库：旧槽位里有、表里没有的 Bot 写入一次，然后从表加载。"""
        try:
            from ..engine.db.migrations.bot_profiles_v6 import legacy_profile_payload
        except ImportError:
            from engine.db.migrations.bot_profiles_v6 import legacy_profile_payload

        migrated: list[str] = []
        with self._lock:
            self._repo = repo
            for profile in self._legacy:
                if repo.get(profile.db_id) is not None:
                    continue
                payload = legacy_profile_payload(profile.to_dict(), connection)
                try:
                    repo.save(BotProfile.from_dict(payload), changed_by="migration", reason="v6 从静态配置迁移")
                    migrated.append(profile.db_id)
                except BotProfileError as exc:
                    logger.error("[WaveMemory] Bot %s 迁移失败: %s", profile.db_id, exc)
            self._migration_report = {"migrated": migrated}
            self.reload()
        if migrated:
            logger.info("[WaveMemory] Bot Profile 已从静态配置迁移: %s", ", ".join(migrated))
        return dict(self._migration_report)

    def reload(self) -> int:
        """从数据库重新加载并通知订阅者，返回身份快照版本号。"""
        with self._lock:
            if self._repo is None:
                return self._apply(self._legacy)
            profiles = self._repo.list(include_disabled=True)
            self._source = "database"
            revision = self._apply(profiles)
        self._notify()
        return revision

    def _apply(self, profiles: Iterable[BotProfile]) -> int:
        all_profiles: dict[str, BotProfile] = {}
        by_qq: dict[str, BotProfile] = {}
        for profile in profiles:
            if profile.db_id in all_profiles:
                continue
            all_profiles[profile.db_id] = profile
            if not profile.enabled:
                continue
            for self_id in profile.self_ids:
                by_qq.setdefault(self_id, profile)
        self._all = all_profiles
        # 原地更新：v5 调用方持有的是这个字典的引用。
        self.profiles.clear()
        self.profiles.update(by_qq)
        enabled = [p for p in all_profiles.values() if p.enabled]
        self.by_db_id.clear()
        self.by_db_id.update({p.db_id: p for p in enabled})
        return bot_identity.publish(
            sender_ids=[sid for p in enabled for sid in p.self_ids],
            identity_terms=[term for p in enabled for term in p.identity_terms],
            experience_sources=[p.experience_source for p in enabled if p.experience_source],
        )

    # ------------------------------------------------------------------ 订阅

    def add_listener(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self)
            except Exception as exc:  # 单个订阅者失败不影响其他模块
                logger.warning("[WaveMemory] Bot 注册表重载通知失败 %s: %s", getattr(listener, "__qualname__", listener), exc)

    # ------------------------------------------------------------------ 查询

    @property
    def source(self) -> str:
        return self._source

    @property
    def attached(self) -> bool:
        return self._repo is not None

    def all(self, *, include_disabled: bool = False) -> list[BotProfile]:
        return [p for p in self._all.values() if include_disabled or p.enabled]

    def get(self, db_id: str) -> BotProfile | None:
        profile = self._all.get(str(db_id or "").strip())
        return profile if profile and profile.enabled else None

    def get_any(self, db_id: str) -> BotProfile | None:
        return self._all.get(str(db_id or "").strip())

    def by_self_id(self, self_id: str) -> BotProfile | None:
        return self.profiles.get(str(self_id or "").strip())

    def by_platform(self, platform_id: str) -> BotProfile | None:
        target = str(platform_id or "").strip()
        if not target:
            return None
        matches = [p for p in self.all() if target in p.platform_ids]
        return matches[0] if len(matches) == 1 else None

    def by_binding(self, host: str, value: str) -> BotProfile | None:
        target = str(value or "").strip()
        if not target:
            return None
        for profile in self.all():
            for binding in profile.binding_for(host):
                if binding.key()[1] == target:
                    return profile
        return None

    def resolve(self, *, bot_id: str = "", deployment: str = "", room: str = "", self_id: str = "") -> BotProfile | None:
        """按调用方给出的任一身份找 Bot；都找不到返回 None（调用方必须拒绝请求）。"""
        if bot_id:
            return self.get(bot_id)
        if deployment:
            return self.by_binding("cortico", deployment)
        if room:
            return self.by_binding("bilibili", room)
        if self_id:
            return self.by_self_id(self_id)
        return None

    def primary(self) -> BotProfile | None:
        enabled = self.all()
        return enabled[0] if enabled else None

    # ------------------------------------------------------------------ 修改

    def _check_conflicts(self, profile: BotProfile) -> None:
        for other in self.all(include_disabled=False):
            if other.db_id == profile.db_id or not profile.enabled:
                continue
            shared_ids = set(other.self_ids) & set(profile.self_ids)
            if shared_ids:
                raise BotConflictError("binding_conflict", f"账号 {', '.join(sorted(shared_ids))} 已绑定到 {other.db_id}")
            other_keys = {b.key() for b in other.bindings if b.host != "astrbot"}
            shared_keys = other_keys & {b.key() for b in profile.bindings if b.host != "astrbot"}
            if shared_keys:
                host, value = sorted(shared_keys)[0]
                raise BotConflictError("binding_conflict", f"{host} 绑定 {value} 已属于 {other.db_id}")
            if profile.session_prefix and profile.session_prefix == other.session_prefix:
                raise BotConflictError("session_prefix_conflict", f"会话前缀 {profile.session_prefix!r} 已被 {other.db_id} 使用")

    def save(self, profile: BotProfile, *, expected_version: int | None, changed_by: str = "", reason: str = "") -> BotProfile:
        if self._repo is None:
            raise BotProfileError("registry_not_attached", "数据库尚未就绪，暂时不能修改 Bot")
        with self._lock:
            self._check_conflicts(profile)
            saved = self._repo.save(profile, expected_version=expected_version, changed_by=changed_by, reason=reason)
        self.reload()
        return saved

    def set_enabled(self, db_id: str, enabled: bool, *, expected_version: int | None, changed_by: str = "") -> BotProfile:
        current = self.get_any(db_id)
        if current is None:
            raise BotProfileError("bot_not_found", f"没有 Bot {db_id!r}")
        data = current.to_dict()
        data["enabled"] = bool(enabled)
        return self.save(
            BotProfile.from_dict(data),
            expected_version=expected_version,
            changed_by=changed_by,
            reason="启用" if enabled else "停用",
        )

    def export(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.all(include_disabled=True)]

    def import_profiles(self, items: Iterable[Any], *, changed_by: str = "") -> dict[str, Any]:
        """逐个导入；已存在的 Bot 覆盖（不做版本检查），失败的单独报告。"""
        imported: list[str] = []
        errors: dict[str, str] = {}
        for raw in items or ():
            key = str((raw or {}).get("db_id") if isinstance(raw, dict) else "?")
            try:
                data = dict(raw)
                data["origin"] = data.get("origin") or "import"
                profile = BotProfile.from_dict(data)
                with self._lock:
                    self._check_conflicts(profile)
                    self._repo.save(profile, expected_version=None, changed_by=changed_by, reason="导入")
                imported.append(profile.db_id)
            except (BotProfileError, TypeError, ValueError) as exc:
                errors[key] = str(exc)
        if imported:
            self.reload()
        return {"imported": imported, "errors": errors}

    # ------------------------------------------------------------------ 诊断

    def status(self) -> dict[str, Any]:
        invalid = self._repo.invalid_rows() if self._repo is not None and hasattr(self._repo, "invalid_rows") else {}
        return {
            "source": self._source,
            "attached": self.attached,
            "revision": bot_identity.revision(),
            "enabled": len(self.all()),
            "total": len(self._all),
            "legacy_slots": [p.db_id for p in self._legacy],
            "migration": dict(self._migration_report),
            "invalid_rows": invalid,
        }


__all__ = ["BotConflictError", "BotRegistry", "legacy_profiles_from_config"]
