"""梗百科的内容安全过滤。

神人包（``assets/holyman/raw``）是网络抽象文化的原始合集，其中夹着攻击真实个人、
拿真实灾难/死亡/战争开玩笑、涉及未成年人、露骨性内容，以及伪装成文案的提示注入。
词条整理、查询工具返回、用梗提示三处都用这里的同一套规则：

- ``vetted=True``：人工整理过的词条释义与例句。提到真实人物本身不算违规
  （如「一眼顶真」解释谐音来源），只有同时出现攻击性字眼才拦；
- ``vetted=False``：未经整理的原始段落与 ``神言.txt`` 语料。只要点名真实人物就整条拦下，
  因为机器判断不了“点名”是不是在攻击。

返回违规类别（字符串）或 ``None``。类别名稳定，供测试与观测使用。
"""

from __future__ import annotations

import re

try:
    from ..identity_safety import is_identity_contamination
except ImportError:  # pragma: no cover - 顶层导入回退
    from services.identity_safety import is_identity_contamination

from .holyman_assets import DEFAULT_BLOCKED

CATEGORY_LABELS: dict[str, str] = {
    "identity_contamination": "身份污染",
    "prompt_injection": "疑似提示注入",
    "real_disaster_death_war": "拿真实灾难/死亡/战争开玩笑",
    "minor": "涉及未成年人",
    "real_person": "攻击或点名真实具体个人",
    "explicit_sexual": "露骨性内容",
    "discrimination": "地域/民族/性别认同歧视",
    "political": "政治敏感",
    "blocked_phrase": "屏蔽词",
}

# 语料与神人包里出现过、或抽象圈常被点名的真实人物（含虚拟主播背后的真人形象）。
# 只用于“拦”，不是完整名单；宁可多拦，不能漏放。
REAL_PERSON_NAMES: tuple[str, ...] = (
    "丁真", "王源", "马嘉祺", "馬嘉祺", "严浩翔", "丁程鑫", "刘耀文", "张真源", "宋亚轩", "贺峻霖",
    "易烊千玺", "千玺", "王俊凯", "孙笑川", "带带大师兄", "户晨风", "周杰伦", "东北雨姐",
    "雷军", "梁文峰", "籽岷", "张益唐", "黄馨", "妖刀", "少羽", "uzi", "嘉然", "向晚",
    "永雏塔菲", "塔菲", "蔡徐坤", "峰哥", "马保国", "乔碧萝", "卢本伟", "药水哥", "李佳琦",
    "马云", "马化腾", "刘强东", "王思聪", "罗永浩", "吴亦凡", "杨笠", "特朗普", "拜登",
    "普京", "泽连斯基", "毛泽东", "习近平", "李克强", "江泽民", "胡锦涛", "bin哥",
)
_HOSTILE_MARKERS: tuple[str, ...] = (
    "傻", "蠢", "弱智", "脑残", "低能", "垃圾", "恶心", "滚", "骂", "去死", "丑", "狗东西",
    "贱", "婊", "煞笔", "sb", "操", "艹", "妈的", "废物", "羞辱", "猥琐", "抽烟", "私生",
)

