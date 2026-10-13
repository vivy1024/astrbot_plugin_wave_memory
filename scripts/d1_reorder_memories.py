"""D1：把 memories.vector 挪到最后一列，让常用过滤列不再落进溢出页。

背景：一行约 4.7KB，超出 4KB 页。SQLite 只把一行的前一段放在主页里，其余放溢出页；
vector 排在第 6 列时，它后面的 timestamp/source/bot_id/session_id/quarantine… 都在溢出页，
按这些列过滤就要把整张表（含 1.5GB 向量）读一遍。vector 放最后后，前面各列总能完整留在
主页里，只有需要向量时才读溢出页。代码不用改：全仓库读 memories 都按列名取值。

只在数据库没有其他连接时运行（线上先停 astrbot 容器）。原库不动::

    python d1_reorder_memories.py <原库> <工作副本> <输出库> [--skip-vacuum]

1. 复制 原库 → 工作副本；
2. 在工作副本里按 SQLite 官方「12 步改表」流程新建表、按 id 顺序复制、删旧表、改名，
   重建索引与全文检索触发器，保留 sqlite_sequence；
3. VACUUM INTO 输出库（紧凑、不带 WAL），输出库切回 WAL；
4. 校验：所有表行数一致、memories 逐行（按列名）哈希一致、quick_check、外键检查。
每一步打印耗时。任何校验失败都以非 0 退出，输出库不能用。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import time

TABLE = "memories"
TMP = "memories__d1"
MOVE_LAST = "vector"
_UNSUPPORTED = ("CHECK", "UNIQUE", "REFERENCES", "COLLATE", "GENERATED", "CONSTRAINT")


def _step(name: str, started: float) -> float:
    now = time.perf_counter()
    print(f"  [{now - started:7.1f}s] {name}", flush=True)
    return now


def _column_defs(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    upper = sql.upper()
    for word in _UNSUPPORTED:
        if word in upper:
            raise SystemExit(f"memories 定义里有 {word}，脚本没处理这种约束，中止")
    autoincrement = "AUTOINCREMENT" in upper
    info = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
    names = [row[1] for row in info]
    if MOVE_LAST not in names:
        raise SystemExit("memories 没有 vector 列，中止")
    order = [name for name in names if name != MOVE_LAST] + [MOVE_LAST]
    by_name = {row[1]: row for row in info}
    defs = []
    for name in order:
        _cid, _name, ctype, notnull, default, pk = by_name[name]
        parts = [f'"{name}"', ctype or ""]
        if pk:
            parts.append("PRIMARY KEY AUTOINCREMENT" if autoincrement else "PRIMARY KEY")
        if notnull:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")
        defs.append(" ".join(part for part in parts if part))
    return order, defs


def reorder(work: str) -> None:
    started = time.perf_counter()
    conn = sqlite3.connect(work, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")  # 大事务用回滚日志：新页追加在文件尾，日志很小
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA cache_size=-262144")  # 256MB
    order, defs = _column_defs(conn)
    if [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")][-1] == MOVE_LAST:
        print("vector 已经在最后一列，不用重排")
        conn.close()
        return
    extras = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name=? AND name!=? AND sql IS NOT NULL",
        (TABLE, TABLE),
    ).fetchall()
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (TABLE,)).fetchone()
    cols = ", ".join(f'"{name}"' for name in order)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"CREATE TABLE {TMP} ({', '.join(defs)})")
        conn.execute(f"INSERT INTO {TMP} ({cols}) SELECT {cols} FROM {TABLE} ORDER BY id")
        t = _step("复制行到新表", started)
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE {TMP} RENAME TO {TABLE}")
        for kind, name, sql in sorted(extras, key=lambda item: item[0] != "index"):
            conn.execute(sql)
        _step(f"删旧表、改名、重建 {sum(1 for e in extras if e[0] == 'index')} 个索引与 "
              f"{sum(1 for e in extras if e[0] == 'trigger')} 个触发器", t)
        if seq is not None:
            conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (TABLE,))
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (TABLE, int(seq[0])))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def vacuum_into(work: str, out: str) -> None:
    started = time.perf_counter()
    if os.path.exists(out):
        raise SystemExit(f"{out} 已存在，中止")
    conn = sqlite3.connect(work, isolation_level=None)
    conn.execute("VACUUM INTO ?", (out,))
    conn.close()
    _step("VACUUM INTO 输出库", started)
    conn = sqlite3.connect(out, isolation_level=None)
    print("  输出库 journal_mode =", conn.execute("PRAGMA journal_mode=WAL").fetchone()[0])
    conn.close()


def _row_digest(conn: sqlite3.Connection, cols: list[str]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM {TABLE} ORDER BY id")
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for row in rows:
            digest.update(repr(row).encode("utf-8", "surrogatepass"))
        count += len(rows)
    return count, digest.hexdigest()


def verify(src: str, out: str) -> bool:
    started = time.perf_counter()
    a = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
    b = sqlite3.connect(out)
    ok = True
    tables = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    tables_b = [r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    if tables != tables_b:
        print("  表清单不一致:", set(tables) ^ set(tables_b))
        ok = False
    for name in tables:
        if name.startswith("sqlite_"):
            continue
        ca = a.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        cb = b.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        if ca != cb:
            print(f"  行数不一致 {name}: {ca} vs {cb}")
            ok = False
    t = _step(f"{len(tables)} 张表行数核对", started)
    cols = [row[1] for row in a.execute(f"PRAGMA table_info({TABLE})")]
    new_cols = [row[1] for row in b.execute(f"PRAGMA table_info({TABLE})")]
    print("  新列顺序末尾:", new_cols[-3:])
    if sorted(cols) != sorted(new_cols) or new_cols[-1] != MOVE_LAST:
        print("  列集合不一致或 vector 不在最后")
        ok = False
    ra, rb = _row_digest(a, cols), _row_digest(b, cols)
    print(f"  memories 原库 {ra[0]} 行 {ra[1][:16]}… / 新库 {rb[0]} 行 {rb[1][:16]}…")
    ok = ok and ra == rb
    t = _step("memories 逐行哈希", t)
    seq_a = a.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (TABLE,)).fetchone()
    seq_b = b.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (TABLE,)).fetchone()
    if seq_a != seq_b:
        print("  sqlite_sequence 不一致", seq_a, seq_b)
        ok = False
    objs_a = sorted(a.execute("SELECT type, name FROM sqlite_master WHERE tbl_name=?", (TABLE,)).fetchall())
    objs_b = sorted(b.execute("SELECT type, name FROM sqlite_master WHERE tbl_name=?", (TABLE,)).fetchall())
    if objs_a != objs_b:
        print("  索引/触发器不一致", set(objs_a) ^ set(objs_b))
        ok = False
    quick = b.execute("PRAGMA quick_check").fetchone()[0]
    fk = b.execute("PRAGMA foreign_key_check").fetchmany(5)
    print("  quick_check:", quick, "| foreign_key_check 前 5 条:", fk)
    ok = ok and quick == "ok"
    fk_a = a.execute("PRAGMA foreign_key_check").fetchmany(5)
    if fk != fk_a:
        print("  外键检查结果与原库不同（原库前 5 条）:", fk_a)
        ok = False
    _step("quick_check 与外键检查", t)
    a.close()
    b.close()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src")
    parser.add_argument("work")
    parser.add_argument("out")
    parser.add_argument("--skip-copy", action="store_true", help="工作副本已就位（不复制）")
    args = parser.parse_args()
    total = time.perf_counter()
    if not args.skip_copy:
        if os.path.exists(args.work):
            raise SystemExit(f"{args.work} 已存在，中止")
        t = time.perf_counter()
        shutil.copyfile(args.src, args.work)
        _step(f"复制原库 → 工作副本（{os.path.getsize(args.work) / 1e9:.2f}GB）", t)
    print("== 重排", flush=True)
    reorder(args.work)
    print(f"  工作副本现在 {os.path.getsize(args.work) / 1e9:.2f}GB")
    print("== VACUUM INTO", flush=True)
    vacuum_into(args.work, args.out)
    print(f"  输出库 {os.path.getsize(args.out) / 1e9:.2f}GB")
    print("== 校验", flush=True)
    ok = verify(args.src, args.out)
    print(f"== {'通过' if ok else '失败'}，总耗时 {time.perf_counter() - total:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
