import ast
import json
import sqlite3
import unittest
from pathlib import Path


def _load_fact_evidence():
    """证据装配实现已从 knowledge.py 收敛到 webui/facts_evidence.py。

    `/api/knowledge/facts` 与 `/api/facts` 共用同一份实现，因此这里改为从新位置抽取；
    knowledge.py 仍保留同名薄封装以兼容既有调用。断言保持不变。
    """
    source_path = Path(__file__).resolve().parents[1] / "webui" / "facts_evidence.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    wanted = {"columns", "row_dicts", "json_value", "fact_evidence"}
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=body, type_ignores=[])
    namespace: dict[str, object] = {"Any": object, "json": json}
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["fact_evidence"]


class FactEvidenceMetadataTest(unittest.TestCase):
    def test_fact_evidence_includes_memory_metadata_and_object_scope(self):
        fact_evidence = _load_fact_evidence()
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE memories (
                id INTEGER PRIMARY KEY,
                bot_id TEXT,
                session_id TEXT,
                visibility TEXT,
                resolution_state TEXT,
                quarantine INTEGER,
                content_hash TEXT,
                timestamp REAL,
                created_at REAL,
                content TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO memories VALUES (323370, 'yushu', '羽书:group:398291136', 'group', 'resolved', 0, 'sha256:abc', 1700000000, 1699999999, '群友说了句谢谢')"""
        )
        items = [{"source_memory_id": 323370}]
        fact_evidence(conn, items, ("yushu", "羽书:group:398291136", "group"))
        evidence = items[0]["evidence"][0]
        self.assertEqual(evidence["id"], "323370")
        self.assertEqual(evidence["content_hash"], "sha256:abc")
        self.assertEqual(evidence["captured_at"], 1700000000)
        self.assertEqual(evidence["summary"], "群友说了句谢谢")
        self.assertEqual(
            evidence["source_scope"],
            {"bot_id": "yushu", "session_id": "羽书:group:398291136", "visibility": "group"},
        )
        conn.close()
