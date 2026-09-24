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
import json
import random
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 领域单测不依赖 AstrBot runtime
    import logging

    logger = logging.getLogger(__name__)

try:
    from ...domain.evidence import EvidenceBinding, EvidenceRef
    from ...domain.scope import RuntimeScope
    from ...engine.db.scoped_learning_projection_repo import ScopedFewShotRepository
except ImportError:  # pragma: no cover - 独立测试兼容
    from domain.evidence import EvidenceBinding, EvidenceRef
    from domain.scope import RuntimeScope
    from engine.db.scoped_learning_projection_repo import ScopedFewShotRepository

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
        self._last_extract: float = 0  # 上次提取时间
        self._last_injected_ids: List[int] = []  # 避免连续注入同一条

        # 从配置读取参数
        self._min_score = float(self._config.get("min_score", 0.7))
        self._max_inject = int(self._config.get("max_inject", 3))
        self._drift_threshold = float(self._config.get("drift_threshold", 0.5))

    # ─── US-5.1: 提取风格范例 ───

    async def extract_candidates(self, bot_id: str = "") -> List[Dict]:
        """从最近 7 天 bot 回复中提取 top 10 候选。每天最多执行一次。"""
        if not self._enabled or not self._llm:
            return []

        now = time.time()
        if now - self._last_extract < 86400:  # 24h 冷却
            return []
        self._last_extract = now

        # 获取最近 7 天的 bot 回复
        seven_days_ago = int(now) - 7 * 86400
        sources = ["bot_reply", *sorted(bot_identity.experience_sources())]
        placeholders = ",".join("?" for _ in sources)
        rows = self._db.conn.execute(
            f"""SELECT id, content FROM memories
               WHERE source IN ({placeholders})
               AND timestamp > ? AND LENGTH(content) >= 20
               ORDER BY RANDOM() LIMIT 50""",
            (*sources, seven_days_ago),
        ).fetchall()

        if not rows:
            return []

        # LLM 评估风格代表性
        candidates = []
        for mem_id, content in rows:
            try:
                if not self._is_healthy_example(content):
                    continue
                result = await self._evaluate_style(content)
                score = result.get("score", 0)
                if score >= self._min_score:
                    candidates.append({
                        "content": content,
                        "score": score,
                        "traits": result.get("traits", []),
                    })
            except Exception:
                continue

            if len(candidates) >= 10:
                break

        # 候选仅返回给学习/审核链；正式表唯一写入口是 add_approved_example。
        logger.info(f"[FewShot] 提取了 {len(candidates)} 条待审核风格候选")
        return candidates

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

    # ─── US-5.2: 注入 few-shot ───

    def get_injection(self, scope: RuntimeScope, max_items: int = None) -> str:
        """获取当前完整 RuntimeScope 下 2-3 条 approved few-shot。"""
        if not self._enabled or not isinstance(scope, RuntimeScope):
            self._last_injected_ids = []
            return ""
        if max_items is None:
            max_items = self._max_inject

        rows = self._repository.list_approved(scope=scope, limit=20)
        if not rows:
            self._last_injected_ids = []
            return ""

        healthy_rows = [
            (int(row["id"]), str(row["content"]))
            for row in rows
            if self._is_healthy_example(row.get("content", ""))
        ]
        if not healthy_rows:
            self._last_injected_ids = []
            return ""

        # 不连续重复同一条
        available = [(r[0], r[1]) for r in healthy_rows if r[0] not in self._last_injected_ids]
        if len(available) < max_items:
            available = healthy_rows  # 库太小时允许重复

        selected = random.sample(available, min(max_items, len(available)))
        self._last_injected_ids = [s[0] for s in selected]

        examples = "\n".join(f"- {s[1]}" for s in selected)
        return f"<style_examples>\n{examples}\n</style_examples>"

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
