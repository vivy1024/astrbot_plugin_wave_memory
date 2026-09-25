"""SelfReflect — 按 Bot 隔离的纠正学习候选。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict, deque
from typing import Optional

try:  # 兼容插件包导入和仓库测试直接导入
    from ..domain.bot_profile import bot_catalog_scope
    from ..domain.evidence import EvidenceBinding, EvidenceRef
    from ..domain.scope import CatalogScope, RuntimeScope, validate_formal_command_scope
    from ..engine.database import WaveMemoryDB
    from ..engine.vector_index import VectorIndex
    from ..engine.book_lore_index import BookLoreIndex
except ImportError:  # pragma: no cover - 由仓库测试/旧调用路径使用
    from domain.bot_profile import bot_catalog_scope
    from domain.evidence import EvidenceBinding, EvidenceRef
    from domain.scope import CatalogScope, RuntimeScope, validate_formal_command_scope
    from engine.database import WaveMemoryDB
    from engine.vector_index import VectorIndex
    from engine.book_lore_index import BookLoreIndex

from astrbot.api import logger
from .llm_fallback import LLMFallbackClient


CORRECTION_PATTERNS = [
    re.compile(r'(你说错|不是这样|你搞错|你弄错|不对吧|说反了|记错了)', re.I),
    re.compile(r'(原文[是写说]|书[里中].*[是写说]|作者[说写])', re.I),
    re.compile(r'(你不懂|你没看|你没读|没看过原文)', re.I),
    re.compile(r'(其实是|实际上是|应该是|正确的是)', re.I),
]
CORRECTION_LEARNING_CANDIDATE = "correction_learning"

CORRECT_PROMPT = """你是{bot_name}。你刚才说了一些不太准确的话，有人纠正了你。

你说的：{bot_reply}
别人纠正：{correction}
相关知识：{knowledge}

