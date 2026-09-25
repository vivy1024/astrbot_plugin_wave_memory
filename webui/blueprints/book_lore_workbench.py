"""书设工作台 API：索引健康、补齐索引、笔记增删改、原文新章节扫描与导入。"""

from __future__ import annotations

from typing import Any

from quart import Blueprint, jsonify, request

from ..api_contract import error_payload
from ..container import get_container
from ..middleware.auth import require_auth

book_lore_workbench_bp = Blueprint("book_lore_workbench", __name__, url_prefix="/api/book-lore/workbench")


def _workbench() -> Any:
    return getattr(get_container(), "book_lore_workbench", None)


def _unavailable():
    return jsonify(error_payload(
        "workbench_unavailable",
        "书设工作台未就绪：书设库、书设向量索引或向量服务没有加载（运行模式是否关闭了书设？）",
        retryable=True,
    )), 503


def _error(exc: Exception):
    code = getattr(exc, "code", "workbench_failed")
    status = 400 if code not in {"book_lore_unavailable", "book_lore_database_unavailable"} else 503
    return jsonify(error_payload(code, str(exc))), status


@book_lore_workbench_bp.route("/overview", methods=["GET"])
@require_auth
async def overview():
    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    try:
        return jsonify(await workbench.overview())
    except Exception as exc:
        return _error(exc)


@book_lore_workbench_bp.route("/notes", methods=["GET"])
@require_auth
async def list_notes():
    import asyncio

    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    args = request.args
    limit = max(1, min(int(args.get("limit") or 50), 200))
    offset = max(0, int(args.get("offset") or 0))
    kwargs: dict[str, Any] = {
        "query": str(args.get("q") or "").strip(),
        "category": str(args.get("category") or ""),
        "arc": str(args.get("arc") or ""),
        "limit": limit,
        "offset": offset,
    }
    # indexed=0：只看还没进索引的；indexed=1：只看已进索引的
    indexed_filter = args.get("indexed")
    if indexed_filter in {"0", "1"} and workbench.index is not None:
        ids = workbench.index.note_ids()
        kwargs["note_ids" if indexed_filter == "1" else "exclude_ids"] = sorted(ids)
    try:
        page = await asyncio.to_thread(lambda: workbench.writer.list_notes(**kwargs))
    except Exception as exc:
        return _error(exc)
    indexed = workbench.index.note_ids() if workbench.index is not None else set()
    for item in page["items"]:
        item["indexed"] = item["id"] in indexed
    return jsonify({**page, "limit": limit, "offset": offset})


@book_lore_workbench_bp.route("/notes/<path:note_id>", methods=["GET"])
@require_auth
async def get_note(note_id: str):
    import asyncio

    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    note = await asyncio.to_thread(workbench.writer.get_note, note_id)
    if note is None:
        return jsonify(error_payload("note_not_found", f"没有笔记 {note_id}")), 404
    note["indexed"] = workbench.index is not None and note_id in workbench.index.note_ids()
    return jsonify({"item": note})


@book_lore_workbench_bp.route("/notes", methods=["POST"])
@require_auth
async def save_note():
    """新增或修改一条笔记：写库、算向量、更新索引，保存后立即能被检索和注入用到。"""
    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    note = dict(body.get("item") or body)
    if not str(note.get("id") or "").strip():
        import re
        import time

        slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", str(note.get("title") or "note"))[:24].strip("_")
        note["id"] = f"note_wb_{int(time.time())}_{slug}"
    try:
        result = await workbench.save_notes([note])
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


@book_lore_workbench_bp.route("/notes/<path:note_id>", methods=["DELETE"])
@require_auth
async def delete_note(note_id: str):
    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    try:
        removed = await workbench.delete_note(note_id)
    except Exception as exc:
        return _error(exc)
    if not removed:
        return jsonify(error_payload("note_not_found", f"没有笔记 {note_id}")), 404
    return jsonify({"ok": True, "deleted": note_id})


@book_lore_workbench_bp.route("/sync-index", methods=["POST"])
@require_auth
async def sync_index():
    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    try:
        result = await workbench.sync_index(limit=max(1, min(int(body.get("limit") or 2000), 5000)))
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


@book_lore_workbench_bp.route("/chapters/preview", methods=["POST"])
@require_auth
async def preview_chapters():
    """粘贴的文本切成哪些章、哪些已在库里。"""
    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    text = str(body.get("text") or "")
    if len(text) > 5_000_000:
        return jsonify(error_payload("text_too_large", "一次最多粘贴 5 MB 文本")), 400
    import asyncio

    return jsonify({"chapters": await asyncio.to_thread(workbench.preview_text, text)})


@book_lore_workbench_bp.route("/chapters/import", methods=["POST"])
@require_auth
async def import_chapters():
    """导入章节：``path``（扫描到的原文）或 ``text``（粘贴），可选 ``numbers``、``arc``、``overwrite``。"""
    workbench = _workbench()
    if workbench is None:
        return _unavailable()
    body = await request.get_json(silent=True) or {}
    numbers = body.get("numbers")
    try:
        result = await workbench.import_chapters(
            text=body.get("text"),
            path=body.get("path"),
            numbers=[int(n) for n in numbers] if isinstance(numbers, list) else None,
            arc=str(body.get("arc") or ""),
            overwrite=bool(body.get("overwrite")),
        )
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


__all__ = ["book_lore_workbench_bp"]
