"""Jargon Blueprint — 黑话管理 WebUI API (US-4.4)"""

from __future__ import annotations

import json
import time

from quart import Blueprint, jsonify, request

from ..container import get_container
from ..middleware.auth import require_auth

jargon_bp = Blueprint("jargon", __name__, url_prefix="/api/jargon")


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _safe_int(val, default):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


@jargon_bp.route("/", methods=["GET"])
@require_auth
async def list_jargon():
    """列出黑话（支持 group_id / status 筛选，支持内容搜索）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"items": [], "total": 0})
    group_id = request.args.get("group_id")
    status = request.args.get("status")  # confirmed / pending / rejected
    search_q = request.args.get("search")  # 搜索词条名或释义

    limit = _safe_int(request.args.get("limit", 50), 50)
    offset = _safe_int(request.args.get("offset", 0), 0)

    where_parts = ["1=1"]
    params = []
    if group_id:
        where_parts.append("group_id = ?")
        params.append(group_id)
    # 改用 status 字段筛选（COALESCE 处理 NULL → 'pending'）
    if status:
        where_parts.append("COALESCE(status, 'pending') = ?")
        params.append(status)
    if search_q:
        where_parts.append("(word LIKE ? OR meaning LIKE ?)")
        sq = f"%{search_q.strip()}%"
        params.extend([sq, sq])

    where_sql = " AND ".join(where_parts)
    sql = f"SELECT id, word, meaning, is_jargon, frequency, confidence, is_global, group_id, contexts, created_at FROM jargon WHERE {where_sql} ORDER BY frequency DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = c.db.conn.execute(sql, params).fetchall()
    # total COUNT 加 WHERE 条件（和列表查询一致）
    count_sql = f"SELECT COUNT(*) FROM jargon WHERE {where_sql}"
    total = c.db.conn.execute(count_sql, params[:-2]).fetchone()[0]
    items = [
        {"id": r[0], "word": r[1], "meaning": r[2], "is_jargon": r[3],
         "frequency": r[4], "confidence": r[5], "is_global": bool(r[6]),
         "group_id": r[7], "contexts": json.loads(r[8] or "[]"), "created_at": r[9]}
        for r in rows
    ]
    return jsonify({"items": items, "total": total})


@jargon_bp.route("/", methods=["POST"])
@require_auth
async def create_jargon():
    """手动创建黑话词条。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500

    body = await request.get_json() or {}
    word = body.get("word", "").strip()
    meaning = body.get("meaning", "").strip()
    group_id = body.get("group_id")
    if group_id:
        group_id = str(group_id).strip()

    if not word:
        return jsonify({"ok": False, "error": "Word is required"}), 400

    # 检查是否已存在
    dup = c.db.conn.execute("SELECT id FROM jargon WHERE word = ? AND (group_id = ? OR (group_id IS NULL AND ? IS NULL))", (word, group_id, group_id)).fetchone()
    if dup:
        return jsonify({"ok": False, "error": f"Jargon '{word}' already exists"}), 400

    now = int(time.time())
    is_global = 1 if not group_id else 0
    c.db.conn.execute(
        "INSERT INTO jargon (word, meaning, is_jargon, status, frequency, confidence, is_global, group_id, contexts, created_at, updated_at) VALUES (?, ?, 1, 'confirmed', 1, 1.0, ?, ?, '[]', ?, ?)",
        (word, meaning, is_global, group_id, now, now)
    )
    c.db.conn.commit()
    new_id = c.db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"ok": True, "id": new_id})


