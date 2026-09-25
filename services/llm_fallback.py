"""WaveMemory LLM 调用客户端 — 通过 AstrBot Provider 系统调用"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # 只用于标注宿主依赖面，运行时不导入
    from ..domain.host_ports import HostContext

logger = logging.getLogger("wavememory")


# 不可自愈错误：重试同一 provider 不会成功，必须跳到下一个渠道。
# 402 余额不足 / 401 鉴权失败 / 403 无权限 / 404 模型不存在。
_UNRECOVERABLE_STATUS = frozenset({401, 402, 403, 404})
# 可重试错误：上游抖动或限流，换渠道或稍后重试都可能成功。
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_STATUS_RE = re.compile(r"(?:error\s+code|status(?:\s+code)?|http)\D{0,3}(\d{3})", re.IGNORECASE)
_UNRECOVERABLE_HINTS = (
    "insufficient balance",
    "insufficient_quota",
    "exceeded your current quota",
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "model not found",
)


def extract_status_code(error: Any) -> Optional[int]:
    """从异常对象或其文本中提取 HTTP 状态码；无法判定时返回 None。"""
    for attr in ("status_code", "http_status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            number = int(value.strip())
            if 100 <= number <= 599:
                return number
    match = _STATUS_RE.search(str(error or ""))
    if match:
        number = int(match.group(1))
        if 100 <= number <= 599:
            return number
    return None


def is_unrecoverable_error(error: Any) -> bool:
    """判断该错误是否不可自愈（余额/鉴权/模型缺失），需要跳过当前 provider。"""
    status = extract_status_code(error)
    if status in _UNRECOVERABLE_STATUS:
        return True
    if status in _RETRYABLE_STATUS:
        return False
    text = str(error or "").lower()
    return any(hint in text for hint in _UNRECOVERABLE_HINTS)


def is_retryable_error(error: Any) -> bool:
    """判断该错误是否值得重试（上游不可用/限流/超时）。"""
    if is_unrecoverable_error(error):
        return False
    status = extract_status_code(error)
    if status in _RETRYABLE_STATUS:
        return True
    if status is not None:
        return False
    return isinstance(error, (TimeoutError, ConnectionError))


def describe_provider_error(provider_id: str, error: Any) -> str:
    """生成统一的 provider 失败描述，便于日志聚合与排障。"""
    status = extract_status_code(error)
    kind = "unrecoverable" if is_unrecoverable_error(error) else "retryable"
    status_text = f"status={status}" if status is not None else "status=unknown"
    return f"provider={provider_id!r} {status_text} kind={kind}: {error}"


class LLMResponse:
    """简单的 LLM 响应包装"""
    def __init__(self, text: str):
        self.completion_text = text


class LLMFallbackClient:
    """通过 AstrBot Provider 系统调用 LLM。

    支持 provider_id fallback 链：按顺序尝试，第一个成功即返回。
    """

    def __init__(self, context: HostContext, provider_ids: list[str] = None, *,
                 log_prefix: str = "[WaveMemory]", **kwargs):
        self.context = context
        self.provider_ids = [p for p in (provider_ids or []) if p]
        self.log_prefix = log_prefix

    async def text_chat(self, *, prompt: str, system_prompt: Optional[str] = None,
                        contexts: Optional[list] = None, **kwargs) -> LLMResponse:
        """通过 AstrBot provider 调用 LLM。"""
        if not self.provider_ids:
            raise RuntimeError(f"{self.log_prefix} No provider_ids configured")

        last_error = None
        for provider_id in self.provider_ids:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    continue

                # 构建 prompt（AstrBot provider 的 text_chat 接口）
                full_prompt = prompt
                if system_prompt:
                    full_prompt = f"{system_prompt}\n\n{prompt}"

                response = await provider.text_chat(
                    prompt=full_prompt,
                    contexts=contexts or [],
                )

                if response and response.completion_text:
                    return LLMResponse(response.completion_text)

            except Exception as e:
                last_error = e
                logger.warning(
                    "%s %s", self.log_prefix, describe_provider_error(provider_id, e)
                )
                continue

        if last_error:
            logger.error(f"{self.log_prefix} LLM 调用失败: {last_error}")
            raise last_error
        raise RuntimeError(f"{self.log_prefix} All providers failed")


async def call_first_available_provider(
    context: Any,
    provider_ids: list[str],
    *,
    log_prefix: str = "[WaveMemory]",
    **text_chat_kwargs: Any,
):
    """按链顺序调用 provider.text_chat，返回首个非空响应。

    与 ``LLMFallbackClient`` 的区别：这里把 ``system_prompt`` 等参数原样透传给
    provider，不做 prompt 拼接。依赖 system_prompt 约束输出格式的调用方
    （如记忆整合要求「只输出 JSON」）必须用这个函数。

    全链失败时抛出最后一个异常；链为空或全部 provider 不存在时抛 RuntimeError。
    """
    chain = [p for p in (provider_ids or []) if p and str(p).strip()]
    if not chain:
        raise RuntimeError(f"{log_prefix} No provider_ids configured")

    last_error: Optional[BaseException] = None
    missing: list[str] = []
    for provider_id in chain:
        provider = context.get_provider_by_id(provider_id) if context else None
        if not provider:
            missing.append(provider_id)
            continue
        try:
            response = await provider.text_chat(**text_chat_kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning("%s %s", log_prefix, describe_provider_error(provider_id, exc))
            continue
        if response and getattr(response, "completion_text", ""):
            return response
        logger.warning(
            "%s provider=%r returned empty completion; trying next",
            log_prefix,
            provider_id,
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"{log_prefix} No usable provider in chain {chain} (missing={missing})"
    )


# 辅助函数：从配置构建 provider_id 链
def parse_provider_ids(value) -> list[str]:
    """解析逗号分隔的 provider_id 字符串。"""
    if not value:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def provider_ids_from_config(config: dict, prefix: str = "provider_") -> list[str]:
    """从配置字典中按 prefix_1, prefix_2, ... 顺序提取 provider_id 列表。"""
    if not config:
        return []
    ids = []
    for i in range(1, 10):
        key = f"{prefix}{i}"
        val = config.get(key, "")
        if val and val.strip():
            ids.append(val.strip())
    return ids


def build_provider_chain(primary: str = "", fallback=None) -> list[str]:
    """构建 provider 优先级链：primary 在前，fallback 在后。"""
    chain = []
    if primary and primary.strip():
        chain.append(primary.strip())
    if fallback:
        if isinstance(fallback, str):
            chain.extend(parse_provider_ids(fallback))
        elif isinstance(fallback, list):
            chain.extend([f for f in fallback if f and f.strip()])
    return chain
