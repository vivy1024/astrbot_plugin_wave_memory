"""Review Candidate 的 Scope 隔离与幂等契约。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path


def _scope(*, bot_id: str = "bot-a", group_id: str = "g1") -> object:
    from domain.scope import RuntimeScope, SessionRef

    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(f"test:group:{group_id}", "test", "group", group_id),
        subject_principal_id="test:user:u1",
    )


class ReviewCandidateScopeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = sqlite3.connect(Path(tmp.name) / "candidates.db")
        self.addCleanup(self.conn.close)
        from services.review.candidate_store import ReviewCandidateStore

        self.store = ReviewCandidateStore(self.conn)
        self.store.ensure_schema()

    # ---- helpers ---------------------------------------------------------

    def _submit(self, *, scope, content="他更愿在群里接受反馈", candidate_type="fact", evidence=None):
        return self.store.create(
            candidate_type=candidate_type,
            content=content,
            evidence=evidence or ["memory:7"],
            reason="多次观察一致",
            scope=scope,
        )

    def _count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM review_candidates").fetchone()[0])

    def _insert_legacy_row(self) -> int:
        cur = self.conn.execute(
            """INSERT INTO review_candidates
               (candidate_type, content, evidence_json, reason, review_status, promoted, actor, created_at, metadata_json)
               VALUES ('fact', '旧候选', '[]', '历史遗留', 'pending', 0, 'agent', 1.0, '{}')"""
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ---- 幂等 ------------------------------------------------------------

    def test_same_scope_resubmission_is_idempotent(self):
        scope = _scope()
        first = self._submit(scope=scope)
        second = self._submit(scope=scope)
        self.assertEqual(first, second, "同一 Scope 的重复提交必须返回原候选")
        self.assertEqual(self._count(), 1, "重复提交不得新增行")

    def test_different_content_is_not_collapsed(self):
        scope = _scope()
        first = self._submit(scope=scope, content="判断甲")
        second = self._submit(scope=scope, content="判断乙")
        self.assertNotEqual(first, second)
        self.assertEqual(self._count(), 2)

    def test_same_content_under_different_scope_is_kept(self):
        """内容相同但 Scope 不同必须各自保留，幂等键不得跨 Scope 合并。"""
        id_a = self._submit(scope=_scope(bot_id="bot-a"))
        id_b = self._submit(scope=_scope(bot_id="bot-b"))
        self.assertNotEqual(id_a, id_b)
        self.assertEqual(self._count(), 2)

    # ---- Scope 隔离 ------------------------------------------------------

    def test_candidates_do_not_leak_across_scopes(self):
        self._submit(scope=_scope(bot_id="bot-a"))
        other = _scope(bot_id="bot-b")
        self.assertEqual(self.store.list_pending(scope=other), [])
        self.assertEqual(self.store.list_all(scope=other), [])

    def test_candidates_do_not_leak_across_sessions(self):
        self._submit(scope=_scope(group_id="g1"))
        self.assertEqual(self.store.list_pending(scope=_scope(group_id="g2")), [])

    def test_get_by_id_is_scope_bound(self):
        candidate_id = self._submit(scope=_scope(bot_id="bot-a"))
        self.assertIsNotNone(self.store.get(candidate_id, scope=_scope(bot_id="bot-a")))
        # 跨 Scope 不得泄漏存在性
        self.assertIsNone(self.store.get(candidate_id, scope=_scope(bot_id="bot-b")))

    def test_legacy_rows_never_appear_in_scope_queries(self):
        legacy_id = self._insert_legacy_row()
        scope = _scope(bot_id="bot-a")
        self.assertEqual(self.store.list_all(scope=scope), [])
        self.assertEqual(self.store.list_pending(scope=scope), [])
        # 全局视图仍可见，并显式标记 legacy
        everything = self.store.list_all(include_legacy=True)
        self.assertIn(legacy_id, [item["id"] for item in everything])
        self.assertTrue([item for item in everything if item["id"] == legacy_id][0]["legacy"])

    # ---- 审批 ------------------------------------------------------------

    def test_review_within_owning_bot_updates_status_and_revision(self):
        candidate_id = self._submit(scope=_scope(bot_id="bot-a"))
        before = self.store.get(candidate_id)
        from webui.blueprints.agent_feedback import review_review_candidate

        row = review_review_candidate(
            self.store, candidate_id, "approve", bot_id="bot-a", reviewer="human"
        )
        self.assertEqual(row["review_status"], "approved")
        self.assertFalse(row["promoted"], "批准只记录人工状态，不自动写入高风险对象")
        self.assertEqual(row["revision"], before["revision"] + 1)
        self.assertEqual(row["metadata"]["last_reviewer"], "human")

    def test_cross_bot_review_is_rejected(self):
        candidate_id = self._submit(scope=_scope(bot_id="bot-a"))
        from webui.blueprints.agent_feedback import review_review_candidate

        with self.assertRaises(ValueError) as ctx:
            review_review_candidate(self.store, candidate_id, "approve", bot_id="bot-b")
        self.assertIn("scope", str(ctx.exception))
        self.assertEqual(self.store.get(candidate_id)["review_status"], "pending")

    # ---- payload 视图 ----------------------------------------------------

    def test_payload_keeps_legacy_visible_but_labelled(self):
        """全局审核台不得隐藏任何候选；legacy 只标注归属，不从天平上消失。"""
        legacy_id = self._insert_legacy_row()
        scoped_id = self._submit(scope=_scope(bot_id="bot-a"))
        from webui.blueprints.agent_feedback import build_agent_feedback_payload

        payload = build_agent_feedback_payload(self.conn)
        listed = {item["id"] for item in payload["review_candidates"]}
        self.assertEqual(listed, {legacy_id, scoped_id})
        self.assertEqual(payload["summary"]["pending_candidates"], 2)
        self.assertEqual(payload["summary"]["legacy_pending_candidates"], 1)
        self.assertEqual([item["id"] for item in payload["legacy_review_candidates"]], [legacy_id])
        flags = {item["id"]: item["legacy"] for item in payload["review_candidates"]}
        self.assertEqual(flags[legacy_id], True)
        self.assertEqual(flags[scoped_id], False)

    def test_payload_filters_by_requested_bot(self):
        self._submit(scope=_scope(bot_id="bot-a"))
        self._submit(scope=_scope(bot_id="bot-b"), content="另一个 bot 的判断")
        from webui.blueprints.agent_feedback import build_agent_feedback_payload

        payload = build_agent_feedback_payload(self.conn, bot_id="bot-b")
        bots = {item["bot_id"] for item in payload["review_candidates"]}
        self.assertEqual(bots, {"bot-b"})

    def test_schema_upgrade_is_idempotent_on_old_table(self):
        """旧库只有 10 列：ensure_schema 必须补列且不报错、不丢数据。"""
        conn = self.conn
        conn.execute("DROP TABLE review_candidates")
        conn.execute(
            """CREATE TABLE review_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_type TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'pending',
                promoted INTEGER NOT NULL DEFAULT 0,
                actor TEXT DEFAULT 'agent',
                created_at REAL NOT NULL,
                metadata_json TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO review_candidates
               (candidate_type, content, evidence_json, reason, review_status, promoted, actor, created_at, metadata_json)
               VALUES ('belief', '旧信念', '[]', '历史', 'pending', 0, 'agent', 1.0, '{}')"""
        )
        conn.commit()

        from services.review.candidate_store import ReviewCandidateStore

        upgraded = ReviewCandidateStore(conn)
        upgraded.ensure_schema()
        upgraded.ensure_schema()  # 幂等

        columns = {row[1] for row in conn.execute("PRAGMA table_info(review_candidates)").fetchall()}
        for expected in ("bot_id", "session_id", "visibility", "idempotency_key", "revision", "source_kind"):
            self.assertIn(expected, columns)
        rows = upgraded.list_all(include_legacy=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["legacy"])
        self.assertEqual(rows[0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
