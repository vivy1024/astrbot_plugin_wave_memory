"""AstrBot event-boundary resolution into canonical RuntimeScope values."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只用于标注宿主依赖面，运行时不导入
    from ..domain.host_ports import HostMessageEvent

try:  # package import in AstrBot runtime
    from ..domain.scope import RuntimeScope, ScopeValidationError, SessionRef
except ImportError:  # top-level import in isolated tests
    from domain.scope import RuntimeScope, ScopeValidationError, SessionRef


class ScopeResolutionError(ScopeValidationError):
    """Fail-closed event resolution error with a stable ``reason_code``."""


@dataclass(frozen=True)
class BotIdentityBinding:
    self_id: str
    db_id: str
    display_name: str = ""
    # 规范会话前缀：非空时替代事件里的平台实例 id，平台改名后新旧记忆仍是同一条线。
    session_prefix: str = ""
    # 同一个 Bot 的第几个平台账号；同 db_id 的多个账号必须编号不同，否则视为配置歧义。
    account_index: int = 0
    # 已知属于该 Bot 的 AstrBot 平台实例 id（session_prefix + astrbot 绑定里显式写的 platform）。
    # 只给没有真实 self_id 的 AstrBot 定时任务事件（CronMessageEvent）认 Bot 用。
    host_platforms: tuple[str, ...] = ()
    # 这个账号自己在 astrbot 绑定里写明的平台实例 id；同一 Bot 多账号时优先选它。
    account_platform: str = ""

    def __post_init__(self) -> None:
        self_id = _stripped(self.self_id)
        db_id = _stripped(self.db_id)
        display_name = _stripped(self.display_name)
        if not self_id:
            raise ScopeResolutionError("unknown_bot_self_id", "binding self_id is empty")
        if not db_id or db_id.isdecimal() or db_id.casefold() in {"bot", "default"}:
            raise ScopeResolutionError(
                "invalid_bot_id",
                "binding db_id must be a stable BotProfile.db_id",
            )
        object.__setattr__(self, "self_id", self_id)
        object.__setattr__(self, "db_id", db_id)
        object.__setattr__(self, "display_name", display_name)
        prefix = _stripped(self.session_prefix)
        if prefix and (":" in prefix or any(ch.isspace() for ch in prefix)):
            raise ScopeResolutionError("invalid_session_prefix", "session_prefix must not contain ':' or whitespace")
        object.__setattr__(self, "session_prefix", prefix)
        platforms: list[str] = []
        for value in self.host_platforms or ():
            text = _stripped(value)
            if text and text not in platforms:
                platforms.append(text)
        object.__setattr__(self, "host_platforms", tuple(platforms))
        object.__setattr__(self, "account_platform", _stripped(self.account_platform))


@dataclass(frozen=True)
class ResolvedEventContext:
    scope: RuntimeScope
    bot_self_id: str
    bot_name: str
    sender_local_id: str
    conversation_local_id: str
    # 事件原始的 AstrBot 平台实例 id（发消息、取群列表要用它，而不是规范前缀）。
    host_platform_id: str = ""


class ScopeResolver:
    """Resolve one live AstrBot event using an exact self-id registry binding.

    The resolver intentionally uses only AstrBot's structured event methods. It
    never splits a legacy ``group:*``/``private:*`` string and never scans by
    display name, QQ-like db_id, or a default bot sentinel.
    """

    def __init__(self, bindings: Iterable[BotIdentityBinding]) -> None:
        by_self_id: dict[str, BotIdentityBinding] = {}
        db_ids: set[tuple[str, int]] = set()
        duplicate_ids: set[str] = set()
        try:
            supplied = tuple(bindings)
        except TypeError as exc:
            raise ScopeResolutionError(
                "ambiguous_bot_registry",
                "bindings must be an iterable of BotIdentityBinding values",
            ) from exc
        for binding in supplied:
            if not isinstance(binding, BotIdentityBinding):
                raise ScopeResolutionError(
                    "ambiguous_bot_registry",
                    "registry contains a non-BotIdentityBinding value",
                )
            if binding.self_id in by_self_id:
                duplicate_ids.add(f"self_id:{binding.self_id}")
            else:
                by_self_id[binding.self_id] = binding
            db_key = (binding.db_id, int(binding.account_index))
            if db_key in db_ids:
                duplicate_ids.add(f"db_id:{binding.db_id}")
            db_ids.add(db_key)
        if duplicate_ids:
            raise ScopeResolutionError(
                "ambiguous_bot_registry",
                f"duplicate Bot registry identifiers: {sorted(duplicate_ids)!r}",
            )
        self._bindings = by_self_id

    @property
    def bindings(self) -> tuple[BotIdentityBinding, ...]:
        return tuple(self._bindings.values())

    def resolve_event(self, event: HostMessageEvent) -> ResolvedEventContext:
        if _is_cron_event(event):
            return self._resolve_cron_event(event)
        self_id = _event_string(event, "get_self_id", "unknown_bot_self_id")
        binding = self._bindings.get(self_id)
        if not self_id or binding is None:
            raise ScopeResolutionError(
                "unknown_bot_self_id",
                "event self_id has no exact Bot registry binding",
            )

        # Resolution order is part of the stable failure contract.
        sender_id = _event_string(event, "get_sender_id", "sender_id_required")
        if not sender_id:
            raise ScopeResolutionError(
                "sender_id_required",
                "event.get_sender_id() did not return a sender id",
            )

        platform_id = _event_string(event, "get_platform_id", "platform_unresolved")
        if not platform_id:
            raise ScopeResolutionError(
                "platform_unresolved",
                "event.get_platform_id() did not return a platform id",
            )

        message_type = _event_value(event, "get_message_type", "chat_kind_unresolved")
        message_type_value = getattr(message_type, "value", message_type)
        if message_type_value == "GroupMessage":
            kind = "group"
        elif message_type_value == "FriendMessage":
            kind = "private"
        else:
            raise ScopeResolutionError(
                "chat_kind_unresolved",
                "event message type is not an exact group or private chat type",
            )

        if kind == "group":
            conversation_id = _event_string(
                event,
                "get_group_id",
                "conversation_id_required",
            )
            if not conversation_id:
                raise ScopeResolutionError(
                    "conversation_id_required",
                    "group event requires event.get_group_id()",
                )
        else:
            # Private conversation identity comes from AstrBot's structured
            # get_session_id() contract. Never parse unified_msg_origin/raw text.
            conversation_id = _event_string(
                event,
                "get_session_id",
                "conversation_id_required",
            )
            if not conversation_id:
                raise ScopeResolutionError(
                    "conversation_id_required",
                    "private event requires event.get_session_id()",
                )

        host_platform_id = platform_id
        platform_id = binding.session_prefix or platform_id
        session = SessionRef(
            id=f"{platform_id}:{kind}:{conversation_id}",
            platform_id=platform_id,
            kind=kind,
            conversation_id=conversation_id,
        )
        scope = RuntimeScope(
            bot_id=binding.db_id,
            visibility=kind,
            session=session,
            subject_principal_id=f"{platform_id}:user:{sender_id}",
        )
        return ResolvedEventContext(
            scope=scope,
            bot_self_id=binding.self_id,
            bot_name=binding.display_name,
            sender_local_id=sender_id,
            conversation_local_id=conversation_id,
            host_platform_id=host_platform_id,
        )


    def _resolve_cron_event(self, event: Any) -> ResolvedEventContext:
        """解析 AstrBot 定时任务 / 后台任务回调的合成事件（CronMessageEvent）。

        CronMessageEvent 没有真实的 Bot self_id（固定为 ``astrbot``）、没有 group 对象，
        发送者 id 被设成会话 id。它唯一可信的信息是 AstrBot 自己的结构化会话：
        ``get_platform_id()``（平台实例）、``get_message_type()`` 与 ``get_session_id()``，
        三者就是 unified_msg_origin 的三段，也是该任务把结果投递回去的会话。

        - Bot 只按平台实例匹配：必须恰好命中一个 Bot（session_prefix 或 astrbot 绑定里显式写的
          platform），不按显示名、不回退默认 Bot；
        - 私聊会话 id 即对方账号，与真实私聊事件的 get_session_id() 同口径，主体就是对方；
        - 群聊没有对话对象，主体留空（需要 subject 的写工具会 fail closed）；
        - unique_session 形式的群会话（``用户_群``）无法确定群号，直接拒绝。
        """
        platform_id = _event_string(event, "get_platform_id", "platform_unresolved")
        if not platform_id or platform_id == "cron":
            # payload 没写 session 时 AstrBot 用 cron:OtherMessage:<job_id>，没有聊天上下文。
            raise ScopeResolutionError(
                "cron_session_unbound",
                "cron event has no delivery chat session",
            )
        candidates = [binding for binding in self._bindings.values() if platform_id in binding.host_platforms]
        db_ids = {binding.db_id for binding in candidates}
        if not candidates:
            raise ScopeResolutionError(
                "unknown_bot_platform",
                "cron event platform has no exact Bot registry binding",
            )
        if len(db_ids) != 1:
            raise ScopeResolutionError(
                "ambiguous_bot_platform",
                "cron event platform is bound to more than one Bot",
            )
        binding = min(
            candidates,
            key=lambda item: (item.account_platform != platform_id, item.account_index),
        )

        message_type = _event_value(event, "get_message_type", "chat_kind_unresolved")
        message_type_value = getattr(message_type, "value", message_type)
        if message_type_value == "GroupMessage":
            kind = "group"
        elif message_type_value == "FriendMessage":
            kind = "private"
        else:
            raise ScopeResolutionError(
                "chat_kind_unresolved",
                "cron event message type is not an exact group or private chat type",
            )
        conversation_id = _event_string(event, "get_session_id", "conversation_id_required")
        if not conversation_id or ":" in conversation_id or any(ch.isspace() for ch in conversation_id):
            raise ScopeResolutionError(
                "conversation_id_required",
                "cron event requires a plain session id",
            )
        if kind == "group" and "_" in conversation_id:
            raise ScopeResolutionError(
                "cron_session_ambiguous",
                "cron group session looks like a unique_session id; group id is unknown",
            )

        host_platform_id = platform_id
        session_platform = binding.session_prefix or platform_id
        session = SessionRef(
            id=f"{session_platform}:{kind}:{conversation_id}",
            platform_id=session_platform,
            kind=kind,
            conversation_id=conversation_id,
        )
        sender_id = conversation_id if kind == "private" else ""
        scope = RuntimeScope(
            bot_id=binding.db_id,
            visibility=kind,
            session=session,
            subject_principal_id=f"{session_platform}:user:{sender_id}" if sender_id else None,
        )
        return ResolvedEventContext(
            scope=scope,
            bot_self_id=binding.self_id,
            bot_name=binding.display_name,
            sender_local_id=sender_id,
            conversation_local_id=conversation_id,
            host_platform_id=host_platform_id,
        )


def _is_cron_event(event: Any) -> bool:
    """AstrBot 的 CronMessageEvent 固定使用平台类型名 ``cron``（真实适配器不会用这个名字）。"""
    method = getattr(event, "get_platform_name", None)
    if not callable(method):
        return False
    try:
        return _stripped(method()) == "cron"
    except Exception:
        return False


def _event_value(event: Any, method_name: str, reason_code: str) -> Any:
    method = getattr(event, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception as exc:
        raise ScopeResolutionError(
            reason_code,
            f"event.{method_name}() failed",
        ) from exc


def _event_string(event: Any, method_name: str, reason_code: str) -> str:
    value = _event_value(event, method_name, reason_code)
    return _stripped(value)


def _stripped(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def bindings_from_profiles(profiles: Iterable[Any]) -> list[BotIdentityBinding]:
    """把 BotRegistry 里已启用的 Profile 展开成 ScopeResolver 绑定（每个平台账号一条）。"""
    out: list[BotIdentityBinding] = []
    for profile in profiles:
        if not getattr(profile, "enabled", True) or not getattr(profile, "db_id", ""):
            continue
        self_ids = list(getattr(profile, "self_ids", None) or [getattr(profile, "qq_id", "")])
        # 平台实例只取显式配置（会话前缀 + astrbot 绑定的 platform），不拿显示名凑数。
        host_platforms = tuple(
            value
            for value in [
                getattr(profile, "session_prefix", "") or "",
                *(
                    getattr(binding, "platform", "") or ""
                    for binding in (getattr(profile, "bindings", None) or [])
                    if getattr(binding, "host", "") == "astrbot"
                ),
            ]
            if value
        )
        account_platforms: dict[str, str] = {}
        for binding in getattr(profile, "bindings", None) or []:
            if getattr(binding, "host", "") == "astrbot" and getattr(binding, "self_id", ""):
                account_platforms.setdefault(binding.self_id, getattr(binding, "platform", "") or "")
        for index, self_id in enumerate(sid for sid in self_ids if sid):
            out.append(
                BotIdentityBinding(
                    self_id=self_id,
                    db_id=profile.db_id,
                    display_name=getattr(profile, "name", ""),
                    session_prefix=getattr(profile, "session_prefix", "") or "",
                    account_index=index,
                    host_platforms=host_platforms,
                    account_platform=account_platforms.get(self_id, ""),
                )
            )
    return out


__all__ = [
    "bindings_from_profiles",
    "BotIdentityBinding",
    "ResolvedEventContext",
    "ScopeResolutionError",
    "ScopeResolver",
]
