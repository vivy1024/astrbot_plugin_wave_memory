"""Read-only dry-run: classify legacy facts into person timeline vs scoped facts.

Does not write. Prints a JSON report. Apply is a later, authorized step.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

QQ_RE = re.compile(r"^\d{5,}$")
IDENTITY_MARKERS = ("我是你的", "认我当", "永远听命令", "身份接管")


def _open_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _is_identity_contamination(text: str) -> bool:
    blob = str(text or "")
    return any(marker in blob for marker in IDENTITY_MARKERS)


def _alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in conn.execute(
        "SELECT subject, object FROM facts WHERE predicate='alias_or_name' AND fact_type='PERSON_ALIAS'"
    ):
        subject = str(row["subject"] or "").strip()
        obj = str(row["object"] or "").strip()
        if QQ_RE.match(subject) and obj:
            mapping[obj] = subject
            mapping[obj.casefold()] = subject
    return mapping


def _resolve_user_id(subject: str, aliases: dict[str, str]) -> str:
    text = str(subject or "").strip()
    if QQ_RE.match(text):
        return text
    if text in aliases:
        return aliases[text]
    if text.casefold() in aliases:
        return aliases[text.casefold()]
    match = re.search(r"(\d{5,})", text)
    if match:
        return match.group(1)
    return ""


def _summary_for(predicate: str, obj: str) -> str:
    pred = str(predicate or "").strip()
    value = str(obj or "").strip()
    if pred == "alias_or_name":
        return f"别名 {value}"[:80]
    if pred == "被称为":
        return f"被称为 {value}"[:80]
    if pred == "认识":
        return f"认识 {value}"[:80]
    return f"{pred} {value}"[:80]


def classify(db_path: str) -> dict:
    conn = _open_ro(db_path)
    aliases = _alias_map(conn)
    buckets = Counter()
    per_person: dict[str, int] = defaultdict(int)
    per_group: dict[str, int] = defaultdict(int)
    unresolved: list[dict[str, str]] = []
    samples = {"person": [], "world": [], "dropped": []}

    rows = conn.execute(
        "SELECT id, subject, predicate, object, group_id, fact_type, confidence FROM facts"
    ).fetchall()
    for row in rows:
        subject = str(row["subject"] or "").strip()
        predicate = str(row["predicate"] or "").strip()
        obj = str(row["object"] or "").strip()
        group_id = str(row["group_id"] or "")
        fact_type = str(row["fact_type"] or "")
        combined = f"{subject} {predicate} {obj}"
        if fact_type == "QUARANTINED_ROLEPLAY" or _is_identity_contamination(combined) or not (subject and predicate and obj):
            buckets["dropped"] += 1
            if len(samples["dropped"]) < 5:
                samples["dropped"].append({"id": row["id"], "subject": subject, "predicate": predicate, "object": obj[:40]})
            continue
        user_id = _resolve_user_id(subject, aliases)
        if user_id:
            buckets["person"] += 1
            per_person[user_id] += 1
            per_group[group_id] += 1
            if len(samples["person"]) < 8:
                samples["person"].append({
                    "id": row["id"],
                    "user_id": user_id,
                    "group_id": group_id,
                    "summary": _summary_for(predicate, obj),
                    "detail": combined[:160],
                })
            continue
        buckets["world"] += 1
        if len(samples["world"]) < 8:
            samples["world"].append({
                "id": row["id"],
                "group_id": group_id,
                "subject": subject,
                "predicate": predicate,
                "object": obj[:80],
            })
        if len(unresolved) < 30 and not QQ_RE.match(subject):
            unresolved.append({"id": str(row["id"]), "subject": subject, "predicate": predicate})

    top_people = sorted(per_person.items(), key=lambda item: item[1], reverse=True)[:15]
    return {
        "database": db_path,
        "total": len(rows),
        "buckets": dict(buckets),
        "unique_people": len(per_person),
        "top_people": [{"user_id": uid, "count": count} for uid, count in top_people],
        "groups_touched": len(per_group),
        "samples": samples,
        "unresolved_subject_samples": unresolved[:20],
        "writes": "none (dry-run)",
    }


def main() -> int:
    default = Path("AstrBot-master/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db")
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(default)
    report = classify(db_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
