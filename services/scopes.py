"""AstrBot event-boundary resolution into canonical RuntimeScope values."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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

    def resolve_event(self, event: Any) -> ResolvedEventContext:
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
        for index, self_id in enumerate(sid for sid in self_ids if sid):
            out.append(
                BotIdentityBinding(
                    self_id=self_id,
                    db_id=profile.db_id,
                    display_name=getattr(profile, "name", ""),
                    session_prefix=getattr(profile, "session_prefix", "") or "",
                    account_index=index,
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