@jargon_bp.route("/<int:jargon_id>/review", methods=["POST"])
@require_auth
async def review_jargon(jargon_id: int):
    """审核：approve / reject。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500
    body = await request.get_json(silent=True) or {}
    action = body.get("action")  # approve / reject
    meaning = body.get("meaning")  # 可选：修正含义
    reject_reason = body.get("reject_reason", "")

    if action not in ("approve", "reject"):
        return jsonify({"error": "action must be approve or reject"}), 400

    now = int(time.time())
    if action == "approve":
        sets = "is_jargon = 1, status = 'confirmed', updated_at = ?"
        params = [now]
        if meaning:
            sets += ", meaning = ?"
            params.append(meaning)
        params.append(jargon_id)
        c.db.conn.execute(f"UPDATE jargon SET {sets} WHERE id = ?", params)
    else:
        c.db.conn.execute(
            "UPDATE jargon SET is_jargon = 0, status = 'rejected', reject_reason = ?, updated_at = ? WHERE id = ?",
            (reject_reason or "manual_reject", now, jargon_id),
        )

    c.db.conn.commit()
    return jsonify({"ok": True, "jargon_id": jargon_id, "action": action})


@jargon_bp.route("/<int:jargon_id>", methods=["PUT"])
@require_auth
async def edit_jargon(jargon_id: int):
    """编辑黑话词条/释义。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500
    body = await request.get_json(silent=True) or {}
    sets = []
    params = []
    if "word" in body:
        sets.append("word = ?")
        params.append(body["word"])
    if "meaning" in body:
        sets.append("meaning = ?")
        params.append(body["meaning"])
    if not sets:
        return jsonify({"error": "Nothing to update"}), 400
    sets.append("updated_at = ?")
    params.append(int(time.time()))
    params.append(jargon_id)
    try:
        c.db.conn.execute(f"UPDATE jargon SET {', '.join(sets)} WHERE id = ?", params)
        c.db.conn.commit()
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return jsonify({"error": "该群已存在同名词条，请使用其他名称"}), 409
        raise
    return jsonify({"ok": True, "jargon_id": jargon_id})


