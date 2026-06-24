"""Beliefs Blueprint — 信念管理 CRUD + approve/archive (US-1.2)"""

from __future__ import annotations

import json
import time

from quart import Blueprint, jsonify, request

from ..container import get_container
from ..middleware.auth import require_auth

beliefs_bp = Blueprint("beliefs", __name__, url_prefix="/api/beliefs")


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# 需要排除的 archived_reason（身份污染 / 清理标记）
_EXCLUDED_REASONS = ("identity_roleplay_contamination", "identity_cleanup_full")


@beliefs_bp.route("/", methods=["GET"])
@require_auth
async def list_beliefs():
    """列出信念（支持 status / bot_id / type 筛选，支持内容搜索）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"items": [], "total": 0, "pending_count": 0})

    status = request.args.get("status")  # pending / active / archived / pending_legacy
    bot_id = request.args.get("bot_id")
    belief_type = request.args.get("type") # self / other / world / value
    search_q = request.args.get("search") # 搜索内容

    try:
        limit = int(request.args.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    where_parts = ["1=1"]
    params = []
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if bot_id:
        where_parts.append("bot_id = ?")
        params.append(bot_id)
    if belief_type:
        where_parts.append("type = ?")
        params.append(belief_type)
    if search_q:
        where_parts.append("content LIKE ?")
        params.append(f"%{search_q.strip()}%")

    # 排除身份污染 / 清理标记的信念
    reason_excl = ",".join("?" for _ in _EXCLUDED_REASONS)
    where_parts.append(f"COALESCE(archived_reason, '') NOT IN ({reason_excl})")
    params.extend(_EXCLUDED_REASONS)

    where_sql = " AND ".join(where_parts)
    sql = f"SELECT id, content, type, strength, bot_id, sources, status, created_at, last_reinforced FROM beliefs WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = c.db.conn.execute(sql, params).fetchall()

    # total COUNT 加 WHERE 条件（和列表查询一致）
    count_sql = f"SELECT COUNT(*) FROM beliefs WHERE {where_sql}"
    total = c.db.conn.execute(count_sql, params[:-2]).fetchone()[0]
    pending_count = c.db.conn.execute(
        f"SELECT COUNT(*) FROM beliefs WHERE status = 'pending' AND COALESCE(archived_reason, '') NOT IN ({reason_excl})",
        list(_EXCLUDED_REASONS),
    ).fetchone()[0]

    items = [
        {"id": r[0], "content": r[1], "type": r[2], "confidence": r[3],
         "source": r[4], "sources": json.loads(r[5] or "[]"), "status": r[6],
         "created_at": r[7], "updated_at": r[8]}
        for r in rows
    ]
    return jsonify({"items": items, "total": total, "pending_count": pending_count})


@beliefs_bp.route("/", methods=["POST"])
@require_auth
async def create_belief():
    """手动创建信念。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    body = await request.get_json(silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content or len(content) < 5:
        return jsonify({"ok": False, "error": "content required (min 5 chars)"}), 400

    belief_type = body.get("type", "world_view")
    if belief_type not in ("person_judgment", "world_view", "self_identity", "preference"):
        belief_type = "world_view"
    try:
        strength = float(body.get("strength", 0.5))
    except (ValueError, TypeError):
        strength = 0.5
    strength = max(0.0, min(1.0, strength))
    bot_id = body.get("bot_id", "bot")
    sources = body.get("sources") or []
    if not isinstance(sources, list):
        sources = []

    try:
        belief_id = c.db.add_belief(
            content=content,
            belief_type=belief_type,
            bot_id=bot_id,
            strength=strength,
            sources=sources[:20],
            status="pending",  # 手动创建也进入待审
        )
        return jsonify({"ok": True, "belief_id": belief_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@beliefs_bp.route("/<int:belief_id>", methods=["PUT"])
@require_auth
async def edit_belief(belief_id: int):
    """编辑信念 content/strength/type。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    body = await request.get_json(silent=True) or {}
    sets = []
    params = []
    if "content" in body and body["content"]:
        sets.append("content = ?")
        params.append(str(body["content"]).strip())
    if "strength" in body and body["strength"] is not None:
        try:
            sets.append("strength = ?")
            params.append(max(0.0, min(1.0, float(body["strength"]))))
        except (ValueError, TypeError):
            pass
    if "type" in body and body["type"]:
        t = body["type"]
        if t in ("person_judgment", "world_view", "self_identity", "preference"):
            sets.append("type = ?")
            params.append(t)
    if not sets:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    sets.append("last_reinforced = ?")
    params.append(int(time.time()))
    params.append(belief_id)
    c.db.conn.execute(f"UPDATE beliefs SET {', '.join(sets)} WHERE id = ?", params)
    c.db.conn.commit()
    return jsonify({"ok": True, "belief_id": belief_id})


@beliefs_bp.route("/<int:belief_id>/evidence", methods=["GET"])
@require_auth
async def belief_evidence(belief_id: int):
    """返回 sources 关联的 memories / episodes。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    row = c.db.conn.execute(
        "SELECT sources FROM beliefs WHERE id = ?", (belief_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "belief not found"}), 404

    sources = json.loads(row[0] or "[]")
    memories = []
    if sources and _table_exists(c.db.conn, "memories"):
        placeholders = ",".join("?" * len(sources))
        mem_rows = c.db.conn.execute(
            f"SELECT id, content, sender_name, timestamp, group_id FROM memories WHERE id IN ({placeholders})",
            sources,
        ).fetchall()
        memories = [
            {"id": r[0], "content": r[1], "sender_name": r[2] or "", "timestamp": r[3], "group_id": r[4]}
            for r in mem_rows
        ]

    # 关联 episodes（如果有 experience_episodes 表）
    episodes = []
    if _table_exists(c.db.conn, "experience_episodes"):
        placeholders = ",".join("?" * len(sources)) if sources else ""
        if placeholders:
            ep_rows = c.db.conn.execute(
                f"SELECT id, trigger_text, outcome, emotional_weight, created_at FROM experience_episodes WHERE id IN ({placeholders})",
                sources,
            ).fetchall()
            episodes = [
                {"id": r[0], "trigger": r[1], "outcome": r[2], "emotional_weight": r[3], "created_at": r[4]}
                for r in ep_rows
            ]

    return jsonify({
        "ok": True,
        "belief_id": belief_id,
        "sources": sources,
        "memories": memories,
        "episodes": episodes,
    })


@beliefs_bp.route("/<int:belief_id>/approve", methods=["POST"])
@require_auth
async def approve_belief(belief_id: int):
    """审核通过：pending → active。检查 evidence（sources 不能为空）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    # 检查是否有 evidence
    row = c.db.conn.execute(
        "SELECT sources FROM beliefs WHERE id = ?", (belief_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "belief not found"}), 404

    sources = json.loads(row[0] or "[]")
    if not sources:
        return jsonify({"ok": False, "error": "Cannot approve belief without evidence (sources is empty)"}), 400

    c.db.conn.execute(
        "UPDATE beliefs SET status = 'active', last_reinforced = ? WHERE id = ? AND status IN ('pending','challenged','pending_legacy')",
        (int(time.time()), belief_id),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "belief_id": belief_id, "new_status": "active"})


@beliefs_bp.route("/<int:belief_id>/archive", methods=["POST"])
@require_auth
async def archive_belief(belief_id: int):
    """归档信念。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500
    c.db.conn.execute(
        "UPDATE beliefs SET status = 'archived', archived_reason = ? WHERE id = ?",
        ("webui_manual", belief_id),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "belief_id": belief_id, "new_status": "archived"})


@beliefs_bp.route("/<int:belief_id>", methods=["DELETE"])
@require_auth
async def delete_belief(belief_id: int):
    """删除信念。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500
    c.db.conn.execute("DELETE FROM beliefs WHERE id = ?", (belief_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": belief_id})


@beliefs_bp.route("/batch-archive", methods=["POST"])
@require_auth
async def batch_archive():
    """批量归档旧信念（v1.1.0 #2.2）。
    body 可选: {"before_ts": 1718000000} 不传则归档所有 active 信念。
    """
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500
    body = await request.get_json(force=True, silent=True) or {}
    before_ts = body.get("before_ts")
    if before_ts:
        cur = c.db.conn.execute(
            "UPDATE beliefs SET status = 'archived', archived_reason = 'batch_archive' WHERE status = 'active' AND created_at < ?",
            (int(before_ts),),
        )
    else:
        cur = c.db.conn.execute(
            "UPDATE beliefs SET status = 'archived', archived_reason = 'batch_archive' WHERE status = 'active'",
        )
    c.db.conn.commit()
    return jsonify({"ok": True, "archived_count": cur.rowcount})


@beliefs_bp.route("/batch-approve", methods=["POST"])
@require_auth
async def batch_approve_beliefs():
    """批量审核通过信念（支持 all_matching 跨页全选）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    body = await request.get_json() or {}
    all_matching = body.get("all_matching", False)
    
    now = int(time.time())
    approved_count = 0
    skipped_ids = []
    
    if all_matching:
        # 跨页全选模式：读取前端发过来的过滤条件
        status = body.get("status")
        bot_id = body.get("bot_id")
        belief_type = body.get("type")
        search_q = body.get("search")
        
        where_parts = ["status IN ('pending','challenged','pending_legacy')"]
        params = []
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if bot_id:
            where_parts.append("bot_id = ?")
            params.append(bot_id)
        if belief_type:
            where_parts.append("type = ?")
            params.append(belief_type)
        if search_q:
            where_parts.append("content LIKE ?")
            params.append(f"%{search_q.strip()}%")
            
        reason_excl = ",".join("?" for _ in _EXCLUDED_REASONS)
        where_parts.append(f"COALESCE(archived_reason, '') NOT IN ({reason_excl})")
        params.extend(_EXCLUDED_REASONS)
        
        where_sql = " AND ".join(where_parts)
        rows = c.db.conn.execute(
            f"SELECT id, sources FROM beliefs WHERE {where_sql}", params
        ).fetchall()
    else:
        # 普通勾选模式
        ids = body.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "ids list or all_matching is required"}), 400
            
        placeholders = ",".join("?" * len(ids))
        rows = c.db.conn.execute(
            f"SELECT id, sources FROM beliefs WHERE id IN ({placeholders})", ids
        ).fetchall()

    for r in rows:
        bid = r[0]
        sources = json.loads(r[1] or "[]")
        # 强制搭配：必须要有证据
        if not sources:
            skipped_ids.append(bid)
            continue
            
        c.db.conn.execute(
            "UPDATE beliefs SET status = 'active', last_reinforced = ? WHERE id = ? AND status IN ('pending','challenged','pending_legacy')",
            (now, bid),
        )
        approved_count += 1
        
    c.db.conn.commit()
    return jsonify({
        "ok": True, 
        "approved_count": approved_count, 
        "skipped_count": len(skipped_ids),
        "skipped_ids": skipped_ids
    })


