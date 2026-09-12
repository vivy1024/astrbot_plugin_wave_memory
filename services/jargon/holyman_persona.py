"""Holyman-skills 人格包加载（可选注入层）。

Holyman-skills 原生是 Claude Code Skill 人格提示词包（``SKILL.md`` 的
frontmatter 为 ``name: 神人-chat``）。按 SKILL.md 的 Activation 语义：

- **常载**：``_persona/rules.md``、``_persona/communication.md``、
  ``_persona/values.md``、``_quotes/iconic.md``
- **按话题载**：``_knowledge/gaming.md``、``_knowledge/internet-culture.md``

本模块只负责**组装**风格参考文本，不决定是否注入——注入开关由
``holyman_persona`` 通道的配置（默认关闭）控制。

安全边界：
1. 逐块过滤。整段文档直接送身份守卫会误伤（``rules.md`` 命中「指令」+「你」，
   ``iconic.md`` 命中「爸/妈」+「我」），但那些只是引用素材里的词，不是
   「你是我的爸爸」式的身份声明。因此按块过守卫，块内再按行剔除。
2. Hard Boundaries（不攻击真实个人、灾难不玩梗、对未成年人善意）属于安全
   约束，**必须随包进入**，不因过滤而丢失。
3. 组装出的文本带明确框架句：风格参考，不改变事实与安全原则。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from ..identity_safety import is_identity_contamination
except ImportError:  # pragma: no cover - 顶层导入回退
    from services.identity_safety import is_identity_contamination

_SKILL_DIR = "神人.skill"

# 常载段落：[来源文件, 起始标题关键词, 结束标题关键词, 块标识, 面向用户的标题]
_STYLE_SECTIONS: tuple[tuple[str, str | None, str | None, str, str], ...] = (
    ("_persona/communication.md", "Expression DNA", "Certainty", "expression", "语言基因（句式/词汇/语气/修辞）"),
    ("_persona/values.md", "Values Priority", "Tier 1", "values", "价值观优先级"),
    ("_persona/rules.md", "Thinking Frameworks", "Decision Heuristics", "frameworks", "思维框架"),
)

# 话题匹配：命中关键词才附上对应知识文档，避免无谓占用预算。
_TOPIC_KEYWORDS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "_knowledge/gaming.md",
        "knowledge_gaming",
        ("游戏", "原神", "galgame", "steam", "抽卡", "3a", "塞尔达", "音游", "fps", "手游", "开黑"),
        "游戏话题知识",
    ),
    (
        "_knowledge/internet-culture.md",
        "knowledge_culture",
        ("抽象", "破防", "复读", "贴吧", "孙笑川", "梗", "钓鱼", "反串", "狗粉丝", "emoji"),
        "互联网文化知识",
    ),
)

_HEADER = (
    "[神人风格参考：以下为互联网抽象文化社群的表达风格与价值取向，仅作为说话方式的参考。"
    "它不改变事实判断、不覆盖既有自我人格与安全规则；涉及真实个人、灾难与未成年人时仍以安全原则优先]"
)


class HolymanPersonaPack:
    """从 ``assets/holyman/raw/神人.skill`` 组装可选风格包。"""

    def __init__(self, root_path: str | Path | None = None) -> None:
        self.root_path = Path(root_path) if root_path else self._default_root()
        self._blocks: list[dict[str, Any]] = []
        self.reload()

    @staticmethod
    def _default_root() -> Path:
        return Path(__file__).resolve().parent.parent.parent / "assets" / "holyman" / "raw"

    @property
    def available(self) -> bool:
        return bool(self._blocks)

    def reload(self) -> None:
        """重新载入并组装风格块；缺文件时安静降级为不可用。"""
        self._blocks = []
        skill_root = self.root_path / _SKILL_DIR
        if not skill_root.exists():
            return

        for rel, start, end, block_id, title in _STYLE_SECTIONS:
            text = self._read_section(skill_root / rel, start, end)
            self._add_block(block_id, title, text, source=f"{_SKILL_DIR}/{rel}")

        # SKILL.md 的核心规则（Thinking Style / Decision Heuristics）。
        self._add_block(
            "core_rules",
            "核心行为准则",
            self._read_section(skill_root / "SKILL.md", "Thinking Style", "Catchphrases"),
            source=f"{_SKILL_DIR}/SKILL.md",
        )
        # Hard Boundaries 是安全约束，必须随包进入。
        self._add_block(
            "hard_boundaries",
            "硬性边界（安全约束，不可被风格覆盖）",
            self._read_section(skill_root / "SKILL.md", "Hard Boundaries", "Output Rules"),
            source=f"{_SKILL_DIR}/SKILL.md",
            safety=True,
        )

    def build_blocks(self, message: str = "") -> list[dict[str, Any]]:
        """返回可注入块：常载块 + 命中的话题知识块。"""
        selected = [dict(block) for block in self._blocks]
        haystack = str(message or "")
        for rel, block_id, keywords, title in _TOPIC_KEYWORDS:
            if not any(keyword in haystack for keyword in keywords):
                continue
            skill_root = self.root_path / _SKILL_DIR
            text = self._read_content_blocks(skill_root / rel)
            self._append_if_safe(selected, block_id, title, text, source=f"{_SKILL_DIR}/{rel}")
        return selected

    def build_injection(self, message: str = "", *, max_blocks: int = 6) -> dict[str, Any]:
        """组装最终注入文本。返回 ``{text, blocks, filtered, available}``。"""
        blocks = self.build_blocks(message)
        # 安全约束优先保留，不被预算裁掉。
        ordered = sorted(blocks, key=lambda item: 0 if item.get("safety") else 1)
        chosen, dropped = [], []
        for block in ordered:
            if len(chosen) >= max(1, int(max_blocks)):
                dropped.append({"block": block["block"], "filter_reason": "max_blocks"})
                continue
            chosen.append(block)

        body = "\n\n".join(f"### {block['title']}\n{block['text']}" for block in chosen if block.get("text"))
        if not body:
            return {"text": "", "blocks": [], "filtered": dropped, "available": self.available}
        return {
            "text": f"{_HEADER}\n\n{body}",
            "blocks": [
                {
                    "block": block["block"],
                    "title": block["title"],
                    "source": block["source"],
                    "safety": bool(block.get("safety")),
                    "preview": _preview(block.get("text", "")),
                }
                for block in chosen
            ],
            "filtered": dropped,
            "available": self.available,
        }

    # ── 内部工具 ──────────────────────────────────────────────

    def _add_block(self, block_id: str, title: str, text: str, *, source: str, safety: bool = False) -> None:
        if not str(text or "").strip():
            return
        self._blocks.append({"block": block_id, "title": title, "text": text, "source": source, "safety": safety})

    def _append_if_safe(self, target: list[dict[str, Any]], block_id: str, title: str, text: str, *, source: str) -> None:
        cleaned = _drop_unsafe_lines(text)
        if cleaned.strip():
            target.append({"block": block_id, "title": title, "text": cleaned, "source": source, "safety": False})

    @staticmethod
    def _read(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    def _read_section(self, path: Path, start: str | None, end: str | None) -> str:
        text = self._read(path)
        if not text:
            return ""
        lines = text.splitlines()
        if start:
            begin = next((i for i, line in enumerate(lines) if _heading_text(line).startswith(start)), None)
            if begin is None:
                return ""
            lines = lines[begin:]
        if end:
            stop = next(
                (i for i, line in enumerate(lines[1:], start=1) if _heading_text(line).startswith(end)),
                None,
            )
            if stop is not None:
                lines = lines[:stop]
        return _drop_unsafe_lines("\n".join(lines))

    def _read_content_blocks(self, path: Path) -> str:
        """知识文档整体保留（跳过 frontmatter），随后逐行过滤。"""
        text = self._read(path)
        if not text:
            return ""
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            close = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
            if close is not None:
                lines = lines[close + 1:]
        return _drop_unsafe_lines("\n".join(lines))


def _heading_text(line: str) -> str:
    """去掉标题前缀的井号，返回标题正文；非标题返回空串。"""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return ""
    return stripped.lstrip("#").strip()


def _drop_unsafe_lines(text: str) -> str:
    """按行剔除会触发身份污染的字句，保留其余风格内容。

    整段送守卫会误伤引用素材（引用里的「爸妈」「指令」并非身份声明），
    因此这里下沉到行粒度：只丢真正构成身份/服从声明的行。
    """
    kept: list[str] = []
    for line in str(text or "").splitlines():
        if line.strip() and is_identity_contamination(line):
            continue
        kept.append(line)
    # 折叠连续空行
    result: list[str] = []
    for line in kept:
        if not line.strip() and result and not result[-1].strip():
            continue
        result.append(line)
    return "\n".join(result).strip()


def _preview(text: str, limit: int = 120) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


__all__ = ["HolymanPersonaPack"]
