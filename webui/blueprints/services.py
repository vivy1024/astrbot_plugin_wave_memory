"""服务与扩展 API：后台服务启停、工具开关、注入通道与扩展加载状态。"""

from __future__ import annotations

from typing import Any

try:
    from quart import Blueprint, jsonify, request
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时的轻量兜底
    class Blueprint:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    def jsonify(value=None, **kwargs):  # type: ignore[no-redef]
        return value if value is not None else kwargs

    request = None  # type: ignore[assignment]

try:
    from ..container import get_container
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover
    def get_container():  # type: ignore[no-redef]
        return None

    def require_auth(func):  # type: ignore[no-redef]
        return func

from ..api_contract import error_payload

services_bp = Blueprint("services", __name__, url_prefix="/api")

_SERVICE_STATUS = {"service_not_found": 404, "service_not_created": 409, "service_not_stoppable": 403}


def _container() -> Any:
    return get_container()


@services_bp.route("/services", methods=["GET"])
@require_auth
async def list_services():
    c = _container()
    services = getattr(c, "service_registry", None)
    tools = getattr(c, "tool_registry", None)
    channel_names = [getattr(ch, "name", "") for ch in (getattr(c, "injection_channels", None) or [])]
    return jsonify({
        "services": services.status() if services is not None else [],
        "services_available": services is not None,
        "tools": tools.describe() if tools is not None else [],
        "tool_registry": tools.status() if tools is not None else None,
        "channels": [name for name in channel_names if name],
    })


@services_bp.route("/services/<name>/<action>", methods=["POST"])
@require_auth
async def control_service(name: str, action: str):
    registry = getattr(_container(), "service_registry", None)
    if registry is None:
        return jsonify(error_payload("registry_unavailable", "服务注册表未就绪", retryable=True)), 503
    handler = {"start": registry.start, "stop": registry.stop, "restart": registry.restart}.get(action)
    if handler is None:
        return jsonify(error_payload("invalid_action", "action 只能是 start / stop / restart")), 400
    try:
        from ...services.service_registry import ServiceRegistryError
    except ImportError:  # pragma: no cover
        from services.service_registry import ServiceRegistryError
    try:
        item = await handler(name)
    except ServiceRegistryError as exc:
        return jsonify(error_payload(exc.code, str(exc))), _SERVICE_STATUS.get(exc.code, 400)
    except Exception as exc:
        return jsonify(error_payload("service_action_failed", f"{action} 失败: {exc}")), 500
    return jsonify({"ok": True, "item": item})


@services_bp.route("/tools/<name>/enabled", methods=["POST"])
@require_auth
async def set_tool_enabled(name: str):
    """停用后 AstrBot 与 Runtime 都不再提供该工具（AstrBot 侧按请求从 ToolSet 里移除）。"""
    registry = getattr(_container(), "tool_registry", None)
    if registry is None:
        return jsonify(error_payload("registry_unavailable", "工具注册表未就绪", retryable=True)), 503
    body = await request.get_json(silent=True) or {}
    try:
        record = registry.set_enabled(name, bool(body.get("enabled")))
    except ValueError as exc:
        return jsonify(error_payload(getattr(exc, "code", "tool_not_found"), str(exc))), 404
    return jsonify({"ok": True, "name": record.name, "enabled": record.enabled})


__all__ = ["services_bp"]
