"""游戏知识库 API：游戏库、知识卡、词条的增改与启用 / 归档，导入导出，记忆表旧知识点迁移，试匹配，
以及各 Bot 自己玩出来的经验卡（查看、删除 / 恢复、「现在想一次」）。

写入全部委托 ``services.game_knowledge.GameKnowledgeService`` 与 ``GameLessonService``（本文件不写 SQL）；
编辑带 ``version``，版本对不上返回 409。批量启用 / 归档可带 ``versions``（逐条核对，对不上的列在 conflicts 里）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from quart import Blueprint, jsonify, request

from ..api_contract import error_payload, mutation_response
from ..container import get_container
from ..middleware.auth import require_auth

try:
    from ...domain.game_knowledge import (
        SOURCE_TYPE_LABELS,
        STATUS_LABELS,
        TRIGGER_RULE_TEXT,
        GameKnowledgeConflict,
        GameKnowledgeError,
        parse_conditions,
    )
    from ...engine.db.game_knowledge_repo import IMPORT_MODES
    from ...services.game_knowledge.lessons import restore_lesson, veto_lesson
except ImportError:  # pragma: no cover - 插件根目录直接导入
    from domain.game_knowledge import (
        SOURCE_TYPE_LABELS,
        STATUS_LABELS,
        TRIGGER_RULE_TEXT,
        GameKnowledgeConflict,
        GameKnowledgeError,
        parse_conditions,
    )
    from engine.db.game_knowledge_repo import IMPORT_MODES
    from services.game_knowledge.lessons import restore_lesson, veto_lesson

game_knowledge_bp = Blueprint("game_knowledge", __name__, url_prefix="/api/game-knowledge")

_KINDS = {"cards": "card", "terms": "term"}


def _service() -> Any:
    return getattr(get_container(), "game_knowledge", None)


def _lessons() -> Any:
    return getattr(get_container(), "game_lessons", None)


def _bots() -> list[dict[str, str]]:
    registry = getattr(get_container(), "bot_registry", None)
    listing = getattr(registry, "all", None)
    if not callable(listing):
        return []
    bots = []
    for profile in listing():
        db_id = str(getattr(profile, "db_id", "") or "")
        if db_id:
            bots.append({"id": db_id, "name": str(getattr(profile, "name", "") or db_id)})
    return bots


def _owner(raw: Any) -> str | None:
    """列表的 ``owner``：shared（默认，共用的攻略）/ all / 某个 Bot 的标识（它自己的经验卡）。"""
    text = str(raw or "shared").strip()
    if text == "all":
        return None
    return "" if text == "shared" else text


def _unavailable():
    return jsonify(error_payload("game_knowledge_unavailable", "游戏知识库未就绪（看启动日志里的「游戏知识库初始化失败」）", retryable=True)), 503


def _error(exc: Exception):
    code = str(getattr(exc, "code", "") or "game_knowledge_failed")
    if code.endswith("_not_found"):
        status = 404
    elif isinstance(exc, GameKnowledgeConflict) or code.endswith("_conflict") or code.endswith("_exists"):
        status = 409
    else:
        status = 400
    payload = error_payload(code, str(exc))
    if isinstance(exc, GameKnowledgeConflict) and exc.actual is not None:
        payload["error"]["current_version"] = exc.actual
    return jsonify(payload), status


def _condition_texts(item: dict[str, Any]) -> list[str]:
    try:
        return [condition.text() for condition in parse_conditions(item.get("conditions"))]
    except GameKnowledgeError:
        return [str(raw) for raw in item.get("conditions") or []]


def _card_payload(card: dict[str, Any]) -> dict[str, Any]:
    keywords, conditions = card.get("keywords") or [], card.get("conditions") or []
    trigger_mode = "keywords" if keywords else ("conditions" if conditions else "none")
    return {
        **card,
        "conditions_text": _condition_texts(card),
        "trigger_mode": trigger_mode,
        "experience": bool(card.get("bot_id")) and card.get("source_type") == "experience",
    }


def _item_payload(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return _card_payload(item) if kind == "card" else dict(item)


def _version(body: dict[str, Any]) -> int | None:
    value = body.get("version")
    if value is None or value == "":
        return None
    return int(value)


def _statuses(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return [part for part in str(raw or "").split(",") if part.strip()]


@game_knowledge_bp.route("/overview", methods=["GET"])
@require_auth
async def overview():
    """游戏库列表（带各状态计数）、可迁移的记忆表旧知识点、Bot 列表与游戏经验是否开启。"""
    service = _service()
    if service is None:
        return _unavailable()
    try:
        games = await asyncio.to_thread(service.list_games)
        legacy = await asyncio.to_thread(service.legacy_sources)
    except Exception as exc:
        return _error(exc)
    lessons = _lessons()
    return jsonify({
        "games": games,
        "legacy_sources": legacy,
        "bots": _bots(),
        "lessons_enabled": bool(lessons is not None and getattr(lessons, "enabled", False)),
        "statuses": STATUS_LABELS,
        "source_types": SOURCE_TYPE_LABELS,
        "import_modes": list(IMPORT_MODES),
        "trigger_rule": TRIGGER_RULE_TEXT,
    })


@game_knowledge_bp.route("/games", methods=["POST"])
@require_auth
async def create_game():
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        game = service.create_game(dict(body.get("item") or body))
    except Exception as exc:
        return _error(exc)
    return jsonify(mutation_response(operation_kind="game_knowledge.game.create", status="succeeded", revision=game["version"], item=game, include_item=True))


@game_knowledge_bp.route("/games/<game_id>", methods=["GET"])
@require_auth
async def get_game(game_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    game = service.get_game(game_id)
    if game is None:
        return jsonify(error_payload("game_not_found", f"没有游戏库 {game_id}")), 404
    return jsonify({"item": game, "stages": service.stage_facets(game["id"])})


@game_knowledge_bp.route("/games/<game_id>", methods=["PUT"])
@require_auth
async def update_game(game_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        expected = _version(body)
        if expected is None:
            raise GameKnowledgeError("version_required", "修改游戏库要带上读到的 version")
        game = service.update_game(game_id, {k: v for k, v in body.items() if k != "version"}, expected_version=expected)
    except Exception as exc:
        return _error(exc)
    return jsonify(mutation_response(operation_kind="game_knowledge.game.update", status="succeeded", revision=game["version"], item=game, include_item=True))


@game_knowledge_bp.route("/games/<game_id>/<any(cards, terms):kind>", methods=["GET"])
@require_auth
async def list_items(game_id: str, kind: str):
    service = _service()
    if service is None:
        return _unavailable()
    args = request.args
    try:
        stage = args.get("stage")
        page = service.list_items(
            _KINDS[kind],
            game_id,
            status=_statuses(args.get("status")) or None,
            stage=stage if stage is not None and stage != "__all__" else None,
            query=str(args.get("q") or ""),
            source_type=str(args.get("source_type") or "") or None,
            limit=int(args.get("limit") or 50),
            offset=int(args.get("offset") or 0),
            **({"bot_id": _owner(args.get("owner"))} if kind == "cards" else {}),
        )
    except Exception as exc:
        return _error(exc)
    page["items"] = [_item_payload(_KINDS[kind], item) for item in page["items"]]
    return jsonify(page)


@game_knowledge_bp.route("/games/<game_id>/<any(cards, terms):kind>/<int:item_id>", methods=["GET"])
@require_auth
async def get_item(game_id: str, kind: str, item_id: int):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        item = service.get_item(_KINDS[kind], game_id, item_id)
    except Exception as exc:
        return _error(exc)
    if item is None:
        return jsonify(error_payload(f"{_KINDS[kind]}_not_found", "找不到这个条目")), 404
    return jsonify({"item": _item_payload(_KINDS[kind], item)})


@game_knowledge_bp.route("/games/<game_id>/<any(cards, terms):kind>", methods=["POST"])
@require_auth
async def create_item(game_id: str, kind: str):
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        item = service.create_item(_KINDS[kind], game_id, dict(body.get("item") or body))
    except Exception as exc:
        return _error(exc)
    return jsonify(mutation_response(
        operation_kind=f"game_knowledge.{_KINDS[kind]}.create", status="succeeded", revision=item["version"],
        item=_item_payload(_KINDS[kind], item), include_item=True,
    ))


@game_knowledge_bp.route("/games/<game_id>/<any(cards, terms):kind>/<int:item_id>", methods=["PUT"])
@require_auth
async def update_item(game_id: str, kind: str, item_id: int):
    """编辑（带 version）；版本对不上 409，返回当前版本号。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    payload = dict(body.get("item") or body)
    try:
        expected = _version(body) if body.get("version") is not None else _version(payload)
        if expected is None:
            raise GameKnowledgeError("version_required", "编辑要带上读到的 version")
        payload.pop("version", None)
        item = service.update_item(_KINDS[kind], game_id, item_id, payload, expected_version=expected)
    except Exception as exc:
        return _error(exc)
    return jsonify(mutation_response(
        operation_kind=f"game_knowledge.{_KINDS[kind]}.update", status="succeeded", revision=item["version"],
        item=_item_payload(_KINDS[kind], item), include_item=True,
    ))


