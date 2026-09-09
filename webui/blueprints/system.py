"""System Blueprint — 系统状态、健康检查"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

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

    class _Request:
        args: dict = {}
    request = _Request()  # type: ignore[assignment]

from ..container import get_container
try:
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时直接放行
    def require_auth(func):
        return func

system_bp = Blueprint("system", __name__, url_prefix="/api")


_CORE_SERVICE_NAMES = {
    "向量索引",
    "Tag 索引",
    "Embedding",
    "Tag 提取",
}

_DERIVED_SERVICE_NAMES = {
    "共现矩阵",
    "脉冲传播",
    "残差金字塔",
    "测地线重排",
    "EPA 基底",
}

_OPTIONAL_SERVICE_NAMES = {
    "MetaThinking",
    "做梦系统",
    "自主学习",
    "自省系统",
    "记忆淘汰",
    "信念引擎",
    "关切追踪",
    "情绪轨迹",
    "黑话系统",
    "风格学习",
}


def _service_role(name: str) -> str:
    if name in _CORE_SERVICE_NAMES:
        return "core"
    if name in _DERIVED_SERVICE_NAMES:
        return "derived"
    if name in _OPTIONAL_SERVICE_NAMES:
        return "optional"
    return "core"


def _service_severity(name: str, status: str) -> str:
    role = _service_role(name)
    if status == "ok":
        return "ok"
    if status in {"error", "timeout"}:
        return "critical"
    if status == "off":
        if role == "core":
            return "critical"
        if role == "optional":
            return "disabled"
        return "degraded"
    if status == "degraded":
        return "degraded"
    return "degraded"


def refresh_dynamic_services_health(c: Any, services: list[Mapping[str, Any]] | None) -> list[dict]:
    """用容器 live 状态覆盖启动时注册表里的异步初始化状态。"""
    refreshed = [dict(service or {}) for service in (services or [])]

    def upsert(name: str, status: str, reason: str, dependency: str = "") -> None:
        for item in refreshed:
            if item.get("name") == name:
                item["status"] = status
                item["reason"] = reason
                if dependency:
                    item["dependency"] = dependency
                item["ts"] = time.time()
                return
        refreshed.append({"name": name, "status": status, "reason": reason, "dependency": dependency, "ts": time.time()})

    epa = getattr(c, "epa", None)
    if epa is not None:
        initialized = bool(getattr(epa, "initialized", False))
        min_tags = int(getattr(epa, "min_tags", 20) or 20)
        upsert(
            "EPA 基底",
            "ok" if initialized else "degraded",
            "" if initialized else f"需 ≥{min_tags} 个 tag 向量",
            "Tag 覆盖率 > 20%",
        )
    return refreshed


def classify_services_health(services: list[Mapping[str, Any]] | None) -> tuple[list[dict], dict]:
    """给服务状态添加角色/严重度，并构造真实但不夸大的总览摘要。"""
    annotated: list[dict] = []
    for service in services or []:
        item = dict(service or {})
        name = str(item.get("name") or "未知服务")
        status = str(item.get("status") or "unknown")
        role = _service_role(name)
        severity = _service_severity(name, status)
        item["name"] = name
        item["status"] = status
        item["role"] = role
        item["severity"] = severity
        annotated.append(item)

    critical_count = sum(1 for item in annotated if item.get("severity") == "critical")
    degraded_count = sum(1 for item in annotated if item.get("severity") == "degraded")
    optional_off_count = sum(1 for item in annotated if item.get("severity") == "disabled")
    ok_count = sum(1 for item in annotated if item.get("severity") == "ok")
    if critical_count > 0:
        overall = "critical"
        label = "异常"
    elif degraded_count > 0 or optional_off_count > 0:
        overall = "degraded"
        label = "可用但降级"
    else:
        overall = "healthy"
        label = "健康"

    return annotated, {
        "overall": overall,
        "label": label,
        "total": len(annotated),
        "ok_count": ok_count,
        "critical_count": critical_count,
        "degraded_count": degraded_count,
        "optional_off_count": optional_off_count,
    }


def _get_services_health(c) -> list:
    """只读取规范健康注册表；注册表缺失时返回显式 unavailable。"""
    try:
        from ...utils.health_registry import get_all_services
        services = get_all_services()
    except Exception as exc:
        return [{"name": "健康注册表", "status": "error", "reason": f"registry_unavailable: {exc}"}]
    if not services:
        return [{"name": "健康注册表", "status": "off", "reason": "registry_empty"}]
    return refresh_dynamic_services_health(c, services)


def count_existing_tagged_memories(conn) -> int:
    """统计正式 RuntimeScope 下已有 scoped Tag 的可用记忆。

    旧 schema 没有 scoped_memory_tags 时保留 memory_tags fallback，便于旧版单测和
    尚未完成迁移的开发数据库继续读取；生产 v2 数据优先走正式 scoped 数据面。
    """
    try:
        return int(conn.execute(
            """SELECT COUNT(DISTINCT m.id)
               FROM memories m
               JOIN scoped_memory_tags smt
                 ON smt.memory_id = m.id
                AND smt.bot_id = m.bot_id
                AND smt.session_id = m.session_id
                AND smt.visibility = m.visibility
              WHERE m.resolution_state = 'resolved'
                AND COALESCE(m.quarantine, 0) = 0
                AND m.visibility = 'group'
                AND m.group_id IS NOT NULL AND m.group_id != ''
                AND m.bot_id IS NOT NULL AND m.bot_id != ''
                AND m.session_id IS NOT NULL AND m.session_id != ''"""
        ).fetchone()[0])
    except Exception:
        return int(conn.execute(
            """SELECT COUNT(DISTINCT m.id)
               FROM memories m
               JOIN memory_tags mt ON mt.memory_id = m.id"""
        ).fetchone()[0])


def count_untagged_memories(conn) -> int:
    """统计当前正式 TagWorker 可以处理的无 Tag 记忆。

    不把 unresolved/隔离/非 group/短文本/noise 或已明确 skipped/failed 的历史记录
    伪装成当前待办；旧 schema 仅用于兼容测试和开发数据库。
    """
    try:
        return int(conn.execute(
            """SELECT COUNT(*) FROM memories m
                WHERE m.resolution_state = 'resolved'
                  AND COALESCE(m.quarantine, 0) = 0
                  AND m.visibility = 'group'
                  AND m.group_id IS NOT NULL AND m.group_id != ''
                  AND m.bot_id IS NOT NULL AND m.bot_id != ''
                  AND m.session_id IS NOT NULL AND m.session_id != ''
                  AND LENGTH(COALESCE(m.content, '')) >= 10
                  AND COALESCE(m.source, '') != 'noise'
                  AND NOT EXISTS (
                      SELECT 1 FROM scoped_memory_tags smt
                       WHERE smt.memory_id = m.id
                         AND smt.bot_id = m.bot_id
                         AND smt.session_id = m.session_id
                         AND smt.visibility = m.visibility
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM tag_extraction_status tes
                       WHERE tes.memory_id = m.id
                         AND tes.status IN ('failed', 'skipped')
                  )"""
        ).fetchone()[0])
    except Exception:
        return int(conn.execute(
            """SELECT COUNT(*) FROM memories m
               WHERE NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = m.id)"""
        ).fetchone()[0])


def _registry_bots(container: Any) -> list[dict[str, Any]]:
    """复用生产 Scope options 的真实 Bot registry，不读取虚构容器私有字段。"""
    source = getattr(container, "scope_options_source", None)
    getter = getattr(source, "get_scope_options", None)
    if not callable(getter):
        return []
    try:
        bots = getter().get("bots", [])
    except Exception:
        return []
    return [
        {
            "qq_id": str(item.get("qq_id") or ""),
            "name": str(item.get("name") or item.get("db_id") or ""),
            "db_id": str(item.get("db_id") or ""),
            "aliases": list(item.get("aliases") or []),
        }
        for item in bots
        if isinstance(item, Mapping) and str(item.get("db_id") or "").strip()
    ]


@system_bp.route("/system", methods=["GET"])
@require_auth
async def system_status():
    """系统健康状态。"""
    c = get_container()
    total_mem = c.db.get_memory_count()
    with_vec = c.db.get_memory_count_with_vector()
    total_tags = c.db.get_tag_count()

    # DB 体积监控 (v1.1.0 #4.4)
    db_size_mb = 0.0
    try:
        db_file = getattr(c.db, 'db_path', '')
        if db_file and os.path.isfile(db_file):
            db_size_mb = round(os.path.getsize(db_file) / (1024 * 1024), 1)
            # WAL 文件也加上
            wal_file = db_file + '-wal'
            if os.path.isfile(wal_file):
                db_size_mb += round(os.path.getsize(wal_file) / (1024 * 1024), 1)
    except Exception:
        pass

    tagged_memories = count_existing_tagged_memories(c.db.conn)

    structured_tags = c.db.conn.execute(
        "SELECT COUNT(*) FROM tags WHERE tag_type != 'keyword'"
    ).fetchone()[0]

    type_dist = c.db.conn.execute(
        "SELECT tag_type, COUNT(*) FROM tags GROUP BY tag_type ORDER BY COUNT(*) DESC"
    ).fetchall()

    cooc_nodes = c.cooccurrence.node_count if c.cooccurrence else 0
    cooc_edges = c.cooccurrence.edge_count if c.cooccurrence else 0

    facts_count = c.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    active_moods = c.db.conn.execute(
        "SELECT group_id, mood_type, intensity, description FROM bot_mood WHERE is_active = 1 AND typeof(start_time) IN ('real', 'integer')"
    ).fetchall()
    person_count = c.db.conn.execute("SELECT COUNT(*) FROM person_registry").fetchone()[0]
    user_profiles_count = c.db.conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]

    # v1.5.0: 认知体系统计
    active_users = c.db.conn.execute(
        "SELECT COUNT(*) FROM user_profiles WHERE interaction_count > 0"
    ).fetchone()[0]
    top_users = c.db.conn.execute(
        "SELECT nickname, interaction_count FROM user_profiles WHERE interaction_count > 0 ORDER BY interaction_count DESC LIMIT 5"
    ).fetchall()

    # 与 /api/options/scopes 共用同一真实 registry 来源。
    registry_bots = _registry_bots(c)

    untagged_count = count_untagged_memories(c.db.conn)
    pending_fewshot = 0
    try:
        pending_fewshot = c.db.conn.execute("SELECT COUNT(*) FROM few_shot_examples WHERE status = 'pending'").fetchone()[0]
    except Exception:
        pass
    has_errors = False
    try:
        from ...utils.health_registry import get_recent_errors
        recent_errs = get_recent_errors(10)
        has_errors = any(e.get("level") == "error" for e in recent_errs)
    except Exception:
        pass

    services_health, services_summary = classify_services_health(_get_services_health(c))

    return jsonify({
        "memories": {"total": total_mem, "with_vector": with_vec, "with_tags": tagged_memories},
        "tags": {"total": total_tags, "structured": structured_tags, "type_distribution": {r[0]: r[1] for r in type_dist}},
        "coverage": {
            "vector_pct": round(with_vec / total_mem * 100, 1) if total_mem > 0 else 0,
            "tag_pct": round(tagged_memories / total_mem * 100, 1) if total_mem > 0 else 0,
        },
        "cooccurrence": {"nodes": cooc_nodes, "edges": cooc_edges},
        "db_size_mb": db_size_mb,
        "epa": {
            "initialized": c.epa.initialized if c.epa else False,
            "reason": "" if (c.epa and c.epa.initialized) else (
                f"需要至少 {c.epa.min_tags} 个带向量的 tag（当前数据不足，持续聊天自动积累后就绪）" if c.epa else "EPA 模块未加载"
            ),
        },
        "services_health": services_health,
        "services_summary": services_summary,
        "lifecycle": {
            "facts": facts_count,
            "persons": person_count,
            "user_profiles": user_profiles_count,
            "active_users": active_users,
            "top_users": [{"name": r[0] or "?", "interactions": r[1]} for r in top_users],
            "active_moods": [{"group_id": m[0], "type": m[1], "intensity": m[2], "desc": m[3]} for m in active_moods],
        },
        "todos": {
            "untagged_count": untagged_count,
            "pending_fewshot": pending_fewshot,
            "has_errors": has_errors,
        },
        "registry_bots": registry_bots
    })


@system_bp.route("/errors", methods=["GET"])
@require_auth
async def recent_errors():
    """最近运行时错误/警告（供 WebUI 展示）。"""
    try:
        from ...utils.health_registry import get_recent_errors, error_count
        return jsonify({"errors": get_recent_errors(30), "total": error_count()})
    except Exception:
        return jsonify({"errors": [], "total": 0})


@system_bp.route("/health", methods=["GET"])
@require_auth
async def health_check():
    """服务健康检查 (US-3.3)。"""
    c = get_container()
    services = {}

    # DB
    try:
        c.db.conn.execute("SELECT 1").fetchone()
        services["database"] = {"status": "ok"}
    except Exception as e:
        services["database"] = {"status": "error", "error": str(e)}

    # Embedding
    services["embedding"] = {"status": "ok" if c.embedding_service else "unavailable"}

    # Memory Index
    count = getattr(c.memory_index, "count", 0)
    services["memory_index"] = {"status": "ok", "count": count}

    # Tag Index
    tag_count = getattr(c.tag_index, "count", 0) if c.tag_index else 0
    services["tag_index"] = {"status": "ok" if c.tag_index else "unavailable", "count": tag_count}

    # Cooccurrence
    services["cooccurrence"] = {"status": "ok" if c.cooccurrence else "unavailable"}

    overall = "healthy" if all(s.get("status") == "ok" for s in services.values()) else "degraded"
    return jsonify({"status": overall, "services": services})


@system_bp.route("/metrics", methods=["GET"])
@require_auth
async def metrics():
    """运行指标 (US-1.5)。"""
    c = get_container()
    return jsonify({
        "memory_count": c.db.get_memory_count(),
        "tag_count": c.db.get_tag_count(),
        "index_count": getattr(c.memory_index, "count", 0),
        "cooccurrence_nodes": c.cooccurrence.node_count if c.cooccurrence else 0,
        "cooccurrence_edges": c.cooccurrence.edge_count if c.cooccurrence else 0,
    })


def _metric_number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _round_metric(value: float) -> float | int:
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded


def build_injection_metric_window(summary: Mapping[str, Any] | None, count: int, from_ts: float, to_ts: float) -> dict:
    """补充时间窗口径摘要，明确 sum 是窗口累计而非单次消耗。"""
    total_summary = (summary or {}).get("total_tokens") if isinstance(summary, Mapping) else {}
    if not isinstance(total_summary, Mapping):
        total_summary = {}
    total_sum = _metric_number(total_summary.get("sum"))
    avg_per_sample = _metric_number(total_summary.get("avg"))
    duration_seconds = max(0.0, float(to_ts) - float(from_ts))
    duration_days = max(duration_seconds / 86400, 1 / 86400)
    return {
        "sample_count": int(count or 0),
        "duration_seconds": _round_metric(duration_seconds),
        "duration_days": _round_metric(duration_days),
        "total_tokens_sum": _round_metric(total_sum),
        "avg_tokens_per_sample": _round_metric(avg_per_sample),
        "avg_tokens_per_day": _round_metric(total_sum / duration_days),
        "p95_tokens_per_sample": _round_metric(_metric_number(total_summary.get("p95"))),
        "max_tokens_per_sample": _round_metric(_metric_number(total_summary.get("max"))),
    }


def _parse_injection_metric_range():
    """解析注入指标查询时间范围。"""
    now = time.time()
    preset = request.args.get("range", "7d")
    preset_seconds = {
        "1d": 86400,
        "3d": 3 * 86400,
        "7d": 7 * 86400,
        "1mo": 31 * 86400,
    }

    from_arg = request.args.get("from")
    to_arg = request.args.get("to")
    if from_arg and to_arg:
        try:
            start = datetime.strptime(from_arg, "%Y-%m-%d")
            end = datetime.strptime(to_arg, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
            from_ts = start.timestamp()
            to_ts = min(end.timestamp(), now)
            span = max(0, to_ts - from_ts)
            range_key = "custom"
        except Exception:
            span = preset_seconds["7d"]
            from_ts = now - span
            to_ts = now
            range_key = "7d"
    else:
        range_key = preset if preset in preset_seconds else "7d"
        span = preset_seconds[range_key]
        from_ts = now - span
        to_ts = now

    if range_key == "1d" or span <= 2 * 86400:
        bucket_seconds = 3600
    elif range_key == "3d":
        bucket_seconds = 3 * 3600
    elif range_key == "7d" or span <= 14 * 86400:
        bucket_seconds = 6 * 3600
    else:
        bucket_seconds = 86400
    return range_key, from_ts, to_ts, bucket_seconds


def build_injection_metrics_payload(db, stats: dict | None, *, range_key: str, from_ts: float, to_ts: float, bucket_seconds: int) -> dict:
    """构造旧聚合注入指标 API payload，便于在无 Quart 环境下集成测试。"""
    payload = dict(stats or {})
    try:
        persisted = db.get_injection_metrics(from_ts, to_ts, bucket_seconds)
        summary = persisted.get("summary", {})
        count = int(persisted.get("count", payload.get("count", 0)) or 0)
        payload.update({
            "range": range_key,
            "from": from_ts,
            "to": to_ts,
            "bucket_seconds": bucket_seconds,
            "summary": summary,
            "series": persisted.get("series", []),
            "ranking": persisted.get("ranking", []),
            "count": count,
            "window": build_injection_metric_window(summary, count, from_ts, to_ts),
        })
    except Exception as e:
        payload.update({
            "range": range_key,
            "from": from_ts,
            "to": to_ts,
            "bucket_seconds": bucket_seconds,
            "summary": {},
            "series": [],
            "ranking": [],
            "count": int(payload.get("count", 0) or 0),
            "window": build_injection_metric_window({}, int(payload.get("count", 0) or 0), from_ts, to_ts),
            "error": str(e),
        })
    return payload


@system_bp.route("/metrics/injection", methods=["GET"])
@require_auth
async def metrics_injection():
    """inject_memory 各通道聚合统计 (US-3.2)。"""
    from ...utils.perf import get_perf_tracker
    tracker = get_perf_tracker()
    stats = tracker.get_injection_stats()
    c = get_container()
    range_key, from_ts, to_ts, bucket_seconds = _parse_injection_metric_range()
    return jsonify(build_injection_metrics_payload(
        c.db,
        stats,
        range_key=range_key,
        from_ts=from_ts,
        to_ts=to_ts,
        bucket_seconds=bucket_seconds,
    ))


@system_bp.route("/metrics/functions", methods=["GET"])
@require_auth
async def metrics_functions():
    """所有 @monitored 函数的 p50/p95 统计。"""
    from ...utils.perf import get_perf_tracker
    tracker = get_perf_tracker()
    return jsonify({"functions": tracker.get_all_stats()})


@system_bp.route("/metrics/cache", methods=["GET"])
@require_auth
async def metrics_cache():
    """缓存命中率统计。"""
    from ...utils.cache import get_cache_manager
    cache = get_cache_manager()
    return jsonify(cache.get_hit_rates())
