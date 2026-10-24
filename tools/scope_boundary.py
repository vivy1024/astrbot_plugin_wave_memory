"""LLM 工具的 canonical Scope 边界。

工具只能复用事件入口已经解析并附着的 RuntimeScope；这里绝不从
``group_id``、Bot 显示名或其他 legacy 字段重新推断身份与会话。
"""

from __future__ import annotations

from typing import Any

try:
    from ..domain.scope import CatalogScope, RuntimeScope, ScopeCodec, validate_formal_command_scope
except ImportError:  # pragma: no cover - direct tools imports in isolated tests
    from domain.scope import CatalogScope, RuntimeScope, ScopeCodec, validate_formal_command_scope


# 插件持有的唯一 canonical ScopeResolver（app/bootstrap.py::_rebuild_scope_resolver 注入，
# 插件卸载时清空）。只用于“事件没经过 on_message”的场景：AstrBot 定时任务 / 后台任务
# 回调构造的 CronMessageEvent 直接进 Agent，工具拿不到入口附着的 RuntimeScope。
_EVENT_SCOPE_RESOLVER: Any = None


def set_event_scope_resolver(resolver: Any) -> None:
    """登记（或用 None 清除）工具回退解析用的 canonical ScopeResolver。"""
    global _EVENT_SCOPE_RESOLVER
    _EVENT_SCOPE_RESOLVER = resolver


def _resolve_unattached_event(event: Any) -> RuntimeScope | None:
    """事件未附着 Scope 时，用与 on_message / after_message_sent 同一个 resolver 解析。

    只接受 resolver 返回的 RuntimeScope；解析失败一律 None（fail closed），绝不从
    group_id 等 legacy 字段自行拼作用域。成功后缓存到事件上，同一轮后续工具直接复用。
    """
    resolver = _EVENT_SCOPE_RESOLVER
    resolve = getattr(resolver, "resolve_event", None)
    if not callable(resolve):
        return None
    try:
        scope = getattr(resolve(event), "scope", None)
    except Exception:
        return None
    if not isinstance(scope, RuntimeScope):
        return None
    try:
        setattr(event, "_wave_memory_runtime_scope", scope)
    except Exception:
        pass
    return scope


def extract_event_runtime_scope(context: Any) -> RuntimeScope | None:
    """返回事件入口已经解析的 RuntimeScope，缺失或类型不符时 fail closed。"""
    try:
        event = getattr(getattr(context, "context", None), "event", None)
        scope = getattr(event, "_wave_memory_runtime_scope", None) if event else None
    except Exception:
        return None
    if isinstance(scope, RuntimeScope):
        return scope
    if event is None or scope is not None:
        # 已附着但类型不对的值是脏数据，不覆盖、不重解析。
        return None
    return _resolve_unattached_event(event)


def extract_group_runtime_scope(context: Any) -> RuntimeScope | None:
    """仅允许带真实 session 的群聊 RuntimeScope。"""
    scope = extract_event_runtime_scope(context)
    if scope is None or scope.visibility != "group" or scope.session is None:
        return None
    return scope


def extract_memory_runtime_scope(context: Any) -> RuntimeScope | None:
    """返回 WaveMemory 基础工具允许的 group/private RuntimeScope。"""
    scope = extract_event_runtime_scope(context)
    if scope is None or scope.visibility not in {"group", "private"} or scope.session is None:
        return None
    if scope.session.kind != scope.visibility or not scope.bot_id or not scope.session.id:
        return None
    if not scope.session.conversation_id:
        return None
    return scope


def require_memory_runtime_scope(context: Any, command_type: str) -> tuple[RuntimeScope | None, str | None]:
    """提取基础记忆工具的 group/private Scope；不降级到 legacy group_id。"""
    scope = extract_memory_runtime_scope(context)
    if scope is None:
        return None, "memory_scope_required"
    # The formal matrix is the source of truth: it now grants private access
    # only to approved base-memory commands and keeps every derived group domain
    # fail-closed.
    decision = validate_formal_command_scope(command_type, scope)
    if not decision.allowed:
        return None, decision.reason_code or "scope_rejected"
    return scope, None


def require_group_runtime_scope(context: Any, command_type: str) -> tuple[RuntimeScope | None, str | None]:
    """提取并校验 formal command 的群聊 RuntimeScope。"""
    scope = extract_group_runtime_scope(context)
    if scope is None:
        return None, "scope_required"
    decision = validate_formal_command_scope(command_type, scope)
    if not decision.allowed:
        return None, decision.reason_code or "scope_rejected"
    return scope, None


def require_catalog_scope(scope: Any, command_type: str) -> tuple[CatalogScope | None, str | None]:
    """校验工具构造时显式注入的 CatalogScope。"""
    if not isinstance(scope, CatalogScope):
        return None, "catalog_scope_required"
    decision = validate_formal_command_scope(command_type, scope)
    if not decision.allowed:
        return None, decision.reason_code or "scope_rejected"
    return scope, None


def scope_envelope(scope: RuntimeScope | CatalogScope) -> dict[str, Any]:
    """Serialize an already validated scope without deriving legacy fields."""
    return ScopeCodec.to_dict(scope)


