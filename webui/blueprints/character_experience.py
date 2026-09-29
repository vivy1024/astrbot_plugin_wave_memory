"""角色经历工作台 API：按 Bot 浏览 / 搜索 / 编辑经历、时间线、当前时间点与设置，
三种来源（从原著提取、按设定编写、手写 / 导入 JSONL）、导出、补算向量、任务续跑与旧数据迁移。

修改都要带上读到的 ``version``：版本对不上返回 409（经历、阶段、Bot 资料同一套写法）。
调用模型的任务（提取、起草时间线、展开）必须先预览，再带 ``confirm=true`` 与预计调用次数 ``expected_calls``
提交，否则返回 409 并附上这次的计划。
"""

from __future__ import annotations

import asyncio
from typing import Any

from quart import Blueprint, Response, jsonify, request

from ..api_contract import error_payload
from ..container import get_container
from ..middleware.auth import require_auth

character_experience_bp = Blueprint("character_experience", __name__, url_prefix="/api/character-experience")

_NOT_FOUND = {"bot_not_found", "experience_not_found", "stage_not_found", "job_not_found"}
_UNAVAILABLE = {"durable_jobs_unavailable", "embedding_unavailable", "model_unavailable"}
_CONFLICT = {"version_conflict", "confirmation_required", "duplicate_content", "stage_in_use", "job_not_resumable"}
UNASSIGNED = "__unassigned__"


def _service() -> Any:
    return getattr(get_container(), "character_experience", None)


def _unavailable():
    return jsonify(error_payload(
        "character_experience_unavailable",
        "角色经历服务未就绪（数据库或 Bot 注册表没有加载）",
        retryable=True,
    )), 503


def _error(exc: Exception):
    code = str(getattr(exc, "code", "") or "character_experience_failed")
    payload = error_payload(code, str(exc))
    if getattr(exc, "ids", None):
        payload["error"]["ids"] = list(exc.ids)  # type: ignore[attr-defined]
    if getattr(exc, "plan", None) is not None:
        payload["plan"] = exc.plan  # type: ignore[attr-defined]
    if code in _NOT_FOUND:
        status = 404
    elif code in _CONFLICT:
        status = 409
    elif code in _UNAVAILABLE:
        status = 503
    else:
        status = 400
    return jsonify(payload), status


async def _body() -> dict[str, Any]:
    body = await request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _items(body: dict[str, Any], key: str = "id") -> list[tuple[Any, int]]:
    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        raise ValueError("items 必须是非空数组，每项带 id 与 version")
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get(key) in (None, "") or entry.get("version") is None:
            raise ValueError(f"items 每项都要带 {key} 与 version")
        out.append((int(entry[key]) if key == "id" else str(entry[key]), int(entry["version"])))
    return out


def _bad_request(exc: Exception):
    return jsonify(error_payload("invalid_request", str(exc))), 400


def _strip_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if k not in {"scene_keys", "event_keys", "setting"}}


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


# ─── 概览与设置 ───

