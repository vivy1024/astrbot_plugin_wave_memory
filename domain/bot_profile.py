"""Bot Profile v2：一个 Bot 的全部身份、绑定、人设与行为设置。

v5 之前 Bot 只能来自 AstrBot 静态配置里的 ``MetaThinking_Bot1/2`` 两个槽位，
人设细节（自称词、书设、日记署名、经历来源）写死在各个服务里。v6 起 Bot 存进
WaveMemory 自己的 ``bot_profiles`` 表，本模块只定义纯数据结构与序列化，不依赖
任何其他层。

兼容约定：
- ``db_id`` 是稳定主键，历史数据全部按它归属，永不改名。
- ``qq_id`` 仍是 AstrBot 主账号，旧代码按它查注册表；其他平台身份放在 ``bindings``。
- ``session_prefix`` 为空时沿用事件里的平台实例 id（v5 行为）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PROFILE_SCHEMA_VERSION = 2

# 绑定的宿主类型。astrbot=QQ 等 AstrBot 平台账号；cortico=Cortico 部署；bilibili=直播间。
BINDING_HOSTS = ("astrbot", "cortico", "bilibili")

# 不能作为 db_id 的保留值：会和库内哨兵、默认值混淆。
RESERVED_DB_IDS = frozenset({"bot", "default", "system", "all", "none"})


class BotProfileError(ValueError):
    """Profile 数据不合法；``code`` 供 API 返回稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set, frozenset)) else str(value).split(",")
    out: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "否", "关闭"}:
            return False
    return bool(value)


def _int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def validate_db_id(db_id: Any) -> str:
    text = _text(db_id)
    if not text:
        raise BotProfileError("db_id_required", "db_id 不能为空")
    if text.isdecimal():
        raise BotProfileError("invalid_db_id", "db_id 不能是纯数字（容易和 QQ 号混淆）")
    if text.casefold() in RESERVED_DB_IDS:
        raise BotProfileError("invalid_db_id", f"db_id 不能使用保留值 {text!r}")
    if len(text) > 64 or any(ch in text for ch in " :/\\\t\n"):
        raise BotProfileError("invalid_db_id", "db_id 只能用字母、数字、下划线、短横线，最长 64")
    return text


@dataclass
class BotBinding:
    """Bot 在某个宿主上的一个身份。"""

    host: str
    self_id: str = ""        # astrbot：平台账号（QQ 号）
    platform: str = ""       # astrbot：平台实例 id（可选，用于会话前缀映射和群名预取）
    deployment: str = ""     # cortico：部署名，如 yushu-live
    room: str = ""           # bilibili：直播间号

    def key(self) -> tuple[str, str]:
        if self.host == "astrbot":
            return ("astrbot", self.self_id)
        if self.host == "cortico":
            return ("cortico", self.deployment)
        return ("bilibili", self.room)

    @classmethod
    def from_dict(cls, data: Any) -> "BotBinding":
        if not isinstance(data, dict):
            raise BotProfileError("invalid_binding", "binding 必须是对象")
        host = _text(data.get("host")).lower()
        if host not in BINDING_HOSTS:
            raise BotProfileError("invalid_binding", f"未知宿主 {host!r}，可选 {', '.join(BINDING_HOSTS)}")
        binding = cls(
            host=host,
            self_id=_text(data.get("self_id")),
            platform=_text(data.get("platform")),
            deployment=_text(data.get("deployment")),
            room=_text(data.get("room")),
        )
        if not binding.key()[1]:
            required = {"astrbot": "self_id", "cortico": "deployment", "bilibili": "room"}[host]
            raise BotProfileError("invalid_binding", f"{host} 绑定缺少 {required}")
        return binding


@dataclass
class PersonaSettings:
    """人设里原本写死在代码中的部分。"""

    # 额外的自称词（名字、别名之外），用于身份安全检测和黑话过滤。
    self_terms: list[str] = field(default_factory=list)
    # 身份安全防护（防猫娘化、防认主）；关闭会放开防护，默认开启。
    identity_guard_enabled: bool = True
    # 追加到身份安全提示里的角色专属规则，每行一条。
    identity_guard_rules: list[str] = field(default_factory=list)
    # 日记署名，空则用显示名。
    diary_signature: str = ""
    # 常驻书设（Runtime 注入的灵魂底色），每行一条。
    lore_lines: list[str] = field(default_factory=list)
    # 书设标题，例如「羽书出自《某书》」。
    lore_title: str = ""
    # 第一人称经历记忆的 source 值；v5 历史数据使用 bzz_experience。
    experience_source: str = ""
    # 自省输出里需要剥掉的前缀（如「角色名：」），名字本身会自动加入。
    reflection_prefixes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "PersonaSettings":
        data = data if isinstance(data, dict) else {}
        return cls(
            self_terms=_text_list(data.get("self_terms")),
            identity_guard_enabled=_bool(data.get("identity_guard_enabled"), True),
            identity_guard_rules=_lines(data.get("identity_guard_rules")),
            diary_signature=_text(data.get("diary_signature")),
            lore_lines=_lines(data.get("lore_lines")),
            lore_title=_text(data.get("lore_title")),
            experience_source=_text(data.get("experience_source")),
            reflection_prefixes=_text_list(data.get("reflection_prefixes")),
        )


def _lines(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).splitlines()
    return [_text(item) for item in items if _text(item)]


