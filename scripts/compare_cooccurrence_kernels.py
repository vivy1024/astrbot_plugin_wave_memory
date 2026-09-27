"""离线对比共现传播核 legacy 与 v91：同一批真实查询的种子标签在两张图上各跑一次脉冲传播。

在 astrbot 容器里运行（只读数据库；种子标签取自运行中的 9876 检索实验室 debug 输出）::

    python compare_cooccurrence_kernels.py [--queries 40] [--session 羽书:group:398291136] [--dump out.json]

输出：种子在图里的比例、有涌现标签的查询占比、涌现数与能量场大小、传播耗时，以及逐条样例（标签名）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = "/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db"


def _build(db_path: str, kernel: str, min_support: int) -> dict:
    sys.path.insert(0, PLUGIN_ROOT)
    from engine.cooccurrence_worker import build

    return build({
        "db_path": db_path, "kernel_version": kernel, "v91_params": {"min_support": min_support},
        "max_neighbors_per_tag": 64, "residual_map": [], "semantic_gain": {}, "pair_similarity": True,
    })


def _router(result: dict, kernel: str):
    from engine.cooccurrence_v91 import V91Params
    from engine.directed_cooccurrence import DirectedCooccurrence
    from engine.spike_routing import SpikeRouter

    matrix = DirectedCooccurrence(None, kernel_version=kernel, v91_params=V91Params())
    matrix.publish(
        {int(s): {int(t): float(w) for t, w in edges} for s, edges in result["forward"]},
        int(result.get("tag_count") or 0),
        wormholes={(int(a), int(b)) for a, b in result.get("wormholes") or []},
        anchor_gain={int(t): float(g) for t, g in result.get("anchor_gain") or []},
    )
    return matrix, SpikeRouter(matrix, residual_map={})


def _seeds(text: str, session: str) -> tuple[list[dict], float]:
    qs = urllib.parse.urlencode({"bot_id": "yushu", "visibility": "group", "session_id": session})
    request = urllib.request.Request(
        "http://127.0.0.1:9876/api/query?" + qs,
        json.dumps({"text": text, "top_k": 5, "debug": True}).encode(),
        {"Content-Type": "application/json"},
    )
    debug = json.loads(urllib.request.urlopen(request, timeout=60).read()).get("debug") or {}
    epa = debug.get("epa") or {}
    return list((debug.get("spike") or {}).get("seed_tags") or []), float(epa.get("logic_depth", 0.5) or 0.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--session", default="羽书:group:398291136")
    parser.add_argument("--min-support", type=int, default=2)
    parser.add_argument("--dump")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    graphs = {}
    for kernel in ("legacy", "v91"):
        started = time.perf_counter()
        result = _build(args.db, kernel, args.min_support)
        matrix, router = _router(result, kernel)
        graphs[kernel] = (matrix, router)
        print(f"[{kernel}] 构建 {time.perf_counter() - started:.1f}s：{matrix.node_count} 个节点、{matrix.edge_count} 条边、"
              f"{len(matrix.wormhole_edges)} 条虫洞、{len(matrix.anchor_gain)} 个锚增益", flush=True)

    texts = []
    for (text,) in conn.execute(
        "SELECT message_preview FROM injection_traces WHERE message_preview != '' ORDER BY timestamp DESC LIMIT 400"
    ):
        text = (text or "").strip()[:200]
        if len(text) >= 4 and text not in texts:
            texts.append(text)
        if len(texts) >= args.queries:
            break

    def name(tag_id: int) -> str:
        row = conn.execute("SELECT name FROM scoped_tags WHERE id=?", (int(tag_id),)).fetchone()
        return row[0] if row else str(tag_id)

    stats = {k: {"seeds": 0, "in_graph": 0, "fired": 0, "emergent": [], "field": [], "ms": []} for k in graphs}
    samples = []
    for text in texts:
        seeds, logic_depth = _seeds(text, args.session)
        if not seeds:
            continue
        sample = {"query": text, "seeds": [name(s["tag_id"]) for s in seeds[:6]]}
        for kernel, (matrix, router) in graphs.items():
            s = stats[kernel]
            s["seeds"] += len(seeds)
            s["in_graph"] += sum(1 for seed in seeds if seed["tag_id"] in matrix.forward)
            started = time.perf_counter()
            out = router.propagate(seeds, epa_result={"logic_depth": logic_depth})
            s["ms"].append((time.perf_counter() - started) * 1000)
            emergent = [item for item in out["activated_tags"] if item["is_emergent"]]
            if kernel == "legacy":
                emergent = [item for item in emergent if item["energy"] > 0.1]  # 与检索引擎的并入门槛一致
            s["fired"] += 1 if emergent else 0
            s["emergent"].append(len(emergent))
            s["field"].append(len(out["energy_field"]))
            sample[kernel] = [f"{name(item['tag_id'])}({item['energy']:.3f})" for item in emergent[:6]]
        samples.append(sample)

    n = len(samples)
    print(f"\n有种子标签的查询 {n} 条")
    for kernel, s in stats.items():
        ms = sorted(s["ms"]) or [0]
        print(f"[{kernel}] 种子在图里 {s['in_graph']}/{s['seeds']}；有涌现的查询 {s['fired']}/{n}；"
              f"涌现数中位 {statistics.median(s['emergent'] or [0])}；能量场节点中位 {statistics.median(s['field'] or [0])}；"
              f"传播耗时 p50 {ms[len(ms)//2]:.1f}ms / max {ms[-1]:.1f}ms")
    print("\n样例：")
    for sample in samples[:12]:
        print(f"- {sample['query'][:40]}\n    种子: {'、'.join(sample['seeds'])}\n    v91 涌现: {'、'.join(sample['v91']) or '（无）'}\n    legacy 涌现: {'、'.join(sample['legacy']) or '（无）'}")
    if args.dump:
        json.dump({"stats": stats, "samples": samples}, open(args.dump, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
