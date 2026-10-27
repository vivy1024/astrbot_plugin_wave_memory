"""Reviewed, short human expressions; lexical situational recall without models.

Only the originating Bot/group may use an expression. Speaker and source remain
in evidence/audit fields, never in the injected style text. This learning lane
does not replace the existing tool-owned Bot few-shot approval projection.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence

try:
    from ..domain.evidence import EvidenceBinding, RawArtifactRef
    from ..domain.scope import RuntimeScope, SessionRef
except ImportError:
    from domain.evidence import EvidenceBinding, RawArtifactRef
    from domain.scope import RuntimeScope, SessionRef

try:
    from ..engine.db.migrations.expression_candidate_dedup import merge_source
except ImportError:
    from engine.db.migrations.expression_candidate_dedup import merge_source

from .identity_safety import is_identity_contamination
from .reply_feedback import (
    SITUATIONS, _write_transaction, artifact_for, content_hash, group_owner,
    json_value, payload_items, positive_id, resolve_messages,
)

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - domain tests run without AstrBot
    import logging

    logger = logging.getLogger(__name__)


_UNSAFE = re.compile(
    r"[\d@<>`\r\n]|https?://|www\.|\S+@\S+|"
    r"(?:我叫|我是|我住|我家|我的(?:名字|姓名|电话|地址|账号|密码|职业|公司)|"
    r"身份证|手机号|电话|联系方式|住址|密码|验证码|密钥|真实姓名|微信|私聊|"
    r"爸爸|妈妈|主人|奴隶|造物主|永远服从|忽略.{0,8}(?:指令|规则)|系统提示|"
    r"傻逼|傻[比叉]|脑残|智障|废物|蠢货|滚|你妈|操你|去死|nmsl|fuck|idiot)|"
    r"\b(?:i\s+am|my\s+name|password|secret|api[_ -]?key|address|phone)\b",
    re.IGNORECASE,
)
# 情景词表只看群友原话里的口语信号，宁可少配也不乱配。多义字（草/行/冲/爽/牛/过了）
# 都带了边界或固定搭配：「草」只认单独成句，「行」只认「行吧/那行」，「爽」排除「不爽」。
# 顺序即优先级：一条消息最多取前两个情景。
_SCENE_PATTERNS = (
    ("boundary", r"别再|不要再|不舒服|不愿意|越界|别闹|别理|闭嘴|(?<!不)过分|别刷了?|别发了|骚扰|"
                 r"(?<![足能就])够了|stop|boundary"),
    ("clarify", r"什么意思|啥意思|何意|怎么|咋(?!呼)|为何|为什么|为啥|凭啥|(?:是|干|说|叫)啥|啥(?=[?？]|$)|"
                r"不懂|没懂|看不懂|不明白|请教|真的假的|真的吗|(?<!不)是吗|意思是|说什么|你说我|"
                r"什么(?=\s*[,，]?\s*\S{0,6}[?？])|^[?？]{2,}|clarif|"
                r"(?<![a-z])(?:why|how)(?![a-z])"),
    ("encourage", r"担心|紧张|难过|伤心|难受|失败|加油|害怕|焦虑|累了|好累|心累|崩溃|破防|寄了|"
                  r"没事(?![干做儿])|别慌|别怕|稳了|顶住|撑住|冲冲冲|冲鸭|worried|nervous"),
    ("celebrate", r"成功|(?:考|审|测|跑|打|通)过了|过关|通过|赢了|完成|恭喜|庆祝|开心|好耶|"
                  r"太强了?|好强|真强|牛逼|牛b|太牛|好牛|真牛|牛啊|(?<![不凉飒])爽|欧皇|"
                  r"(?<![a-z])nb(?![a-z])|congrat|success"),
    ("acknowledge", r"谢谢|多谢|感谢|谢啦|收到|明白|了解|懂了|^好的|(?<![最美很更太真])好的(?=$|[\s,，。!！~～])|好滴|好哒|"
                    r"行吧|那行|可以的|确实|有一说一|有道理|没错|(?<![要若])是的(?!话)|"
                    r"(?<![a-z])ok(?![a-z])|thank|got it"),
    ("humor", r"哈哈|hhh|笑死|好笑|逗乐|绷不住|绷死|蚌埠住|xswl|乐死|(?<![快欢娱音])乐了|"
              r"(?<!\d)233+(?!\d)|(?:^|[\s,，。!！~～])草+(?=$|[\s,，。!！?？~～])|😂|🤣|haha|lol"),
)
_SCENE_REGEXES = tuple((scene, re.compile(pattern, re.I)) for scene, pattern in _SCENE_PATTERNS)
# 「@昵称(QQ号)」是转发/提及前缀，昵称里的字不能参与情景判断。
_MENTION = re.compile(r"@[^@\n]{1,40}?\(\d{5,}\)")
# 问号不再单独判 clarify：只有整句就是一个疑问反应（「?」「啊？」「蛤??」）才算，
# 且仅在没有其它情景命中时兜底。普通问句（「能嫁给我吗?」）不是在求解释。
_LONE_QUESTION = re.compile(r"^(?:[啊蛤哈嗯呃诶欸哦啥]{0,2}|啊这)[?？]+[!！.。…~～]*$")


def expression_is_safe(content, *, speaker_name: str = "") -> bool:
    if not isinstance(content, str) or not 0 < len(content) <= 20 or content != content.strip():
        return False
    normalized = unicodedata.normalize("NFKC", content)
    if any(unicodedata.category(ch).startswith("C") for ch in normalized):
        return False
    return bool(
        not _UNSAFE.search(normalized) and not is_identity_contamination(normalized)
        and not (speaker_name and speaker_name in content)
    )


def situations_for(message: str) -> tuple[str, ...]:
    text = _MENTION.sub(" ", str(message or "")[:1000]).strip()
    scenes = tuple(scene for scene, regex in _SCENE_REGEXES if regex.search(text))[:2]
    if not scenes and _LONE_QUESTION.match(re.sub(r"\s+", "", text)):
        return ("clarify",)
    return scenes


def _rotation_key(seed: str, example_id) -> str:
    """同质量同情景的句式按「种子+id」哈希打散：同一种子结果固定，换种子就换人。"""
    return hashlib.sha256(f"{seed}\x1f{example_id}".encode("utf-8")).hexdigest()


# 频次门槛：一句话被至少两个人说过，或者同一个人说了至少 N 次（默认 3），才算「本群的表达方式」
# 而不是某个人的一次性口头禅。门槛只在待审列表上做「先看哪些」，不影响证据与审核规则。
DEFAULT_AUTO_MIN_OCCURRENCE = 3
# 长期达不到门槛的待审候选：超过这么多天自动作废，不让待审队列无限增长。
DEFAULT_PENDING_TTL_DAYS = 30
# 召回时同一个群友最多贡献几句：避免羽书的口吻越来越像群里最活跃的那一两个人。
DEFAULT_MAX_PER_SPEAKER = 1
# 召回候选池：情景内按新到旧取这么多条再排序打散。
_RECALL_POOL = 120
# 自动转正前的人设一致性检查（不调用模型）：攻击性话术不会自动成为她的说话方式。
_AGGRESSIVE_STYLE = re.compile(
    r"(怼回去|狠狠怼|骂回去|嘴臭|阴阳怪气|你算老几|关你屁事|爱咋咋地|闭嘴吧?你|滚[开远蛋]?)",
    re.IGNORECASE,
)


def _qualified_sql(min_occurrence: int) -> str:
    return f"occurrence_count >= {int(min_occurrence)} OR speaker_count >= 2"


def persona_consistent(content: str, blocklist: Sequence[str] = ()) -> bool:
    """自动转正前的轻量人设检查：攻击性话术与管理员配置的人设禁用词都不自动转正。"""
    text = unicodedata.normalize("NFKC", str(content or ""))
    if _AGGRESSIVE_STYLE.search(text):
        return False
    return not any(word and word in text for word in blocklist)


def _int_or(value, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float_or(value, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class ExpressionReviewError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


class ExpressionPolicy:
    def __init__(
        self,
        connection,
        *,
        enabled: bool = False,
        feedback_service=None,
        now=None,
        auto_min_occurrence: int = DEFAULT_AUTO_MIN_OCCURRENCE,
        pending_ttl_days: float = DEFAULT_PENDING_TTL_DAYS,
        max_per_speaker: int = DEFAULT_MAX_PER_SPEAKER,
        persona_blocklist: Sequence[str] = (),
    ):
        self.connection = connection
        self.enabled = enabled is True
        self.feedback_service = feedback_service
        self.now = now or time.time
        self.auto_min_occurrence = max(2, min(20, _int_or(auto_min_occurrence, DEFAULT_AUTO_MIN_OCCURRENCE)))
        self.pending_ttl_days = max(0.0, _float_or(pending_ttl_days, DEFAULT_PENDING_TTL_DAYS))
        self.max_per_speaker = max(0, _int_or(max_per_speaker, DEFAULT_MAX_PER_SPEAKER))
        self.persona_blocklist = tuple(
            word.strip() for word in (persona_blocklist or ()) if isinstance(word, str) and word.strip()
        )

    def observe(self, scope, payload, *, source_message_ids: Sequence[int], connection=None) -> int:
        if not self.enabled or group_owner(scope) is None or not isinstance(payload, Mapping):
            return 0
        if connection is None:
            with _write_transaction(self.connection) as tx:
                return self.observe(scope, payload, source_message_ids=source_message_ids, connection=tx)
        messages = resolve_messages(connection, scope, source_message_ids)
        owner = group_owner(scope)
        inserted = 0
        for item in payload_items(payload, "expressions", 4):
            source = messages.get(positive_id(item.get("source_memory_id")))
            content, scene = item.get("content"), item.get("situation")
            if (
                source is None or source["speaker_id"] in {"", "bot"}
                or source["speaker_id"] != item.get("speaker_id")
                or not isinstance(scene, str) or scene not in SITUATIONS
                or not expression_is_safe(content, speaker_name=source["speaker_name"])
                or content not in source["content"]
            ):
                continue
            artifact = artifact_for(scope, source)
            created = False
            example_id = self._existing_example(connection, owner, content)
            if example_id is None:
                cursor = connection.execute(
                    """INSERT INTO reply_expression_candidates
                       (bot_id,session_id,visibility,speaker_id,source_memory_id,content,situation,
                        source_hash,artifact_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (*owner, source["speaker_id"], source["id"], content, scene,
                     artifact.content_hash, json_value(artifact.to_dict()), float(self.now())),
                )
                created = max(0, cursor.rowcount) > 0
                if created:
                    example_id = int(cursor.lastrowid)
                else:
                    # 撞上 UNIQUE(bot_id,session_id,visibility,source_memory_id)：同一条消息
                    # 之前已建过候选，读回来把来源记全，避免来源表漏行。
                    example_id = self._example_of_message(connection, owner, source["id"])
            # 同群同内容只保留一条待审项：第二次被说中时不再新建候选行，只把来源并进去。
            # 一条待审项背后可能已经有三个人说过同一句话，这正是频次门槛要看的信号。
            # 返回值仍只数**新建候选**，来源归并不计入 —— 调用方与日志按「新增了几条待审」
            # 理解这个数字，把合并也算进去会让重放看起来像在重复写入。
            if example_id is not None:
                merge_source(
                    connection, example_id=example_id, bot_id=owner[0], session_id=owner[1],
                    visibility=owner[2], source_memory_id=source["id"],
                    speaker_id=source["speaker_id"], situation=scene,
                    artifact_json=json_value(artifact.to_dict()), created_at=float(self.now()),
                )
            inserted += int(created)
        return inserted

    def _existing_example(self, connection, owner, content: str) -> int | None:
        """本群同内容是否已有可用候选：待审的合并进去，已批准的也认（免得又造一条待审）。"""
        row = connection.execute(
            """SELECT id FROM reply_expression_candidates
                WHERE bot_id=? AND session_id=? AND visibility=? AND content=?
                  AND status IN ('pending','approved') ORDER BY id DESC LIMIT 1""",
            (*owner, content),
        ).fetchone()
        return int(row[0]) if row else None

    def _example_of_message(self, connection, owner, source_memory_id: int) -> int | None:
        row = connection.execute(
            """SELECT id FROM reply_expression_candidates
                WHERE bot_id=? AND session_id=? AND visibility=? AND source_memory_id=?""",
            (*owner, source_memory_id),
        ).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def _rows(cursor) -> list[dict]:
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _get(self, scope, example_id, *, connection=None):
        if group_owner(scope) is None:
            return None
        rows = self._rows((connection or self.connection).execute(
            "SELECT * FROM reply_expression_candidates WHERE id=? AND bot_id=? AND session_id=? AND visibility=?",
            (example_id, *group_owner(scope)),
        ))
        return rows[0] if rows else None

    def _source_available(self, scope, row, *, connection=None) -> bool:
        source = resolve_messages(connection or self.connection, scope, [row["source_memory_id"]]).get(row["source_memory_id"])
        if source is None:
            return False
        try:
            artifact = RawArtifactRef.from_dict(json.loads(row["artifact_json"]))
            return bool(
                artifact == artifact_for(scope, source) and source["speaker_id"] == row["speaker_id"]
                and source["speaker_id"] not in {"", "bot"}
                and content_hash(source["content"]) == row["source_hash"]
                and row["content"] in source["content"]
                and expression_is_safe(row["content"], speaker_name=source["speaker_name"])
            )
        except (ValueError, TypeError, KeyError):
            return False

    def _public(self, scope, row) -> dict:
        result = {key: value for key, value in row.items() if not key.endswith("_json")}
        result["evidence_available"] = self._source_available(scope, row)
        result["qualified"] = bool(
            int(row.get("occurrence_count") or 1) >= self.auto_min_occurrence or int(row.get("speaker_count") or 1) >= 2
        )
        result["scope"] = scope.to_dict()
        result["artifact"] = json.loads(row["artifact_json"])
        result["evidence_refs"] = json.loads(row["evidence_refs_json"])
        result["evidence_bindings"] = json.loads(row["evidence_bindings_json"])
        return result

    def list_candidates(self, scope, *, status="pending", limit=25, offset=0, qualified=False) -> tuple[list[dict], int]:
        """Administrative read stays available while learning/injection is disabled.

        ``qualified=True`` 只列出达到频次门槛的候选（出现 ≥2 次或跨 ≥2 人）。
        线上 1190 条待审里只有个位数达标，不过滤的话人根本审不完；门槛只是
        「先看哪些」，其余候选仍在库里，切回全部随时可看。
        """
        if group_owner(scope) is None:
            return [], 0
        if status not in {"pending", "approved", "rejected", "all"}:
            raise ExpressionReviewError("invalid_status")
        where = "bot_id=? AND session_id=? AND visibility=?"
        params = list(group_owner(scope))
        if status != "all":
            where += " AND status=?"
            params.append(status)
        if qualified:
            where += f" AND ({_qualified_sql(self.auto_min_occurrence)})"
        total = self.connection.execute(f"SELECT COUNT(*) FROM reply_expression_candidates WHERE {where}", params).fetchone()[0]
        rows = self._rows(self.connection.execute(
            f"""SELECT * FROM reply_expression_candidates WHERE {where}
                ORDER BY occurrence_count DESC, speaker_count DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(int(limit), 100)), max(0, int(offset))),
        ))
        return [self._public(scope, row) for row in rows], int(total)

    def review(self, scope, example_id: int, *, approved: bool, expected_revision: int,
               reviewer: str, idempotency_key: str, style_only: bool = False, note: str = "") -> dict:
        """Authenticated callers approve with explicit style-only attestation.

        A single transaction validates scope/version/source, writes review audit,
        and creates EvidenceRef/Binding. Replayed review keys never resurrect a
        later revoked expression. The model completion cannot call this API.
        """
        if group_owner(scope) is None:
            raise ExpressionReviewError("group_scope_required")
        if not isinstance(approved, bool) or not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ExpressionReviewError("revision_required")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 128:
            raise ExpressionReviewError("idempotency_key_required")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ExpressionReviewError("reviewer_required")
        if approved and style_only is not True:
            raise ExpressionReviewError("style_only_confirmation_required")
        if not isinstance(note, str) or len(note) > 500:
            raise ExpressionReviewError("invalid_note")
        status = "approved" if approved else "rejected"
        with _write_transaction(self.connection) as tx:
            row = self._get(scope, example_id, connection=tx)
            if row is None:
                raise ExpressionReviewError("scoped_object_not_found", 404)
            previous = tx.execute(
                """SELECT expected_revision,status,reviewer,note,style_only FROM reply_expression_reviews
                   WHERE example_id=? AND idempotency_key=?""", (example_id, idempotency_key),
            ).fetchone()
            expected = (expected_revision, status, reviewer, note, int(style_only is True))
            if previous:
                if tuple(previous) != expected:
                    raise ExpressionReviewError("idempotency_conflict", 409)
            else:
                if row["revision"] != expected_revision:
                    raise ExpressionReviewError("stale_revision", 409)
                if approved and not self._source_available(scope, row, connection=tx):
                    raise ExpressionReviewError("evidence_unavailable_or_unsafe", 422)
                refs, bindings = [], []
                if approved:
                    artifact = RawArtifactRef.from_dict(json.loads(row["artifact_json"]))
                    refs = [artifact.as_evidence_ref().to_dict()]
                    bindings = [EvidenceBinding(
                        evidence_id=artifact.id, target_scope=artifact.source_scope,
                        derivation_chain=("raw_chat", "reviewed_expression"), policy_version="expression-learning/v1",
                    ).to_dict()]
                now = float(self.now())
                tx.execute(
                    """INSERT INTO reply_expression_reviews
                       (example_id,idempotency_key,expected_revision,status,reviewer,note,style_only,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""", (example_id, idempotency_key, *expected, now),
                )
                tx.execute(
                    """UPDATE reply_expression_candidates SET status=?,revision=revision+1,reviewer=?,review_note=?,
                       reviewed_at=?,evidence_refs_json=?,evidence_bindings_json=? WHERE id=? AND revision=?""",
                    (status, reviewer, note, now, json_value(refs), json_value(bindings), example_id, expected_revision),
                )
        return self._public(scope, self._get(scope, example_id))

    def _qualified_columns_ready(self) -> bool:
        tables = {
            str(row[0]) for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "reply_expression_candidates" not in tables:
            return False
        columns = {
            str(row[1]) for row in self.connection.execute(
                "PRAGMA table_info(reply_expression_candidates)"
            ).fetchall()
        }
        return {"occurrence_count", "speaker_count"} <= columns

    def qualified_scopes(self, *, limit: int = 200):
        """返回有达标待审候选的群，供自动审核逐群推进。"""
        if not self._qualified_columns_ready():
            return []
        rows = self.connection.execute(
            f"""SELECT DISTINCT bot_id, session_id, visibility
                  FROM reply_expression_candidates
                 WHERE status='pending' AND ({_qualified_sql(self.auto_min_occurrence)})
                 LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        scopes = []
        for bot_id, session_id, visibility in rows:
            parts = str(session_id or "").split(":", 2)
            if len(parts) != 3 or visibility != "group":
                continue
            platform_id, kind, conversation_id = parts
            if kind != "group":
                continue
            try:
                scopes.append(RuntimeScope(
                    bot_id=str(bot_id), visibility="group",
                    session=SessionRef(
                        id=str(session_id), platform_id=platform_id,
                        kind=kind, conversation_id=conversation_id,
                    ),
                ))
            except Exception:
                continue
        return scopes

    def auto_review_qualified(self, scope, *, limit: int = 50, reviewer: str = "auto_review") -> dict[str, int]:
        """把「本群多人多次说过、来源仍可核」的待审表达直接转正。

        门槛沿用频次列：同句在本群出现 ≥3 次，或至少 2 个不同人说过。来源已删、
        被隔离、内容越界或修订冲突的候选会跳过并保留待审，不静默放行。
        """
        stats = {"qualified": 0, "approved": 0, "skipped": 0, "persona_rejected": 0}
        if group_owner(scope) is None or not self._qualified_columns_ready():
            return stats
        rows = self._rows(self.connection.execute(
            f"""SELECT id, revision, content FROM reply_expression_candidates
                 WHERE bot_id=? AND session_id=? AND visibility=?
                   AND status='pending' AND ({_qualified_sql(self.auto_min_occurrence)})
                 ORDER BY occurrence_count DESC, speaker_count DESC, id DESC
                 LIMIT ?""",
            (*group_owner(scope), max(1, min(int(limit), 200))),
        ))
        seen_content: set[str] = set()
        for row in rows:
            content = str(row.get("content") or "")
            if content in seen_content:
                continue
            seen_content.add(content)
            stats["qualified"] += 1
            if not persona_consistent(content, self.persona_blocklist):
                # 不自动转正也不自动驳回：留在待审，由管理员决定（或到期作废）。
                stats["persona_rejected"] += 1
                continue
            try:
                self.review(
                    scope, int(row["id"]), approved=True,
                    expected_revision=int(row["revision"]), reviewer=reviewer,
                    idempotency_key=f"{reviewer}:expression:{row['id']}:{row['revision']}",
                    style_only=True, note="auto: 本群多人多次使用且来源可核",
                )
                stats["approved"] += 1
            except Exception as exc:
                stats["skipped"] += 1
                logger.debug(f"[Expression] 自动审核跳过 {row['id']}: {type(exc).__name__}")
        return stats

    def auto_review_qualified_all(self, *, limit_per_scope: int = 50) -> dict[str, int]:
        stats = {"scopes": 0, "qualified": 0, "approved": 0, "skipped": 0, "persona_rejected": 0, "expired": 0}
        if not self.enabled:
            return stats
        for scope in self.qualified_scopes():
            result = self.auto_review_qualified(scope, limit=limit_per_scope)
            stats["scopes"] += 1
            for key in ("qualified", "approved", "skipped", "persona_rejected"):
                stats[key] += int(result.get(key, 0))
        stats["expired"] = self.expire_stale_pending()
        return stats

    def expire_stale_pending(self, *, limit: int = 500) -> int:
        """把待审超过 TTL 仍达不到门槛的候选作废（status=rejected，留审计备注，可被管理员恢复）。"""
        if self.pending_ttl_days <= 0 or not self._qualified_columns_ready():
            return 0
        now = float(self.now())
        cutoff = now - self.pending_ttl_days * 86400.0
        with _write_transaction(self.connection) as tx:
            cur = tx.execute(
                f"""UPDATE reply_expression_candidates
                       SET status='rejected', revision=revision+1, reviewer='auto_expire',
                           review_note='auto: 待审超期仍未达到频次门槛', reviewed_at=?
                     WHERE id IN (
                           SELECT id FROM reply_expression_candidates
                            WHERE status='pending' AND created_at < ?
                              AND NOT ({_qualified_sql(self.auto_min_occurrence)})
                            ORDER BY created_at ASC LIMIT ?)""",
                (now, cutoff, max(1, int(limit))),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def recall(self, scope, *, message: str, max_items: int = 2, seed: str | None = None) -> list[dict]:
        """按情景召回已审核表达。

        排序：quality 高者优先，其次情景顺序；两者都相同时按 ``seed`` 确定性打散，
        不再固定取最大 id —— 否则同情景永远是同一对句式。``seed`` 缺省为
        「当前消息内容 + 当天日期」，同一句话当天重复召回结果稳定，换句话就会轮换。
        不写库、不留进程内状态。
        """
        if not self.enabled or group_owner(scope) is None or max_items <= 0:
            return []
        scenes = situations_for(message)
        if not scenes:
            return []
        rows = self._rows(self.connection.execute(
            f"""SELECT * FROM reply_expression_candidates
                WHERE bot_id=? AND session_id=? AND visibility=? AND status='approved'
                  AND situation IN ({','.join('?' for _ in scenes)}) ORDER BY id DESC LIMIT {_RECALL_POOL}""",
            (*group_owner(scope), *scenes),
        ))
        safe = []
        for row in rows:
            if not self._source_available(scope, row):
                continue
            artifact = RawArtifactRef.from_dict(json.loads(row["artifact_json"]))
            expected_ref = artifact.as_evidence_ref().to_dict()
            expected_binding = EvidenceBinding(
                artifact.id, artifact.source_scope, ("raw_chat", "reviewed_expression"), "expression-learning/v1",
            ).to_dict()
            if json.loads(row["evidence_refs_json"]) != [expected_ref] or json.loads(row["evidence_bindings_json"]) != [json.loads(json_value(expected_binding))]:
                continue
            safe.append((self.quality_adjustment(scope, row["id"]), row))
        if seed is None:
            day = time.strftime("%Y-%m-%d", time.localtime(float(self.now())))
            seed = f"{day}\x1f{message or ''}"
        safe.sort(key=lambda pair: (
            -pair[0], scenes.index(pair[1]["situation"]), _rotation_key(seed, pair[1]["id"]),
        ))
        selected, seen = [], set()
        per_speaker: dict[str, int] = {}
        for quality, row in safe:
            if row["content"] in seen:
                continue
            speaker = str(row.get("speaker_id") or "")
            if self.max_per_speaker > 0 and speaker and per_speaker.get(speaker, 0) >= self.max_per_speaker:
                continue
            seen.add(row["content"])
            per_speaker[speaker] = per_speaker.get(speaker, 0) + 1
            selected.append({
                "example_id": f"expression:{row['id']}", "content": row["content"],
                "source": "ExpressionPolicy.recall", "source_memory_id": row["source_memory_id"],
                "speaker_id": row["speaker_id"], "situation": row["situation"],
                "bot_id": scope.bot_id, "session_id": scope.session.id, "visibility": scope.visibility,
                "quality_adjustment": quality,
            })
            if len(selected) >= min(2, int(max_items)):
                break
        return selected

    def record_bookkeeping_usage(self, scope, *, source_message_ids: Sequence[int], connection) -> int:
        """Existing bookkeeping path binds actual literal use; no ingress hook required."""
        if not self.enabled or group_owner(scope) is None:
            return 0
        count = 0
        messages = resolve_messages(connection, scope, source_message_ids)
        for reply in messages.values():
            if reply["speaker_id"] != "bot":
                continue
            rows = connection.execute(
                """SELECT id FROM reply_expression_candidates
                   WHERE bot_id=? AND session_id=? AND visibility=? AND status='approved'
                     AND source_memory_id<? AND instr(?,content)>0
                   ORDER BY length(content) DESC,id DESC LIMIT 3""",
                (*group_owner(scope), reply["id"], reply["content"]),
            ).fetchall()
            count += self.record_reply_usage(
                scope, reply_memory_id=reply["id"], example_ids=[row[0] for row in rows], connection=connection,
            )
        return count

    def record_reply_usage(self, scope, *, reply_memory_id: int, example_ids: Sequence[int], connection=None) -> int:
        """Bind actual use, never recall; accepts the bookkeeping-owned transaction."""
        if not self.enabled or group_owner(scope) is None:
            return 0
        if connection is None:
            with _write_transaction(self.connection) as tx:
                return self.record_reply_usage(scope, reply_memory_id=reply_memory_id, example_ids=example_ids, connection=tx)
        count = 0
        reply = resolve_messages(connection, scope, [reply_memory_id]).get(reply_memory_id)
        if reply is None or reply["speaker_id"] != "bot":
            return 0
        for value in list(example_ids)[:3]:
            example_id = positive_id(value)
            row = self._get(scope, example_id, connection=connection) if example_id else None
            if row is None or row["status"] != "approved" or row["source_memory_id"] >= reply_memory_id or row["content"] not in reply["content"] or not self._source_available(scope, row, connection=connection):
                continue
            cursor = connection.execute(
                """INSERT INTO reply_expression_usage
                   (bot_id,session_id,visibility,reply_memory_id,example_id,created_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (*group_owner(scope), reply_memory_id, example_id, float(self.now())),
            )
            count += max(0, cursor.rowcount)
        return count

    def quality_adjustment(self, scope, example_id: int) -> float:
        if not self.enabled or self.feedback_service is None or not self.feedback_service.enabled or group_owner(scope) is None:
            return 0.0
        ids = [row[0] for row in self.connection.execute(
            """SELECT reply_memory_id FROM reply_expression_usage
               WHERE bot_id=? AND session_id=? AND visibility=? AND example_id=?
               ORDER BY reply_memory_id DESC LIMIT 100""", (*group_owner(scope), example_id),
        ).fetchall()]
        if not ids:
            return 0.0
        signals = self.feedback_service.decision_signals(scope, reply_ids=ids)
        positive = signals["accepted"]
        negative = signals["corrected"] + signals["rejected"]
        # Bounded, deduplicated observational score. Silence contributes zero.
        return round(0.2 * (positive - negative) / (positive + negative + 2), 6)


__all__ = ["ExpressionPolicy", "ExpressionReviewError", "expression_is_safe", "situations_for"]