@dataclass
class BotProfile:
    """配置驱动的 Bot 身份描述。

    前 10 个字段与 v5 的 ``BotProfile`` 同名同义，旧代码无需改动即可继续读取。
    """

    qq_id: str
    name: str
    db_id: str = ""
    aliases: list[str] = field(default_factory=list)
    meta_prompt: str = ""
    proactive_enabled: bool = True
    proactive_interval_seconds: int = 600
    proactive_max_per_hour: int = 3
    exclude_sources: list[str] = field(default_factory=list)
    interest_keywords: list[str] = field(default_factory=list)
    # ---- v6 ----
    enabled: bool = True
    model: str = ""
    session_prefix: str = ""
    bindings: list[BotBinding] = field(default_factory=list)
    persona: PersonaSettings = field(default_factory=PersonaSettings)
    # 注入通道覆盖：{通道名: {enabled/budget_tokens/...}}，叠加在全局通道配置之上。
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 工具开关：allow 非空时只开放列出的工具；deny 里的工具总是关闭。
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    version: int = 0
    origin: str = "config"   # config=从旧静态配置迁移；webui=在 9876 创建；import=导入

    @property
    def all_keywords(self) -> list[str]:
        """该 bot 的所有兴趣关键词（名字 + 别名 + 自定义词）。"""
        words = [self.name] + self.aliases + self.interest_keywords
        return [w for w in words if w]

    @property
    def identity_terms(self) -> list[str]:
        """名字、别名与额外自称词，去重保序。"""
        out: list[str] = []
        for word in [self.name, *self.aliases, *self.persona.self_terms]:
            text = _text(word)
            if text and text not in out:
                out.append(text)
        return out

    @property
    def experience_source(self) -> str:
        return self.persona.experience_source

    @property
    def self_ids(self) -> list[str]:
        """全部 AstrBot 平台账号（主账号在前）。"""
        out = [self.qq_id] if self.qq_id else []
        for binding in self.bindings:
            if binding.host == "astrbot" and binding.self_id and binding.self_id not in out:
                out.append(binding.self_id)
        return out

    @property
    def platform_ids(self) -> list[str]:
        """已知属于该 Bot 的 AstrBot 平台实例 id。"""
        out: list[str] = []
        for value in [self.session_prefix, *(b.platform for b in self.bindings if b.host == "astrbot"), self.name]:
            text = _text(value)
            if text and text not in out:
                out.append(text)
        return out

    def binding_for(self, host: str) -> list[BotBinding]:
        return [binding for binding in self.bindings if binding.host == host]

    def tool_enabled(self, tool_name: str) -> bool:
        if tool_name in self.tools_deny:
            return False
        return not self.tools_allow or tool_name in self.tools_allow

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = PROFILE_SCHEMA_VERSION
        return data

    @classmethod
    def from_dict(cls, data: Any, *, require_qq: bool = False) -> "BotProfile":
        if not isinstance(data, dict):
            raise BotProfileError("invalid_profile", "profile 必须是对象")
        db_id = validate_db_id(data.get("db_id"))
        name = _text(data.get("name"))
        if not name:
            raise BotProfileError("name_required", "显示名不能为空")
        qq_id = _text(data.get("qq_id"))
        if require_qq and not qq_id:
            raise BotProfileError("qq_id_required", "qq_id 不能为空")
        if qq_id and not qq_id.isdecimal():
            raise BotProfileError("invalid_qq_id", "qq_id 必须是纯数字账号")
        bindings: list[BotBinding] = []
        seen: set[tuple[str, str]] = set()
        for raw in data.get("bindings") or ():
            binding = BotBinding.from_dict(raw)
            if binding.key() in seen or binding.key() == ("astrbot", qq_id):
                continue
            seen.add(binding.key())
            bindings.append(binding)
        channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
        return cls(
            qq_id=qq_id,
            name=name,
            db_id=db_id,
            aliases=_text_list(data.get("aliases")),
            meta_prompt=_text(data.get("meta_prompt")),
            proactive_enabled=_bool(data.get("proactive_enabled"), True),
            proactive_interval_seconds=_int(data.get("proactive_interval_seconds"), 600, minimum=30),
            proactive_max_per_hour=_int(data.get("proactive_max_per_hour"), 3, minimum=0),
            exclude_sources=_text_list(data.get("exclude_sources")),
            interest_keywords=_text_list(data.get("interest_keywords")),
            enabled=_bool(data.get("enabled"), True),
            model=_text(data.get("model")),
            session_prefix=_text(data.get("session_prefix")),
            bindings=bindings,
            persona=PersonaSettings.from_dict(data.get("persona")),
            channels={str(k): dict(v) for k, v in channels.items() if isinstance(v, dict)},
            tools_allow=_text_list(data.get("tools_allow")),
            tools_deny=_text_list(data.get("tools_deny")),
            version=_int(data.get("version"), 0, minimum=0),
            origin=_text(data.get("origin")) or "config",
        )


def profile_from_legacy_config(cfg: Any) -> BotProfile:
    """解析 v5 的 ``MetaThinking_BotN`` 配置；缺 qq_id 或 db_id 时拒绝。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not _text(cfg.get("qq_id")) or not _text(cfg.get("db_id")):
        raise BotProfileError("legacy_incomplete", "BotProfile requires explicit qq_id and stable db_id")
    data = dict(cfg)
    data["origin"] = "config"
    return BotProfile.from_dict(data, require_qq=True)


__all__ = [
    "BINDING_HOSTS",
    "PROFILE_SCHEMA_VERSION",
    "BotBinding",
    "BotProfile",
    "BotProfileError",
    "PersonaSettings",
    "profile_from_legacy_config",
    "validate_db_id",
]
