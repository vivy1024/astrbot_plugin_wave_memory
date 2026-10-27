"""Few-Shot 风格学习服务 (US-5.1 ~ US-5.4)

功能:
- 从 bot 历史回复中提取高质量风格范例
- LLM 请求时注入 2-3 条已批准 few-shot
- 风格漂移检测
"""

from __future__ import annotations

try:
    from ...domain import bot_identity
except ImportError:  # top-level import in isolated tests
    from domain import bot_identity

import asyncio
import hashlib
import json
import os
import random
import threading
import re
import time
from html import escape
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 领域单测不依赖 AstrBot runtime
    import logging

    logger = logging.getLogger(__name__)

try:
    from ...domain.evidence import EvidenceBinding, EvidenceRef, RawArtifactRef
    from ...domain.scope import RuntimeScope, SessionRef
    from ...engine.db.scoped_learning_projection_repo import (
        ScopedBotReplyRepository,
        ScopedFewShotRepository,
    )
except ImportError:  # pragma: no cover - 独立测试兼容
    from domain.evidence import EvidenceBinding, EvidenceRef, RawArtifactRef
    from domain.scope import RuntimeScope, SessionRef
    from engine.db.scoped_learning_projection_repo import (
        ScopedBotReplyRepository,
        ScopedFewShotRepository,
    )

from ..identity_safety import is_identity_contamination


# ─── LLM Prompt ───

_EVALUATE_STYLE_PROMPT = """你是一个对话风格分析专家。
请评估以下 bot 回复是否具有鲜明的个人风格特征（语气、用词、句式习惯等）。

Bot 回复：
{reply}

请评分（0.0~1.0），0 表示毫无风格特征，1 表示风格极其鲜明。
只返回一个 JSON：{{"score": 0.7, "traits": ["特征1", "特征2"]}}"""

_DRIFT_CHECK_PROMPT = """你是一个对话风格一致性分析师。

风格范例库：
{examples}

最新回复：
{recent}

请判断最新回复与范例库的风格是否一致。
返回 JSON：{{"similarity": 0.8, "drift_detected": false, "reason": "..."}}"""


_AGGRESSIVE_STYLE_RE = re.compile(
    r"(怼回去|狠狠怼|别客气|骂回去|反击|嘴臭|阴阳怪气|傻逼|脑残|滚|nmsl|你妈|操你|fuck\s*you)",
    re.IGNORECASE,
)


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _scope_for_candidate(candidate: Mapping[str, Any]) -> RuntimeScope | None:
    if str(candidate.get("visibility") or "") != "group":
        return None
    bot_id = str(candidate.get("bot_id") or "").strip()
    session_id = str(candidate.get("session_id") or "").strip()
    parts = session_id.split(":", 2)
    if not bot_id or len(parts) != 3:
        return None
    platform_id, kind, conversation_id = parts
    if kind != "group" or not platform_id or not conversation_id:
        return None
    try:
        return RuntimeScope(
            bot_id=bot_id,
            visibility="group",
            session=SessionRef(
                id=session_id,
                platform_id=platform_id,
                kind=kind,
                conversation_id=conversation_id,
            ),
        )
    except Exception:
        return None


