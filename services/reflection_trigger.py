"""现场反思候选上下文：只读聚合证据，不执行任何资产提升。

可观测性契约（reflection-trigger/v1）：
- 每条跳过路径给出结构化 ``skip_reason``；
- 每个证据源独立采集，依赖故障记录进 ``dependency_failures`` 并告警，
  不再静默吞异常；
- 任一依赖失败只降级该源，绝不影响普通回复，也不阻塞主链路。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .experience_episodes import fetch_recent_episodes
from .identity_safety import is_identity_contamination

try:
    from .belief_engine import APPROVED_FACT_STATUSES
except ImportError:  # pragma: no cover - 插件根目录直接导入
    from services.belief_engine import APPROVED_FACT_STATUSES

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - repository tests run without AstrBot
    import logging

    logger = logging.getLogger(__name__)

STRATEGY_VERSION = "reflection-trigger/v1"

SKIP_NOT_GROUP_SCOPE = "not_group_scope"
SKIP_TOO_SHORT = "too_short"
SKIP_POLLUTED = "polluted_content"
SKIP_COOLDOWN = "cooldown"
SKIP_NO_CANDIDATES = "no_candidates"
SKIP_BUDGET_EXHAUSTED = "budget_exhausted"
SKIP_DEPENDENCY_ERROR = "dependency_error"

SKIP_REASONS = frozenset({
    SKIP_NOT_GROUP_SCOPE,
    SKIP_TOO_SHORT,
    SKIP_POLLUTED,
    SKIP_COOLDOWN,
    SKIP_NO_CANDIDATES,
    SKIP_BUDGET_EXHAUSTED,
    SKIP_DEPENDENCY_ERROR,
})

_MIN_MESSAGE_CHARS = 4
_DEFAULT_CANDIDATE_LIMIT = 25


@dataclass
class ReflectionOutcome:
    """一次现场反思采集的可观测结果。"""

    strategy_version: str = STRATEGY_VERSION
    triggered: bool = False
    skip_reason: str = ""
    prompt: str = ""
    candidate_counts: dict[str, int] = field(default_factory=dict)
    filtered_counts: dict[str, int] = field(default_factory=dict)
    dependency_failures: list[dict[str, str]] = field(default_factory=list)
    budget_truncated: bool = False
    duration_ms: float = 0.0

    def record_failure(self, source: str, error: BaseException | str) -> None:
        detail = error if isinstance(error, str) else f"{type(error).__name__}: {str(error)[:200]}"
        self.dependency_failures.append({"source": source, "error": detail})
        logger.warning(f"[WaveMemory] reflection_trigger source degraded: {source} -> {detail}")

    def to_log_fields(self) -> dict[str, Any]:
        return {
            "strategy_version": self.strategy_version,
            "triggered": self.triggered,
            "skip_reason": self.skip_reason,
            "candidate_counts": dict(self.candidate_counts),
            "filtered_counts": dict(self.filtered_counts),
            "dependency_failures": [item["source"] for item in self.dependency_failures],
            "budget_truncated": self.budget_truncated,
            "duration_ms": self.duration_ms,
        }


class ReflectionTriggerService:
    """为当前群聊请求生成有证据的认知资产提审提示。"""

    def __init__(self, db: Any, *, cooldown_seconds: float = 180.0, max_candidates: int = 6, max_chars: int = 1200):
        self.db = db
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_candidates = max(1, int(max_candidates))
        self.max_chars = max(240, int(max_chars))
        self._last: dict[str, float] = {}

    def build_prompt(self, *, scope: RuntimeScope, message: str, sender_id: str = "", trace_id: str = "") -> str:
        """兼容既有调用方：只返回提示文本。"""
        return self.collect(scope=scope, message=message, sender_id=sender_id, trace_id=trace_id).prompt

    def collect(
        self,
        *,
        scope: RuntimeScope,
        message: str,
        sender_id: str = "",
        trace_id: str = "",
    ) -> ReflectionOutcome:
        started = time.perf_counter()
        outcome = ReflectionOutcome()
        try:
            self._evaluate(scope=scope, message=message, sender_id=sender_id, outcome=outcome)
        except Exception as exc:  # 兜底：现场链路任何情况下都不得抛出到主回复
            outcome.record_failure("strategy", exc)
            outcome.triggered = False
            outcome.prompt = ""
            outcome.skip_reason = outcome.skip_reason or SKIP_DEPENDENCY_ERROR
        finally:
            outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
            if outcome.dependency_failures and not outcome.triggered and not outcome.skip_reason:
                outcome.skip_reason = SKIP_DEPENDENCY_ERROR
        return outcome

    # ---- internals -------------------------------------------------------

    def _evaluate(self, *, scope: Any, message: str, sender_id: str, outcome: ReflectionOutcome) -> None:
        if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
            outcome.skip_reason = SKIP_NOT_GROUP_SCOPE
            return
        text = str(message or "").strip()
        if len(text) < _MIN_MESSAGE_CHARS:
            outcome.skip_reason = SKIP_TOO_SHORT
            return
        if is_identity_contamination(text):
            outcome.filtered_counts["message"] = 1
            outcome.skip_reason = SKIP_POLLUTED
            return

        key = f"{scope.bot_id}:{scope.session.id}:{sender_id}:{text[:80].casefold()}"
        now = time.time()
        if now - self._last.get(key, 0.0) < self.cooldown_seconds:
            outcome.skip_reason = SKIP_COOLDOWN
            return

        candidates = self._collect(scope, sender_id=sender_id, message=text, now=now, outcome=outcome)
        if not candidates:
            if outcome.dependency_failures and not outcome.candidate_counts:
                outcome.skip_reason = SKIP_DEPENDENCY_ERROR
            else:
                outcome.skip_reason = SKIP_NO_CANDIDATES
            return

        header = (
            "【现场认知反思候选】以下只是当前 Scope 的证据候选，不是事实或正式认知。"
            "请结合本轮真实对话自行判断；各工具彼此独立，可调用零个或多个，不能凭空补全。"
        )
        # 提示里必须给出确切工具名：模型面对二十多个 wave_memory_* 工具时，
        # 只写“各自工具”无法诱导它调用正确的那一个。
        footer = (
            "可选动作（按需调用，可零个或多个）："
            "提审事实 wave_memory_propose_fact（必须带 source_quote 支撑原话）；"
            "提审信念 wave_memory_propose_belief（需绑定至少两条已批准事实）；"
            "群文化/梗 wave_memory_mark_cultural_moment（context_note 须完整说明出处原话与语境）；"
            "社交锚点 wave_memory_note_social_anchor；"
            "群友观感 wave_memory_record_social_impression（必须带群友真实原话 source_quote，无原话不记）；"
            "好感 wave_memory_affinity_update（必须带群友触发好感变动的真实原话 source_quote）；"
            "关切 wave_memory_note_concern；"
            "群经历 wave_memory_note_episode（trigger_text 必须描述具体事件与触发原话；注意：episode 不等于 social anchor，群经历属于公共事件而非个人私信交往）；"
            "群聊亲笔日记 wave_memory_record_diary_episode（当聊到深度话题、关键事件或产生强烈自省时，亲笔记录今日生活日记与心智历程，入选 Bot 经历时间线主干）。"
            "所有提审与变动均要求实事求是、铁证如山，后台会自动溯源并绑定真实对话证据。"
        )
        lines = [header]
        used = len(header)
        emitted = 0
        for source, item in candidates[: self.max_candidates]:
            line = f"- {item}"
            if used + len(line) + 1 > self.max_chars:
                outcome.budget_truncated = True
                break
            lines.append(line)
            used += len(line) + 1
            emitted += 1
        if emitted == 0:
            outcome.budget_truncated = True
            outcome.skip_reason = SKIP_BUDGET_EXHAUSTED
            return
        if used + len(footer) + 1 <= self.max_chars:
            lines.append(footer)
        else:
            outcome.budget_truncated = True
        self._last[key] = now
        outcome.prompt = "\n".join(lines)
        outcome.triggered = True

    def _collect(
        self,
        scope: RuntimeScope,
        *,
        sender_id: str,
        message: str,
        now: float,
        outcome: ReflectionOutcome,
    ) -> list[tuple[str, str]]:
        """返回 (source, candidate_text) 列表；单源失败只记录不抛出。"""
        candidates: list[tuple[str, str]] = []
        conn = getattr(self.db, "conn", None)
        if conn is None:
            outcome.record_failure("connection", "database connection unavailable")
            return candidates
        group_id = scope.session.conversation_id
        knowledge = getattr(self.db, "scoped_knowledge", None)
        soul = getattr(self.db, "soul_repository", None)
        fewshot = getattr(self.db, "few_shot_repository", None) or getattr(self.db, "scoped_few_shot", None)

        def accept(source: str, text: str) -> bool:
            body = str(text or "").strip()
            if not body:
                return False
            if is_identity_contamination(body):
                outcome.filtered_counts[source] = outcome.filtered_counts.get(source, 0) + 1
                return False
            candidates.append((source, body))
            outcome.candidate_counts[source] = outcome.candidate_counts.get(source, 0) + 1
            return True

        try:
            # person_unsettled_state 没有 text 列；观感文本存在 traces JSON 数组里。
            # 这里必须读 traces 并解析，否则该源每次查询都抛 OperationalError，
            # 而候选为空 + 依赖失败会让整条反思链路被判为 dependency_error 丢弃。
            rows = conn.execute(
                "SELECT traces FROM person_unsettled_state WHERE bot_id=? AND group_id=? AND user_id=?"
                " ORDER BY updated_at DESC LIMIT 3",
                (scope.bot_id, group_id, str(sender_id or "")),
            ).fetchall()
            texts: list[str] = []
            for row in rows:
                if not row or not row[0]:
                    continue
                try:
                    traces = json.loads(row[0])
                except (TypeError, ValueError):
                    # 单行坏数据只跳过该行，不能让整个源降级。
                    continue
                if not isinstance(traces, list):
                    continue
                for item in traces:
                    if isinstance(item, dict):
                        body = str(item.get("text") or item.get("summary") or "").strip()
                    else:
                        body = str(item or "").strip()
                    if body:
                        texts.append(body[:80])
                if len(texts) >= 3:
                    break
            texts = texts[:3]
            if texts:
                accept("unsettled", "未结算观感，可调用 social_impression：" + "；".join(texts))
        except Exception as exc:
            outcome.record_failure("unsettled", exc)

        try:
            if soul is not None:
                items = (soul.get_state(scope, limit=_DEFAULT_CANDIDATE_LIMIT, offset=0).get("concerns") or {}).get("items") or []
                for item in items:
                    if str(item.get("status") or "active") not in {"active", "progressing", "dormant"}:
                        continue
                    topic = str(item.get("topic") or "").strip()
                    if topic:
                        accept("concern", f"关切 concern:{item.get('id')} [{item.get('status') or 'active'}]: {topic[:80]}")
        except Exception as exc:
            outcome.record_failure("concern", exc)

        try:
            episodes = fetch_recent_episodes(
                conn,
                bot_id=scope.bot_id,
                group_id=group_id,
                user_id=str(sender_id or "") or None,
                limit=2,
                since=now - 14 * 86400,
            )
            for item in episodes:
                summary = "；".join(
                    str(item.get(key) or "").strip()
                    for key in ("trigger_text", "outcome")
                    if str(item.get(key) or "").strip()
                )
                if summary:
                    accept("episode", f"群经历 episode:{item.get('id')} [{item.get('episode_type')}]: {summary[:120]}")
        except Exception as exc:
            outcome.record_failure("episode", exc)

        lowered = message.casefold()
        tokens = [token for token in lowered.replace("？", " ").replace("?", " ").split() if len(token) >= 2]
        if not tokens and len(lowered) >= 2:
            tokens = [lowered]

        if knowledge is None:
            outcome.record_failure("knowledge", "scoped knowledge repository unavailable")
        else:
            try:
                for fact in knowledge.list_scoped_facts(scope, limit=8):
                    if fact.get("status") not in {"active", "approved", "pending"}:
                        continue
                    blob = f"{fact.get('subject')} {fact.get('predicate')} {fact.get('object')}"
                    haystack = blob.casefold()
                    matched = fact.get("status") == "pending" or any(token in haystack or token in lowered for token in tokens)
                    if matched:
                        accept("fact", f"事实 fact:{fact.get('id')} [{fact.get('status')}]: {blob[:100]}")
            except Exception as exc:
                outcome.record_failure("fact", exc)

            try:
                # 信念提审的 source_fact_ids 只接受 scoped_facts 里 active/approved 的 id
                # （见 belief_engine.approved_source_fact_ids）。这里过去误查 scoped_beliefs，
                # 导致下发的是 belief id，模型拿去提审必然被拒。
                approved = [
                    item
                    for item in knowledge.list_scoped_facts(scope, limit=8)
                    if str(item.get("status") or "") in APPROVED_FACT_STATUSES
                ]
                if len(approved) >= 2:
                    ids = ",".join(str(item.get("id")) for item in approved[:3])
                    accept("belief_base", f"已批准事实底座可用于信念提审 source_fact_ids=[{ids}]")
            except Exception as exc:
                outcome.record_failure("belief_base", exc)

            try:
                for belief in knowledge.list_scoped_beliefs(scope, status="pending", limit=2):
                    content = str(belief.get("content") or "").strip()
                    if content:
                        accept("belief_pending", f"待审信念 belief:{belief.get('id')}: {content[:100]}")
            except Exception as exc:
                outcome.record_failure("belief_pending", exc)

            try:
                for jargon in knowledge.list_scoped_jargon(scope, status="pending", limit=2):
                    word = str(jargon.get("word") or "").strip()
                    if word and word in message:
                        accept("jargon", f"待审黑话 jargon:{jargon.get('id')}「{word}」可调用 mark_cultural_moment")
            except Exception as exc:
                outcome.record_failure("jargon", exc)

        try:
            if fewshot is not None:
                rows = fewshot.list_approved(scope=scope, limit=1)
                if rows:
                    accept(
                        "fewshot",
                        "已有风格样例；仅当本轮回复特别能代表风骨时才调用 mark_cultural_moment/exemplar_reply",
                    )
        except Exception as exc:
            outcome.record_failure("fewshot", exc)

        return candidates


__all__ = [
    "ReflectionOutcome",
    "ReflectionTriggerService",
    "SKIP_BUDGET_EXHAUSTED",
    "SKIP_COOLDOWN",
    "SKIP_DEPENDENCY_ERROR",
    "SKIP_NO_CANDIDATES",
    "SKIP_NOT_GROUP_SCOPE",
    "SKIP_POLLUTED",
    "SKIP_REASONS",
    "SKIP_TOO_SHORT",
    "STRATEGY_VERSION",
]
