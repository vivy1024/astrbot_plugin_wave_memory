"""检索评测：固定查询集 + 池化 + 大模型评审，算 nDCG@5 / P@5 / MRR@10。

评测数据（真实查询、评审结果、各次运行）属于用户数据，只放在运行数据目录
``plugin_data/astrbot_plugin_wave_memory/eval/``，不进仓库。在 astrbot 容器里运行::

    python retrieval_eval.py sample --n 100                 # 从注入 trace 抽真实查询，写 queries.jsonl（已存在则不覆盖）
    python retrieval_eval.py run full                       # 用当前线上配置跑一遍，写 runs/full.json
    python retrieval_eval.py run no_geodesic --stages '{"geodesic": false}'
    python retrieval_eval.py judge full no_geodesic         # 池化各次运行的前 10 条，评审尚未评过的（结果缓存）
    python retrieval_eval.py report full no_geodesic        # 指标与逐条差异

评审标准（0/1/2）：2 = 能直接帮助回答或理解这条消息（同一话题/人物/事件，含有用信息）；
1 = 话题相关但帮助有限；0 = 无关，或只是这条消息本身的重复。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

DATA = os.environ.get("WAVEMEMORY_DATA", "/AstrBot/data/plugin_data/astrbot_plugin_wave_memory")
EVAL = os.path.join(DATA, "eval")
DB = os.path.join(DATA, "wave_memory.db")
QUERY_LAB = "http://127.0.0.1:9876/api/query"
JUDGE_PROVIDER = os.environ.get("EVAL_JUDGE_PROVIDER", "nug/antigravity:gemini-3.8-flash-medium")


def _path(*parts: str) -> str:
    path = os.path.join(EVAL, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"\[At:\d+\]|@\S+\(\d+\)", "", str(text or ""))).lower()


# ─── sample ───

def cmd_sample(args) -> None:
    out = _path("queries.jsonl")
    if os.path.exists(out) and not args.force:
        print(f"{out} 已存在（{len(_read_jsonl(out))} 条），加 --force 才重抽")
        return
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        # bot_id 列是平台账号（QQ 号），记忆按 Bot 的 db_id 存；取 bot_profile_id
        "SELECT trace_id, timestamp, group_id, COALESCE(NULLIF(bot_profile_id, ''), bot_id), message_preview FROM injection_traces "
        "WHERE message_preview != '' AND group_id IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    by_group: dict[str, list] = {}
    seen: set[str] = set()
    for trace_id, ts, group_id, bot_id, text in rows:
        text = str(text or "").strip()
        key = _normalize(text)
        # 太短、纯 @/表情/图片的消息没法评
        if len(key) < 4 or key in seen or re.fullmatch(r"(\[图片\]|\[表情\]|[\W_])+", key):
            continue
        seen.add(key)
        by_group.setdefault(str(group_id), []).append((trace_id, ts, bot_id or "yushu", text[:300]))
    rng = random.Random(args.seed)
    total = sum(len(v) for v in by_group.values())
    picked = []
    for group_id, items in by_group.items():
        # 按群的消息量分配名额，小群至少 5 条
        quota = max(min(5, len(items)), round(args.n * len(items) / total))
        picked += [(group_id, item) for item in rng.sample(items, min(quota, len(items)))]
    rng.shuffle(picked)
    picked = picked[: args.n]
    sessions: dict[tuple, str] = {}
    with open(out, "w", encoding="utf-8") as handle:
        for group_id, (trace_id, ts, bot_id, text) in picked:
            key = (bot_id, group_id)
            if key not in sessions:
                row = conn.execute(
                    "SELECT session_id FROM memories WHERE bot_id=? AND group_id=? AND visibility='group' "
                    "GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1",
                    key,
                ).fetchone()
                sessions[key] = row[0] if row else f"{bot_id}:group:{group_id}"
            qid = hashlib.sha1(f"{trace_id}".encode()).hexdigest()[:12]
            handle.write(json.dumps({
                "qid": qid, "text": text, "bot_id": bot_id, "session_id": sessions[key],
                "group_id": group_id, "timestamp": ts,
            }, ensure_ascii=False) + "\n")
    print(f"抽样 {len(picked)} 条 → {out}；各群: " + "、".join(f"{g} {sum(1 for x, _ in picked if x == g)}" for g in by_group))


# ─── run ───

def cmd_run(args) -> None:
    queries = _read_jsonl(_path("queries.jsonl"))
    stages = json.loads(args.stages) if args.stages else {}
    params = json.loads(args.params) if args.params else {}
    results = {}
    started = time.perf_counter()
    for query in queries:
        qs = urllib.parse.urlencode({"bot_id": query["bot_id"], "visibility": "group", "session_id": query["session_id"]})
        body = {"text": query["text"], "top_k": args.k, "stages": stages, "params": params}
        request = urllib.request.Request(f"{QUERY_LAB}?{qs}", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            data = json.loads(urllib.request.urlopen(request, timeout=90).read())
        except Exception as error:
            print(f"  {query['qid']} 失败: {error}")
            continue
        results[query["qid"]] = [
            {"id": int(item["id"]), "score": round(float(item.get("score", 0) or 0), 4)} for item in data.get("results") or []
        ][: args.k]
    path = _path("runs", f"{args.name}.json")
    json.dump({"name": args.name, "stages": stages, "params": params, "at": time.time(), "results": results},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{args.name}: {len(results)}/{len(queries)} 条，{time.perf_counter() - started:.0f}s → {path}")


# ─── judge ───

def _judge_client():
    config = json.load(open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig"))
    provider = next(p for p in config["provider"] if p.get("id") == JUDGE_PROVIDER)
    source = next(s for s in config.get("provider_sources") or [] if s.get("id") == provider.get("provider_source_id"))
    key = source.get("key")
    key = key[0] if isinstance(key, list) else key
    base = str(source["api_base"]).rstrip("/")
    model = provider["model"]
    anthropic = "anthropic" in str(source.get("type") or source.get("provider") or "")

    def call(prompt: str) -> str:
        if anthropic:
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
            body = {"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        else:
            url = base + "/chat/completions"
            body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        data = json.loads(urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=180).read())
        if anthropic:
            return "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
        return data["choices"][0]["message"]["content"]

    return call


PROMPT = """你在评估一个群聊 AI（名叫羽书）的长期记忆检索质量。下面是群里有人发给羽书的一条消息，以及检索出的若干条历史记忆。
请判断每条记忆对羽书「回答或理解这条消息」有多大帮助：
2 = 直接有帮助（同一话题/人物/事件，含有能用上的信息或背景）
1 = 话题相关，但帮助有限
0 = 无关；或者只是这条消息本身（同一句话）的重复
只输出一个 JSON 对象，键是记忆编号，值是 0/1/2，不要解释。