class FewShotService:
    """Few-Shot 风格学习主服务。"""

    def __init__(
        self,
        db: Any,
        llm_client: Any = None,
        embedding_service: Any = None,
        enabled: bool = True,
        config: dict = None,
        repository: Any = None,
        writer: Any = None,
        expression_policy: Any = None,
        bot_reply_repository: Any = None,
        auto_approve: bool = True,
        rotation_state_path: str | None = None,
    ):
        self._db = db
        self._llm = llm_client
        self._embedding = embedding_service
        self._enabled = enabled
        self._config = config or {}
        connection = getattr(db, "conn", db)
        # 默认实例只用于读取，绝不在构造时绕过正式 writer 创建 schema。
        self._repository = repository or ScopedFewShotRepository(connection, ensure_schema=False)
        self._writer = writer
        self.expression_policy = expression_policy
        self._bot_replies = bot_reply_repository or ScopedBotReplyRepository(connection)
        self._last_extract_by_bot: dict[str, float] = {}  # 每个 Bot 上次提取时间
        self._last_injected_ids: List[int] = []  # 避免连续注入同一条
        self._last_injected_by_scope: dict[tuple, list[int]] = {}
        # 防复读记录落盘：只在内存里时，重启后第一轮总是和重启前最后一轮撞同一条。
        self._rotation_state_path = rotation_state_path
        self._rotation_lock = threading.Lock()
        self._load_rotation_state()

        # 从配置读取参数
        self._min_score = float(self._config.get("min_score", 0.7))
        self._max_inject = int(self._config.get("max_inject", 3))
        self._drift_threshold = float(self._config.get("drift_threshold", 0.5))
        self._auto_approve = bool(self._config.get("auto_approve", auto_approve))

    # ─── US-5.1: 提取风格范例 ───

    async def extract_candidates(self, bot_id: str = "") -> List[Dict]:
        """从最近 7 天 bot 回复中提取 top 10 候选。每个 Bot 每天最多执行一次。"""
        if not self._enabled or not self._llm:
            return []

        now = time.time()
        cooldown_key = str(bot_id or "")
        if now - self._last_extract_by_bot.get(cooldown_key, 0.0) < 86400:  # 24h 冷却
            return []
        self._last_extract_by_bot[cooldown_key] = now

        # 获取最近 7 天的 bot 回复。正式库把 Bot 自己的话记在 sender_id='bot'、
        # source='core'；旧库/旧经历仍可能只有 bot_reply 或 persona 登记的经历来源。
        seven_days_ago = int(now) - 7 * 86400
        sources = ["bot_reply", *sorted(bot_identity.experience_sources() - bot_identity.taken_over_sources())]
        placeholders = ",".join("?" for _ in sources)
        columns = {
            str(row[1]) for row in self._db.conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        if not columns:
            return []
        canonical = {"sender_id", "bot_id", "session_id", "visibility"} <= columns
        source_clause = (
            f"((COALESCE(sender_id,'')='bot' AND source='core') OR source IN ({placeholders}))"
            if canonical else f"source IN ({placeholders})"
        )
        clauses = [source_clause, "timestamp > ?", "LENGTH(content) >= 20"]
        params: list[Any] = [*sources, seven_days_ago]
        if "resolution_state" in columns:
            clauses.append("COALESCE(resolution_state,'') IN ('','resolved')")
        if "quarantine" in columns:
            clauses.append("COALESCE(quarantine,0)=0")
        if bot_id and "bot_id" in columns:
            clauses.append("COALESCE(bot_id,'')=?")
            params.append(str(bot_id))
        metadata_columns = [
            name for name in ("bot_id", "session_id", "visibility") if name in columns
        ]
        select_columns = ["id", "content", *metadata_columns]
        rows = self._db.conn.execute(
            f"""SELECT {','.join(select_columns)} FROM memories
               WHERE {' AND '.join(clauses)}
               ORDER BY RANDOM() LIMIT 50""",
            params,
        ).fetchall()

        if not rows:
            return []

        # LLM 评估风格代表性
        candidates = []
        seen_content: set[str] = set()
        for row in rows:
            values = dict(zip(select_columns, row))
            content = str(values.get("content") or "").strip()
            if content in seen_content:
                continue
            seen_content.add(content)
            try:
                if not self._is_healthy_example(content):
                    continue
                result = await self._evaluate_style(content)
                score = result.get("score", 0)
                if score >= self._min_score:
                    candidate = {
                        "memory_id": int(values["id"]),
                        "content": content,
                        "score": score,
                        "traits": result.get("traits", []),
                    }
                    for name in metadata_columns:
                        candidate[name] = str(values.get(name) or "")
                    candidates.append(candidate)
            except Exception:
                continue

            if len(candidates) >= 10:
                break

        # 候选仅返回给学习/审核链；正式表唯一写入口是 add_approved_example。
        logger.info(f"[FewShot] 提取了 {len(candidates)} 条待审核风格候选")
        return candidates

    async def learn_from_history(self, bot_id: str = "") -> dict[str, int]:
        """提取并自动转正：只写有正式证据锚点与本群有效标签的 Bot 回复。"""
        stats: dict[str, int] = {"candidates": 0, "approved": 0, "skipped": 0}
        candidates = await self.extract_candidates(bot_id)
        stats["candidates"] = len(candidates)
        if not candidates or not self._auto_approve:
            return stats
        for candidate in candidates:
            try:
                outcome = await asyncio.to_thread(self._approve_candidate, candidate)
            except Exception as exc:
                outcome = "skipped_error"
                logger.debug(f"[FewShot] 自动转正失败: {type(exc).__name__}")
            if outcome == "approved":
                stats["approved"] += 1
            else:
                stats["skipped"] += 1
                stats[outcome] = stats.get(outcome, 0) + 1
        logger.info(
            f"[FewShot] 风格学习 bot={bot_id or '*'}: 候选 {stats['candidates']}，"
            f"转正 {stats['approved']}，跳过 {stats['skipped']}"
        )
        return stats

    def _approve_candidate(self, candidate: Mapping[str, Any]) -> str:
        memory_id = int(candidate.get("memory_id") or 0)
        scope = _scope_for_candidate(candidate)
        if memory_id <= 0 or scope is None:
            return "skipped_scope"
        try:
            resolved = self._bot_replies.resolve(scope=scope, memory_id=memory_id)
        except Exception:
            return "skipped_evidence"
        content = str(resolved.get("content") or "").strip()
        if not content or content != str(candidate.get("content") or "").strip():
            return "skipped_evidence"
        if not self._is_healthy_example(content):
            return "skipped_unsafe"
        if self._has_approved_content(scope, content):
            return "skipped_duplicate"
        artifact = RawArtifactRef(
            kind="raw_message",
            id=f"memory:{memory_id}",
            content_hash=_content_hash(content),
            captured_at=float(resolved.get("captured_at") or 0.0),
            source_scope=scope,
            available=True,
        )
        binding = EvidenceBinding(
            evidence_id=artifact.id,
            target_scope=scope,
            derivation_chain=("raw_chat", "reviewed_fewshot"),
            policy_version="fewshot-learning/v1",
        )
        traits = tuple(str(item) for item in (candidate.get("traits") or ()))
        self.add_approved_example(
            scope=scope,
            candidate={
                "memory_id": memory_id,
                "score": candidate.get("score", 0.0),
                "traits": list(traits),
                "source": "auto_history",
            },
            evidence_refs=(artifact.as_evidence_ref(),),
            evidence_bindings=(binding,),
            source_tags=tuple(resolved.get("source_tags") or ()),
            query_trace_id=f"auto-fewshot:{scope.bot_id}:{memory_id}",
            content=content,
            score=float(candidate.get("score") or 0.0),
            traits=traits,
            source_candidate_id=memory_id,
            idempotency_key=f"fewshot:auto:{memory_id}:v1",
        )
        return "approved"

    def _has_approved_content(self, scope: RuntimeScope, content: str) -> bool:
        try:
            rows = self._repository.list_approved(scope=scope, limit=500)
        except Exception:
            return False
        return any(str(row.get("content") or "").strip() == content for row in rows)

    def add_approved_example(
        self,
        *,
        scope: RuntimeScope,
        candidate: Mapping[str, Any],
        evidence_refs: Sequence[EvidenceRef],
        evidence_bindings: Sequence[EvidenceBinding],
        source_tags: Sequence[Mapping[str, Any]],
        query_trace_id: str,
        content: str,
        score: float = 0.0,
        traits: Sequence[str] | None = None,
        source_candidate_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """通过可注入正式仓储写入 scoped approved 样例。

        legacy ``few_shot_examples`` 只保留审计价值，不再作为正式写入或注入来源。
        """
        content = str(content or "").strip()
        if not isinstance(scope, RuntimeScope):
            raise TypeError("scope must be RuntimeScope")
        if not content:
            raise ValueError("content is required")
        if not self._is_healthy_example(content):
            raise ValueError("few-shot example failed safety validation")
        candidate_id = source_candidate_id
        if candidate_id is None and isinstance(candidate, Mapping):
            raw_id = candidate.get("id")
            candidate_id = int(raw_id) if raw_id is not None else None
        key = str(idempotency_key or "").strip()
        if not key:
            if candidate_id is None:
                raise ValueError("source_candidate_id or idempotency_key is required")
            key = f"candidate:{candidate_id}"
        writer = getattr(self._writer, "write_approved", None)
        if not callable(writer):
            raise RuntimeError("formal scoped FewShot writer is unavailable")
        return int(writer(
            scope=scope,
            candidate=candidate,
            evidence_refs=tuple(evidence_refs),
            evidence_bindings=tuple(evidence_bindings),
            source_tags=tuple(source_tags),
            query_trace_id=str(query_trace_id or "").strip(),
            content=content,
            score=float(score),
            traits=tuple(traits or ()),
            source_candidate_id=candidate_id,
            idempotency_key=key,
        ))

    def refresh(self, **kwargs: Any) -> None:
        """清理注入游标，确保新晋升样例下一次可参与缓存选择。"""
        self._last_injected_ids = []
        self._last_injected_by_scope.clear()

    # ─── US-5.2: 注入 few-shot ───

    def get_injection(self, scope: RuntimeScope, max_items: int = None, *, message: str = "") -> str:
        """保留原工具/调用接口；群友表达需要当前情景且已经独立审核。"""
        selected = self.get_injection_examples(scope, max_items=max_items, message=message)
        if not selected:
            return ""
        examples = "\n".join(f"- {escape(item['content'])}" for item in selected)
        return f"<style_examples>\n{examples}\n</style_examples>"

    def get_injection_examples(self, scope: RuntimeScope, max_items: int = None, *, message: str = "") -> list[dict]:
        """Return text and provenance together, avoiding cross-thread audit cursors."""
        if not self._enabled or not isinstance(scope, RuntimeScope) or scope.visibility != "group":
            self._last_injected_ids = []
            return []
        if max_items is None:
            max_items = self._max_inject
        max_items = max(0, min(3, int(max_items)))
        if not max_items:
            self._last_injected_ids = []
            return []

        rows = self._repository.list_approved(scope=scope, limit=20)
        healthy_rows = [
            (int(row["id"]), str(row["content"]))
            for row in rows
            if self._is_healthy_example(row.get("content", ""))
        ]
        learned = []
        if self.expression_policy is not None:
            try:
                learned = self.expression_policy.recall(
                    scope, message=message, max_items=min(2, max_items - int(bool(healthy_rows))),
                )
            except Exception as exc:
                logger.debug(f"[FewShot] 表达读取未就绪: {type(exc).__name__}")
        legacy_limit = max_items - len(learned)
        key = (scope.bot_id, scope.session.id, scope.visibility)
        previous = self._last_injected_by_scope.get(key, [])

        # 不连续重复同一条
        available = [(r[0], r[1]) for r in healthy_rows if r[0] not in previous]
        if len(available) < legacy_limit:
            available = healthy_rows  # 库太小时允许重复

        selected = random.sample(available, min(legacy_limit, len(available)))
        self._last_injected_ids = [s[0] for s in selected]
        changed = self._last_injected_by_scope.get(key) != self._last_injected_ids
        if len(self._last_injected_by_scope) >= 128 and key not in self._last_injected_by_scope:
            self._last_injected_by_scope.pop(next(iter(self._last_injected_by_scope)))
        self._last_injected_by_scope[key] = self._last_injected_ids
        if changed:
            self._save_rotation_state()
        return [
            {"example_id": ident, "content": content, "source": "FewShotService.get_injection"}
            for ident, content in selected
        ] + learned

    def _load_rotation_state(self) -> None:
        path = self._rotation_state_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            entries = raw.get("last_injected") if isinstance(raw, Mapping) else None
            for item in entries or []:
                if not isinstance(item, Mapping):
                    continue
                key = (str(item.get("bot_id") or ""), str(item.get("session_id") or ""), str(item.get("visibility") or ""))
                ids = [int(value) for value in item.get("ids") or [] if isinstance(value, int) and not isinstance(value, bool)]
                if all(key) and len(self._last_injected_by_scope) < 128:
                    self._last_injected_by_scope[key] = ids
        except Exception as exc:
            logger.debug(f"[FewShot] 防复读记录读取失败，按空记录开始: {exc!r}")
            self._last_injected_by_scope.clear()

    def _save_rotation_state(self) -> None:
        path = self._rotation_state_path
        if not path:
            return
        with self._rotation_lock:
            payload = {"last_injected": [
                {"bot_id": key[0], "session_id": key[1], "visibility": key[2], "ids": list(ids)}
                for key, ids in list(self._last_injected_by_scope.items())
            ]}
            partial = f"{path}.partial"
            try:
                with open(partial, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(partial, path)
            except Exception as exc:
                logger.debug(f"[FewShot] 防复读记录保存失败: {exc!r}")

    # ─── US-5.4: 风格漂移检测 ───

    async def check_drift(self, recent_reply: str, scope: RuntimeScope | None = None) -> Optional[Dict]:
        """检查当前完整 RuntimeScope 下的风格漂移。"""
        if not self._enabled or not self._llm or not isinstance(scope, RuntimeScope):
            return None

        rows = self._repository.list_approved(scope=scope, limit=5)
        if len(rows) < 3:
            return None  # 范例太少无法判断

        examples_text = "\n".join(f"- {row['content']}" for row in rows)
        prompt = _DRIFT_CHECK_PROMPT.format(examples=examples_text, recent=recent_reply)

        try:
            result = await self._call_llm(prompt)
            similarity = result.get("similarity", 1.0)
            if similarity < self._drift_threshold:
                logger.warning(f"[FewShot] 风格漂移检测: similarity={similarity:.2f}")
                return {"similarity": similarity, "drift_detected": True, "reason": result.get("reason", "")}
        except Exception:
            pass
        return None

    async def evaluate_reply(self, reply: str) -> Dict:
        """正式候选入口使用的公开质量评估；缺 LLM 或不安全回复一律失败。"""
        text = str(reply or "").strip()
        if not self._enabled or self._llm is None:
            raise RuntimeError("FewShot quality assessor is unavailable")
        if not self._is_healthy_example(text):
            raise ValueError("few-shot reply failed safety validation")
        result = await self._evaluate_style(text)
        if not isinstance(result, Mapping):
            raise ValueError("FewShot quality assessment is invalid")
        return dict(result)

    # ─── 内部方法 ───

    def _is_healthy_example(self, content: str) -> bool:
        """Return True only for style examples that are safe to re-inject."""
        text = str(content or "").strip()
        if not text:
            return False
        if is_identity_contamination(text):
            return False
        if _AGGRESSIVE_STYLE_RE.search(text):
            return False
        return True

    async def _evaluate_style(self, reply: str) -> Dict:
        prompt = _EVALUATE_STYLE_PROMPT.format(reply=reply[:500])
        return await self._call_llm(prompt)

    async def _call_llm(self, prompt: str) -> Dict:
        import re
        response = await self._llm.text_chat(prompt=prompt)
        if not response or not response.completion_text:
            return {}
        text = response.completion_text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
        return {}
