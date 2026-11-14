"""WaveMemory v6.0.0 停服离线数据库对齐与瘦身脚本。

重要红线：
- 严禁对运行中的线上数据库执行本脚本！
- 必须先 `docker stop astrbot`（或把快照拷贝到本机安全路径），在离线副本上跑完校验后再切回容器。

完成以下 6 项确定性对齐与瘦身：
1. 将遗留 `user_profiles` 的互动统计与昵称合并进 `person_scene_stats`，并移除 `user_profiles`。
2. 将 `person_unsettled_state` 中历史按群拆分的 `group_id != ''` 行合并到 `group_id = ''` 的「一人一池」。
3. 清理 `memories` 中 `bot_id IS NULL` 的无效/冗余数据（`bzz_experience`、`oni_lore`、`noise` 及空群测试 `chat`），
   并将剩余 `chat`/`core` 完整归属 `yushu`（补齐 `bot_id='yushu'`, `visibility='group'`, `session_id='<prefix>:group:'||group_id`）。
4. 将这批早期 `yushu` 记忆关联的旧 `memory_tags`/`tags` 迁移补全至 `scoped_tags` + `scoped_memory_tags`，并清理已删除记忆的孤儿标签引用。
5. 补齐所有正式 `chat`/`core` 记忆缺失的 `fts_memories_cjk` 中文全文索引。
6. 清理 `scoped_beliefs` 归档表数据、保留最近 N 天的 `background_job_runs`、清空 `*_v51` 修复备份明细（保留空哨兵表以防重复扫描），并执行 `VACUUM` 与 `PRAGMA quick_check`。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.db import fts_cjk
from engine.db.migrations.person_scene_stats import _PERSON_SCENE_STATS_SCHEMA

MAX_UNSETTLED_TRACES = 20
MAX_UNSETTLED_ENERGY = 2.0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _resolve_yushu_prefix(conn: sqlite3.Connection) -> str:
    if not _table_exists(conn, "bot_profiles"):
        return "羽书"
    row = conn.execute(
        "SELECT payload FROM bot_profiles WHERE db_id='yushu' LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return "羽书"
    try:
        payload = json.loads(row[0])
        prefix = str(payload.get("session_prefix") or payload.get("name") or "").strip()
        return prefix or "羽书"
    except Exception:
        return "羽书"


def _ensure_person_scene_stats(conn: sqlite3.Connection) -> None:
    for stmt in _PERSON_SCENE_STATS_SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _migrate_user_profiles(conn: sqlite3.Connection, *, apply: bool) -> dict[str, int]:
    if not _table_exists(conn, "user_profiles"):
        return {"source_rows": 0, "migrated_rows": 0, "dropped_table": 0}
    source_rows = int(conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0])
    if not apply:
        return {"source_rows": source_rows, "migrated_rows": source_rows, "dropped_table": 1}

    _ensure_person_scene_stats(conn)
    now = time.time()
    conn.execute(
        """
        INSERT INTO person_scene_stats (
            bot_id, group_id, user_id, nickname, interaction_count, first_seen, last_seen, updated_at
        )
        SELECT
            COALESCE(NULLIF(TRIM(bot_id), ''), 'yushu'),
            COALESCE(TRIM(group_id), ''),
            TRIM(user_id),
            COALESCE(TRIM(nickname), ''),
            COALESCE(interaction_count, 0),
            COALESCE(first_seen, 0),
            COALESCE(last_seen, 0),
            ?
        FROM user_profiles
        WHERE TRIM(COALESCE(user_id, '')) != ''
          AND TRIM(COALESCE(group_id, '')) != ''
        ON CONFLICT(bot_id, group_id, user_id) DO UPDATE SET
            nickname = CASE
                WHEN TRIM(excluded.nickname) != '' AND excluded.last_seen >= person_scene_stats.last_seen
                    THEN excluded.nickname
                WHEN TRIM(person_scene_stats.nickname) = '' THEN excluded.nickname
                ELSE person_scene_stats.nickname
            END,
            interaction_count = MAX(person_scene_stats.interaction_count, excluded.interaction_count),
            first_seen = CASE
                WHEN person_scene_stats.first_seen <= 0 THEN excluded.first_seen
                WHEN excluded.first_seen > 0 THEN MIN(person_scene_stats.first_seen, excluded.first_seen)
                ELSE person_scene_stats.first_seen
            END,
            last_seen = MAX(person_scene_stats.last_seen, excluded.last_seen),
            updated_at = MAX(person_scene_stats.updated_at, excluded.updated_at)
        """,
        (now,),
    )
    migrated = int(conn.execute("SELECT COUNT(*) FROM person_scene_stats").fetchone()[0])
    conn.execute("DROP TABLE user_profiles")
    return {"source_rows": source_rows, "migrated_rows": migrated, "dropped_table": 1}


def _merge_unsettled_state(conn: sqlite3.Connection, *, apply: bool) -> dict[str, int]:
    if not _table_exists(conn, "person_unsettled_state"):
        return {"legacy_group_rows": 0, "merged_pairs": 0}
    legacy_rows = conn.execute(
        """
        SELECT bot_id, user_id, group_id, energy, interaction_count, traces, updated_at
        FROM person_unsettled_state
        WHERE COALESCE(group_id, '') != ''
        """
    ).fetchall()
    pairs = {(r[0], r[1]) for r in legacy_rows}
    if not apply or not legacy_rows:
        return {"legacy_group_rows": len(legacy_rows), "merged_pairs": len(pairs)}

    for bot_id, user_id in sorted(pairs):
        all_rows = conn.execute(
            """
            SELECT group_id, energy, interaction_count, traces, updated_at
            FROM person_unsettled_state
            WHERE bot_id=? AND user_id=?
            """,
            (bot_id, user_id),
        ).fetchall()
        total_energy = 0.0
        total_count = 0
        latest_updated = 0.0
        merged_traces: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for _gid, energy, count, traces_raw, updated_at in all_rows:
            total_energy += float(energy or 0.0)
            total_count += int(count or 0)
            latest_updated = max(latest_updated, float(updated_at or 0.0))
            try:
                parsed = json.loads(traces_raw or "[]")
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    dedup_key = (
                        str(item.get("quote") or item.get("summary") or ""),
                        str(item.get("ts") or ""),
                    )
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    merged_traces.append(item)
        merged_traces.sort(key=lambda x: float(x.get("ts") or 0.0))
        if len(merged_traces) > MAX_UNSETTLED_TRACES:
            merged_traces = merged_traces[-MAX_UNSETTLED_TRACES:]
        total_energy = min(MAX_UNSETTLED_ENERGY, max(0.0, total_energy))
        conn.execute("DELETE FROM person_unsettled_state WHERE bot_id=? AND user_id=?", (bot_id, user_id))
        conn.execute(
            """
            INSERT INTO person_unsettled_state (bot_id, user_id, group_id, energy, interaction_count, traces, updated_at)
            VALUES (?, ?, '', ?, ?, ?, ?)
            """,
            (
                bot_id,
                user_id,
                round(total_energy, 6),
                total_count,
                json.dumps(merged_traces, ensure_ascii=False),
                latest_updated or time.time(),
            ),
        )
    return {"legacy_group_rows": len(legacy_rows), "merged_pairs": len(pairs)}


def _align_null_bot_memories(conn: sqlite3.Connection, *, apply: bool) -> dict[str, int]:
    if not _table_exists(conn, "memories"):
        return {"deleted_obsolete": 0, "aligned_yushu": 0, "migrated_scoped_tags": 0}

    prefix = _resolve_yushu_prefix(conn)
    obsolete_ids = [
        int(r[0])
        for r in conn.execute(
            """
            SELECT id FROM memories
            WHERE bot_id IS NULL
              AND (
                  COALESCE(source, '') IN ('bzz_experience', 'oni_lore', 'noise')
                  OR COALESCE(TRIM(group_id), '') = ''
              )
            """
        ).fetchall()
    ]
    align_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE bot_id IS NULL
              AND COALESCE(source, '') IN ('chat', 'core')
              AND COALESCE(TRIM(group_id), '') != ''
            """
        ).fetchone()[0]
    )
    missing_tag_links = 0
    if _table_exists(conn, "memory_tags") and _table_exists(conn, "tags"):
        missing_tag_links = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_tags mt
                JOIN memories m ON m.id = mt.memory_id
                WHERE m.bot_id IS NULL
                  AND COALESCE(m.source, '') IN ('chat', 'core')
                  AND COALESCE(TRIM(m.group_id), '') != ''
                """
            ).fetchone()[0]
        )

    if not apply:
        return {
            "deleted_obsolete": len(obsolete_ids),
            "aligned_yushu": align_count,
            "migrated_scoped_tags": missing_tag_links,
        }

    # 1. 删除无效/冗余 bot_id=NULL 行（先建主键索引 TEMP 表，避免多轮全表扫描）
    if obsolete_ids:
        has_mt = _table_exists(conn, "memory_tags")
        has_cjk = _table_exists(conn, "fts_memories_cjk")
        conn.execute("DROP TABLE IF EXISTS temp._v6_obsolete_mids")
        conn.execute("CREATE TEMP TABLE _v6_obsolete_mids (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO _v6_obsolete_mids(id) VALUES (?)", ((mid,) for mid in obsolete_ids))
        if has_mt:
            conn.execute("DELETE FROM memory_tags WHERE memory_id IN (SELECT id FROM _v6_obsolete_mids)")
        if has_cjk:
            conn.execute("DELETE FROM fts_memories_cjk WHERE rowid IN (SELECT id FROM _v6_obsolete_mids)")
        conn.execute("DELETE FROM memories WHERE id IN (SELECT id FROM _v6_obsolete_mids)")
        conn.execute("DROP TABLE IF EXISTS temp._v6_obsolete_mids")

    # 2. 对齐剩余正式 chat/core 记忆到 yushu（主键索引 TEMP 表记录本次对齐的 id，避免全表扫）
    conn.execute("DROP TABLE IF EXISTS temp._v6_aligned_mids")
    conn.execute(
        """
        CREATE TEMP TABLE _v6_aligned_mids (
            memory_id INTEGER PRIMARY KEY,
            target_session_id TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO _v6_aligned_mids(memory_id, target_session_id)
        SELECT id,
               CASE
                   WHEN COALESCE(TRIM(session_id), '') = '' THEN ? || ':group:' || TRIM(group_id)
                   ELSE session_id
               END
        FROM memories
        WHERE bot_id IS NULL
          AND COALESCE(source, '') IN ('chat', 'core')
          AND COALESCE(TRIM(group_id), '') != ''
        """,
        (prefix,),
    )
    conn.execute(
        """
        UPDATE memories
        SET bot_id = 'yushu',
            visibility = COALESCE(NULLIF(TRIM(visibility), ''), 'group'),
            session_id = COALESCE(
                (SELECT target_session_id FROM _v6_aligned_mids a WHERE a.memory_id = memories.id),
                session_id
            )
        WHERE id IN (SELECT memory_id FROM _v6_aligned_mids)
        """
    )

    # 3. 迁移这些记忆遗留在 memory_tags/tags 中的标签到 scoped_tags/scoped_memory_tags
    migrated_tags = 0
    if _table_exists(conn, "memory_tags") and _table_exists(conn, "tags") and _table_exists(conn, "scoped_tags") and _table_exists(conn, "scoped_memory_tags"):
        now = time.time()
        conn.execute(
            """
            INSERT OR IGNORE INTO scoped_tags (
                bot_id, session_id, visibility, name, tag_type, description,
                confidence, metadata, created_at, updated_at, revision, status, aliases
            )
            SELECT DISTINCT
                'yushu',
                a.target_session_id,
                'group',
                t.name,
                COALESCE(NULLIF(t.tag_type, ''), 'keyword'),
                COALESCE(t.description, ''),
                COALESCE(t.confidence, 0.0),
                COALESCE(t.metadata, '{}'),
                COALESCE(t.created_at, ?),
                COALESCE(t.updated_at, COALESCE(t.created_at, ?)),
                1,
                'active',
                COALESCE(t.aliases, '[]')
            FROM _v6_aligned_mids a
            JOIN memory_tags mt ON mt.memory_id = a.memory_id
            JOIN tags t ON t.id = mt.tag_id
            """,
            (now, now),
        )
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO scoped_memory_tags (
                bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at
            )
            SELECT
                'yushu',
                a.target_session_id,
                'group',
                a.memory_id,
                st.id,
                COALESCE(mt.position, 0),
                COALESCE(mt.relevance, 1.0),
                COALESCE(t.created_at, ?)
            FROM _v6_aligned_mids a
            JOIN memory_tags mt ON mt.memory_id = a.memory_id
            JOIN tags t ON t.id = mt.tag_id
            JOIN scoped_tags st
              ON st.bot_id = 'yushu'
             AND st.session_id = a.target_session_id
             AND st.visibility = 'group'
             AND st.name = t.name
            """,
            (now,),
        )
        migrated_tags = max(0, int(cur.rowcount or 0))
    conn.execute("DROP TABLE IF EXISTS temp._v6_aligned_mids")

    return {
        "deleted_obsolete": len(obsolete_ids),
        "aligned_yushu": align_count,
        "migrated_scoped_tags": migrated_tags,
    }


