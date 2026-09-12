"""Jargon RuntimeScope 服务。legacy ``jargon`` 表绝不参与正式路径。"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from astrbot.api import logger
try:
    from ...domain.scope import RuntimeScope
    from ...engine.database import normalize_jargon_word
except ImportError:
    from domain.scope import RuntimeScope
    from engine.database import normalize_jargon_word

from .holyman_reference import HolymanReference
from .inference import JargonInferenceEngine, JargonInjector
from .statistical_filter import JargonStatisticalFilter, scope_key

_TECHNICAL_NOISE_WORDS = {"id", "ids", "json", "api", "url", "uri", "http", "https", "get", "post", "put", "patch", "delete", "from", "has", "object", "objects", "array", "list", "dict", "map", "set", "type", "types", "value", "values", "data", "item", "items", "key", "keys", "param", "params", "args", "kwargs", "none", "null", "true", "false", "bool", "str", "int", "float", "class", "method", "function", "return", "import", "async", "await", "self", "this", "const", "let", "var"}
_ORDINARY_WORDS = {"吃饭", "睡觉", "上班", "下班", "回家", "出门", "上课", "工作", "学习", "考试", "好的", "可以", "谢谢", "没事", "不用", "不是", "没有", "手机", "电脑", "学校", "今天", "昨天", "明天", "现在", "刚才", "马上", "哈哈", "哈哈哈", "嗯嗯", "呵呵", "朋友", "同学", "老师", "家人", "爸爸", "妈妈", "真的", "确实", "其实", "当然", "知道", "不知道", "怎么", "什么", "为什么", "这个", "那个"}


class JargonService:
    """黑话挖掘与注入门面；任何非群 RuntimeScope 都 fail-closed。"""

    def __init__(self, db: Any, llm_client: Any = None, enabled: bool = True, config: dict | None = None, trace_store: Any = None):
        self._db, self._enabled, self._config, self._llm = db, enabled, config or {}, llm_client
        self._trace_store = trace_store or getattr(db, "injection_trace_store", None)
        self._repo = getattr(db, "scoped_knowledge", None)
        self._min_frequency = int(self._config.get("min_frequency", 5))
        self._min_messages = int(self._config.get("min_messages", 10))
        self._mine_cooldown = int(self._config.get("mine_cooldown", 20))
        self._top_k = int(self._config.get("top_k", 20))
        self._max_context = int(self._config.get("max_context", 15))
        self._confidence_threshold = float(self._config.get("confidence_threshold", .5))
        enabled_validate = self._config.get("llm_validate", True)
        self._llm_validate = True if enabled_validate is None else bool(enabled_validate)
        self._inference_thresholds = [int(value.strip()) for value in str(self._config.get("inference_thresholds", "3,6,10,20,40,60,100")).split(",") if value.strip()]
        self._holyman = HolymanReference(
            root_path=self._config.get("holyman_root_path"),
            max_examples=int(self._config.get("holyman_max_examples", 3)),
        )
        self._filter = JargonStatisticalFilter(
            context_keep=int(self._config.get("context_keep", 10)),
            window_days=int(self._config.get("window_days", 7)),
            jieba_threshold=int(self._config.get("jieba_threshold", 100)),
            weight_idf=float(self._config.get("weight_idf", .4)),
            weight_burst=float(self._config.get("weight_burst", .3)),
            weight_concentration=float(self._config.get("weight_concentration", .3)),
            candidate_router=self.classify_candidate,
        )
        blocklist_checker = getattr(db, "is_jargon_blocked", None)
        self._inference = JargonInferenceEngine(
            llm_client,
            max_context=self._max_context,
            blocklist_checker=blocklist_checker,
        ) if llm_client else None
        self._injector = JargonInjector(
            db,
            max_inject=int(self._config.get("max_inject", 3)),
            holyman_reference=self._holyman,
            blocklist_checker=blocklist_checker,
            global_limit=int(self._config.get(
                "global_max_inject",
                int(self._config.get("bot_global_max_inject", 3)) + int(self._config.get("reference_max_inject", 3)),
            )),
        )
        self._last_mine: Dict[tuple[str, str, str], float] = {}
        self._msg_count: Dict[tuple[str, str, str], int] = {}

    @staticmethod
    def _group_scope(scope: RuntimeScope | None) -> RuntimeScope | None:
        return scope if scope_key(scope) is not None else None

    def _repository_available(self) -> bool:
        return self._repo is not None

    def _is_globally_blocked(self, word: str) -> bool:
        checker = getattr(self._db, "is_jargon_blocked", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(word))
        except Exception as exc:
            logger.warning("[Jargon] blocklist check failed closed for %r: %s", word, exc)
            return True

    def _resolve_source_memory_id(self, scope: RuntimeScope, source_contexts: List[Dict[str, Any]], word: str) -> int | None:
        """用同一 RuntimeScope 内已落库消息解析候选锚点，绝不按旧 group_id 回退。"""
        conn = getattr(self._db, "conn", None)
        if conn is None or scope.session is None:
            return None
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
            required = {"id", "content", "timestamp", "bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
            if not required <= columns:
                return None
            sender_clause = " AND (? = '' OR sender_id = ?)" if "sender_id" in columns else ""
            message_clause = " AND memory_type='message'" if "memory_type" in columns else ""
            for context in source_contexts:
                try:
                    timestamp = float(context.get("timestamp"))
                except (TypeError, ValueError):
                    continue
                sender_id = str(context.get("sender_id") or "")
                params: list[Any] = [
                    scope.bot_id,
                    scope.session.id,
                    scope.visibility,
                    timestamp - 120,
                    timestamp + 120,
                ]
                if sender_clause:
                    params.extend([sender_id, sender_id])
                params.extend([f"%{word}%", timestamp])
                row = conn.execute(
                    "SELECT id FROM memories WHERE bot_id=? AND session_id=? AND visibility=? "
                    "AND resolution_state='resolved' AND COALESCE(quarantine,0)=0 "
                    "AND timestamp BETWEEN ? AND ?"
                    f"{sender_clause}{message_clause} AND content LIKE ? "
                    "ORDER BY ABS(timestamp - ?) ASC LIMIT 1",
                    params,
                ).fetchone()
                if row is not None:
                    return int(row[0])
        except Exception:
            return None
        return None

    def feed_message(self, text: str, runtime_scope: RuntimeScope | None, sender_id: str = "", timestamp: float | None = None) -> None:
        key = scope_key(runtime_scope)
        if not self._enabled or key is None:
            return
        self._filter.feed(text, runtime_scope, sender_id, timestamp=timestamp or time.time())
        self._msg_count[key] = self._msg_count.get(key, 0) + 1

    def should_mine(self, runtime_scope: RuntimeScope | None) -> bool:
        key = scope_key(runtime_scope)
        if not self._enabled or key is None or not self._repository_available():
            return False
        return self._msg_count.get(key, 0) >= self._min_messages and time.time() - self._last_mine.get(key, 0) >= self._mine_cooldown

    async def mine(self, runtime_scope: RuntimeScope | None, query_trace_id: str | None = None) -> List[Dict[str, Any]]:
        """以 (bot_id, session_id, visibility) 独立挖掘和持久化。"""
        scope, key = self._group_scope(runtime_scope), scope_key(runtime_scope)
        if not self._enabled or scope is None or key is None or not self._repository_available():
            return []
        self._msg_count[key], self._last_mine[key] = 0, time.time()
        raw_candidates = self._filter.get_candidates(scope, min_freq=self._min_frequency, top_k=self._top_k)
        group_id = scope.session.conversation_id if scope.session else ""
        candidates: list[dict[str, Any]] = []
        for raw_candidate in raw_candidates:
            classification = self.classify_candidate(
                raw_candidate.get("word", ""),
                group_id,
                (raw_candidate.get("source_contexts") or [{}])[0],
                raw_candidate.get("contexts") or [],
            )
            if not classification.get("enter_llm"):
                continue
            candidate = dict(raw_candidate)
            candidate["word"] = classification["word"]
            candidates.append(candidate)
        if self._llm_validate and self._llm and candidates:
            candidates = await self._llm_validate_candidates(candidates)
        candidates = [candidate for candidate in candidates if not self._is_globally_blocked(candidate.get("word", ""))]
        if not candidates:
            return []
        results: List[Dict[str, Any]] = []
        trace_status = "pending"
        if query_trace_id and self._trace_store is not None:
            try:
                trace_status = "verified" if self._trace_store.get_for_scope(str(query_trace_id), scope) else "invalid"
            except Exception:
                trace_status = "invalid"
        for candidate in candidates:
            word, contexts, now = normalize_jargon_word(candidate["word"]), candidate.get("contexts") or [], int(time.time())
            if self._is_globally_blocked(word):
                continue
            source_contexts = candidate.get("source_contexts") or []
            source_memory_id = self._resolve_source_memory_id(scope, source_contexts, word)
            existing = next((row for row in self._repo.list_scoped_jargon(scope, limit=100) if row.get("word") == word), None)
            if existing and source_memory_id is None:
                source_memory_id = existing.get("source_memory_id")
            source_context = json.dumps(source_contexts, ensure_ascii=False) if source_contexts else (existing.get("source_context") if existing else None)
            frequency = int(candidate.get("frequency", 0))
            is_jargon = None
            meaning, confidence, status = "", 0.0, "pending"
            # scoped_jargon 没有 legacy last_infer_freq；每次触发均以当前 Scope 的上下文重判。
            if self._inference and contexts:
                try:
                    inferred = await self._inference.infer(word, contexts)
                    is_jargon, meaning, confidence = inferred.get("is_jargon"), inferred.get("meaning", ""), float(inferred.get("confidence", 0.0) or 0.0)
                except Exception as exc:
                    logger.debug("[Jargon] inference error for %r: %s", word, exc)
            status = "confirmed" if is_jargon is True and meaning and confidence >= self._confidence_threshold else ("rejected" if is_jargon is False else "pending")
            if existing and is_jargon is None:
                meaning, is_jargon, confidence, status = existing.get("meaning", ""), existing.get("is_jargon"), float(existing.get("confidence", 0.0) or 0.0), existing.get("status", "pending")
            source_tags: list[dict[str, Any]] = []
            tag_chain_status = "missing"
            tag_getter = getattr(self._repo, "list_scoped_memory_tags", None)
            if callable(tag_getter) and source_memory_id is not None:
                try:
                    source_tags = list(tag_getter(scope, [int(source_memory_id)]) or [])
                    tag_chain_status = "complete" if source_tags else "empty"
                except Exception:
                    tag_chain_status = "unavailable"
            if status == "confirmed" and (trace_status != "verified" or tag_chain_status != "complete"):
                status = "pending"
            if self._is_globally_blocked(word):
                continue
            self._repo.upsert_scoped_jargon(
                scope, word=word, meaning=meaning, status=status, is_jargon=is_jargon,
                frequency=frequency, confidence=confidence, contexts=contexts,
                source_memory_id=source_memory_id,
                source_context=source_context,
                provenance={"source": "wave_memory", "producer": "jargon_mining", "candidate_type": "local_jargon_candidate", "source_tags": source_tags, "evidence": {"memory_ids": [source_memory_id] if source_memory_id is not None else [], "contexts": contexts[:10]}, "scope": scope.session.id if scope.session else scope.bot_id, "query_trace_id": str(query_trace_id or ""), "trace_status": trace_status, "tag_chain_status": tag_chain_status},
            )
            if is_jargon is True and meaning:
                results.append({"word": word, "meaning": meaning, "confidence": confidence})
        return results

    def review(
        self,
        runtime_scope: RuntimeScope,
        jargon_id: int,
        action: str,
        query_trace_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """通过领域服务执行 scoped 候选审核；reject 同时写入全局拉黑。"""
        scope = self._group_scope(runtime_scope)
        if scope is None or not self._repository_available():
            raise ValueError("jargon_review_command_unavailable")
        if action not in {"approve", "reject"}:
            raise ValueError("invalid_review_action")
        current = next(
            (row for row in self._repo.list_scoped_jargon(scope, limit=10000) if int(row.get("id", -1)) == int(jargon_id)),
            None,
        )
        if current is None:
            raise LookupError("scoped_object_not_found")
        if action == "approve" and not current.get("source_memory_id"):
            raise ValueError("jargon_anchor_required")
        provenance = dict(current.get("provenance") or {})
        if action == "approve":
            if not query_trace_id or self._trace_store is None or not self._trace_store.get_for_scope(str(query_trace_id), scope):
                raise ValueError("jargon_query_trace_required")
            if not provenance.get("source_tags") or not provenance.get("evidence"):
                raise ValueError("jargon_evidence_required")
        status = "confirmed" if action == "approve" else "rejected"
        rejection_reason = str(reason or "user_global_reject").strip() or "user_global_reject"
        if action == "reject":
            add_block = getattr(self._db, "add_jargon_blocklist", None)
            if not callable(add_block):
                raise ValueError("jargon_blocklist_command_unavailable")
            add_block(current["word"], reason=rejection_reason, source="user_global_reject")
        provenance = dict(current.get("provenance") or {})
        provenance.update({"reviewed_by": "webui", "review_action": action})
        if action == "reject":
            provenance["reject_reason"] = rejection_reason
            provenance["blocklist_source"] = "user_global_reject"
        self._repo.upsert_scoped_jargon(
            scope,
            word=current["word"],
            meaning=current.get("meaning") or "",
            status=status,
            is_jargon=action == "approve",
            frequency=int(current.get("frequency") or 0),
            confidence=float(current.get("confidence") or 0.0),
            contexts=current.get("contexts") or [],
            source_memory_id=current.get("source_memory_id"),
            source_context=current.get("source_context"),
            provenance=provenance,
        )
        return {"id": int(jargon_id), "status": status, "scope": scope}

    def update_meaning(self, runtime_scope: RuntimeScope, jargon_id: int, meaning: str) -> dict[str, Any]:
        """更新 scoped 黑话释义；任何语义修改都重新进入待审核。"""
        scope = self._group_scope(runtime_scope)
        meaning = str(meaning or "").strip()
        if scope is None or not self._repository_available():
            raise ValueError("jargon_update_command_unavailable")
        if not meaning or len(meaning) > 2000:
            raise ValueError("invalid_jargon_meaning")
        current = next(
            (row for row in self._repo.list_scoped_jargon(scope, limit=10000) if int(row.get("id", -1)) == int(jargon_id)),
            None,
        )
        if current is None:
            raise LookupError("scoped_object_not_found")
        provenance = dict(current.get("provenance") or {})
        provenance.update({"edited_by": "webui", "edit_requires_review": True})
        self._repo.upsert_scoped_jargon(
            scope,
            word=current["word"],
            meaning=meaning,
            status="pending",
            is_jargon=None,
            frequency=int(current.get("frequency") or 0),
            confidence=float(current.get("confidence") or 0.0),
            contexts=current.get("contexts") or [],
            source_memory_id=current.get("source_memory_id"),
            source_context=current.get("source_context"),
            provenance=provenance,
        )
        updated = next(
            row for row in self._repo.list_scoped_jargon(scope, limit=10000)
            if int(row.get("id", -1)) == int(jargon_id)
        )
        return updated

    def archive(self, runtime_scope: RuntimeScope, jargon_id: int) -> dict[str, Any]:
        """从正式注入集合归档 scoped 黑话，不执行物理删除。"""
        scope = self._group_scope(runtime_scope)
        if scope is None or not self._repository_available():
            raise ValueError("jargon_archive_command_unavailable")
        current = next(
            (row for row in self._repo.list_scoped_jargon(scope, limit=10000) if int(row.get("id", -1)) == int(jargon_id)),
            None,
        )
        if current is None:
            raise LookupError("scoped_object_not_found")
        provenance = dict(current.get("provenance") or {})
        provenance.update({"archived_by": "webui", "archive_reason": "manual_remove"})
        self._repo.upsert_scoped_jargon(
            scope,
            word=current["word"],
            meaning=current.get("meaning") or "",
            status="archived",
            is_jargon=False,
            frequency=int(current.get("frequency") or 0),
            confidence=float(current.get("confidence") or 0.0),
            contexts=current.get("contexts") or [],
            source_memory_id=current.get("source_memory_id"),
            source_context=current.get("source_context"),
            provenance=provenance,
        )
        return {"id": int(jargon_id), "status": "archived", "scope": scope}

    # ─── 广域（Bot 级）黑话 ───
    # 群级 scoped_jargon 只能被本群看到；这里的方法读写 bot_jargon，供该 Bot 的所有群共享。

    def _bot_jargon_repo(self) -> Any:
        repo = getattr(self._db, "bot_jargon", None)
        if repo is None:
            raise ValueError("bot_jargon_repository_unavailable")
        return repo

    def promote_to_global(self, runtime_scope: RuntimeScope, jargon_id: int) -> dict[str, Any]:
        """把本群一条已生效黑话提升为该 Bot 的广域黑话。

        只接受 ``status='confirmed'``：未生效（pending/rejected/archived）的候选不该被
        扩散到所有群。提升是幂等 upsert，重复点击不会产生重复行。
        """
        scope = self._group_scope(runtime_scope)
        if scope is None or not self._repository_available():
            raise ValueError("jargon_promote_command_unavailable")
        current = next(
            (row for row in self._repo.list_scoped_jargon(scope, limit=10000) if int(row.get("id", -1)) == int(jargon_id)),
            None,
        )
        if current is None:
            raise LookupError("scoped_object_not_found")
        word = normalize_jargon_word(current.get("word") or "")
        if not word:
            raise ValueError("invalid_jargon_word")
        meaning = str(current.get("meaning") or "").strip()
        if current.get("status") != "confirmed":
            raise ValueError("jargon_not_confirmed")
        if not meaning:
            raise ValueError("jargon_meaning_required")
        if self._is_globally_blocked(word):
            raise ValueError("jargon_globally_blocked")
        repo = self._bot_jargon_repo()
        repo.upsert_bot_jargon(
            scope.bot_id,
            word=word,
            meaning=meaning,
            status="active",
            source="promoted",
            confidence=float(current.get("confidence") or 0.0),
            origin_scope=scope.session.id if scope.session else None,
            provenance={"promoted_from_scope": scope.session.id if scope.session else None, "promoted_by": "webui"},
        )
        return {"word": word, "status": "active", "source": "promoted", "bot_id": scope.bot_id}

    def list_global_jargon(self, bot_id: str, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """列出该 Bot 的广域黑话：内置资产条目 + Bot 级覆盖层合并后的完整视图。

        内置可匹配口癖默认启用（无覆盖行即 ``active``），停用/自定义释义/群内提升统一
        落 ``bot_jargon``。返回顺序与注入优先级一致：Bot 级覆盖层在前，内置资产在后。
        """
        repo = self._bot_jargon_repo()
        rows = repo.list_bot_jargon(bot_id, limit=max(1, min(int(limit) or 1, 500)))
        overrides: dict[str, dict[str, Any]] = {}
        tombstoned: set[str] = set()
        for row in rows:
            word = normalize_jargon_word(row.get("word"))
            if not word:
                continue
            if row.get("source") == "manual_deleted":
                tombstoned.add(word)
                continue
            overrides[word] = row

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for word, row in overrides.items():
            seen.add(word)
            items.append({
                "id": row.get("id"),
                "word": row.get("word"),
                "meaning": str(row.get("meaning") or ""),
                "status": str(row.get("status") or "active"),
                "source": str(row.get("source") or "manual"),
                "confidence": row.get("confidence"),
                "origin_scope": row.get("origin_scope"),
                "reference_key": row.get("reference_key"),
                "updated_at": row.get("updated_at"),
                "is_builtin": False,
            })

        for word, payload in self._holyman.runtime_matchable_entries().items():
            normalized = normalize_jargon_word(word)
            # 有覆盖行（含已停用/已删除）的同词条目已在上面处理，此处只剩「默认启用」的内置条目。
            if not normalized or normalized in seen or normalized in tombstoned:
                continue
            if not self._holyman_is_matchable(word):
                continue
            seen.add(normalized)
            payload = payload if isinstance(payload, dict) else {"meaning": str(payload or "")}
            items.append({
                "id": None,
                "word": normalized,
                "meaning": str(payload.get("meaning") or payload.get("explanation") or "").strip(),
                "status": "active",
                "source": "holyman_skills",
                "confidence": payload.get("confidence"),
                "origin_scope": None,
                "reference_key": word,
                "updated_at": None,
                "is_builtin": True,
            })

        if status is not None:
            items = [item for item in items if item["status"] == status]
        return items

    def _holyman_is_matchable(self, word: str) -> bool:
        checker = getattr(self._holyman, "_is_matchable_phrase", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(word))
        except Exception:
            return True

    def upsert_global_jargon(
        self,
        bot_id: str,
        *,
        word: str,
        meaning: str,
        status: str = "active",
        confidence: float = 0.0,
    ) -> dict[str, Any]:
        """WebUI 手工新增/编辑广域黑话。

        以 ``source='manual'`` 覆盖写入，顺带解掉删除墓碑（``manual_deleted``）——否则
        用户重新添加同一个词时会看到它依然不生效。
        """
        word = normalize_jargon_word(word)
        if not word:
            raise ValueError("invalid_jargon_word")
        meaning = str(meaning or "").strip()
        if not meaning:
            raise ValueError("jargon_meaning_required")
        if self._is_globally_blocked(word):
            raise ValueError("jargon_globally_blocked")
        self._bot_jargon_repo().upsert_bot_jargon(
            bot_id, word=word, meaning=meaning, status=status,
            source="manual", confidence=confidence, provenance={"edited_by": "webui"},
        )
        return {"word": word, "status": status, "source": "manual", "bot_id": bot_id}

    def set_global_jargon_status(self, bot_id: str, *, word: str, status: str) -> dict[str, Any]:
        """启用/停用一条广域黑话。

        内置资产条目默认启用且没有覆盖行，因此停用它们时要先落一条 enabled↔disabled 的
        覆盖行；停用保留内置释义（便于一键恢复），启用则清掉墓碑让它真正回到注入集合。
        """
        normalized = normalize_jargon_word(word)
        if not normalized:
            raise ValueError("invalid_jargon_word")
        repo = self._bot_jargon_repo()
        current = repo.find_bot_jargon(bot_id, word=normalized)
        if current is not None and current.get("source") != "manual_deleted":
            return repo.set_bot_jargon_status(bot_id, word=normalized, status=status)
        if normalized not in self._builtin_meanings():
            raise LookupError("scoped_object_not_found")
        if normalized in self._builtin_meanings() and current is None:
            repo.upsert_bot_jargon(
                bot_id, word=normalized, meaning="", status=status, source="manual",
                provenance={"overlay": "builtin_toggle"},
            )
            return {"id": None, "word": normalized, "status": status, "bot_id": bot_id, "is_builtin": True}
        return repo.set_bot_jargon_status(bot_id, word=normalized, status=status)

    def delete_global_jargon(self, bot_id: str, *, word: str) -> dict[str, Any]:
        """移除一条广域黑话。

        内置资产条目不能物理删除（下次启动就会重新出现），因此对内置词只落一条墓碑行，
        让「移除」在注入层真正生效且不被资产重新复活。
        """
        normalized = normalize_jargon_word(word)
        if not normalized:
            raise ValueError("invalid_jargon_word")
        repo = self._bot_jargon_repo()
        current = repo.find_bot_jargon(bot_id, word=normalized)
        if current is not None:
            return repo.delete_bot_jargon(bot_id, word=normalized)
        if normalized not in self._builtin_meanings():
            raise LookupError("scoped_object_not_found")
        repo.upsert_bot_jargon(
            bot_id, word=normalized, meaning="", status="inactive", source="manual",
            provenance={"overlay": "builtin_delete"},
        )
        return repo.delete_bot_jargon(bot_id, word=normalized)

    def _builtin_meanings(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for word, payload in self._holyman.runtime_matchable_entries().items():
            normalized = normalize_jargon_word(word)
            if not normalized or not self._holyman_is_matchable(word):
                continue
            payload = payload if isinstance(payload, dict) else {"meaning": str(payload or "")}
            result[normalized] = str(payload.get("meaning") or payload.get("explanation") or "").strip()
        return result

    def get_injection(self, text: str, runtime_scope: RuntimeScope | None) -> str:
        if not self._enabled or self._group_scope(runtime_scope) is None:
            return ""
        # Curated Holyman references remain available even when no local confirmed
        # jargon exists; local entries still require the scoped repository.
        return self._injector.get_injection(text, runtime_scope)

    def get_last_injection_items(self) -> List[Dict[str, Any]]:
        return self._injector.get_last_injection_items()

    def classify_candidate(
        self,
        word: str,
        group_id: str = "",
        source_ctx: Optional[Dict[str, Any]] = None,
        contexts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """统一候选分类；只有本群未知黑话允许进入统计、LLM 和正式写入。"""
        word = normalize_jargon_word(word)
        source_ctx, contexts = source_ctx or {}, contexts or []
        base = {"word": word, "candidate_type": "local_jargon_candidate", "enter_llm": True, "reject_reason": None, "source": "wave_memory", "scope": "local", "meaning": "", "confidence": 0.0, "reference_only": False}
        if self._is_globally_blocked(word):
            return {**base, "enter_llm": False, "reject_reason": "global_blocklist"}
        if self._is_known_person_alias(word, group_id, source_ctx):
            return {**base, "candidate_type": "person_alias", "enter_llm": False, "reject_reason": "person_alias_diverted"}
        if self._is_technical_noise_candidate(word):
            return {**base, "candidate_type": "technical_noise", "enter_llm": False, "reject_reason": "technical_noise_filtered"}
        if not word or self._is_ordinary_word_candidate(word):
            return {**base, "candidate_type": "ordinary_word", "enter_llm": False, "reject_reason": "ordinary_word_filtered"}
        holyman_match = self._holyman.match(word, "\n".join(contexts))
        if holyman_match.get("matched"):
            return {
                **base,
                "candidate_type": "holyman_reference_hit",
                "enter_llm": False,
                "source": "holyman_skills",
                "source_layer": holyman_match.get("source_layer") or "curated",
                "meaning": holyman_match.get("explanation", ""),
                "confidence": float(holyman_match.get("confidence", 0.0) or 0.0),
                "reference_only": True,
                "matched_term": holyman_match.get("term") or word,
            }
        return base

    def _is_known_person_alias(self, word: str, group_id: str, source_ctx: Dict[str, Any]) -> bool:
        word = (word or "").strip()
        if not word:
            return False
        if word in {str(source_ctx.get("sender_name") or "").strip(), str(source_ctx.get("sender_id") or "").strip()}:
            return True
        conn = getattr(self._db, "conn", None)
        if conn is None:
            return False
        bot_id = str(source_ctx.get("bot_id") or "").strip()
        profile_predicate = "group_id = ? AND nickname = ? AND bot_id = ?" if bot_id else "group_id = ? AND nickname = ?"
        profile_params = (group_id, word, bot_id) if bot_id else (group_id, word)
        checks = (
            ("memories", "group_id = ? AND sender_name = ?", (group_id, word)),
            ("user_profiles", profile_predicate, profile_params),
        )
        for table, predicate, params in checks:
            try:
                if conn.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", params).fetchone():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _is_technical_noise_candidate(word: str) -> bool:
        word = (word or "").strip()
        return word.lower() in _TECHNICAL_NOISE_WORDS or bool(re.match(r"^https?://", word, re.I) or re.search(r"[/\\]", word) and re.search(r"\.[A-Za-z0-9]{1,8}$", word) or re.fullmatch(r"[a-fA-F0-9]{7,64}", word) or re.fullmatch(r"v?\d+(?:\.\d+){1,3}", word))

    @staticmethod
    def _is_ordinary_word_candidate(word: str) -> bool:
        word = (word or "").strip()
        return not word or "@" in word or len(word) < 2 or len(word) > 12 or bool(re.match(r"^[\d\s.]+$", word) or re.match(r"^[^\w\u4e00-\u9fff]+$", word) or re.search(r"[，。！？!?、；;：:\s]", word) or re.match(r"^[A-Za-z]+$", word) and len(word) > 6 or re.match(r"^\[.+\]$", word)) or word in _ORDINARY_WORDS

    @staticmethod
    def _should_filter_candidate(word: str) -> bool:
        return JargonService._is_technical_noise_candidate(word) or JargonService._is_ordinary_word_candidate(word)

    def _should_reinfer(self, current_freq: int, last_infer_freq: int) -> bool:
        return any(last_infer_freq < threshold <= current_freq for threshold in self._inference_thresholds)

    async def _llm_validate_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        snippets = "\n".join(f"- {context}" for candidate in candidates for context in candidate.get("contexts", [])[:20])
        prompt = f"""**近期聊天片段**\n{snippets}\n\n**候选词列表**\n{', '.join(str(c['word']) for c in candidates)}\n\n只输出 JSON 数组：其中确有本群特殊语义、且不是普通词/昵称/人名/品牌名的词条。"""
        try:
            response = await self._llm.text_chat(prompt=prompt)
            text = str(getattr(response, "completion_text", "") or "").strip()
            match = re.search(r"\[[\s\S]*?\]", text)
            accepted = set(json.loads(match.group())) if match else set()
            return [candidate for candidate in candidates if candidate["word"] in accepted]
        except Exception as exc:
            logger.debug("[Jargon] LLM validation error: %s", exc)
            return []


__all__ = ["JargonService"]