# 真实灾难、战争与死亡。只收具体事件与死亡字眼，不收“灾难”“苦难”这类抽象词，
# 以免把「地狱笑话」这类释义本身误伤。
_DISASTER_WAR_MARKERS: tuple[str, ...] = (
    "广岛", "长崎", "原子弹", "核弹", "核武器", "南京大屠杀", "大屠杀", "屠杀", "汶川", "地震",
    "海啸", "空难", "坠机", "911", "九一一", "恐袭", "二战", "一战", "抗战", "日本鬼子", "侵华",
    "战争", "疫情", "新冠", "核泄漏", "福岛", "切尔诺贝利", "纳粹", "集中营",
)
_DEATH_MARKERS: tuple[str, ...] = (
    "死掉", "去世", "遇难", "身亡", "丧生", "自杀", "自残", "割手", "割腕", "跳楼",
    "尸体", "干尸", "骨灰", "遗体", "花圈", "你妈死", "死全家",
)
# 「死了」单独判断：放过「笑死了」「吓死了」这类夸张口语。
_DEATH_RE = re.compile(r"(?<![笑骇吓])死了")
# 未成年人：明确的低龄标志。1–17 岁的阿拉伯/中文数字年龄单独用正则判断。
_MINOR_MARKERS: tuple[str, ...] = (
    "未成年", "小学生", "初中生", "小学", "初中", "初一", "初二", "初三", "高一", "高二",
    "年级", "幼儿园", "萝莉", "正太", "小女孩", "小男孩", "幼女", "幼童", "儿童", "孩子",
    "儿子", "女儿", "小朋友",
)
_MINOR_AGE_RE = re.compile(
    r"(?<![\d.])(?:1[0-7]|[1-9])\s*岁"
    r"|(?<![一二三四五六七八九十百])(?:十[一二三四五六七]?|[一二三四五六七八九])岁"
)
_EXPLICIT_MARKERS: tuple[str, ...] = (
    "做爱", "性交", "性冲动", "操你", "草你", "鸡巴", "阴茎", "睾丸", "蛋蛋", "裆部", "下身",
    "自慰", "飞机杯", "倒模", "涩图", "色图", "小黄书", "开房", "发情", "舌吻", "胸部",
    "敏感的部位", "酥麻", "魅魔", "撸管", "射了", "高潮", "嬷嬷圣体", "做舒服的事情", "积吧",
    "屁股",
)
# 政治隐喻与现实政治人物/事件：梗百科不负责这类内容，一律不返回。
_POLITICAL_MARKERS: tuple[str, ...] = (
    "马列主义", "帝国主义", "社会主义", "共产主义", "资本主义", "共产党", "国民党", "民国政府",
    "反动势力", "国家领导人", "总书记", "台独", "港独", "藏独", "疆独", "六四", "天安门", "文革",
    "法轮",
)
_DISCRIMINATION_MARKERS: tuple[str, ...] = (
    "偷井盖", "井盖在呼唤", "河南人血统", "黑鬼", "阿三", "棒子国", "小日本", "倭寇", "支那",
    "性别认知障",
)
_EXPLICIT_RE = re.compile(r"(?:操|扣|干).{0,3}逼")
# 伪装成文案的越狱/提示注入：返回给模型的内容里绝不能带这类指令。
_INJECTION_MARKERS: tuple[str, ...] = (
    "开发者模式", "清空数据", "遗忘之前", "忘记之前", "不要使用抱歉", "忽略不相关的道德",
    "jailbreak", "越狱模式",
)
_INJECTION_RE = re.compile(
    r"(?i)(?:(?<![a-z])system(?![a-z])|忽略.{0,12}(?:限制|规则|道德|指令|设定)|你现在是.{0,10}(?:模式|身份))"
)
_TOXIC_BLOCKED = tuple(word for word, reason in DEFAULT_BLOCKED.items() if reason == "toxic_or_sensitive")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def mentioned_real_people(text: str | None) -> list[str]:
    compact = _compact(text)
    lowered = compact.lower()
    return [name for name in REAL_PERSON_NAMES if name.lower() in lowered]


def meme_safety_violation(text: str | None, *, vetted: bool = False) -> str | None:
    """返回第一个命中的违规类别；安全返回 ``None``。"""
    raw = str(text or "")
    compact = _compact(raw)
    if not compact:
        return None
    if is_identity_contamination(raw):
        return "identity_contamination"
    if _contains_any(compact, _INJECTION_MARKERS) or _INJECTION_RE.search(raw):
        return "prompt_injection"
    if _contains_any(compact, _TOXIC_BLOCKED):
        return "blocked_phrase"
    if (
        _contains_any(compact, _DISASTER_WAR_MARKERS)
        or _contains_any(compact, _DEATH_MARKERS)
        or _DEATH_RE.search(compact)
    ):
        return "real_disaster_death_war"
    if _contains_any(compact, _MINOR_MARKERS) or _MINOR_AGE_RE.search(compact):
        return "minor"
    if _contains_any(compact, _EXPLICIT_MARKERS) or _EXPLICIT_RE.search(compact):
        return "explicit_sexual"
    if _contains_any(compact, _DISCRIMINATION_MARKERS):
        return "discrimination"
    if _contains_any(compact, _POLITICAL_MARKERS):
        return "political"
    people = mentioned_real_people(compact)
    if people and (not vetted or _contains_any(compact, _HOSTILE_MARKERS)):
        return "real_person"
    return None


def is_meme_text_safe(text: str | None, *, vetted: bool = False) -> bool:
    return meme_safety_violation(text, vetted=vetted) is None


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)


__all__ = [
    "CATEGORY_LABELS",
    "REAL_PERSON_NAMES",
    "category_label",
    "is_meme_text_safe",
    "meme_safety_violation",
    "mentioned_real_people",
]