@beliefs_bp.route("/batch-delete", methods=["POST"])
@require_auth
async def batch_delete_beliefs():
    """批量删除信念（支持 all_matching 跨页全选）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    body = await request.get_json() or {}
    all_matching = body.get("all_matching", False)
    
    if all_matching:
        status = body.get("status")
        bot_id = body.get("bot_id")
        belief_type = body.get("type")
        search_q = body.get("search")
        
        where_parts = ["1=1"]
        params = []
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if bot_id:
            where_parts.append("bot_id = ?")
            params.append(bot_id)
        if belief_type:
            where_parts.append("type = ?")
            params.append(belief_type)
        if search_q:
            where_parts.append("content LIKE ?")
            params.append(f"%{search_q.strip()}%")
            
        reason_excl = ",".join("?" for _ in _EXCLUDED_REASONS)
        where_parts.append(f"COALESCE(archived_reason, '') NOT IN ({reason_excl})")
        params.extend(_EXCLUDED_REASONS)
        
        where_sql = " AND ".join(where_parts)
        cur = c.db.conn.execute(f"DELETE FROM beliefs WHERE {where_sql}", params)
    else:
        ids = body.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "ids list or all_matching is required"}), 400
            
        placeholders = ",".join("?" * len(ids))
        cur = c.db.conn.execute(f"DELETE FROM beliefs WHERE id IN ({placeholders})", ids)
        
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted_count": cur.rowcount})
