"""中文全文索引回填任务：每次 to_thread 落在不同线程上也能跑完（线上首跑踩过同线程限制）。"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import logging
import sqlite3
import threading
from types import SimpleNamespace

from plugin_source import plugin_method

from engine.db import fts_cjk


class _NewThreadPerCall(concurrent.futures.ThreadPoolExecutor):
    """每个任务开一个新线程：复现线程池把相邻步骤派到不同线程的情况。"""

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run():
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001
                future.set_exception(exc)

        threading.Thread(target=run).start()
        return future


def _load_handler():
    method = plugin_method("_maintenance_rebuild_fts_cjk")
    source = ast.unparse(method).replace("from ..engine.db import fts_cjk", "from engine.db import fts_cjk")
    namespace = {"asyncio": asyncio, "logger": logging.getLogger("test")}
    exec(compile(source, "maintenance.py", "exec"), namespace)
    return namespace["_maintenance_rebuild_fts_cjk"]


class _Jobs:
    def __init__(self):
        self.progress = []

    async def update_progress(self, run_id, **kwargs):
        self.progress.append(kwargs["progress"])


def test_backfill_survives_thread_hops(tmp_path):
    path = str(tmp_path / "wm.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, memory_type TEXT, quarantine INTEGER)")
    conn.executemany("INSERT INTO memories VALUES (?, ?, 'message', 0)", [(i, f"张羽第{i}次来") for i in range(1, 4502)])
    conn.commit()
    conn.close()

    jobs = _Jobs()
    plugin = SimpleNamespace(db=SimpleNamespace(db_path=path), write_gateway=SimpleNamespace(jobs=jobs))
    handler = _load_handler()

    async def scenario():
        asyncio.get_running_loop().set_default_executor(_NewThreadPerCall())
        return await handler(plugin, SimpleNamespace(run_id="r1"), SimpleNamespace(payload={}), SimpleNamespace(lease_owner="t"))

    result = asyncio.run(scenario())
    assert result["ready"] and result["rows"] == 4501 and result["indexed"] == 4501
    assert [p["cursor"] for p in jobs.progress] == [2000, 4000, 4501]
    check = sqlite3.connect(path)
    assert fts_cjk.is_ready(check) and len(fts_cjk.query_ids(check, ["张羽"], limit=10000)) == 4501