@character_experience_bp.route("/bots/<bot_id>/overview", methods=["GET"])
@require_auth
async def overview(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        return jsonify(await _run(service.overview, bot_id))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise


@character_experience_bp.route("/bots/<bot_id>/settings", methods=["PUT"])
@require_auth
async def save_settings(bot_id: str):
    """改 Bot 资料里的角色经历设置（启用、当前时间点、召回条数、注入上限等），带 Bot 资料的 version。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    if body.get("version") is None or not isinstance(body.get("patch"), dict):
        return jsonify(error_payload("version_required", "要带上读到的 Bot 资料 version 与 patch")), 400
    try:
        saved = await _run(service.update_settings, bot_id, body["patch"], expected_version=int(body["version"]))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "version": saved.version, "settings": saved.to_dict()["character_experience"]})


# ─── 经历 ───

@character_experience_bp.route("/bots/<bot_id>/experiences", methods=["GET"])
@require_auth
async def list_experiences(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    args = request.args
    try:
        limit = max(1, min(int(args.get("limit") or 50), 200))
        offset = max(0, int(args.get("offset") or 0))
    except ValueError as exc:
        return _bad_request(exc)
    stage = str(args.get("stage") or "")
    statuses = [part for part in str(args.get("status") or "").split(",") if part]
    items, total = await _run(
        service.repo.list,
        bot_id,
        statuses=statuses or None,
        # 不带 stage = 全部阶段；__unassigned__ = 只看没分阶段的
        stage_key=None if not stage else ("" if stage == UNASSIGNED else stage),
        query=str(args.get("q") or ""),
        canon_level=str(args.get("canon") or ""),
        source_kind=str(args.get("source_kind") or ""),
        order=str(args.get("order") or "timeline"),
        limit=limit,
        offset=offset,
    )
    return jsonify({"items": items, "total": total, "limit": limit, "offset": offset})


@character_experience_bp.route("/bots/<bot_id>/experiences/<int:experience_id>", methods=["GET"])
@require_auth
async def get_experience(bot_id: str, experience_id: int):
    service = _service()
    if service is None:
        return _unavailable()
    item = await _run(service.repo.get, bot_id, experience_id)
    if item is None:
        return jsonify(error_payload("experience_not_found", f"没有这条经历 #{experience_id}")), 404
    stage = await _run(service.repo.get_stage, bot_id, item["stage_key"]) if item["stage_key"] else None
    return jsonify({"item": item, "stage": stage})


@character_experience_bp.route("/bots/<bot_id>/experiences", methods=["POST"])
@require_auth
async def create_experience(bot_id: str):
    """手写一条经历（默认草稿）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    fields = dict(body.get("item") or body)
    fields.pop("version", None)
    try:
        created = await _run(service.create, bot_id, fields)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "item": created}), 201


@character_experience_bp.route("/bots/<bot_id>/experiences/<int:experience_id>", methods=["PUT"])
@require_auth
async def update_experience(bot_id: str, experience_id: int):
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    if body.get("version") is None:
        return jsonify(error_payload("version_required", "修改要带上读到的 version")), 400
    fields = dict(body.get("fields") or {})
    try:
        updated = await _run(service.repo.update, bot_id, experience_id, fields, expected_version=int(body["version"]))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    job = None
    if updated["status"] == "active" and not updated["has_vector"] and service.embedding is not None:
        job = await _maybe_embed(service, bot_id)
    return jsonify({"ok": True, "item": updated, "embed_job": job})


async def _maybe_embed(service: Any, bot_id: str) -> Any:
    try:
        return await service.start_embedding(bot_id)
    except Exception:  # 补算向量排不上队不影响本次修改；工作台还能手动补
        return None


@character_experience_bp.route("/bots/<bot_id>/experiences/status", methods=["POST"])
@require_auth
async def set_status(bot_id: str):
    """批量启用 / 归档 / 退回草稿；任一条版本对不上整体不写（409）。启用后自动排补算向量。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        items = _items(body)
    except (TypeError, ValueError) as exc:
        return _bad_request(exc)
    try:
        changed = await _run(service.repo.set_status, bot_id, items, str(body.get("status") or ""))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    job = await _maybe_embed(service, bot_id) if body.get("status") == "active" and service.embedding is not None else None
    return jsonify({"ok": True, "changed": changed, "embed_job": job})


@character_experience_bp.route("/bots/<bot_id>/experiences/stage", methods=["POST"])
@require_auth
async def set_stage(bot_id: str):
    """批量移到某个阶段（空串 = 不分阶段）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        items = _items(body)
    except (TypeError, ValueError) as exc:
        return _bad_request(exc)
    try:
        changed = await _run(service.repo.set_stage, bot_id, items, str(body.get("stage_key") or ""))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "changed": changed})


