"""可选的 provider-backed Soul Context 适配层。

Scoped Soul 只消费正式 provider 返回的值；没有 provider、provider 返回空数据或
provider 失败时，统一返回 unavailable，绝不填充伪造的默认时区/精力/困倦值。
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, Protocol

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - top-level repository imports
    from domain.scope import RuntimeScope


class SoulContextProvider(Protocol):
    """运行时 Soul Context provider 的最小契约。"""

    def get_soul_context(
        self,
        *,
        scope: RuntimeScope,
        now: float | None = None,
    ) -> Mapping[str, Any]: ...


def unavailable_soul_context(reason_code: str = "formal_soul_context_unavailable") -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason_code": reason_code,
        "timezone": None,
        "circadian": None,
        "energy": None,
        "sleepiness": None,
    }


def _finite_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field}_invalid")
    return number


def normalize_soul_context(value: Mapping[str, Any], *, captured_at: float | None = None) -> dict[str, Any]:
    """校验 provider 输出并限制为前端/审计可见的稳定字段。"""
    if not isinstance(value, Mapping):
        raise ValueError("soul_context_invalid")
    if str(value.get("status") or "available") == "unavailable":
        return unavailable_soul_context(str(value.get("reason_code") or "soul_context_unavailable"))

    timezone = value.get("timezone")
    if timezone is not None and (not isinstance(timezone, str) or not timezone.strip()):
        raise ValueError("timezone_invalid")
    circadian = value.get("circadian")
    if circadian is not None and not isinstance(circadian, Mapping):
        raise ValueError("circadian_invalid")
    energy = _finite_number(value.get("energy"), "energy")
    sleepiness = _finite_number(value.get("sleepiness"), "sleepiness")
    source = value.get("source") or value.get("provider")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise ValueError("source_invalid")
    mind_state = value.get("mind_state")
    if mind_state is not None and not isinstance(mind_state, Mapping):
        mind_state = None

    has_value = any(item is not None for item in (timezone, circadian, energy, sleepiness, mind_state))
    if not has_value:
        return unavailable_soul_context("soul_context_empty")
    result: dict[str, Any] = {
        "status": "available",
        "reason_code": None,
        "timezone": timezone,
        "circadian": dict(circadian) if isinstance(circadian, Mapping) else None,
        "energy": energy,
        "sleepiness": sleepiness,
    }
    if mind_state is not None:
        result["mind_state"] = dict(mind_state)
    if source:
        result["source"] = source.strip()
    if captured_at is not None:
        result["captured_at"] = float(captured_at)
    return result


def resolve_soul_context(
    provider: SoulContextProvider | Any | None,
    *,
    scope: RuntimeScope,
    now: float | None = None,
) -> dict[str, Any]:
    """从 provider 获取当前 Scope 的 Soul Context；失败时 fail-closed。"""
    if provider is None:
        return unavailable_soul_context()
    captured_at = float(now if now is not None else time.time())
    try:
        if hasattr(provider, "get_soul_context"):
            raw = provider.get_soul_context(scope=scope, now=captured_at)
        elif hasattr(provider, "provide"):
            raw = provider.provide(scope=scope, now=captured_at)
        elif callable(provider):
            raw = provider(scope=scope, now=captured_at)
        else:
            return unavailable_soul_context("soul_context_provider_invalid")
        return normalize_soul_context(raw, captured_at=captured_at)
    except Exception:
        return unavailable_soul_context("soul_context_provider_error")


__all__ = [
    "SoulContextProvider",
    "normalize_soul_context",
    "resolve_soul_context",
    "unavailable_soul_context",
]