@game_knowledge_bp.route("/games/<game_id>/<any(cards, terms):kind>/status", methods=["POST"])
@require_auth
async def set_status(game_id: str, kind: str):
    """批量启用 / 归档 / 退回草稿：``ids``、``status``，可选 ``versions``（id → 读到的版本）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify(error_payload("ids_required", "ids 必须是非空数组")), 400
    if len(ids) > 500:
        return jsonify(error_payload("too_many_ids", "一次最多改 500 条")), 400
    versions = body.get("versions") if isinstance(body.get("versions"), dict) else None
    try:
        result = await asyncio.to_thread(service.set_status, _KINDS[kind], game_id, ids, str(body.get("status") or ""), versions=versions)
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


@game_knowledge_bp.route("/games/<game_id>/import/preview", methods=["POST"])
@require_auth
async def preview_import(game_id: str):
    """只解析不写入：``data``（JSON 文件内容）、``format``（auto / generic / oni_tutorial_cards / oni_timeline）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    if "data" not in body:
        return jsonify(error_payload("data_required", "请求体要带 data（导入文件的 JSON 内容）")), 400
    try:
        result = await asyncio.to_thread(
            service.preview_import, game_id, body.get("data"),
            fmt=str(body.get("format") or "auto"), status_mode=str(body.get("status") or "auto"),
        )
    except Exception as exc:
        return _error(exc)
    return jsonify(result)