def _repair_cross_bot_sender_ids(conn: sqlite3.Connection, *, apply: bool) -> dict[str, int]:
    """还原跨 Bot 误标为 ``sender_id='bot'`` 的记录为对应 Bot 的真实 QQ 号。

    历史写入逻辑在 ``sender_id in self._bot_qq_ids`` 时统一写成 ``sender_id='bot'``，
    导致 ``bot_id='baizz'`` 下面混入大量 ``sender_name='羽书'`` 且 ``sender_id='bot'`` 的发言，
    注入时被错贴成 ``[你的历史回复]``。本函数根据 ``bot_profiles`` 的 ``db_id -> name / qq_id``
    映射，找出「记忆所属 ``bot_id`` 的名字集合不包含该记录 ``sender_name``，而恰好属于另一只己方
    Bot 的名字」的错标行，将其 ``sender_id`` 恢复为那只发言 Bot 的主 ``qq_id``。
    """
    if not _table_exists(conn, "memories") or not _table_exists(conn, "bot_profiles"):
        return {"mismatched_rows": 0, "repaired_rows": 0}

    profiles_rows = conn.execute("SELECT db_id, payload FROM bot_profiles").fetchall()
    bot_names: dict[str, set[str]] = {}
    name_to_qq: dict[str, str] = {}
    for db_id, payload_raw in profiles_rows:
        db_key = str(db_id or "").strip()
        if not db_key or not payload_raw:
            continue
        try:
            payload = json.loads(payload_raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        qq_id = str(payload.get("qq_id") or "").strip()
        if not qq_id:
            for b in payload.get("bindings") or []:
                if isinstance(b, dict) and b.get("host") == "astrbot" and str(b.get("self_id") or "").strip():
                    qq_id = str(b.get("self_id")).strip()
                    break
        names: set[str] = set()
        raw_name = str(payload.get("name") or "").strip()
        if raw_name:
            names.add(raw_name.casefold())
        for alias in payload.get("aliases") or []:
            text = str(alias or "").strip()
            if text:
                names.add(text.casefold())
        bot_names[db_key] = names
        if raw_name and qq_id:
            name_to_qq.setdefault(raw_name.casefold(), qq_id)
        for alias in payload.get("aliases") or []:
            text = str(alias or "").strip()
            if text and qq_id:
                name_to_qq.setdefault(text.casefold(), qq_id)

    if len(bot_names) < 2 or not name_to_qq:
        return {"mismatched_rows": 0, "repaired_rows": 0}

    candidates = conn.execute(
        """
        SELECT id, COALESCE(bot_id, ''), COALESCE(sender_name, '')
        FROM memories
        WHERE sender_id = 'bot'
          AND COALESCE(TRIM(sender_name), '') != ''
        """
    ).fetchall()

    repairs: list[tuple[str, int]] = []
    for mid, mem_bot_id, s_name in candidates:
        s_key = str(s_name or "").strip().casefold()
        if not s_key:
            continue
        target_qq = name_to_qq.get(s_key)
        if not target_qq:
            continue
        own_names = bot_names.get(str(mem_bot_id or "").strip(), set())
        if s_key in own_names:
            continue
        repairs.append((target_qq, int(mid)))

    if not apply or not repairs:
        return {"mismatched_rows": len(repairs), "repaired_rows": 0}

    conn.executemany("UPDATE memories SET sender_id = ? WHERE id = ?", repairs)
    return {"mismatched_rows": len(repairs), "repaired_rows": len(repairs)}


def _backfill_fts_cjk(conn: sqlite3.Connection, *, apply: bool) -> dict[str, int]:
    if not _table_exists(conn, "memories") or not _table_exists(conn, "fts_memories_cjk"):
        return {"missing_rows": 0, "indexed_rows": 0}
    missing_ids = [
        int(r[0])
        for r in conn.execute(
            """
            SELECT m.id
            FROM memories m
            WHERE COALESCE(TRIM(m.group_id), '') != ''
              AND COALESCE(m.source, '') IN ('chat', 'core')
              AND COALESCE(m.content, '') != ''
              AND COALESCE(m.quarantine, 0) = 0
              AND COALESCE(m.memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')
              AND NOT EXISTS (
                  SELECT 1 FROM fts_memories_cjk f WHERE f.rowid = m.id
              )
            """
        ).fetchall()
    ]
    if not apply or not missing_ids:
        return {"missing_rows": len(missing_ids), "indexed_rows": 0}

    indexed = 0
    for mid in missing_ids:
        if fts_cjk.sync_memory(conn, mid) == "indexed":
            indexed += 1
    return {"missing_rows": len(missing_ids), "indexed_rows": indexed}


def _prune_auxiliary_tables(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    retention_days: int,
    prune_outbox_days: int = 0,
    prune_traces_days: int = 0,
) -> dict[str, int]:
    stats: dict[str, int] = {
        "cleared_v51_backup_rows": 0,
        "cleared_scoped_beliefs": 0,
        "pruned_job_runs": 0,
        "pruned_orphan_memory_tags": 0,
        "pruned_outbox_rows": 0,
        "pruned_trace_rows": 0,
    }
    cutoff = time.time() - max(1, retention_days) * 86400.0

    for tbl in ("memory_importance_repair_v51", "tag_status_repair_v51", "display_name_repair_v51"):
        if _table_exists(conn, tbl):
            cnt = int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
            stats["cleared_v51_backup_rows"] += cnt
            if apply and cnt:
                # 只清明细不删表，保留「已修复哨兵表」避免下次启动重新扫全表
                conn.execute(f"DELETE FROM {tbl}")

    if _table_exists(conn, "scoped_beliefs"):
        cnt = int(conn.execute("SELECT COUNT(*) FROM scoped_beliefs").fetchone()[0])
        stats["cleared_scoped_beliefs"] = cnt
        if apply and cnt:
            conn.execute("DELETE FROM scoped_beliefs")

    if _table_exists(conn, "background_job_runs"):
        cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM background_job_runs WHERE created_at < ? AND status IN ('succeeded','failed','cancelled','completed')",
                (cutoff,),
            ).fetchone()[0]
        )
        stats["pruned_job_runs"] = cnt
        if apply and cnt:
            conn.execute(
                "DELETE FROM background_job_runs WHERE created_at < ? AND status IN ('succeeded','failed','cancelled','completed')",
                (cutoff,),
            )

    if _table_exists(conn, "memory_tags") and _table_exists(conn, "memories"):
        cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_tags WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = memory_tags.memory_id)"
            ).fetchone()[0]
        )
        stats["pruned_orphan_memory_tags"] = cnt
        if apply and cnt:
            conn.execute(
                "DELETE FROM memory_tags WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = memory_tags.memory_id)"
            )

    if prune_outbox_days > 0:
        outbox_cutoff = time.time() - prune_outbox_days * 86400.0
        if _table_exists(conn, "domain_outbox") and _table_exists(conn, "outbox_deliveries"):
            old_eids = [
                r[0]
                for r in conn.execute(
                    """
                    SELECT d.event_id FROM domain_outbox d
                    WHERE d.created_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM outbox_deliveries od
                          WHERE od.event_id = d.event_id AND od.state != 'completed'
                      )
                    """,
                    (outbox_cutoff,),
                ).fetchall()
            ]
            stats["pruned_outbox_rows"] += len(old_eids)
            if apply and old_eids:
                conn.execute("DROP TABLE IF EXISTS temp._v6_old_events")
                conn.execute("CREATE TEMP TABLE _v6_old_events (event_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO _v6_old_events(event_id) VALUES (?)", ((e,) for e in old_eids))
                conn.execute("DELETE FROM outbox_deliveries WHERE event_id IN (SELECT event_id FROM _v6_old_events)")
                conn.execute("DELETE FROM domain_outbox WHERE event_id IN (SELECT event_id FROM _v6_old_events)")
                conn.execute("DROP TABLE IF EXISTS temp._v6_old_events")
        if _table_exists(conn, "write_operations"):
            wo_cnt = int(
                conn.execute(
                    "SELECT COUNT(*) FROM write_operations WHERE status = 'committed' AND COALESCE(committed_at, created_at) < ?",
                    (outbox_cutoff,),
                ).fetchone()[0]
            )
            stats["pruned_outbox_rows"] += wo_cnt
            if apply and wo_cnt:
                conn.execute(
                    "DELETE FROM write_operations WHERE status = 'committed' AND COALESCE(committed_at, created_at) < ?",
                    (outbox_cutoff,),
                )

    if prune_traces_days > 0 and _table_exists(conn, "injection_traces"):
        trace_cutoff = time.time() - prune_traces_days * 86400.0
        t_cnt = int(conn.execute("SELECT COUNT(*) FROM injection_traces WHERE timestamp < ?", (trace_cutoff,)).fetchone()[0])
        stats["pruned_trace_rows"] = t_cnt
        if apply and t_cnt:
            if _table_exists(conn, "injection_trace_channels"):
                conn.execute(
                    "DELETE FROM injection_trace_channels WHERE trace_id IN (SELECT trace_id FROM injection_traces WHERE timestamp < ?)",
                    (trace_cutoff,),
                )
            conn.execute("DELETE FROM injection_traces WHERE timestamp < ?", (trace_cutoff,))

    return stats


