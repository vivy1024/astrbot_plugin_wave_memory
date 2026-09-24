"""注入请求上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

try:
    from ...domain.scope import RuntimeScope
except ImportError:  # 兼容独立测试/外部调用 services.injection
    from domain.scope import RuntimeScope


@dataclass(frozen=True)
class InjectionContext:
    """一次 `inject_memory` 调用的只读上下文。

    通道只读取该上下文并返回 `InjectionResult`，不得直接修改 ProviderRequest。
    """

    event: Any
    req: Any
    message: str
    group_id: str | None
    sender_id: str
    sender_name: str
    bot_id: str
    bot_profile_id: str
    scope: RuntimeScope | None = None
    recent_context: list[str] = field(default_factory=list)
    mode: str = "full"
    config: dict[str, Any] = field(default_factory=dict)
    channel_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    query_options: Any = None
    query_collector: Any = None
    dry_run: bool = False
    config_revision: str = ""
    config_provenance: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    now: float = 0.0
    trace_id: str = ""
    # 调用来源：astrbot=消息钩子；cortico 等=Runtime API。写进 trace，观测台可按来源筛选。
    source: str = "astrbot"