@game_knowledge_bp.route("/games/<game_id>/import", methods=["POST"])
@require_auth
async def run_import(game_id: str):
    """导入：``data``、``format``、``status``（auto / draft / active）、``on_existing``（skip / update / merge）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    if "data" not in body:
        return jsonify(error_payload("data_required", "请求体要带 data（导入文件的 JSON 内容）")), 400
    try:
        result = await asyncio.to_thread(
            service.import_data, game_id, body.get("data"),
            fmt=str(body.get("format") or "auto"),
            status_mode=str(body.get("status") or "auto"),
            on_existing=str(body.get("on_existing") or "skip"),
        )
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


@game_knowledge_bp.route("/games/<game_id>/export", methods=["GET"])
@require_auth
async def export_game(game_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        document = await asyncio.to_thread(service.export_game, game_id, statuses=_statuses(request.args.get("status")))
    except Exception as exc:
        return _error(exc)
    return jsonify(document)


@game_knowledge_bp.route("/games/<game_id>/trial", methods=["POST"])
@require_auth
async def trial_match(game_id: str):
    """试匹配：``message``、``keywords``、``state``、``include_drafts``；返回会注入哪些卡与原因。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        result = await asyncio.to_thread(
            service.trial_match, game_id,
            message=str(body.get("message") or ""),
            keywords=body.get("keywords"),
            state=body.get("state") if isinstance(body.get("state"), dict) else {},
            include_drafts=bool(body.get("include_drafts")),
            bot_id=str(body.get("bot_id") or ""),
        )
    except Exception as exc:
        return _error(exc)
    return jsonify(result)


