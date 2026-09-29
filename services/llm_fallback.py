"""WaveMemory LLM 调用客户端 — 通过 AstrBot Provider 系统调用"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
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
    """简单的 LLM 响应包装；provider_id 是实际答出这次的渠道（便于记录「所用模型」）。"""
    def __init__(self, text: str, provider_id: str = ""):
        self.completion_text = text
        self.provider_id = provider_id


_MAX_ERROR_TEXT = 300
_MAX_MISSING_TRACKED = 8


class LLMCallStats:
    """单个任务的调用统计（本次启动以来，只在内存里）。

    一次 ``text_chat`` 记一次调用：链上任一 provider 返回非空即成功；整条链都失败
    （或链为空）记一次失败。链上某个 provider 报错但后面的顶上了，只计入
    ``provider_errors``，不算失败。在 AstrBot 主循环与 9876 线程之间共享，用锁保护。
    """

    def __init__(self, task: str = "") -> None:
        self.task = task
        self._lock = threading.Lock()
        self.calls = 0
        self.succeeded = 0
        self.failed = 0
        self.provider_errors = 0
        self.last_provider = ""
        self.last_success_at: float | None = None
        self.last_error = ""
        self.last_error_provider = ""
        self.last_error_at: float | None = None
        self.missing: list[str] = []

    def begin(self) -> None:
        with self._lock:
            self.calls += 1

    def succeed(self, provider_id: str) -> None:
        with self._lock:
            self.succeeded += 1
            self.last_provider = str(provider_id)
            self.last_success_at = time.time()

    def provider_error(self, provider_id: str, error: Any) -> None:
        with self._lock:
            self.provider_errors += 1
            self.last_error = describe_provider_error(provider_id, error)[:_MAX_ERROR_TEXT]
            self.last_error_provider = str(provider_id)
            self.last_error_at = time.time()

    def provider_missing(self, provider_id: str) -> None:
        with self._lock:
            if provider_id not in self.missing:
                self.missing.append(str(provider_id))
                del self.missing[:-_MAX_MISSING_TRACKED]

    def fail(self, error: Any, *, from_provider: bool = False) -> None:
        """整条链失败。``from_provider``：错误就是链上最后一个 provider 抛的，
        :meth:`provider_error` 已记下带状态码的描述，这里不覆盖。"""
        with self._lock:
            self.failed += 1
            if not from_provider:
                self.last_error = (str(error or "") or type(error).__name__)[:_MAX_ERROR_TEXT]
                self.last_error_provider = ""
            self.last_error_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "provider_errors": self.provider_errors,
                "last_provider": self.last_provider,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "last_error_provider": self.last_error_provider,
                "last_error_at": self.last_error_at,
                "missing_providers": list(self.missing),
            }


ChainResolver = Callable[[Optional[str]], list[str]]


def _clean_chain(provider_ids: Any) -> list[str]:
    return [p for p in (provider_ids or []) if p]


class LLMFallbackClient:
    """通过 AstrBot Provider 系统调用 LLM。

    支持 provider_id fallback 链：按顺序尝试，第一个成功即返回。

    两种用法：
    - 固定链：``provider_ids`` 在构造时给定，可用 :meth:`set_provider_ids` 原地替换；
    - 路由链：传 ``chain_resolver(bot_id) -> list[str]``（由 ``services.llm_routing`` 提供），
      每次调用都按当前模型分工解析，9876 保存模型配置后无需重建客户端。
      :meth:`for_bot` 返回绑定某个 Bot 的客户端，用于允许按 Bot 覆盖模型的任务。
    """

    def __init__(self, context: HostContext, provider_ids: list[str] = None, *,
                 log_prefix: str = "[WaveMemory]",
                 chain_resolver: ChainResolver | None = None,
                 stats: LLMCallStats | None = None,
                 bot_id: str | None = None,
                 **kwargs):
        self.context = context
        self._provider_ids = _clean_chain(provider_ids)
        self._chain_resolver = chain_resolver
        self._bot_id = bot_id or None
        self.stats = stats
        self.log_prefix = log_prefix
        self.last_provider_id = ""

    @property
    def provider_ids(self) -> list[str]:
        """当前生效的回退链（路由客户端每次读取都按最新模型分工解析）。"""
        if self._chain_resolver is not None:
            try:
                return _clean_chain(self._chain_resolver(self._bot_id))
            except Exception as exc:  # 路由解析失败不能让调用崩溃，退回构造时的链
                logger.warning("%s 模型分工解析失败，使用构造时的回退链: %s", self.log_prefix, exc)
        return list(self._provider_ids)

    @provider_ids.setter
    def provider_ids(self, value: list[str]) -> None:
        self.set_provider_ids(value)

    def set_provider_ids(self, provider_ids: list[str]) -> None:
        """原地替换为固定回退链（同时取消路由解析）。"""
        self._provider_ids = _clean_chain(provider_ids)
        self._chain_resolver = None

    @property
    def routed(self) -> bool:
        return self._chain_resolver is not None

    @property
    def bot_id(self) -> str | None:
        return self._bot_id

    def for_bot(self, bot_id: str | None) -> "LLMFallbackClient":
        """绑定 Bot 的客户端：只对路由客户端有意义，固定链客户端原样返回自己。"""
        if self._chain_resolver is None or not bot_id or bot_id == self._bot_id:
            return self
        return LLMFallbackClient(
            self.context,
            self._provider_ids,
            log_prefix=self.log_prefix,
            chain_resolver=self._chain_resolver,
            stats=self.stats,
            bot_id=str(bot_id),
        )

    async def text_chat(self, *, prompt: str, system_prompt: Optional[str] = None,
                        contexts: Optional[list] = None, **kwargs) -> LLMResponse:
        """通过 AstrBot provider 调用 LLM。"""
        chain = self.provider_ids
        stats = self.stats
        if stats is not None:
            stats.begin()
        if not chain:
            error = RuntimeError(f"{self.log_prefix} No provider_ids configured")
            if stats is not None:
                stats.fail(error)
            raise error

        last_error = None
        for provider_id in chain:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    if stats is not None:
                        stats.provider_missing(provider_id)
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
                    self.last_provider_id = provider_id
                    if stats is not None:
                        stats.succeed(provider_id)
                    return LLMResponse(response.completion_text, provider_id=provider_id)

            except Exception as e:
                last_error = e
                if stats is not None:
                    stats.provider_error(provider_id, e)
                logger.warning(
                    "%s %s", self.log_prefix, describe_provider_error(provider_id, e)
                )
                continue

        if last_error:
            logger.error(f"{self.log_prefix} LLM 调用失败: {last_error}")
            if stats is not None:
                stats.fail(last_error, from_provider=True)
            raise last_error
        error = RuntimeError(f"{self.log_prefix} All providers failed")
        if stats is not None:
            stats.fail(error)
        raise error


async def call_first_available_provider(
    context: Any,
    provider_ids: list[str],
    *,
    log_prefix: str = "[WaveMemory]",
    stats: LLMCallStats | None = None,
    **text_chat_kwargs: Any,
):
    """按链顺序调用 provider.text_chat，返回首个非空响应。

    与 ``LLMFallbackClient`` 的区别：这里把 ``system_prompt`` 等参数原样透传给
    provider，不做 prompt 拼接。依赖 system_prompt 约束输出格式的调用方
    （如记忆整合要求「只输出 JSON」）必须用这个函数。

    全链失败时抛出最后一个异常；链为空或全部 provider 不存在时抛 RuntimeError。
    ``stats`` 可选，用于模型分工页展示调用/失败次数。
    """
    if stats is not None:
        stats.begin()
    chain = [p for p in (provider_ids or []) if p and str(p).strip()]
    if not chain:
        error = RuntimeError(f"{log_prefix} No provider_ids configured")
        if stats is not None:
            stats.fail(error)
        raise error

    last_error: Optional[BaseException] = None
    missing: list[str] = []
    for provider_id in chain:
        provider = context.get_provider_by_id(provider_id) if context else None
        if not provider:
            missing.append(provider_id)
            if stats is not None:
                stats.provider_missing(provider_id)
            continue
        try:
            response = await provider.text_chat(**text_chat_kwargs)
        except Exception as exc:
            last_error = exc
            if stats is not None:
                stats.provider_error(provider_id, exc)
            logger.warning("%s %s", log_prefix, describe_provider_error(provider_id, exc))
            continue
        if response and getattr(response, "completion_text", ""):
            if stats is not None:
                stats.succeed(provider_id)
            return response
        logger.warning(
            "%s provider=%r returned empty completion; trying next",
            log_prefix,
            provider_id,
        )

    if last_error is not None:
        if stats is not None:
            stats.fail(last_error, from_provider=True)
        raise last_error
    error = RuntimeError(
        f"{log_prefix} No usable provider in chain {chain} (missing={missing})"
    )
    if stats is not None:
        stats.fail(error)
    raise error


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
