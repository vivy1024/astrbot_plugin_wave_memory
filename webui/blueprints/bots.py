"""Bot 管理 API：列出、新增、编辑、停用、导入导出 Bot Profile。

保存后注册表原地热重载（ScopeResolver、MetaThinking、好感引擎等同步刷新），
不需要重启 AstrBot。编辑必须带上读到的 ``version``，版本不一致返回 409。
"""

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

try:
    from domain.bot_profile import BINDING_HOSTS, BotProfile, BotProfileError
    from services.config.channel_config import apply_channel_overrides, build_default_channel_config
except ImportError:  # pragma: no cover - AstrBot 包导入路径
    from ...domain.bot_profile import BINDING_HOSTS, BotProfile, BotProfileError
    from ...services.config.channel_config import apply_channel_overrides, build_default_channel_config

from ..api_contract import error_payload

bots_bp = Blueprint("bots", __name__, url_prefix="/api")

_STATUS_BY_CODE = {
    "bot_not_found": 404,
    "version_conflict": 409,
    "binding_conflict": 409,
    "session_prefix_conflict": 409,
    "registry_not_attached": 503,
}


def _registry() -> Any:
    container = get_container()
    return getattr(container, "bot_registry", None) if container is not None else None


def _error(exc: BotProfileError):
    code = getattr(exc, "code", "invalid_profile")
    return jsonify(error_payload(code, str(exc))), _STATUS_BY_CODE.get(code, 400)


def _unavailable():
    return jsonify(error_payload("registry_unavailable", "Bot 注册表未就绪", retryable=True)), 503


def _item(profile: BotProfile) -> dict[str, Any]:
    data = profile.to_dict()
    data["self_ids"] = profile.self_ids
    data["identity_terms"] = profile.identity_terms
    return data


def _counts(db_id: str) -> dict[str, Any]:
    """该 Bot 名下的数据量；只做轻量统计，失败时返回空。"""
    db = getattr(get_container(), "db", None)
    if db is None:
        return {}
    out: dict[str, Any] = {}
    for key, sql in (
        ("user_profiles", "SELECT COUNT(*) FROM user_profiles WHERE bot_id=?"),
        ("experience_episodes", "SELECT COUNT(*) FROM experience_episodes WHERE bot_id=?"),
    ):
        try:
            row = db.conn.execute(sql, (db_id,)).fetchone()
            out[key] = int(row[0] if row else 0)
        except Exception:
            continue
    return out


@bots_bp.route("/bots", methods=["GET"])
@require_auth
async def list_bots():
    registry = _registry()
    if registry is None:
        return _unavailable()
    return jsonify({
        "items": [_item(p) for p in registry.all(include_disabled=True)],
        "status": registry.status(),
        "binding_hosts": list(BINDING_HOSTS),
    })


@bots_bp.route("/bots/<db_id>", methods=["GET"])
@require_auth
async def get_bot(db_id: str):
    registry = _registry()
    if registry is None:
        return _unavailable()
    profile = registry.get_any(db_id)
    if profile is None:
        return jsonify(error_payload("bot_not_found", f"没有 Bot {db_id!r}")), 404
    repo = getattr(registry, "_repo", None)
    history = repo.history(db_id, limit=10) if repo is not None else []
    return jsonify({"item": _item(profile), "counts": _counts(db_id), "history": history})


@bots_bp.route("/bots/<db_id>", methods=["PUT"])
@require_auth
async def save_bot(db_id: str):
    """新增（version=0）或更新（version=读到的版本）。"""
    registry = _registry()
    if registry is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    data = dict(body.get("item") or body)
    data["db_id"] = db_id
    expected = body.get("version", data.get("version"))
    try:
        expected_version = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_version", "version 必须是整数")), 400
    if expected_version is None:
        return jsonify(error_payload("version_required", "保存必须带上读到的 version（新建用 0）")), 400
    existing = registry.get_any(db_id)
    if existing is None:
        data.setdefault("origin", "webui")
    else:
        data.setdefault("origin", existing.origin)
    try:
        profile = BotProfile.from_dict(data)
        if profile.channels:
            # 通道覆盖在注入时严格校验，非法值会让该 Bot 的注入整体失败；保存时先拦住。
            try:
                apply_channel_overrides(build_default_channel_config(), {"channels": profile.channels})
            except ValueError as exc:
                return jsonify(error_payload("invalid_channels", f"通道覆盖不合法：{exc}")), 400
        saved = registry.save(
            profile,
            expected_version=expected_version,
            changed_by=str(body.get("changed_by") or "webui"),
            reason=str(body.get("reason") or ""),
        )
    except BotProfileError as exc:
        return _error(exc)
    return jsonify({"ok": True, "item": _item(saved), "status": registry.status()})


@bots_bp.route("/bots/<db_id>/enabled", methods=["POST"])
@require_auth
async def set_bot_enabled(db_id: str):
    registry = _registry()
    if registry is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        saved = registry.set_enabled(
            db_id,
            bool(body.get("enabled")),
            expected_version=int(body["version"]) if body.get("version") is not None else None,
            changed_by="webui",
        )
    except BotProfileError as exc:
        return _error(exc)
    return jsonify({"ok": True, "item": _item(saved)})


@bots_bp.route("/bots/export", methods=["GET"])
@require_auth
async def export_bots():
    registry = _registry()
    if registry is None:
        return _unavailable()
    return jsonify({"schema": "wavememory.bots/v2", "items": registry.export()})


@bots_bp.route("/bots/import", methods=["POST"])
@require_auth
async def import_bots():
    registry = _registry()
    if registry is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list):
        return jsonify(error_payload("invalid_payload", "items 必须是数组")), 400
    result = registry.import_profiles(items, changed_by="webui-import")
    return jsonify({"ok": not result["errors"], **result})


@bots_bp.route("/bots/reload", methods=["POST"])
@require_auth
async def reload_bots():
    registry = _registry()
    if registry is None:
        return _unavailable()
    revision = registry.reload()
    return jsonify({"ok": True, "revision": revision, "status": registry.status()})


__all__ = ["bots_bp"]