@jargon_bp.route("/<int:jargon_id>", methods=["DELETE"])
@require_auth
async def delete_jargon(jargon_id: int):
    """删除黑话。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500
    c.db.conn.execute("DELETE FROM jargon WHERE id = ?", (jargon_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": jargon_id})


@jargon_bp.route("/<int:jargon_id>/toggle_global", methods=["POST"])
@require_auth
async def toggle_global(jargon_id: int):
    """切换全局状态。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500
    row = c.db.conn.execute("SELECT is_global FROM jargon WHERE id = ?", (jargon_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row[0] else 1
    c.db.conn.execute("UPDATE jargon SET is_global = ?, updated_at = ? WHERE id = ?", (new_val, int(time.time()), jargon_id))
    c.db.conn.commit()
    return jsonify({"ok": True, "jargon_id": jargon_id, "is_global": bool(new_val)})


@jargon_bp.route("/batch-review", methods=["POST"])
@require_auth
async def batch_review_jargon():
    """批量审核确认/否决黑话词条。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500

    body = await request.get_json() or {}
    ids = body.get("ids", [])
    action = body.get("action", "approve")  # approve 或 reject
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "ids list is required"}), 400

    if action not in {"approve", "reject"}:
        return jsonify({"ok": False, "error": "invalid action"}), 400

    now = int(time.time())
    placeholders = ",".join("?" * len(ids))
    if action == "approve":
        c.db.conn.execute(
            f"UPDATE jargon SET is_jargon = 1, status = 'confirmed', updated_at = ? WHERE id IN ({placeholders})",
            [now] + ids,
        )
    else:
        # reject 
        c.db.conn.execute(
            f"UPDATE jargon SET is_jargon = 0, status = 'rejected', reject_reason = 'webui_batch_rejected', updated_at = ? WHERE id IN ({placeholders})",
            [now] + ids,
        )

    c.db.conn.commit()
    return jsonify({"ok": True, "reviewed_count": len(ids), "action": action})


@jargon_bp.route("/batch-delete", methods=["POST"])
@require_auth
async def batch_delete_jargon():
    """批量删除黑话词条。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500

    body = await request.get_json() or {}
    ids = body.get("ids", [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "ids list is required"}), 400

    placeholders = ",".join("?" * len(ids))
    cur = c.db.conn.execute(f"DELETE FROM jargon WHERE id IN ({placeholders})", ids)
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted_count": cur.rowcount})


@jargon_bp.route("/holyman", methods=["GET"])
@require_auth
async def get_holyman():
    """获取 Holyman 预设黑话及其数据库激活状态。"""
    c = get_container()
    from pathlib import Path
    
    # 1. 读取本地 presets json 资产
    local_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "holyman"
    phrases_file = local_dir / "phrases.json"
    phrases = {}
    if phrases_file.exists():
        try:
            phrases = json.loads(phrases_file.read_text(encoding="utf-8"))
        except Exception:
            pass
            
    # 2. 查询数据库中已激活的条目
    db_items = {}
    if _table_exists(c.db.conn, "jargon"):
        rows = c.db.conn.execute(
            "SELECT id, word, meaning, status FROM jargon WHERE scope = 'global' AND source = 'holyman_skills' AND is_jargon = 1 AND status = 'confirmed'"
        ).fetchall()
        for r in rows:
            db_items[r[1]] = {"id": r[0], "meaning": r[2], "status": r[3]}
            
    # 3. 构造 items 组合结果
    items = []
    for word, meaning in phrases.items():
        if word in db_items:
            items.append({
                "word": word,
                "meaning": meaning,
                "is_activated": True,
                "db_id": db_items[word]["id"],
                "custom_meaning": db_items[word]["meaning"]
            })
        else:
            items.append({
                "word": word,
                "meaning": meaning,
                "is_activated": False,
                "db_id": None,
                "custom_meaning": None
            })
            
    return jsonify({"items": items})


@jargon_bp.route("/holyman/toggle", methods=["POST"])
@require_auth
async def toggle_holyman():
    """激活或去激活预设 Holyman 词条。"""
    c = get_container()
    if not _table_exists(c.db.conn, "jargon"):
        return jsonify({"ok": False, "error": "jargon table not found"}), 500
        
    body = await request.get_json() or {}
    word = body.get("word", "").strip()
    meaning = body.get("meaning", "").strip()
    activate = body.get("activate", False)
    
    if not word:
        return jsonify({"ok": False, "error": "word is required"}), 400
        
    now = int(time.time())
    
    if activate:
        # 双重检查是否已存在
        dup = c.db.conn.execute(
            "SELECT id FROM jargon WHERE word = ? AND scope = 'global' AND source = 'holyman_skills'", 
            (word,)
        ).fetchone()
        
        if dup:
            return jsonify({"ok": True, "db_id": dup[0]})
            
        c.db.conn.execute(
            "INSERT INTO jargon (word, meaning, is_jargon, status, frequency, confidence, is_global, group_id, contexts, created_at, updated_at, scope, source) VALUES (?, ?, 1, 'confirmed', 5, 0.9, 1, 'global_fallback', '[]', ?, ?, 'global', 'holyman_skills')",
            (word, meaning, now, now)
        )
        c.db.conn.commit()
        new_id = c.db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"ok": True, "db_id": new_id})
    else:
        c.db.conn.execute(
            "DELETE FROM jargon WHERE word = ? AND scope = 'global' AND source = 'holyman_skills'",
            (word,)
        )
        c.db.conn.commit()
        return jsonify({"ok": True})


@jargon_bp.route("/holyman/sync", methods=["POST"])
@require_auth
async def sync_holyman():
    """同步 Holyman 词库。"""
    body = await request.get_json() or {}
    use_proxy = body.get("use_proxy", True)
    
    from ...services.jargon.sync import HolymanSyncService
    sync_service = HolymanSyncService()
    
    res = await sync_service.sync_from_github(use_proxy=use_proxy)
    if res.get("ok"):
        c = get_container()
        if hasattr(c, "jargon_service") and c.jargon_service:
            if hasattr(c.jargon_service, "_holyman") and c.jargon_service._holyman:
                c.jargon_service._holyman.reload()
                
    return jsonify(res)