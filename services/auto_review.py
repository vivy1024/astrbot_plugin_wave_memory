"""自动审核：事实、信念、黑话在有证据时直接转正（配置 Learning_Settings.auto_approve_enabled，默认关闭）。

v5 起这三类知识只能由人在 WebUI 逐条批准，线上长期无人审：事实堆在待审，信念要求「≥2 条已批准事实」
永远建不起来，黑话连批准所需的证据字段都没有。开启后按下面的规则自动处理，不满足规则的仍留给人工：

- 事实：有支撑原话、原话定位到了本群记忆、原话里没有未解释的黑话 → 走与 WebUI 同一个审核命令批准。
  与已有事实冲突时审核命令会落 ``conflict``（不生效），仍需人工裁决。
- 信念：走 WebUI 同一个转正流程（要求 ≥2 条已批准事实或 ≥2 条经历证据）。
- 黑话：本群至少 ``jargon_min_memories`` 条记忆、``jargon_min_senders`` 位不同发言人用过这个词。

审核失败不影响提审本身：原记录保持待审。
"""

from __future__ import annotations

import time
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope

ACTOR = "auto_review"


class AutoReviewer:
    def __init__(
        self,
        db: Any,
        write_gateway: Any = None,
        *,
        enabled: bool = False,
        jargon_min_memories: int = 3,
        jargon_min_senders: int = 2,
    ):
        self.db = db
        self.write_gateway = write_gateway
        self.enabled = bool(enabled)
        self.jargon_min_memories = max(1, int(jargon_min_memories))
        self.jargon_min_senders = max(1, int(jargon_min_senders))
        self.stats: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    # ─── 事实 ───

    async def review_fact(self, scope: RuntimeScope, fact_id: int) -> str | None:
        """返回审核后的状态（active / conflict），不满足规则或未开启时返回 None。"""
        if not self.enabled or self.write_gateway is None:
            return None
        repo = getattr(self.db, "scoped_knowledge", None)
        try:
            row = next(
                (item for item in repo.list_scoped_facts(scope, limit=200) if int(item.get("id", -1)) == int(fact_id)),
                None,
            ) if repo is not None else None
            if row is None or row.get("status") != "pending":
                return None
            provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
            if not str(provenance.get("source_quote") or "").strip() or not row.get("source_memory_id"):
                self._count("fact_skipped_no_evidence")
                return None
            if provenance.get("needs_jargon_review"):
                self._count("fact_skipped_jargon")
                return None
            try:
                from .scoped_knowledge_mutations import ScopedKnowledgeMutationGateway, ScopedKnowledgeMutationTarget
            except ImportError:  # pragma: no cover
                from services.scoped_knowledge_mutations import ScopedKnowledgeMutationGateway, ScopedKnowledgeMutationTarget
            result = await ScopedKnowledgeMutationGateway(self.write_gateway).review_fact(
                scope=scope,
                target=ScopedKnowledgeMutationTarget("fact", int(fact_id), int(row.get("revision") or 1)),
                action="approve",
                reason=ACTOR,
                idempotency_key=f"{ACTOR}:fact:{fact_id}:{row.get('revision')}",
            )
            status = str(getattr(result, "status", "") or "")
            self._count(f"fact_{status or 'unknown'}")
            return status or None
        except Exception as exc:
            self._count("fact_error")
            logger.warning(f"[WaveMemory] 事实自动审核失败（保持待审）: {exc!r}")
            return None

    # ─── 信念 ───

    def review_belief(self, scope: RuntimeScope, belief_id: int) -> str | None:
        if not self.enabled:
            return None
        repo = getattr(self.db, "scoped_knowledge", None)
        if repo is None:
            return None
        try:
            try:
                from .belief_lifecycle import BeliefLifecycleService
            except ImportError:  # pragma: no cover
                from services.belief_lifecycle import BeliefLifecycleService
            result = BeliefLifecycleService(repo).transition(scope, int(belief_id), "approve")
            status = str(result.get("status") or "")
            self._count(f"belief_{status or 'unknown'}")
            return status or None
        except ValueError as exc:  # 证据不足等：保持待审
            self._count(f"belief_skipped_{exc}")
            return None
        except Exception as exc:
            self._count("belief_error")
            logger.warning(f"[WaveMemory] 信念自动审核失败（保持待审）: {exc!r}")
            return None

    # ─── 黑话 ───

    def jargon_usage(self, scope: RuntimeScope, word: str, *, limit: int = 200) -> tuple[int, int]:
        """本群用过这个词的 (记忆数, 不同发言人数)。优先走中文全文索引，未就绪时退回 LIKE。"""
        conn = getattr(self.db, "conn", None)
        if conn is None or not word or scope.session is None:
            return 0, 0
        params = (scope.bot_id, scope.session.id, scope.visibility)
        rows = []
        try:
            try:
                from ..engine.db import fts_cjk
            except ImportError:  # pragma: no cover
                from engine.db import fts_cjk
            if fts_cjk.is_ready(conn):
                ids = fts_cjk.query_ids(conn, [word], limit=limit * 5)
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    rows += conn.execute(
                        f"""SELECT id, sender_id, content FROM memories
                             WHERE id IN ({','.join('?' * len(chunk))})
                               AND bot_id=? AND session_id=? AND visibility=? AND COALESCE(sender_id,'') != 'bot'""",
                        (*chunk, *params),
                    ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, sender_id, content FROM memories
                        WHERE bot_id=? AND session_id=? AND visibility=? AND COALESCE(sender_id,'') != 'bot'
                          AND content LIKE ? ORDER BY id DESC LIMIT ?""",
                    (*params, f"%{word}%", limit),
                ).fetchall()
        except Exception as exc:
            logger.debug(f"[WaveMemory] 黑话用量统计失败: {exc!r}")
            return 0, 0
        hits = [row for row in rows if word in str(row[2] or "")]
        return len(hits), len({str(row[1] or "") for row in hits})

    def review_jargon(self, scope: RuntimeScope, jargon_id: int) -> str | None:
        if not self.enabled:
            return None
        repo = getattr(self.db, "scoped_knowledge", None)
        if repo is None:
            return None
        try:
            row = next(
                (item for item in repo.list_scoped_jargon(scope, limit=10000) if int(item.get("id", -1)) == int(jargon_id)),
                None,
            )
            if row is None or row.get("status") != "pending" or not str(row.get("meaning") or "").strip():
                return None
            memories, senders = self.jargon_usage(scope, str(row.get("word") or ""))
            if memories < self.jargon_min_memories or senders < self.jargon_min_senders:
                self._count("jargon_skipped_usage")
                return None
            provenance = dict(row.get("provenance") or {})
            provenance.update({
                "reviewed_by": ACTOR,
                "review_action": "approve",
                "auto_review": {"memories": memories, "senders": senders, "at": time.time()},
            })
            repo.upsert_scoped_jargon(
                scope,
                word=row["word"],
                meaning=row.get("meaning") or "",
                status="confirmed",
                is_jargon=True,
                frequency=max(int(row.get("frequency") or 0), memories),
                confidence=float(row.get("confidence") or 0.0),
                contexts=row.get("contexts") or [],
                source_memory_id=row.get("source_memory_id"),
                source_context=row.get("source_context"),
                provenance=provenance,
            )
            self._count("jargon_confirmed")
            return "confirmed"
        except Exception as exc:
            self._count("jargon_error")
            logger.warning(f"[WaveMemory] 黑话自动审核失败（保持待审）: {exc!r}")
            return None


# 工具在各自构造处拿不到插件级依赖，沿用 impression_timeline 社交上限的做法：启动时注入一个实例。
_REVIEWER: AutoReviewer | None = None


def set_auto_reviewer(reviewer: AutoReviewer | None) -> None:
    global _REVIEWER
    _REVIEWER = reviewer


def get_auto_reviewer() -> AutoReviewer | None:
    return _REVIEWER


__all__ = ["AutoReviewer", "get_auto_reviewer", "set_auto_reviewer"]
