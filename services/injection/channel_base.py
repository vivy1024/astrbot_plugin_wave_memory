"""注入通道基础协议与结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


VALID_INJECTION_STATUSES = frozenset({"hit", "empty", "disabled", "skipped", "timeout", "error"})


def estimate_injection_tokens(text: str) -> int:
    """轻量 token 估算，避免基础数据结构依赖完整 perf 模块。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass
class InjectionResult:
    """单个注入通道的结构化输出。"""

    channel: str
    status: str
    text: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    filtered: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    latency_ms: float = 0.0
    tokens: int = 0
    chars: int = 0
    score: float | None = None
    # 仅用于正式 affinity 的全量印象摘要，不能被普通召回预算静默丢弃。
    preserve_full_text: bool = False

    def __post_init__(self) -> None:
        if self.status not in VALID_INJECTION_STATUSES:
            raise ValueError(f"invalid injection status: {self.status}")
        if not self.chars and self.text:
            self.chars = len(self.text)
        if not self.tokens and self.text:
            self.tokens = estimate_injection_tokens(self.text)

    @classmethod
    def hit(
        cls,
        channel: str,
        text: str,
        *,
        items: list[dict[str, Any]] | None = None,
        filtered: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        score: float | None = None,
        latency_ms: float = 0.0,
        preserve_full_text: bool = False,
    ) -> "InjectionResult":
        return cls(
            channel=channel,
            status="hit",
            text=text,
            items=items or [],
            filtered=filtered or [],
            warnings=warnings or [],
            score=score,
            latency_ms=latency_ms,
            preserve_full_text=preserve_full_text,
        )

    @classmethod
    def empty(cls, channel: str, *, latency_ms: float = 0.0, reason: str = "") -> "InjectionResult":
        warnings = [reason] if reason else []
        return cls(channel=channel, status="empty", warnings=warnings, latency_ms=latency_ms)

    @classmethod
    def disabled(cls, channel: str, *, reason: str) -> "InjectionResult":
        return cls(channel=channel, status="disabled", warnings=[reason])

    @classmethod
    def skipped(cls, channel: str, *, reason: str) -> "InjectionResult":
        return cls(channel=channel, status="skipped", warnings=[reason])

    @classmethod
    def timeout(cls, channel: str, *, timeout_ms: int | float) -> "InjectionResult":
        return cls(channel=channel, status="timeout", error=f"channel timed out after {timeout_ms}ms")

    @classmethod
    def error_result(cls, channel: str, exc: BaseException | str) -> "InjectionResult":
        return cls(channel=channel, status="error", error=str(exc))


@runtime_checkable
class InjectionChannel(Protocol):
    """注入通道接口。通道不得直接修改 ProviderRequest。"""

    name: str

    async def build(self, ctx: Any) -> InjectionResult:
        ...