@game_knowledge_bp.route("/games/<game_id>/lessons", methods=["GET"])
@require_auth
async def list_lessons(game_id: str):
    """某个 Bot 在这个游戏里自己写下的经验卡（``bot_id`` 必填，``status`` 可选）与最近的回想记录。"""
    service = _service()
    if service is None:
        return _unavailable()
    args = request.args
    bot_id = str(args.get("bot_id") or "").strip()
    if not bot_id:
        return jsonify(error_payload("bot_id_required", "要带上 bot_id（看哪个 Bot 的经验）")), 400
    try:
        page = service.list_items(
            "card", game_id, bot_id=bot_id, status=_statuses(args.get("status")) or None,
            query=str(args.get("q") or ""), limit=int(args.get("limit") or 100), offset=int(args.get("offset") or 0),
        )
        lessons = _lessons()
        if lessons is not None:
            activity = await asyncio.to_thread(lessons.overview, game_id, bot_id=bot_id)
        else:
            activity = {"enabled": False, "runs": [], "stats": {}, "pending": []}
    except Exception as exc:
        return _error(exc)
    page["items"] = [_card_payload(item) for item in page["items"]]
    return jsonify({**page, **activity})


@game_knowledge_bp.route("/games/<game_id>/lessons/reflect", methods=["POST"])
@require_auth
async def reflect_lessons(game_id: str):
    """「现在想一次」：让这个 Bot 在后台回想还没想过的游戏经历（所有游戏一起想），立即返回。"""
    lessons = _lessons()
    if lessons is None:
        return jsonify(error_payload("game_lessons_disabled", "游戏经验没有开启（Learning_Settings.game_lesson_enabled）")), 409
    body = await request.get_json(silent=True) or {}
    try:
        result = await lessons.request_reflect(str(body.get("bot_id") or ""))
    except Exception as exc:
        return _error(exc)
    if result.get("status") == "bot_required":
        return jsonify(error_payload("bot_id_required", "要带上 bot_id")), 400
    return jsonify({"ok": result.get("status") in {"started", "running"}, **result})


@game_knowledge_bp.route("/games/<game_id>/lessons/<int:card_id>/<any(veto, restore):action>", methods=["POST"])
@require_auth
async def change_lesson(game_id: str, card_id: int, action: str):
    """删掉（否决：她不会再写意思相近的）或恢复一条经验：``bot_id``、``version``。游戏经验没开也能管。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        expected = _version(body)
        if expected is None:
            raise GameKnowledgeError("version_required", "要带上读到的 version")
        change = veto_lesson if action == "veto" else restore_lesson
        item = await asyncio.to_thread(change, service.repo, game_id, card_id, version=expected, bot_id=str(body.get("bot_id") or ""))
        service._invalidate(game_id)
    except Exception as exc:
        return _error(exc)
    return jsonify(mutation_response(
        operation_kind=f"game_knowledge.lesson.{action}", status="succeeded", revision=item["version"],
        item=_card_payload(item), include_item=True,
    ))


@game_knowledge_bp.route("/legacy/<source>/scan", methods=["POST"])
@require_auth
async def scan_legacy(source: str):
    """统计记忆表里某个旧来源的行数（整表扫描，库大时要几秒到几十秒）。"""
    service = _service()
    if service is None:
        return _unavailable()
    try:
        result = await asyncio.to_thread(service.scan_legacy, source)
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


@game_knowledge_bp.route("/legacy/<source>/migrate", methods=["POST"])
@require_auth
async def migrate_legacy(source: str):
    """把记忆表旧知识点迁移成游戏库草稿；原记忆行不删不改，重复执行只补新行。"""
    service = _service()
    if service is None:
        return _unavailable()
    try:
        result = await asyncio.to_thread(service.migrate_legacy, source)
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


__all__ = ["game_knowledge_bp"]
