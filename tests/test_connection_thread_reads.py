"""多个线程同时读库：每个线程用自己的只读连接，结果不串位、不报 misuse。

线上 6 个同步注入通道放进工作线程后与事件循环共用一个读连接，Python sqlite3 的语句缓存
按连接共享，两个线程同时跑同一句 SQL 会互相踩：``bad parameter or other API misuse``、
取到 None、``tuple index out of range``（好感 / 心情通道约四成回复报错）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from engine.db.connection import ConnectionManager


def _manager(tmp_path) -> ConnectionManager:
    cm = ConnectionManager(str(tmp_path / "t.db"))
    cm.execute_write("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    cm.executemany_write("INSERT INTO t (id, v) VALUES (?, ?)", [(i, f"v{i}") for i in range(1, 401)])
    cm.commit()
    return cm


def test_concurrent_reads_from_threads_do_not_interfere(tmp_path):
    cm = _manager(tmp_path)
    errors: list[str] = []

    def worker(seed: int) -> None:
        for step in range(300):
            key = (seed * 37 + step) % 400 + 1
            try:
                rows = cm.conn.execute("SELECT id, v FROM t WHERE id >= ? ORDER BY id LIMIT 5", (key,)).fetchall()
                if not rows or rows[0][0] != key or rows[0][1] != f"v{key}":
                    errors.append(f"wrong rows for {key}: {rows[:1]}")
            except Exception as exc:  # noqa: BLE001 - 收集所有错误
                errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(16)))
    cm.close()
    assert errors == []


def test_worker_threads_get_their_own_read_connection(tmp_path):
    cm = _manager(tmp_path)
    main = cm._reader()
    seen = []
    thread = threading.Thread(target=lambda: seen.append(cm._reader()))
    thread.start()
    thread.join()
    assert seen and seen[0] is not main
    assert seen[0].execute("SELECT count(*) FROM t").fetchone()[0] == 400
    cm.close()


def test_thread_reads_see_committed_writes_and_survive_reopen(tmp_path):
    cm = _manager(tmp_path)
    box = {}

    def read(key):
        box[key] = cm.conn.execute("SELECT count(*) FROM t").fetchone()[0]

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(read, "before").result()
        cm.execute_write("INSERT INTO t (id, v) VALUES (1000, 'x')")
        cm.commit()
        pool.submit(read, "after").result()
        cm.reopen()
        pool.submit(read, "reopened").result()
    cm.close()
    assert box == {"before": 400, "after": 401, "reopened": 401}