@character_experience_bp.route("/bots/<bot_id>/recall", methods=["POST"])
@require_auth
async def try_recall(bot_id: str):
    """试一试：输入一句话，看她按当前设置会想起哪几条（不写任何数据；未启用的 Bot 也能试）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify(error_payload("message_required", "请输入一句话")), 400
    vector = None
    if service.embedding is not None:
        try:
            vector = await service.embedding.get_embedding(message)
        except Exception:
            vector = None
    settings = service.settings_for(bot_id)
    result = await service.recall(bot_id, message, settings=settings, query_vector=vector)
    try:
        from ...services.injection.channels.character_experience import render_line
    except ImportError:  # pragma: no cover - 仓库测试把插件根目录当顶层包
        from services.injection.channels.character_experience import render_line
    for item in result["items"]:
        item["rendered_text"] = render_line(item)
    result["embedding_used"] = vector is not None
    return jsonify(result)


# ─── 时间线 ───

@character_experience_bp.route("/bots/<bot_id>/stages", methods=["GET"])
@require_auth
async def list_stages(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    return jsonify({"items": await _run(service.repo.list_stages, bot_id)})


@character_experience_bp.route("/bots/<bot_id>/stages/<stage_key>", methods=["PUT"])
@require_auth
async def save_stage(bot_id: str, stage_key: str):
    """新增（version=0）或修改一个阶段。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    if body.get("version") is None:
        return jsonify(error_payload("version_required", "保存要带上读到的 version（新建用 0）")), 400
    try:
        saved = await _run(service.repo.save_stage, bot_id, stage_key, dict(body.get("fields") or {}), expected_version=int(body["version"]))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "item": saved})


@character_experience_bp.route("/bots/<bot_id>/stages/<stage_key>", methods=["DELETE"])
@require_auth
async def delete_stage(bot_id: str, stage_key: str):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        version = int(request.args.get("version"))
    except (TypeError, ValueError):
        return jsonify(error_payload("version_required", "删除要带上读到的 version")), 400
    settings = service.settings_for(bot_id)
    if settings.current_stage == stage_key:
        return jsonify(error_payload("stage_is_current", "这是当前时间点所在的阶段，先把当前时间点换到别的阶段")), 409
    try:
        await _run(service.repo.delete_stage, bot_id, stage_key, expected_version=version)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "deleted": stage_key})


@character_experience_bp.route("/bots/<bot_id>/stages/reorder", methods=["POST"])
@require_auth
async def reorder_stages(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        items = _items(body, key="stage_key")
        stages = await _run(service.repo.reorder_stages, bot_id, items)
    except (TypeError, ValueError) as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        return _bad_request(exc)
    return jsonify({"ok": True, "items": stages})


@character_experience_bp.route("/bots/<bot_id>/stages/confirm", methods=["POST"])
@require_auth
async def confirm_stages(bot_id: str):
    """确认起草的时间线阶段（确认后才能展开成经历）。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        items = _items(body, key="stage_key")
        done = await _run(service.repo.confirm_stages, bot_id, items)
    except (TypeError, ValueError) as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        return _bad_request(exc)
    return jsonify({"ok": True, "confirmed": done})


# ─── 导入导出 ───

@character_experience_bp.route("/bots/<bot_id>/import", methods=["POST"])
@require_auth
async def import_jsonl(bot_id: str):
    """导入 JSONL（兼容 v5 bzz_memories_raw.jsonl 的字段）。默认进草稿，同内容已存在时默认跳过。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        result = await _run(
            service.import_jsonl,
            bot_id,
            str(body.get("text") or ""),
            status=str(body.get("status") or "draft"),
            canon_level=str(body.get("canon_level") or "canon"),
            on_duplicate=str(body.get("on_duplicate") or "skip"),
        )
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    if body.get("status") == "active" and service.embedding is not None:
        result["embed_job"] = await _maybe_embed(service, bot_id)
    return jsonify({"ok": not result["error_count"], **result})


@character_experience_bp.route("/bots/<bot_id>/export", methods=["GET"])
@require_auth
async def export_jsonl(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    statuses = [part for part in str(request.args.get("status") or "").split(",") if part]
    try:
        text = await _run(service.export_jsonl, bot_id, statuses=statuses or None)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    response = Response(text, mimetype="application/x-ndjson")
    response.headers["Content-Disposition"] = f'attachment; filename="character-experience-{bot_id}.jsonl"'
    return response


# ─── 三种来源的后台任务 ───

async def _start(func, bot_id: str, body: dict[str, Any]):
    try:
        result = await func(bot_id, body)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, **result}), 202