现在用你自己的话，一两句话，把正确的理解记在心里。
要求：
- 第一人称
- 像自言自语一样，不是在回应别人
- 不要道歉或承认错误，直接表达正确的认知
- 不要超过80字
- 直接输出内容"""


class SelfReflectService:
    """检测纠正信号并创建待审核候选；不直接写入线上记忆或 facts。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        embedding_service,
        llm_client: LLMFallbackClient,
        book_lore_index: Optional[BookLoreIndex],
        lore_db_path: str,
        bot_name: str = "bot",
        bot_qq_id: str = "",
        bot_aliases: list[str] = None,
        cooldown_seconds: float = 300.0,
        *,
        bot_id: str | None = None,
        repositories=None,
        candidate_service: LearningCandidateService | None = None,
        catalog_scope: CatalogScope | None = None,
        profile_lookup=None,
    ):
        self.db = db
        self.memory_index = memory_index
        self.embedding = embedding_service
        self.llm = llm_client
        self.book_lore_index = book_lore_index
        self.lore_db_path = lore_db_path
        self.catalog_scope = catalog_scope
        self.profile_lookup = profile_lookup
        self.bot_name = bot_name
        self.bot_aliases = list(bot_aliases or [])
        self.bot_qq_id = bot_qq_id
        self.bot_id = self._normalize_bot_id(bot_id, allow_empty=True)
        self.cooldown = cooldown_seconds

        self._correction_patterns = list(CORRECTION_PATTERNS)
        all_names = [bot_name] + (bot_aliases or [])
        name_pattern = "|".join(re.escape(n) for n in all_names if n)
        if name_pattern:
            self._correction_patterns.append(re.compile(rf'({name_pattern}).*不[是会对]', re.I))

        # 每个稳定 BotProfile.db_id 都有独立 deque，绝不以 QQ/name 作为隔离键。
        self._recent_replies: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self._cooldown_map: dict[tuple[str, str, str], float] = {}
        self._reflect_count = 0
        self.candidate_service = candidate_service

    @staticmethod
    def _normalize_bot_id(value: str | None, *, allow_empty: bool = False) -> str:
        normalized = str(value or "").strip()
        if not normalized and allow_empty:
            return ""
        if not normalized:
            raise ValueError("bot_id (BotProfile.db_id) is required")
        if normalized.isdecimal():
            raise ValueError("bot_id must be BotProfile.db_id, not a QQ number")
        return normalized

    def _resolve_bot_id(self, bot_id: str | None) -> str:
        # 调用方显式传入优先；否则只能使用构造时绑定的稳定 db_id。
        return self._normalize_bot_id(bot_id or self.bot_id)

    @staticmethod
    def _group_runtime_scope(scope: RuntimeScope | None, *, bot_id: str) -> RuntimeScope | None:
        """仅接受已解析的同 Bot 群 Scope，绝不从旧 group_id 反推。"""
        if not isinstance(scope, RuntimeScope):
            return None
        if scope.bot_id != bot_id or scope.visibility != "group" or scope.session is None:
            return None
        return scope

    def record_reply(
        self,
        reply_text: str,
        group_id: str,
        bot_id: str | None = None,
        *,
        scope: RuntimeScope | None = None,
        message_id: str | None = None,
    ):
        """记录已解析 Scope 下指定 Bot 的回复，供后续纠正候选匹配。"""
        try:
            stable_bot_id = self._resolve_bot_id(bot_id)
        except ValueError:
            return
        runtime_scope = self._group_runtime_scope(scope, bot_id=stable_bot_id)
        if runtime_scope is None:
            return
        group_id = str(group_id or "").strip()
        if group_id != runtime_scope.session.conversation_id or not str(reply_text or "").strip():
            return
        self._recent_replies[stable_bot_id].append({
            "text": str(reply_text),
            "session_id": runtime_scope.session.id,
            "message_id": str(message_id or "").strip(),
            "timestamp": time.time(),
        })

    async def check_correction(
        self,
        message: str,
        sender_name: str,
        group_id: str,
        bot_id: str | None = None,
        *,
        scope: RuntimeScope | None = None,
        message_id: str | None = None,
    ) -> bool:
        """检查已解析群 Scope 中的纠正并创建待审核候选。"""
        try:
            stable_bot_id = self._resolve_bot_id(bot_id)
        except ValueError:
            return False
        runtime_scope = self._group_runtime_scope(scope, bot_id=stable_bot_id)
        if runtime_scope is None:
            return False
        group_id = str(group_id or "").strip()
        message_id = str(message_id or "").strip()
        if group_id != runtime_scope.session.conversation_id or not message_id:
            return False

        now = time.time()
        recent_reply = None
        for reply in reversed(self._recent_replies.get(stable_bot_id, ())):
            if reply["session_id"] == runtime_scope.session.id and (now - reply["timestamp"]) < 60:
                recent_reply = reply
                break
        if not recent_reply or not any(p.search(message or "") for p in self._correction_patterns):
            return False

        topic_key = recent_reply["text"][:30]
        cooldown_key = (stable_bot_id, runtime_scope.session.id, topic_key)
        if cooldown_key in self._cooldown_map and (now - self._cooldown_map[cooldown_key]) < self.cooldown:
            return False
        self._cooldown_map[cooldown_key] = now

        try:
            success = await self._learn_from_correction(
                bot_reply=recent_reply["text"],
                correction=message,
                bot_id=stable_bot_id,
                group_id=group_id,
                scope=runtime_scope,
                message_id=message_id,
                sender_name=sender_name,
                bot_reply_message_id=recent_reply.get("message_id", ""),
            )
            if success:
                self._reflect_count += 1
                logger.info(f"[SelfReflect] Correction candidate #{self._reflect_count}: {message[:50]}...")
            return success
        except Exception as exc:
            logger.warning(f"[SelfReflect] Correction candidate failed: {exc}")
            return False

    async def _search_book_lore(self, bot_id: str, bot_reply: str, correction: str) -> tuple[str, list[dict]]:
        """只在显式 CatalogScope 下检索书设参考。"""
        hits_evidence: list[dict] = []
        profile = None
        if self.profile_lookup is not None:
            try:
                profile = self.profile_lookup(bot_id)
            except Exception:
                profile = None
        catalog_scope, _reason = bot_catalog_scope(profile, self.catalog_scope)
        if not isinstance(catalog_scope, CatalogScope):
            return "（无额外参考）", hits_evidence
        catalog_decision = validate_formal_command_scope("catalog.read", catalog_scope)
        if not catalog_decision.allowed:
            return "（无额外参考）", hits_evidence
        if not self.book_lore_index or not self.lore_db_path:
            return "（无额外参考）", hits_evidence
        try:
            vec = await self.embedding.get_embedding(f"{bot_reply} {correction}")
            if vec is None:
                return "（无额外参考）", hits_evidence
            hits = self.book_lore_index.search_communities(vec, k=2)
            with sqlite3.connect(self.lore_db_path) as conn:
                for cid, score in hits:
                    if score < 0.3:
                        continue
                    row = conn.execute(
                        "SELECT title, summary FROM book_communities WHERE id = ?", (cid,)
                    ).fetchone()
                    if row:
                        hits_evidence.append({
                            "community_id": str(cid),
                            "score": float(score),
                            "title": row[0],
                            "summary": row[1][:200],
                            "bot_id": bot_id,
                            "catalog_scope": catalog_scope.to_dict(),
                        })
        except Exception as exc:
            logger.debug(f"[SelfReflect] BookLore lookup failed: {exc}")
        knowledge = "".join(f"{hit['title']}：{hit['summary']}\n" for hit in hits_evidence)
        return knowledge or "（无额外参考）", hits_evidence

    async def _learn_from_correction(
        self,
        bot_reply: str,
        correction: str,
        *,
        bot_id: str | None = None,
        group_id: str | None = None,
        scope: RuntimeScope | None = None,
        message_id: str | None = None,
        sender_name: str = "",
        bot_reply_message_id: str | None = None,
    ) -> bool:
        """生成有 Scope/EvidenceBinding 的纠正候选，不写最终领域对象。"""
        try:
            stable_bot_id = self._resolve_bot_id(bot_id)
        except ValueError:
            return False
        runtime_scope = self._group_runtime_scope(scope, bot_id=stable_bot_id)
        if runtime_scope is None:
            return False
        group_id = str(group_id or "").strip()
        if group_id != runtime_scope.session.conversation_id:
            return False
        message_id = str(message_id or "").strip()
        bot_reply = str(bot_reply or "").strip()
        correction = str(correction or "").strip()
        if not message_id or not bot_reply or not correction:
            return False

        knowledge, lore_hits = await self._search_book_lore(stable_bot_id, bot_reply, correction)
        prompt = CORRECT_PROMPT.format(
            bot_name=self.bot_name,
            bot_reply=bot_reply[:200],
            correction=correction[:200],
            knowledge=knowledge[:500],
        )
        response = await self.llm.text_chat(prompt=prompt)
        text = str(getattr(response, "completion_text", "") or "").strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        names = [self.bot_name, *self.bot_aliases]
        prefixes = [f"{name}{sep}" for name in names if name for sep in ("：", ":")] + ["内心：", "内心:"]
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):]
        text = text.strip()
        if len(text) < 8 or len(text) > 150:
            return False

        captured_at = time.time()
        correction_hash = hashlib.sha256(correction.encode("utf-8")).hexdigest()
        correction_ref = EvidenceRef(
            kind="raw_message",
            id=f"message:{runtime_scope.session.id}:{message_id}",
            content_hash=correction_hash,
            captured_at=captured_at,
            source_scope=runtime_scope,
            available=True,
        )
        correction_binding = EvidenceBinding(
            evidence_id=correction_ref.id,
            target_scope=runtime_scope,
            derivation_chain=("raw_message", "scoped_candidate"),
            policy_version="self-reflect/v1",
        )
        scope_payload = runtime_scope.to_dict()
        evidence = {
            "bot_id": stable_bot_id,
            "group_id": group_id,
            "scope": scope_payload,
            "target_scope": scope_payload,
            "bot_reply": bot_reply,
            "bot_reply_message_id": str(bot_reply_message_id or ""),
            "user_correction": correction,
            "sender_name": str(sender_name or ""),
            "timestamp": captured_at,
            "message_ref": {
                "message_id": message_id,
                "group_id": group_id,
                "session_id": runtime_scope.session.id,
            },
            "evidence_ref": correction_ref.to_dict(),
            "evidence_refs": [correction_ref.to_dict()],
            "evidence_binding": correction_binding.to_dict(),
            "evidence_bindings": [correction_binding.to_dict()],
            "book_lore_hits": lore_hits,
            "generated_text": text,
        }
        # 候选去重由 bot_id + canonical session + candidate_type + source_fingerprint 共同保证。
        fingerprint_data = json.dumps(
            {
                "bot_id": stable_bot_id,
                "bot_reply": bot_reply,
                "correction": correction,
                "session_id": runtime_scope.session.id,
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        if self.candidate_service is not None:
            source_fingerprint = "self-reflect:" + hashlib.sha256(fingerprint_data).hexdigest()
            source_id = None
            repositories = getattr(self.candidate_service, "repositories", None)
            source_repository = getattr(repositories, "sources", None)
            if source_repository is not None:
                try:
                    source_id = source_repository.create(
                        bot_id=stable_bot_id,
                        source_type="self_reflect",
                        name="correction_feedback",
                        config={"bot_id": stable_bot_id, "scope": scope_payload},
                    )
                except Exception as exc:
                    logger.debug(f"[SelfReflect] Could not register source: {exc}")
            self.candidate_service.create(
                bot_id=stable_bot_id,
                candidate_type=CORRECTION_LEARNING_CANDIDATE,
                content=text,
                evidence=evidence,
                source_fingerprint=source_fingerprint,
                source_id=source_id,
                reason="SelfReflect 检测到用户纠正，待审核后晋升",
                metadata={
                    "source": "self_reflect",
                    "bot_id": stable_bot_id,
                    "session_id": runtime_scope.session.id,
                },
            )
        return True

    def cleanup_cooldown(self):
        now = time.time()
        expired = [key for key, timestamp in self._cooldown_map.items() if (now - timestamp) > self.cooldown * 2]
        for key in expired:
            del self._cooldown_map[key]

    @property
    def stats(self) -> dict:
        return {
            "reflect_count": self._reflect_count,
            "recent_replies_buffered": sum(len(buffer) for buffer in self._recent_replies.values()),
        }
