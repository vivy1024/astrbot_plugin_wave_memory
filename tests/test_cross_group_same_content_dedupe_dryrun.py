from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from scripts.cross_group_same_content_dedupe_dryrun import (
    inventory,
    mark_deleted_in_hot_hnsw,
    soft_delete,
)


def _mk() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            sender_id TEXT, content TEXT, group_id TEXT,
            bot_id TEXT, session_id TEXT, timestamp REAL,
            quarantine INTEGER, memory_type TEXT, source TEXT, provenance TEXT,
            sender_name TEXT
        )"""
    )
    return conn


class CrossGroupDedupeDryrunTest(unittest.TestCase):
    def test_inventory_naive_counts_and_prefers_group(self):
        conn = _mk()
        rows = [
            (1, "u1", "hello world", "111111111", "yushu", "羽书:group:111111111", 1.0),
            (2, "u1", "hello world", "222222222", "yushu", "羽书:group:222222222", 2.0),
            (3, "u1", "hello world", "100000001", "yushu", "羽书:group:100000001", 1.5),
            (4, "u2", "unique", "111111111", "yushu", "羽书:group:111111111", 1.0),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,0,'message','live',NULL,NULL)",
                r,
            )
        conn.commit()
        report = inventory(
            conn,
            content_prefix=200,
            prefer_groups=["100000001"],
            top_families=10,
            min_groups=2,
            mode="naive",
        )
        self.assertEqual(report["family_count"], 1)
        self.assertEqual(report["theoretical_extra_rows"], 2)
        fam = report["top_families"][0]
        self.assertEqual(fam["keeper_group"], "100000001")
        self.assertEqual(fam["drop_count"], 2)
        conn.close()

    def test_cluster_keeps_far_apart_same_phrase(self):
        conn = _mk()
        # same text in 2 groups at t=1 and t=100000 → outside 600s window → no drop
        rows = [
            (1, "u1", "hi again", "111111111", "yushu", "s1", 1.0),
            (2, "u1", "hi again", "222222222", "yushu", "s2", 100000.0),
            # fanout clone near same time
            (3, "u1", "clone me", "111111111", "yushu", "s1", 50.0),
            (4, "u1", "clone me", "222222222", "yushu", "s2", 51.0),
            (5, "u1", "clone me", "100000001", "yushu", "s3", 52.0),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,0,'message','live',NULL,NULL)",
                r,
            )
        conn.commit()
        report = inventory(
            conn,
            content_prefix=200,
            prefer_groups=["100000001"],
            top_families=10,
            min_groups=2,
            mode="cluster",
            window_sec=600.0,
        )
        # families: "hi again" (2 groups) + "clone me" (3 groups)
        self.assertEqual(report["family_count"], 2)
        # only clone cluster drops 2 (keep prefer group 100000001)
        self.assertEqual(report["cluster_drop_rows"], 2)
        self.assertEqual(len(report["drop_ids"]), 2)
        self.assertNotIn(1, report["drop_ids"])
        self.assertNotIn(2, report["drop_ids"])
        self.assertIn(5, [5])  # keeper exists
        self.assertNotIn(5, report["drop_ids"])  # prefer-group keeper kept
        conn.close()

    def test_soft_delete_idempotent(self):
        conn = _mk()
        conn.execute(
            "INSERT INTO memories VALUES (1,'u1','x','111111111','yushu','s',1,0,'message','live',NULL,NULL)"
        )
        conn.execute(
            "INSERT INTO memories VALUES (2,'u1','x','222222222','yushu','s',2,0,'message','live',NULL,NULL)"
        )
        conn.commit()
        # no fts table in bare fixture — disable purge
        r1 = soft_delete(conn, [2], purge_fts=False)
        self.assertEqual(r1["updated"], 1)
        row = conn.execute(
            "SELECT quarantine, memory_type FROM memories WHERE id=2"
        ).fetchone()
        self.assertEqual(int(row[0]), 1)
        self.assertEqual(row[1], "deleted")
        r2 = soft_delete(conn, [2], purge_fts=False)
        self.assertEqual(r2["skipped_already_inactive"], 1)
        self.assertEqual(r2["updated"], 0)
        self.assertIsNone(r2.get("hnsw"))
        conn.close()

    def test_soft_delete_optional_hnsw_mark_deleted(self):
        conn = _mk()
        conn.execute(
            "INSERT INTO memories VALUES (1,'u1','x','111111111','yushu','s',1,0,'message','live',NULL,NULL)"
        )
        conn.execute(
            "INSERT INTO memories VALUES (2,'u1','x','222222222','yushu','s',2,0,'message','live',NULL,NULL)"
        )
        conn.commit()
        with mock.patch(
            "scripts.cross_group_same_content_dedupe_dryrun.mark_deleted_in_hot_hnsw",
            return_value={"requested": 1, "marked": 1, "saved": True, "generation": 99},
        ) as mocked:
            result = soft_delete(
                conn,
                [2],
                purge_fts=False,
                hnsw_index_dir=Path("/tmp/fake-hnsw-dir"),
                hnsw_save=True,
            )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["hnsw"]["generation"], 99)
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args[0][1], [2])
        conn.close()

    def test_mark_deleted_in_hot_hnsw_roundtrip(self):
        try:
            import hnswlib  # noqa: F401
        except ImportError:
            self.skipTest("hnswlib not installed")
        from engine.vector_index import VectorIndex

        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp)
            index = VectorIndex(
                dimension=8,
                max_elements=16,
                index_path=None,
                kind="memory",
                allow_resize=False,
                strict_manifest=False,
            )
            index.index_path = str(index_dir / "memory.hnsw")
            vectors = np.eye(3, 8, dtype=np.float32)
            index.add([10, 20, 30], vectors)
            index.save(db_watermark=0)

            result = mark_deleted_in_hot_hnsw(
                index_dir,
                [20],
                dimension=8,
                max_elements=16,
                save=True,
            )
            self.assertEqual(result["marked"], 1)
            self.assertTrue(result["saved"])
            self.assertIsNotNone(result["generation"])

            reloaded = VectorIndex(
                dimension=8,
                max_elements=16,
                index_path=str(index_dir / "memory.hnsw"),
                kind="memory",
                allow_resize=False,
                strict_manifest=True,
            )
            # knn should not return the marked-deleted label as a normal neighbor
            hits = reloaded.search(vectors[0], k=3)
            hit_ids = [int(h[0]) for h in hits]
            self.assertNotIn(20, hit_ids)


if __name__ == "__main__":
    unittest.main()