def scope_error_message(action: str, reason_code: str) -> str:
    """统一、用户可读且含稳定 reason code 的 fail-closed 返回值。"""
    labels = {
        "scope_required": "当前事件没有已解析的群聊作用域",
        "memory_scope_required": "当前事件没有已解析的记忆作用域",
        "scope_mismatch": "目标不属于当前作用域",
        "legacy_scope_unresolved": "旧数据没有可验证的 canonical 作用域",
        "scope_migration_required": "该工具依赖的 legacy 数据尚未完成 Scope 迁移",
        "catalog_scope_required": "工具没有注入可验证的 CatalogScope",
    }
    detail = labels.get(reason_code, "当前作用域不允许此操作")
    message = f"{detail}，已拒绝{action}（{reason_code}）"
    if reason_code in {"memory_scope_required", "scope_required"}:
        message += "。" + NO_CHAT_CONTEXT_GUIDANCE
    return message


# 拒绝返回给模型的可操作说明：定时/后台运行拿不到记忆时，模型曾改用 shell/python 直连
# 线上 SQLite，触发 WAL 被删、库损坏（2026-10-21、10-23 两次事故）。
NO_CHAT_CONTEXT_GUIDANCE = (
    "这通常是没有聊天上下文的定时任务或后台运行，本轮 WaveMemory 记忆工具不可用。"
    "不要用 shell、python、文件读写或任何其他方式直接读取记忆数据库文件"
    "（wave_memory.db、data_v4.db 及其 -wal/-shm），那会损坏正在运行的数据库；"
    "请直接基于已有信息完成任务，需要时如实说明这次拿不到记忆。"
)


def require_read_runtime_scope(context: Any, command_type: str) -> tuple[RuntimeScope | None, str | None]:
    """提取只读检索类工具的 group/private Scope。"""
    return require_memory_runtime_scope(context, command_type)


def resolve_source_memory_id(
    db: Any,
    scope: RuntimeScope,
    *,
    quote: str | None = None,
    explicit_id: Any = None,
    sender_id: str | None = None,
) -> int | None:
    """自动溯源当前 RuntimeScope 范围内的有效记忆 ID。

    优先级：
    1. 显式指定的合法正整数 explicit_id（经由同 Scope 校验）；
    2. 若提供原话 quote（>=2字符），在当前 Scope 的健康 memories 中反查包含该原话的最新记录；
    3. 若指定了 sender_id，在当前 Scope 中查找该 sender_id 最新一条健康记忆；
    4. 兜底回退：当前 Scope 内最新一条未隔离的健康消息记忆（即当轮触发对话）。
    """
    conn = getattr(db, "conn", None) or getattr(db, "_conn", None) or db
    if conn is None or scope is None or scope.session is None:
        return None

    # 1. 显式 ID 优先采用
    if explicit_id not in (None, ""):
        try:
            eid = int(explicit_id)
            if eid > 0:
                return eid
        except Exception:
            pass

    # 2. 按原话反查
    clean_quote = str(quote or "").strip()
    if len(clean_quote) >= 2:
        try:
            safe_quote = clean_quote.replace("/", "//").replace("%", "/%").replace("_", "/_")
            row = conn.execute(
                "SELECT id FROM memories WHERE bot_id=? AND session_id=? AND visibility=? "
                "AND content LIKE ? ESCAPE '/' AND resolution_state='resolved' AND COALESCE(quarantine,0)=0 "
                "ORDER BY timestamp DESC LIMIT 1",
                (scope.bot_id, scope.session.id, scope.visibility, f"%{safe_quote}%"),
            ).fetchone()
            if row:
                return int(row[0])
        except Exception:
            pass

    # 3. 按发言人反查最新消息
    if sender_id:
        try:
            row = conn.execute(
                "SELECT id FROM memories WHERE bot_id=? AND session_id=? AND visibility=? "
                "AND (sender_id=? OR sender_name=?) AND resolution_state='resolved' AND COALESCE(quarantine,0)=0 "
                "ORDER BY timestamp DESC LIMIT 1",
                (scope.bot_id, scope.session.id, scope.visibility, str(sender_id), str(sender_id)),
            ).fetchone()
            if row:
                return int(row[0])
        except Exception:
            pass

    # 4. 兜底：当前 Scope 最新一条有效消息记录
    try:
        row = conn.execute(
            "SELECT id FROM memories WHERE bot_id=? AND session_id=? AND visibility=? "
            "AND resolution_state='resolved' AND COALESCE(quarantine,0)=0 "
            "ORDER BY timestamp DESC LIMIT 1",
            (scope.bot_id, scope.session.id, scope.visibility),
        ).fetchone()
        if row:
            return int(row[0])
    except Exception:
        pass

    return None


__all__ = [
    "extract_event_runtime_scope",
    "extract_group_runtime_scope",
    "extract_memory_runtime_scope",
    "require_memory_runtime_scope",
    "require_read_runtime_scope",
    "require_catalog_scope",
    "require_group_runtime_scope",
    "resolve_source_memory_id",
    "scope_envelope",
    "scope_error_message",
    "set_event_scope_resolver",
    "NO_CHAT_CONTEXT_GUIDANCE",
]