def cleanup_database(
    db_path: Path,
    *,
    apply: bool = False,
    retention_days: int = 14,
    prune_outbox_days: int = 0,
    prune_traces_days: int = 0,
    vacuum: bool = True,
) -> dict[str, Any]:
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")

    size_before = path.stat().st_size
    if apply:
        conn = sqlite3.connect(str(path), timeout=60.0)
    else:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)

    try:
        if apply:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")

        report: dict[str, Any] = {
            "db_path": str(path),
            "mode": "apply" if apply else "dry_run",
            "size_before_bytes": size_before,
            "user_profiles": _migrate_user_profiles(conn, apply=apply),
            "unsettled_state": _merge_unsettled_state(conn, apply=apply),
            "null_bot_memories": _align_null_bot_memories(conn, apply=apply),
            "cross_bot_sender_repairs": _repair_cross_bot_sender_ids(conn, apply=apply),
            "fts_cjk": _backfill_fts_cjk(conn, apply=apply),
            "auxiliary": _prune_auxiliary_tables(
                conn,
                apply=apply,
                retention_days=retention_days,
                prune_outbox_days=prune_outbox_days,
                prune_traces_days=prune_traces_days,
            ),
        }

        if apply:
            conn.commit()
            if vacuum:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    pass
                conn.execute("VACUUM")
            quick = conn.execute("PRAGMA quick_check").fetchone()
            report["quick_check"] = quick[0] if quick else "unknown"
            report["size_after_bytes"] = path.stat().st_size
        else:
            report["quick_check"] = "skipped (dry_run)"
            report["size_after_bytes"] = size_before
        return report
    except BaseException:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WaveMemory v6.0.0 停服离线库对齐与瘦身")
    parser.add_argument("--db", required=True, help="离线副本 wave_memory.db 路径")
    parser.add_argument("--apply", action="store_true", help="执行实际写入与 VACUUM（默认仅 dry-run 报告）")
    parser.add_argument("--retention-days", type=int, default=14, help="background_job_runs 保留天数（默认 14）")
    parser.add_argument("--prune-outbox-days", type=int, default=0, help="可选：清理 N 天前已完成的 domain_outbox/outbox_deliveries/write_operations（0=不清理）")
    parser.add_argument("--prune-traces-days", type=int, default=0, help="可选：清理 N 天前的 injection_traces/channels（0=不清理）")
    parser.add_argument("--no-vacuum", action="store_true", help="执行写入后跳过 VACUUM")
    args = parser.parse_args(argv)

    result = cleanup_database(
        Path(args.db),
        apply=args.apply,
        retention_days=args.retention_days,
        prune_outbox_days=args.prune_outbox_days,
        prune_traces_days=args.prune_traces_days,
        vacuum=not args.no_vacuum,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
