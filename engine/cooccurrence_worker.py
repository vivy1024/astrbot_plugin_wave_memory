"""共现图构建子进程：读库、算图，结果以 JSON 写到 stdout。

全量构建是纯 Python 双重循环（线上约 105 万条标签关联、250 万次配对），放在插件进程的线程里
会一直占着 GIL，事件循环与写线程都抢不到，注入被拖到十几秒、写事务超时报 database is locked。
放到独立进程里只占另一个 CPU 核。

输入（stdin JSON）：``db_path``、``max_neighbors_per_tag``、``residual_map``（[[tag_id, 能量], …]）、
``semantic_gain``（SemanticGainConfig 字段）、``pair_similarity``（是否按 ``tag_pair_similarity`` 表做语义增益）。
输出（stdout JSON）：``forward``、``tag_count``、``elapsed_sec``。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import types


# 表不大时整表载入（线上几千行），否则逐对查询并缓存
PRELOAD_PAIR_LIMIT = 500_000


class _TablePairSimilarity:
    """与 PairSimilarityService 同一数据源：表里没有的标签对相似度为 0。"""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._pairs: dict[tuple[int, int], float] = {}
        self._preloaded = False
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM tag_pair_similarity").fetchone()[0])
        except sqlite3.Error:
            self._preloaded = True  # 没有这张表：全部按 0
            return
        if total <= PRELOAD_PAIR_LIMIT:
            rows = connection.execute("SELECT tag_id_a, tag_id_b, similarity FROM tag_pair_similarity").fetchall()
            self._pairs = {(int(a), int(b)): float(s) for a, b, s in rows}
            self._preloaded = True

    def get_similarity(self, tag_a: int, tag_b: int) -> float:
        if tag_a == tag_b:
            return 1.0
        key = (min(tag_a, tag_b), max(tag_a, tag_b))
        cached = self._pairs.get(key)
        if cached is not None or self._preloaded:
            return 0.0 if cached is None else cached
        row = self._connection.execute(
            "SELECT similarity FROM tag_pair_similarity WHERE tag_id_a=? AND tag_id_b=?", key
        ).fetchone()
        value = float(row[0]) if row else 0.0
        self._pairs[key] = value
        return value


def build(request: dict) -> dict:
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    from engine.cooccurrence_budget import RowBudgetParams
    from engine.directed_cooccurrence import DirectedCooccurrence
    from engine.semantic_gain import SemanticGainConfig

    started = time.perf_counter()
    connection = sqlite3.connect(f"file:{request['db_path']}?mode=ro", uri=True, timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        db = types.SimpleNamespace(
            conn=connection,
            get_tag_count=lambda: int(connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0]),
        )
        gain_fields = request.get("semantic_gain") or {}
        matrix = DirectedCooccurrence(
            db,
            pair_sim_service=_TablePairSimilarity(connection) if request.get("pair_similarity") else None,
            residual_map={int(tag): float(value) for tag, value in request.get("residual_map") or []},
            semantic_gain_config=SemanticGainConfig(**gain_fields) if gain_fields else None,
            max_neighbors_per_tag=int(request.get("max_neighbors_per_tag") or 0),
            kernel_version=str(request.get("kernel_version") or "global_max"),
            kernel_params=RowBudgetParams.from_dict(request.get("kernel_params")),
        )
        matrix.rebuild()
        return {
            "forward": [[src, [[tgt, weight] for tgt, weight in neighbors.items()]] for src, neighbors in matrix.forward.items()],
            "tag_count": matrix._tag_count,
            "kernel_version": matrix.kernel_version,
            "wormholes": [[src, tgt] for src, tgt in matrix.wormhole_edges],
            "anchor_gain": [[tag, gain] for tag, gain in matrix.anchor_gain.items()],
            "elapsed_sec": round(time.perf_counter() - started, 2),
        }
    finally:
        connection.close()


def main() -> int:
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    # stdout 只留给结果：AstrBot 的 logger 等会往 stdout 打日志，先把 fd 1 指到 stderr
    result_stream = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    result = build(request)
    result_stream.write(json.dumps(result, separators=(",", ":")).encode("utf-8"))
    result_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
