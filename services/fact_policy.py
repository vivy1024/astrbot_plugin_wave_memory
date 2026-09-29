"""Deterministic fact admission and half-open validity policy; no model calls.

Configure once with ``set_fact_policy(FactPolicy(...))`` or inject a policy into
the proposal/review services. Relative dates require a verified source timestamp.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def normalized_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def valid_subject(value: Any) -> bool:
    text = normalized_text(value)
    return bool(text and any(c.isalnum() for c in text) and text not in {
        "null", "none", "undefined", "unknown", "n/a", "nan", "未知", "无", "不详",
        "某人", "某个用户", "用户", "群友", "我", "你", "他", "她", "它", "我们", "他们",
        "i", "you", "he", "she", "they", "someone", "user",
    })


def timestamp(value: Any, tz: Any = timezone.utc, *, end_date: bool = False) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_fact_time")
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            text = str(value).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                day = date.fromisoformat(text) + timedelta(days=int(end_date))
                dt = datetime.combine(day, time.min, tz)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
            result = dt.timestamp()
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise ValueError("invalid_fact_time") from exc


def valid_interval(start: Any, end: Any) -> tuple[float | None, float | None]:
    start, end = timestamp(start), timestamp(end)
    if start is not None and end is not None and start >= end:
        raise ValueError("invalid_fact_interval")
    return start, end


def is_effective(row: Mapping[str, Any], now: float) -> bool:
    if str(row.get("status") or "active") not in {"active", "approved"}:
        return False
    try:
        start, end = valid_interval(row.get("valid_from"), row.get("valid_until"))
        return (start is None or start <= now) and (end is None or now < end)
    except ValueError:
        return False


def ineffective_reason(row: Mapping[str, Any], now: float) -> str | None:
    status = str(row.get("status") or "pending")
    if status not in {"active", "approved"}:
        return status if status in {"pending", "conflict", "rejected", "quarantined", "deleted", "superseded"} else "pending"
    try:
        start, end = valid_interval(row.get("valid_from"), row.get("valid_until"))
    except ValueError:
        return "invalid_validity"
    if start is not None and now < start:
        return "not_yet_valid"
    if end is not None and now >= end:
        return "expired"
    return None


@dataclass(frozen=True)
class FactValidity:
    valid_from: float | None = None
    valid_until: float | None = None
    reason: str = "unbounded"
    needs_review: bool = False

    def metadata(self) -> dict[str, Any]:
        return {"reason": self.reason, "needs_review": self.needs_review}


# These identify assertions about events, not when the speaker learned a fact.
_EVENT = re.compile(r"请假|休假|放假|值班|加班|出差|开会|参加|考试|面试|聚会|聚餐|出发|回来|回家|在家|上班|休息|生病|有空|没空|不在|在线|离线|安排|计划|打算|准备去|去|吃|喝|玩|meeting|attend|leave|available|busy|plan|travel|visit", re.I)
_LEARNED = re.compile(r"(?:认识到|意识到|了解到|得知|知道了?|发现了|发现|听说|learned|realized|found out)\s*[,，:：]?", re.I)
_RELATIVE = re.compile(r"大后天|后天|明天|明晚|今天|今晚|今早|今晨|昨天|昨晚|(?<![a-z])(?:today|tonight|tomorrow|yesterday)(?![a-z])", re.I)
_DATE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?!\d)")
_CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)")
_VAGUE = re.compile(r"最近|近期|这几天|过几天|改天|暂时|目前|下周|下个月|这周|今年|\bsoon\b|\brecently\b|\bnext week\b", re.I)


@dataclass(frozen=True)
class FactPolicy:
    timezone: str = "UTC"
    single_value_predicates: tuple[str, ...] = (
        "住在", "居住地", "所在地", "常住地", "工作单位", "职业", "姓名", "年龄", "出生日期",
        "最喜欢", "lives in", "current location", "current employer", "occupation",
    )

    def __post_init__(self) -> None:
        self.tz  # Validate configuration without falling back to the host timezone.
        if isinstance(self.single_value_predicates, str):
            raise ValueError("single_value_predicates must be a sequence")

    @property
    def tz(self):
        return timezone.utc if self.timezone in {"UTC", "Etc/UTC"} else ZoneInfo(self.timezone)

    def is_single_value(self, predicate: str) -> bool:
        return normalized_text(predicate) in {normalized_text(p) for p in self.single_value_predicates}

    def validity(self, *, predicate: str, object: str, source_quote: str = "",
                 source_timestamp: Any = None, valid_from: Any = None,
                 valid_until: Any = None) -> FactValidity:
        if valid_from is not None or valid_until is not None:
            start = timestamp(valid_from, self.tz)
            end = timestamp(valid_until, self.tz, end_date=True)
            valid_interval(start, end)
            return FactValidity(start, end, "explicit")

        assertion = f"{predicate} {object}"
        quote = str(source_quote or "")
        learned = _LEARNED.search(quote)
        if learned:
            quote = quote[learned.end():]
        text = assertion + " " + quote
        relative = _RELATIVE.findall(text)
        dates = _DATE.findall(text)
        bounded = bool(_EVENT.search(assertion.replace(" ", "")) or re.search(r"仅|只在|只限|当天|当晚|\bonly\b", text, re.I))
        if not relative and not dates:
            ambiguous = bool(_VAGUE.search(text))
            return FactValidity(reason="unclear_time" if ambiguous else "unbounded", needs_review=ambiguous)
        if not bounded:
            # An explicit birthday/historical date is content, not an expiry.
            if not relative and re.search(r"出生|生日|成立|建于|born|birthday|founded", assertion, re.I):
                return FactValidity()
            return FactValidity(reason="unclear_time", needs_review=True)
        if len(set(dates)) > 1 or len(set(x.casefold() for x in relative)) > 1:
            return FactValidity(reason="ambiguous_time_range", needs_review=True)
        try:
            if dates:
                day = date(*map(int, dates[0]))
            else:
                anchor = timestamp(source_timestamp)
                if anchor is None:
                    return FactValidity(reason="source_time_required", needs_review=True)
                offsets = {"昨天": -1, "昨晚": -1, "yesterday": -1, "明天": 1, "明晚": 1, "tomorrow": 1, "后天": 2, "大后天": 3}
                day = datetime.fromtimestamp(anchor, self.tz).date() + timedelta(days=offsets.get(relative[0].casefold(), 0))
            start = datetime.combine(day, time.min, self.tz).timestamp()
            end = datetime.combine(day + timedelta(days=1), time.min, self.tz).timestamp()
            # Prefer quote-local clocks; repeating the same time in the triple
            # must not create an artificial range. Unclear ranges stay pending.
            clock_text = quote if _CLOCK.search(quote) else assertion
            clocks = list(_CLOCK.finditer(clock_text))
            if len(clocks) > 2:
                return FactValidity(reason="ambiguous_time_range", needs_review=True)
            if clocks:
                first = clocks[0]
                start = datetime.combine(day, time(int(first[1]), int(first[2])), self.tz).timestamp()
                if len(clocks) == 2:
                    second = clocks[1]
                    connector = clock_text[first.end():second.start()].strip()
                    if connector not in {"到", "至", "-", "~", "～", "to", "until"}:
                        return FactValidity(reason="ambiguous_time_range", needs_review=True)
                    end = datetime.combine(day, time(int(second[1]), int(second[2])), self.tz).timestamp()
                    if end <= start:
                        # Without an explicit next-day marker an overnight range
                        # is ambiguous; do not infer a calendar rollover.
                        return FactValidity(reason="ambiguous_time_range", needs_review=True)
                elif re.search(r"截止|截至|直到|until|before", clock_text[:first.start()], re.I):
                    end, start = start, datetime.combine(day, time.min, self.tz).timestamp()
        except (ValueError, OverflowError, OSError):
            return FactValidity(reason="invalid_source_time", needs_review=True)
        return FactValidity(start, end, "explicit_date" if dates else "source_relative_day")


_POLICY = FactPolicy()


def get_fact_policy() -> FactPolicy:
    return _POLICY


def set_fact_policy(policy: FactPolicy) -> None:
    global _POLICY
    if not isinstance(policy, FactPolicy):
        raise TypeError("FactPolicy required")
    _POLICY = policy
