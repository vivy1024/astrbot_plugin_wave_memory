"""注入观测台按来源（AstrBot / Cortico）筛选；v5 旧 trace 视为 astrbot。"""

from __future__ import annotations

import sqlite3
import time

from services.injection.trace_store import InjectionTraceStore


def _trace(trace_id: str, metadata: dict) -> dict:
    return {
        "trace_id": trace_id,
        "timestamp": time.time(),
        "mode": "full",
        "group_id": "1",
        "sender_id": "u",
        "sender_name": "u",
        "bot_id": "yushu",
        "bot_profile_id": "yushu",
        "metadata": metadata,
        "message": "hi",
        "final_text": "block",
        "total_latency_ms": 1.0,
        "total_tokens": 1,
        "total_chars": 5,
        "status": "ok",
        "error": "",
    }


def test_query_filters_by_source(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    store = InjectionTraceStore(conn)
    store.ensure_schema()
    store.record(_trace("legacy", {}), [])
    store.record(_trace("astrbot", {"source": "astrbot"}), [])
    store.record(_trace("cortico", {"source": "cortico"}), [])
    window = {"from_ts": 0, "to_ts": time.time() + 10}
    assert {t["trace_id"] for t in store.query(**window, source="cortico")} == {"cortico"}
    assert {t["trace_id"] for t in store.query(**window, source="astrbot")} == {"legacy", "astrbot"}
    assert store.count(**window, source="astrbot") == 2
    assert {t["source"] for t in store.query(**window)} == {"astrbot", "cortico"}
    conn.close()