@character_experience_bp.route("/bots/<bot_id>/extract/preview", methods=["POST"])
@require_auth
async def extract_preview(bot_id: str):
    """从原著提取的预览：场景数、已处理数、预计调用次数、各阶段分配与样例。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        plan = await _run(service.extraction_plan, bot_id, body)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify(_strip_plan(plan))


@character_experience_bp.route("/bots/<bot_id>/extract", methods=["POST"])
@require_auth
async def extract_start(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    return await _start(service.start_extraction, bot_id, await _body())


@character_experience_bp.route("/bots/<bot_id>/timeline/preview", methods=["POST"])
@require_auth
async def timeline_preview(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        plan = await _run(service.timeline_plan, bot_id, await _body())
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify(_strip_plan(plan))


@character_experience_bp.route("/bots/<bot_id>/timeline/draft", methods=["POST"])
@require_auth
async def timeline_draft(bot_id: str):
    """按设定起草人生时间线（一次模型调用）；起草的阶段是草稿，改好确认后才能展开。"""
    service = _service()
    if service is None:
        return _unavailable()
    return await _start(service.start_timeline_draft, bot_id, await _body())


@character_experience_bp.route("/bots/<bot_id>/expand/preview", methods=["POST"])
@require_auth
async def expand_preview(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        plan = await _run(service.expansion_plan, bot_id, await _body())
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify(_strip_plan(plan))


@character_experience_bp.route("/bots/<bot_id>/expand", methods=["POST"])
@require_auth
async def expand_start(bot_id: str):
    """把已确认阶段的关键事件逐条展开成第一人称经历（标为「编造」，记下依据的设定段落）。"""
    service = _service()
    if service is None:
        return _unavailable()
    return await _start(service.start_expansion, bot_id, await _body())


@character_experience_bp.route("/bots/<bot_id>/embed", methods=["POST"])
@require_auth
async def embed_start(bot_id: str):
    """启用的经历里还没有向量的，排一个补算向量任务（不调用大模型）。"""
    service = _service()
    if service is None:
        return _unavailable()
    try:
        job = await service.start_embedding(bot_id)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "job": job}), 202 if job else 200


@character_experience_bp.route("/bots/<bot_id>/jobs", methods=["GET"])
@require_auth
async def list_jobs(bot_id: str):
    service = _service()
    if service is None:
        return _unavailable()
    return jsonify({"items": await _run(service.list_jobs, bot_id, limit=20)})


@character_experience_bp.route("/jobs/<run_id>/resume", methods=["POST"])
@require_auth
async def resume_job(run_id: str):
    """失败或已取消的任务续跑：已处理过的场景跳过，只处理剩下的。取消用 /api/maintenance/jobs/<id>/cancel。"""
    service = _service()
    if service is None:
        return _unavailable()
    try:
        result = await service.resume_job(run_id)
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, **result}), 202


@character_experience_bp.route("/legacy-migration", methods=["POST"])
@require_auth
async def legacy_migration():
    """一次性旧数据迁移（启动时已自动执行；目标 Bot 当时还不存在时可以在这里补做）。``dry_run`` 只预览。"""
    service = _service()
    if service is None:
        return _unavailable()
    body = await _body()
    try:
        reports = await _run(service.migrate_legacy, dry_run=bool(body.get("dry_run")))
    except Exception as exc:
        if hasattr(exc, "code"):
            return _error(exc)
        raise
    return jsonify({"ok": True, "reports": reports})


__all__ = ["character_experience_bp"]
