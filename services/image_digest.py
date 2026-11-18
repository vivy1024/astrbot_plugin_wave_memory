"""群聊看图识梗缓存 + 多人交叉聊天引用线索解耦。

三层省 Token 架构：
1. 第一层（0 Token）：从 OneBot / AstrBot 的 Image 组件提取自带 ``summary`` 与稳定哈希；
   对 ``[滑稽]``、``[狗头]`` 等自带描述直接命中，泛词 ``[动画表情]`` / ``[图片]`` 自动剔除。
2. 第二层（哈希去重 + 按需单次视觉摘要）：仅当「有人向 Bot 搭话/读空气唤醒」或「群内高频复读
   ≥ ``min_hits_for_auto_vision`` 次」时，才在后台触发一次视觉摘要并写入 SQLite 缓存，后续复读
   全部 0 Token 命中；受 ``daily_vision_limit`` 每日配额硬上限保护。
3. 第三层（交叉聊天引用解耦）：从消息链提取 ``Reply`` 引用组件（支持内嵌正文或回查记忆库），
   生成 ``[回复 @张三「原话摘要」] `` 前缀与结构化字段，供入库与事后记账显式串联上下文。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - focused repository tests without AstrBot
    import logging

    logger = logging.getLogger(__name__)

try:
    from ..domain.display_name import sanitize_display_name
    from ..engine.db.migrations.image_digest_cache import ensure_image_digest_schema
    from .identity_safety import (
        AUTHORITY_TERMS,
        CONTRACT_TERMS,
        KINSHIP_TERMS,
        OWNERSHIP_TERMS,
        is_catgirl_persona_contamination,
        is_identity_contamination,
    )
except ImportError:  # pragma: no cover - top-level import in isolated tests
    from domain.display_name import sanitize_display_name
    from engine.db.migrations.image_digest_cache import ensure_image_digest_schema
    from services.identity_safety import (
        AUTHORITY_TERMS,
        CONTRACT_TERMS,
        KINSHIP_TERMS,
        OWNERSHIP_TERMS,
        is_catgirl_persona_contamination,
        is_identity_contamination,
    )


_GENERIC_IMAGE_SUMMARIES = frozenset(
    {
        "动画表情",
        "图片",
        "表情",
        "表情包",
        "图像",
        "贴纸",
        "闪照",
        "原创表情",
        "收藏表情",
        "商城表情",
        "自定义表情",
        "image",
        "picture",
        "photo",
        "sticker",
        "emoji",
        "gif",
    }
)

_STICKER_HINT_RE = re.compile(
    r"(动画表情|表情包|收藏表情|商城表情|原创表情|自定义表情|贴纸|sticker|meme)",
    re.IGNORECASE,
)

_OUTER_BRACKETS = (
    ("[", "]"),
    ("【", "】"),
    ("(", ")"),
    ("（", "）"),
    ("「", "」"),
    ("『", "』"),
    ("<", ">"),
)

_HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|bmp|avif|ico|svg|image)$", re.IGNORECASE)
_EMBEDDED_MD5_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")
_WHITESPACE_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_NESTED_REPLY_PREFIX_RE = re.compile(r"^(?:\[回复\s+@[^\]\n]{1,80}\]\s*)+")
_LEADING_SUMMARY_LABEL_RE = re.compile(r"^(?:图片|表情|表情包|动画表情|画面|内容)\s*[:：]\s*")

_IDENTITY_UNSAFE_TERMS = KINSHIP_TERMS + CONTRACT_TERMS + OWNERSHIP_TERMS + AUTHORITY_TERMS


def _get_field(obj: Any, *names: str) -> Any:
    """兼容对象属性与 dict 映射读取，包括嵌套在 ``data`` 字段里的 OneBot 原始结构。"""
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val not in (None, ""):
                return val
    nested = obj.get("data") if isinstance(obj, Mapping) else getattr(obj, "data", None)
    if nested is not None and nested is not obj:
        for name in names:
            if isinstance(nested, Mapping):
                if name in nested and nested[name] not in (None, ""):
                    return nested[name]
            else:
                val = getattr(nested, name, None)
                if val not in (None, ""):
                    return val
    return None


def _collapse_inline_text(value: Any) -> str:
    text = _CTRL_RE.sub("", str(value or ""))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _strip_outer_brackets(text: str) -> str:
    cleaned = text.strip()
    changed = True
    while changed and len(cleaned) >= 2:
        changed = False
        for left, right in _OUTER_BRACKETS:
            if cleaned.startswith(left) and cleaned.endswith(right):
                inner = cleaned[len(left) : len(cleaned) - len(right)].strip()
                if left in inner or right in inner:
                    continue
                cleaned = inner
                changed = True
                break
    return cleaned.strip()


def _truncate_chars(text: str, max_chars: int, *, ellipsis: str = "") -> str:
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    if ellipsis and limit > len(ellipsis):
        return text[: limit - len(ellipsis)].rstrip() + ellipsis
    return text[:limit].rstrip()


def _clean_meaningful_summary(raw: Any) -> tuple[str, bool]:
    """返回 ``(有效语义摘要, 是否携带了动画表情特征)``。"""
    text = _collapse_inline_text(raw)
    if not text:
        return "", False
    sticker_hint = bool(_STICKER_HINT_RE.search(text))
    unwrapped = _strip_outer_brackets(text)
    unwrapped = _LEADING_SUMMARY_LABEL_RE.sub("", unwrapped).strip()
    unwrapped = _strip_outer_brackets(unwrapped)
    if not unwrapped:
        return "", sticker_hint
    normalized_key = unwrapped.casefold().strip(" .!?,，。！？~～:：/-_")
    if not normalized_key or normalized_key in _GENERIC_IMAGE_SUMMARIES:
        return "", sticker_hint
    unwrapped = re.sub(r"^\[?(?:动画表情|表情包|收藏表情|原创表情)\]?\s*", "", unwrapped).strip()
    unwrapped = _strip_outer_brackets(unwrapped)
    if not unwrapped or unwrapped.casefold() in _GENERIC_IMAGE_SUMMARIES:
        return "", sticker_hint
    return unwrapped, sticker_hint


def _sanitize_summary_text(raw: Any, *, max_chars: int = 28) -> str:
    """清洗摘要文本：去换行/外层括号/标签前缀、过滤身份污染词并按上限截断。"""
    cleaned, _ = _clean_meaningful_summary(raw)
    if not cleaned:
        return ""
    cleaned = cleaned.replace("[", "（").replace("]", "）")
    compact = cleaned.replace(" ", "")
    if any(term in compact for term in _IDENTITY_UNSAFE_TERMS):
        return ""
    if is_catgirl_persona_contamination(cleaned) or is_identity_contamination(cleaned):
        return ""
    return _truncate_chars(cleaned, max_chars)


def _derive_image_hash(comp: Any, *, url: str = "") -> str:
    """从图片组件中提取跨群友复读一致的稳定键。"""
    for field_name in ("md5", "file_unique", "hash", "sha256"):
        raw = _collapse_inline_text(_get_field(comp, field_name))
        if not raw:
            continue
        stem = _IMAGE_EXT_RE.sub("", os.path.basename(raw))
        if _HEX_HASH_RE.match(stem):
            return stem.lower()
        if _SAFE_TOKEN_RE.match(stem):
            return f"id:{stem.lower()}"

    file_val = _collapse_inline_text(_get_field(comp, "file", "filename", "name", "path"))
    if file_val and not file_val.startswith(("http://", "https://", "base64://", "data:")):
        base = _IMAGE_EXT_RE.sub("", os.path.basename(file_val.replace("\\", "/"))).strip("{}")
        m = _EMBEDDED_MD5_RE.search(base)
        if m:
            return m.group(1).lower()
        if _HEX_HASH_RE.match(base):
            return base.lower()
        if _SAFE_TOKEN_RE.match(base):
            return f"file:{base.lower()}"

    for candidate in (file_val, url):
        if not candidate:
            continue
        m = _EMBEDDED_MD5_RE.search(candidate)
        if m:
            return m.group(1).lower()
        if candidate.startswith(("http://", "https://")):
            parsed = urlparse(candidate)
            stable_target = f"{parsed.netloc.lower()}{parsed.path}"
            return "url:" + hashlib.sha1(stable_target.encode("utf-8")).hexdigest()[:24]
        return "raw:" + hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:24]
    return ""


def extract_image_meta(comp: Any, *, max_summary_chars: int = 28) -> dict[str, Any]:
    """0-Token 提取单张 OneBot / AstrBot 图片组件元数据。"""
    raw_desc = _get_field(comp, "summary", "text", "alt", "prompt")
    meaningful, sticker_from_text = _clean_meaningful_summary(raw_desc)
    safe_summary = _sanitize_summary_text(meaningful, max_chars=max_summary_chars) if meaningful else ""

    sub_type = _get_field(comp, "subType", "sub_type", "subtype")
    is_sticker = sticker_from_text
    if sub_type is not None:
        try:
            is_sticker = is_sticker or int(sub_type) in {1, 7, 11}
        except (TypeError, ValueError):
            is_sticker = is_sticker or str(sub_type).strip().lower() in {"sticker", "meme", "emoji"}

    url = _collapse_inline_text(_get_field(comp, "url", "file"))
    if url.startswith(("base64://", "data:")):
        url = ""
    image_hash = _derive_image_hash(comp, url=url)
    return {
        "image_hash": image_hash,
        "raw_summary": safe_summary,
        "is_sticker": bool(is_sticker),
        "url": url,
    }


@contextmanager
def _write_tx(connection: Any):
    write_tx = getattr(connection, "write_transaction", None)
    if callable(write_tx):
        with write_tx() as tx:
            yield tx
        return
    if getattr(connection, "in_transaction", False):
        yield connection
        return
    with connection:
        yield connection


def _format_tag(summary: str, *, is_sticker: bool) -> str:
    kind = "表情" if is_sticker else "图片"
    return f"[{kind}: {summary}]" if summary else f"[{kind}]"


class ImageDigestService:
    """图片/表情包哈希去重与按需单次视觉摘要缓存服务。"""

    def __init__(
        self,
        connection: Any,
        *,
        vision_summarizer: Callable[[str], Awaitable[str]] | None = None,
        min_hits_for_auto_vision: int = 3,
        max_summary_chars: int = 28,
        daily_vision_limit: int = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connection = connection
        self.vision_summarizer = vision_summarizer
        self.min_hits_for_auto_vision = max(2, int(min_hits_for_auto_vision))
        self.max_summary_chars = max(8, min(80, int(max_summary_chars)))
        self.daily_vision_limit = max(0, int(daily_vision_limit))
        self.clock = clock
        self._lock = threading.RLock()
        self._daily_bucket: str = ""
        self._daily_used: int = 0
        self._in_flight: set[str] = set()
        ensure_image_digest_schema(self.connection)

    def _day_key(self, ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    def _quota_available(self, ts: float) -> bool:
        if self.daily_vision_limit <= 0:
            return False
        day = self._day_key(ts)
        with self._lock:
            if self._daily_bucket != day:
                self._daily_bucket = day
                self._daily_used = 0
            return self._daily_used < self.daily_vision_limit

    def _consume_quota(self, ts: float) -> bool:
        if self.daily_vision_limit <= 0:
            return False
        day = self._day_key(ts)
        with self._lock:
            if self._daily_bucket != day:
                self._daily_bucket = day
                self._daily_used = 0
            if self._daily_used >= self.daily_vision_limit:
                return False
            self._daily_used += 1
            return True

    def observe_and_format_images(
        self,
        images: Sequence[Any],
        *,
        directed_or_woke: bool = False,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """同步观察消息内图片组件，返回 ``(行内标签列表, 待后台视觉解析任务列表)``。"""
        if not images:
            return [], []
        now = float(self.clock())
        tags: list[str] = []
        pending_tasks: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()

        with self._lock, _write_tx(self.connection) as tx:
            for comp in images:
                meta = extract_image_meta(comp, max_summary_chars=self.max_summary_chars)
                img_hash = meta["image_hash"]
                raw_summary = meta["raw_summary"]
                is_sticker = bool(meta["is_sticker"])
                url = meta["url"]

                if not img_hash:
                    tags.append(_format_tag(raw_summary, is_sticker=is_sticker))
                    continue

                row = tx.execute(
                    "SELECT summary, is_sticker, hit_count, last_url, status, source "
                    "FROM image_digest_cache WHERE image_hash=?",
                    (img_hash,),
                ).fetchone()

                if raw_summary:
                    new_hits = (int(row[2]) + 1) if row is not None else 1
                    stick_flag = int(is_sticker or (bool(row[1]) if row is not None else False))
                    keep_url = url or (str(row[3] or "") if row is not None else "")
                    if row is None:
                        tx.execute(
                            """INSERT INTO image_digest_cache
                               (image_hash, summary, is_sticker, hit_count, last_url, status, source, created_at, updated_at)
                               VALUES (?, ?, ?, 1, ?, 'resolved', 'onebot_summary', ?, ?)""",
                            (img_hash, raw_summary, stick_flag, keep_url, now, now),
                        )
                    else:
                        tx.execute(
                            """UPDATE image_digest_cache
                                  SET summary=?, is_sticker=?, hit_count=?, last_url=?, status='resolved',
                                      source=CASE WHEN source='' THEN 'onebot_summary' ELSE source END, updated_at=?
                                WHERE image_hash=?""",
                            (raw_summary, stick_flag, new_hits, keep_url, now, img_hash),
                        )
                    tags.append(_format_tag(raw_summary, is_sticker=bool(stick_flag)))
                    continue

                if row is not None:
                    cached_summary = str(row[0] or "")
                    stick_flag = bool(row[1]) or is_sticker
                    new_hits = int(row[2] or 0) + 1
                    eff_url = url or str(row[3] or "")
                    status = str(row[4] or "pending")
                    tx.execute(
                        "UPDATE image_digest_cache SET hit_count=?, is_sticker=?, last_url=?, updated_at=? WHERE image_hash=?",
                        (new_hits, int(stick_flag), eff_url, now, img_hash),
                    )
                    if status == "resolved" and cached_summary:
                        tags.append(_format_tag(cached_summary, is_sticker=stick_flag))
                        continue
                else:
                    new_hits = 1
                    stick_flag = is_sticker
                    eff_url = url
                    status = "pending"
                    tx.execute(
                        """INSERT INTO image_digest_cache
                           (image_hash, summary, is_sticker, hit_count, last_url, status, source, created_at, updated_at)
                           VALUES (?, '', ?, 1, ?, 'pending', '', ?, ?)""",
                        (img_hash, int(stick_flag), eff_url, now, now),
                    )

                tags.append(_format_tag("", is_sticker=stick_flag))

                should_resolve = (
                    self.vision_summarizer is not None
                    and bool(eff_url)
                    and img_hash not in seen_hashes
                    and img_hash not in self._in_flight
                    and (directed_or_woke or new_hits >= self.min_hits_for_auto_vision)
                    and self._quota_available(now)
                )
                if should_resolve:
                    seen_hashes.add(img_hash)
                    pending_tasks.append(
                        {
                            "image_hash": img_hash,
                            "url": eff_url,
                            "is_sticker": stick_flag,
                            "hit_count": new_hits,
                        }
                    )

        return tags, pending_tasks

    async def resolve_pending(self, tasks: Sequence[Mapping[str, Any]]) -> int:
        """后台执行按需单次视觉摘要，写回缓存；异常静默落 failed 不影响主对话。"""
        if not tasks or self.vision_summarizer is None:
            return 0
        resolved_count = 0
        for item in tasks:
            img_hash = str(item.get("image_hash") or "").strip()
            url = str(item.get("url") or "").strip()
            if not img_hash or not url:
                continue
            with self._lock:
                if img_hash in self._in_flight:
                    continue
                row = self.connection.execute(
                    "SELECT status, summary FROM image_digest_cache WHERE image_hash=?",
                    (img_hash,),
                ).fetchone()
                if row is not None and str(row[0]) == "resolved" and str(row[1] or "").strip():
                    continue
                now_pre = float(self.clock())
                if not self._consume_quota(now_pre):
                    break
                self._in_flight.add(img_hash)

            try:
                raw_out = await self.vision_summarizer(url)
                summary = _sanitize_summary_text(raw_out, max_chars=self.max_summary_chars)
                now_done = float(self.clock())
                with self._lock, _write_tx(self.connection) as tx:
                    if summary:
                        tx.execute(
                            """UPDATE image_digest_cache
                                  SET summary=?, status='resolved', source='vision_llm', updated_at=?
                                WHERE image_hash=?""",
                            (summary, now_done, img_hash),
                        )
                        resolved_count += 1
                    else:
                        tx.execute(
                            "UPDATE image_digest_cache SET status='failed', updated_at=? WHERE image_hash=?",
                            (now_done, img_hash),
                        )
            except Exception as exc:
                logger.debug(f"[ImageDigest] vision summarize failed for {img_hash}: {exc!r}")
                now_err = float(self.clock())
                with self._lock, _write_tx(self.connection) as tx:
                    tx.execute(
                        "UPDATE image_digest_cache SET status='failed', updated_at=? WHERE image_hash=?",
                        (now_err, img_hash),
                    )
            finally:
                with self._lock:
                    self._in_flight.discard(img_hash)
        return resolved_count


def _extract_embedded_reply_text(comp: Any) -> str:
    direct = _collapse_inline_text(_get_field(comp, "message_str", "text", "content", "raw_message"))
    if direct:
        return _NESTED_REPLY_PREFIX_RE.sub("", direct).strip()
    chain = _get_field(comp, "chain", "message")
    if isinstance(chain, Sequence) and not isinstance(chain, (str, bytes)):
        parts: list[str] = []
        for sub in chain:
            cname = sub.__class__.__name__
            if cname == "Plain" or isinstance(sub, Mapping) and sub.get("type") == "text":
                txt = _collapse_inline_text(_get_field(sub, "text"))
                if txt:
                    parts.append(txt)
            elif cname == "Image" or isinstance(sub, Mapping) and sub.get("type") == "image":
                meta = extract_image_meta(sub)
                parts.append(_format_tag(meta["raw_summary"], is_sticker=meta["is_sticker"]))
        joined = _collapse_inline_text(" ".join(parts))
        return _NESTED_REPLY_PREFIX_RE.sub("", joined).strip()
    return ""


def extract_reply_thread_prefix(
    event: Any,
    *,
    memory_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    max_quote_chars: int = 28,
) -> dict[str, Any]:
    """识别消息链中的 ``Reply`` 引用回复组件，生成防多人串线的显式上下文前缀。"""
    empty = {
        "reply_to_event_id": "",
        "reply_to_sender_id": "",
        "reply_to_sender_name": "",
        "reply_excerpt": "",
        "inline_prefix": "",
    }
    msg_obj = getattr(event, "message_obj", None)
    components = getattr(msg_obj, "message", None) if msg_obj is not None else None
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        return empty

    reply_comp = None
    for comp in components:
        cname = comp.__class__.__name__
        ctype = comp.get("type") if isinstance(comp, Mapping) else None
        if cname == "Reply" or ctype == "reply":
            reply_comp = comp
            break
    if reply_comp is None:
        return empty

    reply_id = _collapse_inline_text(_get_field(reply_comp, "id", "message_id", "seq"))
    sender_id = _collapse_inline_text(_get_field(reply_comp, "sender_id", "user_id", "qq"))
    sender_name = sanitize_display_name(
        str(_get_field(reply_comp, "sender_nickname", "sender_name", "nickname") or "")
    )
    excerpt = _extract_embedded_reply_text(reply_comp)

    if (not excerpt or not sender_name) and reply_id and callable(memory_lookup):
        try:
            found = memory_lookup(reply_id)
        except Exception:
            found = None
        if isinstance(found, Mapping):
            if not sender_id:
                sender_id = _collapse_inline_text(found.get("sender_id"))
            if not sender_name:
                sender_name = sanitize_display_name(str(found.get("sender_name") or ""))
            if not excerpt:
                raw_content = _collapse_inline_text(found.get("content"))
                excerpt = _NESTED_REPLY_PREFIX_RE.sub("", raw_content).strip()

    excerpt = excerpt.replace("「", "『").replace("」", "』")
    if excerpt:
        excerpt = _truncate_chars(excerpt, max(6, int(max_quote_chars)), ellipsis="…")

    who = sender_name or sender_id
    if who and excerpt:
        prefix = f"[回复 @{who}「{excerpt}」] "
    elif excerpt:
        prefix = f"[回复「{excerpt}」] "
    elif who:
        prefix = f"[回复 @{who}] "
    else:
        prefix = ""

    return {
        "reply_to_event_id": reply_id,
        "reply_to_sender_id": sender_id,
        "reply_to_sender_name": sender_name,
        "reply_excerpt": excerpt,
        "inline_prefix": prefix,
    }


__all__ = [
    "ImageDigestService",
    "extract_image_meta",
    "extract_reply_thread_prefix",
]
