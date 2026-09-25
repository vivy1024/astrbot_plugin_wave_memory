"""宿主端口：WaveMemory 核心对宿主（AstrBot）的全部依赖面。

核心代码只通过下面这些方法接触宿主；换宿主（或在 Cortico 侧直接嵌入）时实现这几组接口即可，
不需要改服务层。``scripts/boot_check.py`` 用真实 AstrBot 源码核对这些方法都存在，
AstrBot 升级改名时会第一时间报出来。

依赖面与使用方（v6 实测，``grep`` 可复核）：

- :class:`HostMessageEvent` —— 消息事件。``services/scopes.py`` 解析作用域（self/sender/platform/
  message_type/group/session），入站管线读正文与平台消息号。Runtime API 不经过它：观察写入用
  ``app/common._ObservationEvent``，工具调用用 ``services/tool_registry._RuntimeEvent``。
- :class:`HostLLMProvider` —— LLM 调用。``services/llm_fallback.py`` 按 provider 链调用 ``text_chat``。
- :class:`HostTool` —— 交给宿主的函数工具。``tools/*`` 的实例，同时被 Runtime API 直接调用。
- :class:`HostContext` —— 插件上下文。查 provider、登记/撤下工具（``app/startup.py``）、
  枚举平台实例取群名（``services/platform_context.py``）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HostMessageEvent(Protocol):
    def get_self_id(self) -> str: ...
    def get_sender_id(self) -> str: ...
    def get_platform_id(self) -> str: ...
    def get_message_type(self) -> Any: ...
    def get_group_id(self) -> str: ...
    def get_session_id(self) -> str: ...
    def get_message_str(self) -> str: ...


@runtime_checkable
class HostLLMProvider(Protocol):
    async def text_chat(self, **kwargs: Any) -> Any:
        """返回带 ``completion_text`` 的响应对象。"""
        ...


@runtime_checkable
class HostTool(Protocol):
    name: str
    description: str
    parameters: dict

    async def call(self, context: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class HostToolManager(Protocol):
    def remove_func(self, name: str) -> None: ...


@runtime_checkable
class HostContext(Protocol):
    def get_provider_by_id(self, provider_id: str) -> Any: ...
    def get_all_providers(self) -> list[Any]: ...
    def add_llm_tools(self, *tools: Any) -> None: ...
    def get_llm_tool_manager(self) -> HostToolManager: ...


# boot_check 用它逐个核对真实宿主类：端口名 → 必须存在的成员
PORT_MEMBERS: dict[str, tuple[str, ...]] = {
    "HostMessageEvent": (
        "get_self_id", "get_sender_id", "get_platform_id", "get_message_type",
        "get_group_id", "get_session_id", "get_message_str",
    ),
    "HostContext": ("get_provider_by_id", "get_all_providers", "add_llm_tools", "get_llm_tool_manager"),
    "HostToolManager": ("remove_func",),
    "HostLLMProvider": ("text_chat",),
}


def missing_members(cls: type, port: str) -> list[str]:
    """``cls`` 缺了端口 ``port`` 的哪些成员。"""
    return [name for name in PORT_MEMBERS[port] if not callable(getattr(cls, name, None))]


__all__ = [
    "HostContext",
    "HostLLMProvider",
    "HostMessageEvent",
    "HostTool",
    "HostToolManager",
    "PORT_MEMBERS",
    "missing_members",
]
