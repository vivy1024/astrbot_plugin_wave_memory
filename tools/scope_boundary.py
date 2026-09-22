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


def extract_event_runtime_scope(context: Any) -> RuntimeScope | None:
    """返回事件入口已经解析的 RuntimeScope，缺失或类型不符时 fail closed。"""
    try:
        event = getattr(getattr(context, "context", None), "event", None)
        scope = getattr(event, "_wave_memory_runtime_scope", None) if event else None
    except Exception:
        return None
    return scope if isinstance(scope, RuntimeScope) else None


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
    return f"{detail}，已拒绝{action}（{reason_code}）"


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
]
