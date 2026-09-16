"""Layered Holyman jargon assets.

Holyman-skills is a reference corpus, not a flat activatable phrase list.
Only curated phrases should participate in confirmed runtime matching.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from ..identity_safety import is_identity_contamination
except ImportError:
    from services.identity_safety import is_identity_contamination

HOLYMAN_SOURCE = "holyman_skills"

def make_runtime_catchphrase(meaning: str, *, category: str = "catchphrase", confidence: float = 0.92) -> dict[str, Any]:
    return {
        "meaning": meaning,
        "category": category,
        "source": "curated/core",
        "kind": "curated_phrase",
        "confidence": confidence,
        "safety_level": "safe_reference",
        "layer": "catchphrase",
        "reference_only": True,
        "runtime_match": True,
    }


CORE_CURATED_PHRASES: dict[str, dict[str, Any]] = {
    "v我50": make_runtime_catchphrase("长篇铺垫或煽情叙述后突然索要 50 元，常关联疯狂星期四，用来制造荒诞转折。", category="catchphrase", confidence=0.98),
    "叠甲": make_runtime_catchphrase("提前声明立场、限制讨论范围或自我免责，以避免被攻击或误解。", category="internet-culture", confidence=0.96),
    "不是哥们": make_runtime_catchphrase("面对离谱、荒谬或难以接受的内容时使用的吐槽起手式，语气偏惊讶和无语。", confidence=0.95),
    "差不多得了": make_runtime_catchphrase("用于制止过度复读、争论、玩梗或情绪输出，意思是提醒对方适可而止。", confidence=0.95),
    "疯狂星期四": make_runtime_catchphrase("肯德基星期四促销梗，常出现在长篇故事结尾并转向借钱或求 V 的荒诞文案。", category="copypasta", confidence=0.96),
    "你说得对，但是": make_runtime_catchphrase("常见反串或复制粘贴起手式，表面认可对方，随后突然切入夸张传教或长文。", category="copypasta", confidence=0.95),
    "动了XX的蛋糕": make_runtime_catchphrase("把失败或冲突荒诞地归因于触碰了某个群体利益，用于反串阴谋化解释。", category="abstract-rhetoric", confidence=0.9),
    "别急": make_runtime_catchphrase("常用于让对方不要急于反应或破防，也可作为轻度调侃式安抚。", confidence=0.95),
    "那咋了": make_runtime_catchphrase("用冷处理方式回应指责或质疑，表达不在乎、反问或摆烂态度。", confidence=0.92),
    "又幻想了": make_runtime_catchphrase("用于调侃过度脑补、自我代入或不现实的想象。", confidence=0.92),
    "玩原神玩的": make_runtime_catchphrase("面对各种离谱问题或异常行为时，荒诞地直接归因于玩某款游戏，属于经典甩锅解构梗。", category="gaming", confidence=0.95),
    "急了": make_runtime_catchphrase("网络对线中用于嘲讽对方情绪失控、失去理智的轻量还击词。", category="catchphrase", confidence=0.94),
    "破防": make_runtime_catchphrase("指心理防线被彻底击溃，因触及痛处或真相而产生强烈的情绪波动。", category="internet-culture", confidence=0.95),
    "红温": make_runtime_catchphrase("形容人在争论或游戏中因愤怒、着急而面红耳赤、血压上升的破防状态。", category="gaming", confidence=0.92),
    "小丑": make_runtime_catchphrase("自嘲或嘲讽他人自以为重要、付出真心却沦为滑稽可笑的笑柄。", category="internet-culture", confidence=0.94),
    "赢麻了": make_runtime_catchphrase("反串或自嘲式胜利狂欢，用于夸张地表达全方位获胜，现多带反讽意味。", category="abstract-rhetoric", confidence=0.93),
    "大赢特赢": make_runtime_catchphrase("赢学讽刺表达，用极度膨胀和确信的口吻宣布胜利，多用于反串。", category="abstract-rhetoric", confidence=0.92),
    "开战": make_runtime_catchphrase("社群吵架对线冲锋号，表达群内恢复热闹对喷的荒诞喜悦。", category="catchphrase", confidence=0.9),
    "复读机": make_runtime_catchphrase("指群聊中无脑复制粘贴同一句话进行刷屏或跟风的人。", category="internet-culture", confidence=0.95),
    "鼠鼠": make_runtime_catchphrase("当代年轻人的自嘲称谓，比喻自己生活在底层、胆小怯懦、卑微无助的处境。", category="internet-culture", confidence=0.94),
    "哈基米": make_runtime_catchphrase("原指赛马娘角色台词，后被泛化指代可爱小猫或宠物，带有荒诞戏谑感。", category="internet-culture", confidence=0.9),
    "神人": make_runtime_catchphrase("原指行为极其抽象、荒诞、出人意料的奇葩群体，兼具讽刺与调侃。", category="internet-culture", confidence=0.93),
    "小团体": make_runtime_catchphrase("指大群内部私下建立的小圈子，常用来吐槽自己被排挤或无法融入话题。", category="internet-culture", confidence=0.92),
    "发病": make_runtime_catchphrase("指在群聊或社交媒体上突然情绪失控、大段宣泄或写长篇情感小作文的行为。", category="internet-culture", confidence=0.93),
    "地狱笑话": make_runtime_catchphrase("以他人苦难、灾难或敏感话题为笑料的黑色幽默，争议极大且高度冒犯。", category="abstract-rhetoric", confidence=0.9),
    "上流": make_runtime_catchphrase("反讽自己或他人的虚伪做作，把低俗或日常琐事包装成高级阶层的荒谬举止。", category="abstract-rhetoric", confidence=0.9),
    "逆天": make_runtime_catchphrase("形容言论、行为或事件极其离谱、违背常理，令人难以置信。", category="catchphrase", confidence=0.95),
    "太对了哥": make_runtime_catchphrase("敷衍附和对方的经典应付话术，表面完全同意，实际表达懒得争论或看耍猴。", category="catchphrase", confidence=0.93),
    "抽象": make_runtime_catchphrase("当代互联网亚文化总称，指脱离常规逻辑、荒诞、解构一切严肃性的行为风格。", category="internet-culture", confidence=0.96),
    "带节奏": make_runtime_catchphrase("指蓄意挑起争议、引导群体舆论偏向、制造对立或引发互撕的行为。", category="internet-culture", confidence=0.94),
    "塌房": make_runtime_catchphrase("指偶像、公众人物或人设因丑闻败露而形象彻底幻灭崩塌。", category="internet-culture", confidence=0.92),
    "拷打": make_runtime_catchphrase("指在网络对线或争论中，针对对方言论漏洞进行无情且压倒性的质问与反驳。", category="internet-culture", confidence=0.93),
    "对线": make_runtime_catchphrase("借用游戏术语，指两人或阵营在社交平台正面对吵、互相输出观点的过程。", category="gaming", confidence=0.94),
    "典中典": make_runtime_catchphrase("经典中的经典，用于讽刺某种具有典型特征的离谱言论或刻板行为。", category="abstract-rhetoric", confidence=0.94),
    "乐子人": make_runtime_catchphrase("不站队、不严肃参与争论，纯粹把网络冲突当成娱乐和笑料看戏的人。", category="internet-culture", confidence=0.95),
    "入脑": make_runtime_catchphrase("形容某种思想、观点、旋律或人物深度占据大脑，达到狂热或魔怔状态。", category="gaming", confidence=0.91),
    "结晶": make_runtime_catchphrase("粉圈用语，指经历多次洗礼后提纯出来的狂热、盲目且极具攻击性的极端粉丝。", category="internet-culture", confidence=0.92),
    "厨力": make_runtime_catchphrase("二次元用语，指对某个角色或作品狂热喜爱的程度与投入的精力金钱。", category="gaming", confidence=0.92),
    "缝合": make_runtime_catchphrase("指将原本毫无关联的多款游戏、梗或文化元素强行拼接杂糅在一起的手法。", category="abstract-rhetoric", confidence=0.93),
    "下头": make_runtime_catchphrase("指原本兴致盎然或有好感，却因对方某句令人不适的话或行为瞬间扫兴反感。", category="internet-culture", confidence=0.93),
    "一眼顶真": make_runtime_catchphrase("利用藏族小伙丁真的谐音，调侃一眼就能看出来的造假、反串或假新闻。", category="internet-culture", confidence=0.93),
    "白日梦": make_runtime_catchphrase("指完全脱离现实、不切实际的自我幻想或过度脑补。", category="catchphrase", confidence=0.91),
    "爆金币": make_runtime_catchphrase("源于暗黑破坏神掉落金币，网络黑话中常指从长辈、父母或对方身上榨取财物。", category="internet-culture", confidence=0.91),
    "精神胜利": make_runtime_catchphrase("源于鲁迅阿Q正传，指在现实受挫时通过自我麻痹或口头便宜获得心理平衡。", category="abstract-rhetoric", confidence=0.92),
    "典急孝": make_runtime_catchphrase("对线三部曲缩写：经典、破防急了、孝子维护，用于给对方流水线扣帽子。", category="abstract-rhetoric", confidence=0.93),
    "谁问你了": make_runtime_catchphrase("极其霸道且不讲理的话语打断方式，用反问剥夺对方说话的合法性。", category="catchphrase", confidence=0.92),
    "给他们一点小小的": make_runtime_catchphrase("套用宏大叙事句式，狂妄宣布要用自己的某种特色给对方以震撼。", category="copypasta", confidence=0.91),
    "抛开事实不谈": make_runtime_catchphrase("讽刺无视客观真相、强行从道德制高点或情绪角度诡辩的荒谬论调。", category="abstract-rhetoric", confidence=0.94),
    "笑死，根本": make_runtime_catchphrase("用夸张的句式表达自己毫无感觉、毫不受影响，实则在嘴硬掩饰。", category="catchphrase", confidence=0.92),
    "感觉不如": make_runtime_catchphrase("任何事物都能强行对比并得出贬低结论的万能踩一捧一起手式。", category="gaming", confidence=0.93),
    "你最好真的是在说": make_runtime_catchphrase("当对方言论具有强烈的双关、隐喻或涉嫌违规时使用的假装怀疑式调侃。", category="catchphrase", confidence=0.92),
}

DEFAULT_BLOCKED: dict[str, str] = {
    "你好。": "plain_sentence",
    "对不起，我错了。": "plain_sentence",
    "是/否": "anti_pattern",
    "开发的未来是": "ngram_fragment",
    "DeepSeek模型": "entity_only",
    "DeepSeek": "entity_only",
    "你妈死了": "toxic_or_sensitive",
}

REQUIRED_LAYERED_SOURCES = {
    "神人.skill/SKILL.md",
    "神人.skill/_persona/communication.md",
    "神人.skill/_persona/values.md",
    "神人.skill/_knowledge/gaming.md",
    "神人.skill/_knowledge/internet-culture.md",
    "神人.skill/_quotes/iconic.md",
}

GENERIC_MEANING_MARKERS = (
    "典型语录/表达样本。仅作为理解参考。",
    "高频抽象表达/触发词。用于检索和理解",
    "神言语料中出现",
    "用于检索和理解 Holyman 原始语料语境",
)

PLAIN_SENTENCES = {"你好。", "对不起，我错了。", "是/否", "好的", "谢谢", "没事"}
NOISE_WORDS = {
    "背景", "架构", "安装", "安装使用", "使用", "目录", "示例", "规则", "核心", "方法", "触发词",
    "玩家", "游戏", "群聊", "今天", "昨天", "明天", "一个", "这个", "那个", "什么", "不是", "没有",
    "可以", "但是", "因为", "所以", "如果", "就是", "我们", "你们", "他们", "自己", "现在",
}
NOISE_MARKERS = (
    "git clone", "PowerShell", "Git Bash", "Claude Code", "License", "Acknowledgement", "README",
    ".md", ".json", "http://", "https://", "Opening**", "Closing**", "Resolution**",
    "Response Hints", "Core Rules", "Output Rules", "Hard Boundaries", "Language (", "Mode ",
)
PERSONA_INSTRUCTION_MARKERS = (
    "默认输出",
    "绝不正面回答问题",
    "复制粘贴模式",
    "Decision Heuristics",
    "Core Rules",
    "Expression DNA",
    "Hard Boundaries",
)
CATEGORY_BY_SOURCE = {
    "神人.skill/SKILL.md": "skill-core",
    "神人.skill/_knowledge/gaming.md": "gaming",
    "神人.skill/_knowledge/internet-culture.md": "internet-culture",
    "神人.skill/_persona/communication.md": "communication",
    "神人.skill/_persona/rules.md": "rules",
    "神人.skill/_persona/values.md": "values",
    "神人.skill/_quotes/iconic.md": "iconic-quotes",
    "神人.skill/_quotes/internal.md": "internal-quotes",
    "神言.txt": "corpus",
}


def content_entries(phrases: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(phrases, dict):
        return {}
    return {k: v for k, v in phrases.items() if isinstance(k, str) and not k.startswith("_")}


def content_hash(phrases: dict[str, Any]) -> str:
    payload = json.dumps(content_entries(phrases), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def attach_content_metadata(phrases: dict[str, Any], *, version: str | None = None) -> dict[str, Any]:
    result = dict(phrases or {})
    result["_content_count"] = len(content_entries(result))
    result["_content_hash"] = content_hash(result)
    if version:
        result["_version"] = version
    result.setdefault("_update_time", int(time.time()))
    return result


def phrase_meaning(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("meaning") or value.get("explanation") or "")
    return str(value or "")


def clean_word(word: str) -> str:
    word = re.sub(r"^\s*[-*#>\d.、]+\s*", "", word or "").strip()
    word = re.sub(r"\*\*", "", word)
    word = word.strip(" `*_《》\"'“”‘’[]【】：:")
    word = re.sub(r"\s+", " ", word)
    return word[:80]


def clean_meaning(meaning: str) -> str:
    return re.sub(r"\s+", " ", meaning or "").strip()[:500]


def has_unbalanced_brackets(word: str) -> bool:
    pairs = [("（", "）"), ("(", ")"), ("[", "]"), ("【", "】")]
    return any((word or "").count(left) != (word or "").count(right) for left, right in pairs)


def is_generic_meaning(meaning: str) -> bool:
    return any(marker in (meaning or "") for marker in GENERIC_MEANING_MARKERS)


def is_entity_only(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{5,}", word or ""))


def is_plain_sentence(word: str) -> bool:
    if word in PLAIN_SENTENCES:
        return True
    return bool(re.search(r"[。！？!?]$", word or "") and len(word or "") <= 12)


def is_valid_phrase_entry(word: str, value: Any, blocked: dict[str, str] | None = None) -> tuple[bool, str]:
    blocked = blocked or DEFAULT_BLOCKED
    word = clean_word(word)
    meaning = phrase_meaning(value)
    kind = value.get("kind") if isinstance(value, dict) else "legacy"
    if not word or len(word) < 2:
        return False, "empty_or_short"
    if word in blocked:
        return False, blocked[word]
    if word in NOISE_WORDS:
        return False, "noise_word"
    if any(marker.lower() in word.lower() for marker in NOISE_MARKERS):
        return False, "noise_marker"
    if kind == "corpus_frequency":
        return False, "corpus_frequency"
    if has_unbalanced_brackets(word):
        return False, "truncated"
    if is_plain_sentence(word):
        return False, "plain_sentence"
    if is_entity_only(word):
        return False, "entity_only"
    if is_generic_meaning(meaning):
        return False, "generic_meaning"
    if re.fullmatch(r"[A-Za-z0-9 /_().~↑↓<>=*:-]+", word):
        return False, "entity_only"
    if len(word) > 30 and word not in CORE_CURATED_PHRASES:
        return False, "too_long"
    if not clean_meaning(meaning):
        return False, "empty_meaning"
    return True, "ok"


def make_phrase(word: str, meaning: str, *, category: str, source: str, kind: str = "curated_phrase", confidence: float = 0.72) -> dict[str, Any]:
    return {
        "meaning": clean_meaning(meaning),
        "category": category,
        "source": source,
        "kind": kind,
        "confidence": confidence,
        "safety_level": "safe_reference",
        "layer": "catchphrase",
        "reference_only": True,
        "runtime_match": kind in {"curated_phrase", "manual", "legacy"},
    }


def add_phrase(phrases: dict[str, Any], word: str, meaning: str, *, category: str, source: str, kind: str = "curated_phrase", confidence: float = 0.72, blocked: dict[str, str] | None = None) -> None:
    word = clean_word(word)
    value = make_phrase(word, meaning, category=category, source=source, kind=kind, confidence=confidence)
    ok, _ = is_valid_phrase_entry(word, value, blocked)
    if ok and word not in phrases:
        phrases[word] = value


def parse_curated_phrases(fetched: dict[str, str], existing_phrases: dict[str, Any] | None = None) -> dict[str, Any]:
    blocked = dict(DEFAULT_BLOCKED)
    phrases: dict[str, Any] = {word: dict(value) for word, value in CORE_CURATED_PHRASES.items()}

    for word, value in content_entries(existing_phrases or {}).items():
        normalized = clean_word(word)
        if normalized in phrases:
            continue
        source = value.get("source") if isinstance(value, dict) else "legacy"
        kind = value.get("kind") if isinstance(value, dict) else "legacy"
        # Old generated assets wrongly promoted Holyman skill rules and knowledge concepts
        # into activatable phrases. Keep only true legacy/manual entries here; fresh
        # Holyman markdown remains reference-only via concepts/examples/candidates.
        if isinstance(source, str) and source.startswith("神人.skill/"):
            continue
        if kind in {"bold_term", "colon_term", "corpus_frequency", "quote_term", "corpus_quote"}:
            continue
        ok, _ = is_valid_phrase_entry(normalized, value, blocked)
        if ok:
            meaning = phrase_meaning(value)
            category = value.get("category") if isinstance(value, dict) else "legacy"
            add_phrase(phrases, normalized, meaning, category=category or "legacy", source=source or "legacy", kind=kind or "legacy", blocked=blocked)

    for word, value in CORE_CURATED_PHRASES.items():
        phrases[word] = dict(value)
    return phrases


def parse_concepts(fetched: dict[str, str]) -> list[dict[str, Any]]:
    """抽取只读文化概念。

    从 raw markdown 中抽取章节概念与文化知识，收集真实的段落与列表正文作为摘要。
    严格排除 pure-meta 来源（如 _meta/sources.md），且绝不为空内容填充假模板摘要。
    """
    concepts: list[dict[str, Any]] = []
    for source, text in (fetched or {}).items():
        if (
            source == "README.md"
            or source.startswith("神人.skill/_quotes/")
            or source.startswith("神人.skill/_meta/")
            or source == "神言.txt"
        ):
            continue
        lines = (text or "").splitlines()
        total_lines = len(lines)
        idx = 0
        while idx < total_lines:
            raw = lines[idx]
            line = raw.strip()
            marker = _heading_marker(line)
            if marker is None:
                idx += 1
                continue
            title = clean_word(line[marker:])
            heading_level = marker - 1  # ##=2, ###=3
            if not title or title in NOISE_WORDS:
                idx += 1
                continue
            # 收集该章节正文，直到遇到下一个任何级别的标题，或 frontmatter/分隔符
            summary_lines: list[str] = []
            idx += 1
            while idx < total_lines:
                next_raw = lines[idx]
                next_line = next_raw.strip()
                if _heading_marker(next_line) is not None:
                    break
                if next_line == "---":
                    idx += 1
                    break
                clean_content = next_line.lstrip("-* ").strip()
                if clean_content and not clean_content.startswith("#") and not clean_content.startswith("```"):
                    summary_lines.append(clean_content)
                idx += 1
                if len(summary_lines) >= 8:
                    while idx < total_lines:
                        chk = lines[idx].strip()
                        if _heading_marker(chk) is not None:
                            break
                        idx += 1
                    break

            if not summary_lines:
                # 若正文为空，不生成假摘要概念
                continue

            summary_text = " ".join(summary_lines)
            summary = clean_meaning(summary_text)
            if not summary:
                continue

            concepts.append({
                "id": f"{source}.{len(concepts) + 1}",
                "title": title,
                "summary": summary,
                "source": source,
                "tags": [CATEGORY_BY_SOURCE.get(source, "unknown")],
                "confidence": 0.78,
                "source_category": CATEGORY_BY_SOURCE.get(source, "unknown"),
                "layer": "concept",
                "reference_only": True,
                "runtime_match": False,
            })
    return concepts


def _heading_marker(line: str) -> int | None:
    """返回标题标记长度（`## `=3、`### `=4、`#### `=5），非标题返回 None。"""
    if not line.startswith("#"):
        return None
    hashes = len(line) - len(line.lstrip("#"))
    if hashes < 2 or hashes > 3:
        return None
    if len(line) <= hashes or line[hashes] != " ":
        return None
    return hashes + 1


def parse_examples(fetched: dict[str, str], phrases: dict[str, Any]) -> list[dict[str, Any]]:
    """抽取只读语录/声音样本与对话示例。

    准确识别：
    1. `iconic.md` 中的 `数字. > "正文"`，并将紧随其后的 `> — 出处说明` 绑定为 attribution；
    2. 对话围栏中的 Char 回复提取为 dialogue 示例，排除 User 问话；
    3. 排除纯导语（筛选标准）与 `//` 注释；
    4. 对成对最外层引号进行精准剥除，保留内部引用。
    """
    examples: list[dict[str, Any]] = []
    terms = list(content_entries(phrases).keys())

    for source, text in (fetched or {}).items():
        if not (source.startswith("神人.skill/_quotes/") or source.endswith("communication.md")):
            continue
        category = CATEGORY_BY_SOURCE.get(source, "unknown")
        lines = (text or "").splitlines()
        total_lines = len(lines)
        idx = 0
        in_fence = False
        headings: list[tuple[int, str]] = []
        sample_block = False
        dialogue_role = ""

        while idx < total_lines:
            line = lines[idx].strip()

            if not in_fence:
                heading = re.match(r"^(#{1,6})\s+(.+)$", line)
                if heading:
                    level = len(heading.group(1))
                    headings = [(n, title) for n, title in headings if n < level]
                    headings.append((level, heading.group(2)))
                    sample_block = False
                    idx += 1
                    continue
                if line.startswith("**Real Sentence Samples**"):
                    sample_block = True
                    idx += 1
                    continue
                if line == "---" or line.startswith("**"):
                    sample_block = False

            section_type = ""
            for level, title in headings:
                if level == 1:
                    continue
                if "Signature Patterns" in title or "标志性句式模板" in title:
                    section_type = "template"
                elif "Example Exchanges" in title or "双向对话示例" in title:
                    section_type = "dialogue"
            # 无标题的独立语录片段兼容旧导入；完整文档的 H1 导语不属于语录章节。
            quote_section = source.startswith("神人.skill/_quotes/") and (
                not headings or any(n >= 2 for n, _ in headings)
            )

            if line.startswith("```"):
                in_fence = not in_fence
                dialogue_role = ""
                idx += 1
                continue

            # 围栏只是格式；句式、真实样本与对话由章节上下文决定。
            if in_fence:
                if line.startswith("//"):
                    idx += 1
                    continue
                if section_type == "dialogue":
                    if line.startswith(("User:", "User：")):
                        dialogue_role = "User"
                        idx += 1
                        continue
                    if line.startswith(("Char:", "Char：")):
                        dialogue_role = "Char"
                        line = line[5:].strip()
                    if dialogue_role != "Char":
                        idx += 1
                        continue
                    content = _strip_outer_quotes(line)
                    example_type = "dialogue"
                    attribution = "Example Exchanges (Char)"
                elif section_type == "template" or sample_block or quote_section:
                    content = _strip_outer_quotes(line)
                    example_type = section_type or "voice_sample"
                    attribution = ""
                else:
                    idx += 1
                    continue
                if content and not is_identity_contamination(content):
                    linked = [t for t in terms if t and t in content][:5]
                    examples.append({
                        "text": content,
                        "attribution": attribution,
                        "example_type": example_type,
                        "linked_terms": linked,
                        "category": category,
                        "source": source,
                        "safe_for_prompt": example_type != "template" and len(content) <= 180,
                        "layer": "quotes_knowledge",
                        "reference_only": True,
                        "runtime_match": False,
                    })
                idx += 1
                continue

            # 跳过注释与导语
            if line.startswith("//") or "筛选标准：" in line or "展示在不同场景下如何用" in line:
                idx += 1
                continue

            # 匹配形如 "1. > \"正文\"" 或 "1. > 正文" 的标志性编号语录
            numbered_quote_match = re.match(r"^\d+\.\s*>\s*(.+)$", line)
            if numbered_quote_match and quote_section and not section_type:
                quote_raw = numbered_quote_match.group(1).strip()
                # 去除最外层的一对成对引号（保留内部嵌套）
                quote_text = _strip_outer_quotes(quote_raw)
                attribution = ""
                # 预读下一行检查是否有 `> —` 出处标注
                if idx + 1 < total_lines:
                    next_line = lines[idx + 1].strip()
                    attr_match = re.match(r"^>\s*—\s*(.+)$", next_line)
                    if attr_match:
                        attribution = attr_match.group(1).strip()
                        idx += 1  # 消费掉出处行，避免作为独立语录被重复提取

                if quote_text and not is_identity_contamination(quote_text):
                    linked = [t for t in terms if t and t in quote_text][:5]
                    examples.append({
                        "text": quote_text,
                        "attribution": attribution,
                        "example_type": "voice_sample",
                        "linked_terms": linked,
                        "category": category,
                        "source": source,
                        "safe_for_prompt": len(quote_text) <= 150,
                        "layer": "quotes_knowledge",
                        "reference_only": True,
                        "runtime_match": False,
                    })
                idx += 1
                continue

            # 匹配独立出处说明（若还有孤立的 `> —`，不单独当成语录）
            if re.match(r"^>\s*—", line):
                idx += 1
                continue

            # 普通引用行 `> 正文`
            if line.startswith(">") and quote_section and not section_type:
                quote_text = _strip_outer_quotes(line.lstrip(">").strip())
                if quote_text and len(quote_text) > 10 and not is_identity_contamination(quote_text):
                    linked = [t for t in terms if t and t in quote_text][:5]
                    examples.append({
                        "text": quote_text,
                        "attribution": "",
                        "example_type": "voice_sample",
                        "linked_terms": linked,
                        "category": category,
                        "source": source,
                        "safe_for_prompt": len(quote_text) <= 120,
                        "layer": "quotes_knowledge",
                        "reference_only": True,
                        "runtime_match": False,
                    })
                idx += 1
                continue

            # communication 里的列表句式
            if section_type == "template" and line.startswith(("- ", "* ")) and len(line) > 25:
                content = _strip_outer_quotes(line[2:].strip())
                # 排除纯语言规范性列表项
                if not content.startswith("**") and not is_identity_contamination(content):
                    linked = [t for t in terms if t and t in content][:5]
                    examples.append({
                        "text": content,
                        "attribution": "",
                        "example_type": "template",
                        "linked_terms": linked,
                        "category": category,
                        "source": source,
                        "safe_for_prompt": False,
                        "layer": "quotes_knowledge",
                        "reference_only": True,
                        "runtime_match": False,
                    })
                idx += 1
                continue

            idx += 1

    return examples[:400]


def _strip_outer_quotes(text: str) -> str:
    """仅去除最外层成对的一对包裹引号，保留内部嵌套的引号。"""
    t = text.strip()
    if len(t) >= 2:
        if (t.startswith('"') and t.endswith('"')) or (t.startswith('“') and t.endswith('”')):
            return t[1:-1].strip()
        if t.startswith("'") and t.endswith("'"):
            return t[1:-1].strip()
    return t


def parse_corpus(corpus_data: str) -> list[dict[str, Any]]:
    texts: list[str] = []
    try:
        parsed = json.loads(corpus_data or "[]")
        items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
        if isinstance(items, list):
            for item in items:
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                if text.strip():
                    texts.append(text.strip())
        elif isinstance(parsed, str) and parsed.strip():
            texts.append(parsed.strip())
    except Exception:
        texts = [line.strip() for line in (corpus_data or "").splitlines() if line.strip()]
    return [
        {
            "id": f"holyman-corpus-{idx:04d}",
            "text": text,
            "source": "神言.txt",
            "length": len(text),
            "tags": ["corpus"],
            "risk_flags": ["reference_only"] + (["long_text"] if len(text) > 120 else []),
            "safe_for_prompt": False,
            "layer": "corpus",
            "reference_only": True,
            "runtime_match": False,
        }
        for idx, text in enumerate(texts, start=1)
    ]


def generate_candidates(corpus: list[Any], phrases: dict[str, Any], blocked: dict[str, str] | None = None) -> list[dict[str, Any]]:
    blocked = blocked or DEFAULT_BLOCKED
    phrase_words = set(content_entries(phrases).keys())
    counter: dict[str, int] = {}
    for item in corpus:
        text = item.get("text", "") if isinstance(item, dict) else str(item)
        for quoted in re.findall(r"[\"“「『]([^\"”」』]{2,24})[\"”」』]", text):
            word = clean_word(quoted)
            if word not in phrase_words and is_valid_phrase_entry(word, {"meaning": "候选", "kind": "candidate"}, blocked)[0]:
                counter[word] = counter.get(word, 0) + 1
        for word in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,12}", text):
            word = clean_word(word)
            if word not in phrase_words and is_valid_phrase_entry(word, {"meaning": "候选", "kind": "candidate"}, blocked)[0]:
                counter[word] = counter.get(word, 0) + 1
    return [
        {
            "id": f"holyman-candidate-{idx:04d}",
            "word": word,
            "reason": "corpus_candidate",
            "count": count,
            "source": "神言.txt",
            "status": "pending_review",
            "reject_reason": "",
            "layer": "candidate",
            "reference_only": True,
            "runtime_match": False,
        }
        for idx, (word, count) in enumerate(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:300], start=1)
    ]


def build_manifest(fetched: dict[str, str], *, remote_version: str = "") -> dict[str, Any]:
    files = []
    for path, content in sorted((fetched or {}).items()):
        data = (content or "").encode("utf-8")
        files.append({
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "parse_status": "ok" if content is not None else "missing",
        })
    return {
        "source": "https://github.com/ykdeso/holyman-skills",
        "remote_version": remote_version,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def _declared_corpus_count(raw_sources: dict[str, str]) -> int | None:
    for name in ("README.md", "神人.skill/_meta/sources.md"):
        text = raw_sources.get(name, "") if isinstance(raw_sources, dict) else ""
        match = re.search(r"神言\.txt[（(]\s*(\d+)\s*条", text or "")
        if match:
            return int(match.group(1))
    return None


def _source_corpus_items_count(raw_sources: dict[str, str]) -> int | None:
    raw = raw_sources.get("神言.txt", "") if isinstance(raw_sources, dict) else ""
    if not raw:
        return None
    parsed = json.loads(raw)
    items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return None
    return sum(1 for item in items if ((item.get("text", "") if isinstance(item, dict) else str(item)).strip()))


def quality_report(assets: dict[str, Any]) -> dict[str, Any]:
    phrases = content_entries(assets.get("phrases") or {})
    blocked = assets.get("blocked") or DEFAULT_BLOCKED
    raw_sources = assets.get("raw_sources") or {}
    declared_corpus_count = _declared_corpus_count(raw_sources)
    try:
        source_corpus_items_count = _source_corpus_items_count(raw_sources)
    except Exception:
        source_corpus_items_count = None
    parsed_corpus_count = len(assets.get("corpus") or [])
    raw_parsed_mismatch = source_corpus_items_count is not None and source_corpus_items_count != parsed_corpus_count
    declared_source_mismatch = declared_corpus_count is not None and source_corpus_items_count is not None and declared_corpus_count != source_corpus_items_count
    concepts_list = assets.get("concepts") or []
    examples_list = assets.get("examples") or []
    template_summaries = sum(1 for c in concepts_list if "文化概念" in str(c.get("summary") or ""))
    orphan_attributions = sum(1 for e in examples_list if str(e.get("text") or "").strip().startswith(("—", "- —")))

    errors = {
        "corpus_frequency_in_phrases": 0,
        "generic_meaning_in_phrases": 0,
        "unbalanced_bracket_in_phrases": 0,
        "plain_sentence_in_phrases": 0,
        "blocked_phrase_in_phrases": 0,
        "entity_only_in_phrases": 0,
        "missing_core_terms": 0,
        "missing_layered_sources": 0,
        "persona_instruction_in_phrases": 0,
        "template_summaries_in_concepts": template_summaries,
        "orphan_attributions_in_examples": orphan_attributions,
        "raw_parsed_corpus_mismatch": 1 if raw_parsed_mismatch else 0,
    }
    for word, value in phrases.items():
        meaning = phrase_meaning(value)
        kind = value.get("kind") if isinstance(value, dict) else "legacy"
        haystack = f"{word} {meaning}"
        if any(marker in haystack for marker in PERSONA_INSTRUCTION_MARKERS):
            errors["persona_instruction_in_phrases"] += 1
        if kind == "corpus_frequency":
            errors["corpus_frequency_in_phrases"] += 1
        if is_generic_meaning(meaning):
            errors["generic_meaning_in_phrases"] += 1
        if has_unbalanced_brackets(word):
            errors["unbalanced_bracket_in_phrases"] += 1
        if is_plain_sentence(word):
            errors["plain_sentence_in_phrases"] += 1
        if word in blocked:
            errors["blocked_phrase_in_phrases"] += 1
        if is_entity_only(word):
            errors["entity_only_in_phrases"] += 1
    for word, value in CORE_CURATED_PHRASES.items():
        if word not in phrases or is_generic_meaning(phrase_meaning(phrases.get(word))) or len(phrase_meaning(phrases.get(word))) < 12:
            errors["missing_core_terms"] += 1
    manifest = assets.get("manifest") or {}
    manifest_files = manifest.get("files") if isinstance(manifest, dict) else []
    if manifest_files:
        ok_paths = {
            item.get("path")
            for item in manifest_files
            if isinstance(item, dict) and item.get("parse_status") == "ok"
        }
        errors["missing_layered_sources"] = len(REQUIRED_LAYERED_SOURCES - ok_paths)
    status = "ready" if phrases and all(count == 0 for count in errors.values()) else "blocked"
    if raw_parsed_mismatch:
        corpus_count_note = f"Raw 神言.txt contains {source_corpus_items_count} non-empty items, but corpus.json parsed {parsed_corpus_count}."
    elif declared_source_mismatch:
        corpus_count_note = f"README declares {declared_corpus_count} items; current raw JSON contains {source_corpus_items_count} non-empty items and all were parsed."
    elif source_corpus_items_count is not None:
        corpus_count_note = f"Raw JSON contains {source_corpus_items_count} non-empty items and all were parsed."
    else:
        corpus_count_note = "Corpus source count unavailable; using parsed corpus count only."
    return {
        "status": status,
        "phrases_count": len(phrases),
        "concepts_count": len(assets.get("concepts") or []),
        "examples_count": len(assets.get("examples") or []),
        "corpus_count": parsed_corpus_count,
        "candidates_count": len(assets.get("candidates") or []),
        "blocked_count": len(blocked),
        "declared_corpus_count": declared_corpus_count,
        "source_corpus_items_count": source_corpus_items_count,
        "parsed_corpus_count": parsed_corpus_count,
        "corpus_count_mismatch": bool(raw_parsed_mismatch or declared_source_mismatch),
        "corpus_count_note": corpus_count_note,
        "errors": errors,
        "generated_at": int(time.time()),
    }


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