消息：{query}

记忆：
{items}
"""


def cmd_judge(args) -> None:
    queries = {q["qid"]: q for q in _read_jsonl(_path("queries.jsonl"))}
    qrels_path = _path("qrels.jsonl")
    judged = {(r["qid"], r["id"]): r["grade"] for r in _read_jsonl(qrels_path)}
    pool: dict[str, set[int]] = {}
    for name in args.runs:
        run = json.load(open(_path("runs", f"{name}.json"), encoding="utf-8"))
        for qid, items in run["results"].items():
            pool.setdefault(qid, set()).update(item["id"] for item in items[: args.depth])
    todo = {qid: sorted(ids - {i for (q, i) in judged if q == qid}) for qid, ids in pool.items()}
    todo = {qid: ids for qid, ids in todo.items() if ids and qid in queries}
    print(f"需要评审 {sum(len(v) for v in todo.values())} 条（{len(todo)} 个查询）")
    if not todo:
        return
    call = _judge_client()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    with open(qrels_path, "a", encoding="utf-8") as out:
        for n, (qid, ids) in enumerate(todo.items(), 1):
            rows = {
                int(r[0]): r for r in conn.execute(
                    f"SELECT id, sender_name, content FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
                ).fetchall()
            }
            numbered = [i for i in ids if i in rows]
            lines = "\n".join(
                f"[{k}] {rows[i][1] or '某人'}：{str(rows[i][2] or '')[:220]}" for k, i in enumerate(numbered, 1)
            )
            grades = None
            for attempt in range(3):
                try:
                    text = call(PROMPT.format(query=queries[qid]["text"], items=lines))
                    match = re.search(r"\{.*\}", text, re.S)
                    raw = json.loads(match.group(0)) if match else {}
                    grades = {numbered[int(k) - 1]: max(0, min(2, int(v))) for k, v in raw.items() if str(k).isdigit() and 0 < int(k) <= len(numbered)}
                    break
                except Exception as error:
                    print(f"  {qid} 第 {attempt + 1} 次评审失败: {error!r}")
                    time.sleep(3)
            if not grades:
                continue
            for memory_id, grade in grades.items():
                out.write(json.dumps({"qid": qid, "id": memory_id, "grade": grade, "judge": JUDGE_PROVIDER}) + "\n")
            out.flush()
            if n % 10 == 0:
                print(f"  已评审 {n}/{len(todo)} 个查询")
    print("完成")


# ─── report ───

def _metrics(items: list[int], grades: dict[int, int]) -> dict:
    top5 = items[:5]
    dcg = sum((2 ** grades.get(i, 0) - 1) / math.log2(rank + 2) for rank, i in enumerate(top5))
    ideal = sorted(grades.values(), reverse=True)[:5]
    idcg = sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(ideal))
    rr = next((1 / (rank + 1) for rank, i in enumerate(items[:10]) if grades.get(i, 0) >= 1), 0.0)
    return {
        "ndcg5": dcg / idcg if idcg > 0 else 0.0,
        "p5": sum(1 for i in top5 if grades.get(i, 0) >= 1) / 5,
        "p5_strict": sum(1 for i in top5 if grades.get(i, 0) >= 2) / 5,
        "mrr10": rr,
        "unjudged5": sum(1 for i in top5 if i not in grades),
    }


def cmd_report(args) -> None:
    queries = {q["qid"]: q for q in _read_jsonl(_path("queries.jsonl"))}
    qrels: dict[str, dict[int, int]] = {}
    for r in _read_jsonl(_path("qrels.jsonl")):
        qrels.setdefault(r["qid"], {})[int(r["id"])] = int(r["grade"])
    runs = {name: json.load(open(_path("runs", f"{name}.json"), encoding="utf-8"))["results"] for name in args.runs}
    common = sorted(set.intersection(*(set(r) for r in runs.values())) & set(qrels))
    print(f"共同评测查询 {len(common)} 条（池内已评 {sum(len(v) for v in qrels.values())} 条）\n")
    print(f"{'运行':<22}{'nDCG@5':>8}{'P@5':>8}{'P@5(2分)':>10}{'MRR@10':>8}{'未评':>6}")
    per_query = {}
    for name, results in runs.items():
        values = [_metrics([i["id"] for i in results[q]], qrels[q]) for q in common]
        per_query[name] = {q: v["ndcg5"] for q, v in zip(common, values)}
        avg = lambda key: sum(v[key] for v in values) / max(1, len(values))
        print(f"{name:<22}{avg('ndcg5'):>8.3f}{avg('p5'):>8.3f}{avg('p5_strict'):>10.3f}{avg('mrr10'):>8.3f}{sum(v['unjudged5'] for v in values):>6}")
    if len(args.runs) >= 2:
        base, other = args.runs[0], args.runs[1]
        diffs = sorted(((per_query[other][q] - per_query[base][q], q) for q in common))
        wins = sum(1 for d, _ in diffs if d > 0.05)
        losses = sum(1 for d, _ in diffs if d < -0.05)
        print(f"\n{other} 相对 {base}：nDCG@5 提升 >0.05 的 {wins} 条，下降 >0.05 的 {losses} 条")
        for d, q in diffs[:3] + diffs[-3:]:
            print(f"  {d:+.3f} {queries[q]['text'][:50]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample"); s.add_argument("--n", type=int, default=100); s.add_argument("--seed", type=int, default=20260927); s.add_argument("--force", action="store_true")
    r = sub.add_parser("run"); r.add_argument("name"); r.add_argument("--stages"); r.add_argument("--params"); r.add_argument("--k", type=int, default=10)
    j = sub.add_parser("judge"); j.add_argument("runs", nargs="+"); j.add_argument("--depth", type=int, default=10)
    p = sub.add_parser("report"); p.add_argument("runs", nargs="+")
    args = parser.parse_args()
    {"sample": cmd_sample, "run": cmd_run, "judge": cmd_judge, "report": cmd_report}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
