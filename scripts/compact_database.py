"""离线压缩 wave_memory.db（v5.1 存储瘦身）。**必须在 AstrBot 停止时运行**，否则会与插件的写线程抢锁。

做的事（每步都可重复执行，已经做过的会跳过）：

1. WAL checkpoint；
2. 清理超过保留期的 outbox 事件、已完成投递、写操作日志与投影水位（与插件内 7 天清理同一逻辑）；
3. 超过 ``--trace-payload-days`` 的注入 trace 去掉完整载荷，超过 ``--trace-days`` 的整条删除；
4. 删除重复的前缀索引；
5. 中文全文索引 ``fts_memories_cjk`` 重建为不存正文的 contentless 表（SQLite 3.43+）；
6. 旧全文索引合并碎片段（optimize）；
7. VACUUM 并 quick_check。

Docker 部署的用法（容器已停）::

    docker run --rm --entrypoint python -v astrbot_data_runtime:/AstrBot/data \
        -v <插件源码目录>:/src:ro soulter/astrbot:latest \
        /src/scripts/compact_database.py /AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db --plugin /src

加 ``--dry-run`` 只统计不修改。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

REDUNDANT_INDEXES = ("idx_scoped_tags_scope_name", "idx_scoped_memory_tags_scope_memory")
CJK = "fts_memories_cjk"


def _mb(value: float) -> str:
    return f"{value / 1e6:,.0f} MB"


def _file_size(path: str) -> int:
    return sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}


def _step(title: str):
    print(f"\n== {title}", flush=True)
    return time.perf_counter()


def _done(started: float, detail: str = "") -> None:
    print(f"   完成 {time.perf_counter() - started:.1f}s {detail}", flush=True)


def prune_outbox(conn: sqlite3.Connection, days: float, dry_run: bool) -> None:
    from engine.db.outbox_repo import OutboxRepository

    started = _step(f"清理 {days:g} 天前的 outbox / 写操作 / 投影水位")
    before = time.time() - days * 86400
    if dry_run:
        count = conn.execute(
            "SELECT COUNT(*) FROM domain_outbox WHERE archived_at IS NOT NULL AND archived_at < ?", (before,)
        ).fetchone()[0]
        _done(started, f"(dry-run) 可删事件约 {count:,}")
        return
    totals = {"events": 0, "deliveries": 0, "operations": 0, "projections": 0}
    while True:
        result = OutboxRepository.prune_history(conn, before=before, limit=50000)
        conn.commit()
        for key in totals:
            totals[key] += int(result.get(key, 0))
        if not any(result.values()):
            break
        print(f"   ... {totals}", flush=True)
    _done(started, str(totals))


def trim_traces(conn: sqlite3.Connection, payload_days: float, trace_days: float, dry_run: bool) -> None:
    if "injection_traces" not in _tables(conn):
        return
    started = _step(f"注入 trace：{payload_days:g} 天前去掉载荷，{trace_days:g} 天前删除")
    now = time.time()
    if dry_run:
        count = conn.execute(
            "SELECT COUNT(*) FROM injection_traces WHERE timestamp < ? AND payload_json IS NOT NULL",
            (now - payload_days * 86400,),
        ).fetchone()[0]
        _done(started, f"(dry-run) 可去载荷 {count:,} 条")
        return
    stale = [row[0] for row in conn.execute(
        "SELECT trace_id FROM injection_traces WHERE timestamp < ?", (now - trace_days * 86400,)
    )]
    for start in range(0, len(stale), 500):
        chunk = stale[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM injection_trace_channels WHERE trace_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM injection_traces WHERE trace_id IN ({marks})", chunk)
    stripped = conn.execute(
        "UPDATE injection_traces SET payload_json=NULL WHERE timestamp < ? AND payload_json IS NOT NULL",
        (now - payload_days * 86400,),
    ).rowcount
    conn.commit()
    _done(started, f"删除 {len(stale):,} 条，去载荷 {stripped:,} 条")


def drop_redundant_indexes(conn: sqlite3.Connection, dry_run: bool) -> None:
    started = _step("删除重复索引")
    present = [name for name in REDUNDANT_INDEXES if name in _tables(conn)]
    if not dry_run:
        for name in present:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()
    _done(started, ", ".join(present) or "无")


def rebuild_cjk_contentless(conn: sqlite3.Connection, dry_run: bool) -> None:
    started = _step("中文全文索引改为 contentless")
    tables = _tables(conn)
    if CJK not in tables:
        _done(started, "没有中文索引，跳过")
        return
    if f"{CJK}_content" not in tables:
        _done(started, "已经是 contentless，跳过")
        return
    if sqlite3.sqlite_version_info < (3, 43, 0):
        _done(started, f"SQLite {sqlite3.sqlite_version} 不支持 contentless_delete，跳过")
        return
    rows = conn.execute(f"SELECT COUNT(*) FROM {CJK}").fetchone()[0]
    if dry_run:
        _done(started, f"(dry-run) 将重建 {rows:,} 行")
        return
    conn.execute(f"DROP TABLE IF EXISTS {CJK}_rebuild")
    conn.execute(
        f"CREATE VIRTUAL TABLE {CJK}_rebuild USING fts5(tokens, content='', contentless_delete=1, tokenize='unicode61')"
    )
    conn.execute(f"INSERT INTO {CJK}_rebuild(rowid, tokens) SELECT rowid, tokens FROM {CJK}")
    copied = conn.execute(f"SELECT COUNT(*) FROM {CJK}_rebuild").fetchone()[0]
    if copied != rows:
        conn.rollback()
        raise SystemExit(f"中文索引重建行数不一致：{copied} != {rows}，已回滚")
    conn.execute(f"DROP TABLE {CJK}")
    conn.execute(f"ALTER TABLE {CJK}_rebuild RENAME TO {CJK}")
    conn.execute(f"INSERT INTO {CJK}({CJK}) VALUES ('optimize')")
    conn.commit()
    _done(started, f"{rows:,} 行")


def optimize_legacy_fts(conn: sqlite3.Connection, dry_run: bool) -> None:
    if "fts_memories" not in _tables(conn) or dry_run:
        return
    started = _step("旧全文索引合并碎片段")
    conn.execute("INSERT INTO fts_memories(fts_memories) VALUES ('optimize')")
    conn.commit()
    _done(started)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db")
    parser.add_argument("--plugin", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--outbox-days", type=float, default=7.0)
    parser.add_argument("--trace-payload-days", type=float, default=3.0)
    parser.add_argument("--trace-days", type=float, default=14.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-vacuum", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, args.plugin)
    if not os.path.exists(args.db):
        raise SystemExit(f"找不到数据库：{args.db}")
    print(f"SQLite {sqlite3.sqlite_version}；数据库 {args.db}；压缩前 {_mb(_file_size(args.db))}", flush=True)

    conn = sqlite3.connect(args.db, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        # 拿不到写锁说明 AstrBot 还在运行
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as error:
        raise SystemExit(f"数据库正被占用（{error}），请先停止 AstrBot")

    started = _step("WAL checkpoint")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _done(started)

    prune_outbox(conn, args.outbox_days, args.dry_run)
    trim_traces(conn, args.trace_payload_days, args.trace_days, args.dry_run)
    drop_redundant_indexes(conn, args.dry_run)
    rebuild_cjk_contentless(conn, args.dry_run)
    optimize_legacy_fts(conn, args.dry_run)

    if not args.dry_run and not args.skip_vacuum:
        started = _step("VACUUM（需要数分钟）")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _done(started)

    started = _step("quick_check")
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    _done(started, result)
    conn.close()
    print(f"\n压缩后 {_mb(_file_size(args.db))}", flush=True)
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
