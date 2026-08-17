"""
agent/planner.py

规划器：对扫描器输出的声明计算 suspicion_score、判断验证复杂度，并排序生成 VerificationPlan。

suspicion_score 计算策略（规则启发式，不调用 LLM）：
  - 含具体数字/百分比       +0.35
  - 含引用标志词            +0.20
  - 含绝对断言词            +0.20
  - 含时间节点              +0.15
  - 声明过短（< 8 字）      -0.15（信息量不足）

最终 score 裁剪到 [0.0, 1.0]。

复杂度分类（迭代四·方向1）：
  - simple：单事实核验（纯数字/日期/专名），快速通道
  - medium：需 1-3 工具交叉验证，标准流程
  - complex：因果链/多证据/跨领域，完整辩论
"""

from __future__ import annotations

import re

from .models import Claim, ComplexityLevel, VerificationPlan

# ---- suspicion 规则特征定义 ----

_NUMBER_PATTERN = re.compile(
    r"\d+(?:\.\d+)?%?"          # 数字或百分比
    r"|\d+(?:亿|万|千|百|十)?"  # 中文数量词
)

_CITATION_KEYWORDS = [
    "据", "根据", "研究显示", "报告称", "数据显示",
    "专家表示", "分析认为", "调查发现", "据悉",
]

_ABSOLUTE_KEYWORDS = [
    "唯一", "绝对", "最", "第一", "全球", "世界",
    "史上", "有史以来", "从未", "必然", "一定",
]

_TIME_PATTERN = re.compile(
    r"\d{4}年|\d+月|\d+日"
    r"|去年|今年|上半年|下半年|近年来|历史上"
)


def _compute_score(text: str) -> float:
    score = 0.0

    if _NUMBER_PATTERN.search(text):
        score += 0.35

    if any(kw in text for kw in _CITATION_KEYWORDS):
        score += 0.20

    if any(kw in text for kw in _ABSOLUTE_KEYWORDS):
        score += 0.20

    if _TIME_PATTERN.search(text):
        score += 0.15

    if len(text) < 8:
        score -= 0.15

    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# 复杂度分类（迭代四·方向1）
# ---------------------------------------------------------------------------

# ── Simple 信号：单一事实点，可快速核验 ──

_SIMPLE_PATTERNS: list[tuple[re.Pattern, float]] = [
    # 纯数字/日期声明："2023年GDP增速5.2%"
    (re.compile(r"^\d{4}年\S{2,8}(?:增速?|增长?|下降?|达到?)\d+\.?\d*%?$"), 0.90),
    # 短声明（允许逗号，仅禁止句号/分号/感叹/问号/换行等句子终止符）
    (re.compile(r"^[^。；！？\n]{1,40}$"), 0.50),
]

_SIMPLE_INDICATORS: list[tuple[str, float]] = [
    # 单一专有名词（如 "ChatGPT发布于2022年11月"）
    ("成立于", 0.20),
    ("推出于", 0.20),
    ("发布于", 0.20),
    ("出生于", 0.20),
    ("位于", 0.15),
    ("缩写为", 0.20),
    ("全称为", 0.20),
    # 纯定义性声明
    ("是指", 0.15),
    ("指的是", 0.15),
]

# ── Complex 信号：需要多源交叉验证 ──

_COMPLEX_PATTERNS: list[tuple[re.Pattern, float]] = [
    # 因果链
    (re.compile(r"因为|所以|导致|因此|由于|从而|进而|以致"), 0.30),
    # 多层引用
    (re.compile(r"据.*报道.*称|据.*透露.*表示"), 0.25),
    # 并列复杂结构
    (re.compile(r"不仅.*而且|一方面.*另一方面|既.*又.*还"), 0.20),
    # ── 事件/案件强信号（0.30 — 单独命中即可超越 0.20，阻止 simple）──
    # 暴力/袭击/灾难中高度特异的词，出现几乎必然需要交叉验证
    (re.compile(
        r"连刺|刺杀|枪击|枪手|持刀|恐怖袭击|劫持|绑架|坠毁|沉没|"
        r"遇害|遇难|丧生|屠戮|斩首|伏击|引爆|暴乱|海啸|山崩|踩踏"
    ), 0.30),
    # ── 事件/案件弱信号（0.12 — 单独命中无法超越 0.20，需 ≥2 个弱信号协同）──
    # 常见词（"遭到""死亡""事故"等）在多领域都会出现，单次出现不足以判定需要深度核查
    (re.compile(
        r"遭到|死亡|身亡|受伤|重伤|事故|火灾|地震|洪水|"
        r"爆炸|失踪|逮捕|判刑|定罪|通缉|袭击|突发|嫌疑人"
    ), 0.12),
]
"""
注意：含 ≥3 个有效数字/百分比的声明不再用正则检测（正则易拆分 "5.2%"），
改为在 _eval_complex_signals 中基于 _count_numbers() 判断。
"""

_COMPLEX_INDICATORS: list[tuple[str, float]] = [
    ("研究表明", 0.25),
    ("临床试验", 0.55),
    ("统计数据显示", 0.25),
    ("对比分析", 0.25),
    ("政策影响", 0.25),
    ("经济效益", 0.25),
    ("环境影响", 0.25),
    ("长期跟踪", 0.30),
    # 跨领域/需多源验证的复杂主题
    ("气候变化", 0.25),
    ("碳排放", 0.25),
    ("金融危机", 0.25),
    ("公共卫生", 0.25),
    # 引用类指示词（需外部验证）
    ("专家认为", 0.25),
    ("分析人士指出", 0.25),
    ("报告指出", 0.20),
    # 通用引用词
    ("根据", 0.15),
    # ── 事件/案件/灾害关键词（低权重，需 2+ 命中才显著影响分类）──
    # 原则：不重复 patterns 中已有的词；权重 ≤ 0.15 保证单次命中不足以越过 0.20 阈值
    ("警方", 0.15),
    ("嫌犯", 0.15),
    ("被害人", 0.15),
    ("目击者", 0.15),
    ("送医", 0.15),
    ("国籍", 0.12),
    ("留学生", 0.12),
    ("海外", 0.12),
    ("监控录像", 0.15),
    ("通报", 0.12),
    ("伤亡", 0.15),
    ("遇难者", 0.15),
    ("幸存", 0.12),
    ("救援", 0.12),
    ("紧急状态", 0.15),
]

# ── 阈值 ──

_SIMPLE_CONFIDENT_THRESHOLD = 0.60   # simple_score >= 此值 且 complex_score < 0.20 → 确定为 simple
_COMPLEX_CONFIDENT_THRESHOLD = 0.50  # complex_score >= 此值 → 确定为 complex
# 其余情况 → medium


def _count_numbers(text: str) -> int:
    """统计文本中独立数字/百分比的数量（排除年份和月份）。"""
    all_nums = re.findall(r"\d+\.?\d*%?", text)
    count = 0
    for n in all_nums:
        # 排除 4 位年份（19xx / 20xx）
        if re.match(r"^(?:19|20)\d{2}$", n):
            continue
        # 排除纯月份（1-12 且无小数点）
        if re.match(r"^(?:[1-9]|1[0-2])$", n):
            continue
        count += 1
    return count


def _count_entities(text: str) -> int:
    """粗略统计专有名词数量（中文大写字母开头词 / 英文大写词）。"""
    cn_entities = len(re.findall(r"[A-Z一-鿿]{2,}(?:公司|集团|机构|大学|医院|政府|部门|基金|指数)", text))
    en_entities = len(re.findall(r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)*", text))
    return cn_entities + en_entities


def _eval_simple_signals(text: str) -> tuple[float, list[str]]:
    """评估声明属于 simple 的信号强度，返回 (score, reasons)。"""
    score = 0.0
    reasons: list[str] = []

    for pattern, weight in _SIMPLE_PATTERNS:
        if pattern.search(text):
            score += weight
            reasons.append(f"simple_pattern:{pattern.pattern[:30]}...")

    for keyword, weight in _SIMPLE_INDICATORS:
        if keyword in text:
            score += weight
            reasons.append(f"simple_indicator:{keyword}")

    # 正面信号：数字少
    num_count = _count_numbers(text)
    if num_count <= 1:
        score += 0.10
        reasons.append("single_number")
    elif num_count >= 4:
        score -= 0.20  # 数字多 → 不太可能是 simple
        reasons.append("many_numbers")

    # 正面信号：实体少
    entity_count = _count_entities(text)
    if entity_count <= 1:
        score += 0.08
        reasons.append("few_entities")
    elif entity_count >= 4:
        score -= 0.15
        reasons.append("many_entities")

    # 短文本更倾向于 simple
    if len(text) <= 50:
        score += 0.10
        reasons.append("short_text")
    elif len(text) >= 200:
        score -= 0.20
        reasons.append("long_text")

    # ── 事件/案件污染惩罚 ──
    # 短声明若描述具体事件（刺伤、遇害、事故等），需要搜索+辟谣交叉验证，
    # 即使文本短、数字少也不应是 simple。对 simple_score 施加惩罚，
    # 使此类声明至少进入 medium（3-4 步），避免在 2 步内来不及得出结论。
    #
    # ★ 两级信号设计：
    #   - 强信号（连刺/枪击/劫持…）：高度特异，1 次命中即惩罚 0.30
    #   - 弱信号（死亡/事故/突发…）：常见词，需 ≥2 次命中才惩罚（共 0.25）
    #   避免 "遭到拒绝"、"死亡率统计" 等非事件声明被误伤。
    _EVENT_STRONG_POLLUTION = [
        "连刺", "刺杀", "枪击", "枪手", "持刀", "恐怖袭击",
        "劫持", "绑架", "坠毁", "沉没", "遇害", "遇难", "丧生",
        "引爆", "屠戮", "斩首", "暴乱", "海啸", "山崩", "踩踏",
    ]
    _EVENT_WEAK_POLLUTION = [
        "遭到", "身亡", "死亡", "受伤", "重伤", "袭击", "爆炸",
        "逮捕", "判刑", "定罪", "通缉", "失踪", "火灾", "地震",
        "洪水", "事故", "警方", "嫌犯", "嫌疑人", "被害人",
        "送医", "突发", "紧急",
    ]
    strong_hits = sum(1 for kw in _EVENT_STRONG_POLLUTION if kw in text)
    weak_hits = sum(1 for kw in _EVENT_WEAK_POLLUTION if kw in text)
    event_penalty = 0.0
    if strong_hits >= 1:
        event_penalty += min(0.50, strong_hits * 0.30)
    if weak_hits >= 2:
        event_penalty += 0.25
    if event_penalty > 0:
        score -= event_penalty
        reasons.append(
            f"event_pollution:strong={strong_hits} weak={weak_hits} penalty=-{event_penalty:.2f}"
        )

    return (max(0.0, min(1.0, score)), reasons)


def _eval_complex_signals(text: str) -> tuple[float, list[str]]:
    """评估声明属于 complex 的信号强度，返回 (score, reasons)。"""
    score = 0.0
    reasons: list[str] = []

    for pattern, weight in _COMPLEX_PATTERNS:
        if pattern.search(text):
            score += weight
            reasons.append(f"complex_pattern:{pattern.pattern[:30]}...")

    for keyword, weight in _COMPLEX_INDICATORS:
        if keyword in text:
            score += weight
            reasons.append(f"complex_indicator:{keyword}")

    # ── 因果关系密度加成 ──
    # 单次"因为"不足以判定复杂，但"因为...导致...所以..."连环出现则确信。
    _CAUSALITY_KEYWORDS = ["因为", "所以", "导致", "因此", "由于", "从而", "进而", "以致"]
    causality_hits = sum(1 for kw in _CAUSALITY_KEYWORDS if kw in text)
    if causality_hits >= 3:
        score += 0.20
        reasons.append(f"dense_causality:{causality_hits}")
    elif causality_hits >= 2:
        score += 0.10
        reasons.append(f"moderate_causality:{causality_hits}")

    # 引用密度加成：同时出现 ≥2 个引用/报道关键词
    _CITATION_DENSITY_KEYWORDS = ["据", "研究显示", "报告称", "数据显示", "专家表示", "专家认为", "分析人士指出", "报道"]
    citation_hits = sum(1 for kw in _CITATION_DENSITY_KEYWORDS if kw in text)
    if citation_hits >= 2:
        score += 0.15
        reasons.append(f"dense_citation:{citation_hits}")

    # 数字多 → 更复杂
    num_count = _count_numbers(text)
    if num_count >= 3:
        score += 0.15
        reasons.append("many_numbers")

    # 实体多 → 更复杂
    entity_count = _count_entities(text)
    if entity_count >= 3:
        score += 0.15
        reasons.append("many_entities")

    # 长文本 → 更复杂
    if len(text) >= 150:
        score += 0.10
        reasons.append("long_text")

    return (max(0.0, min(1.0, score)), reasons)


def classify_complexity(text: str) -> tuple[ComplexityLevel, float]:
    """
    基于规则的声明复杂度分类（不调用 LLM）。

    返回 (等级, 置信度)。

    分类逻辑：
    1. 同时评估 simple 和 complex 信号
    2. simple_score 高 且 complex_score 低且无复杂信号污染 → simple
    3. complex_score 高 → complex
    4. 其余 → medium
    """
    simple_score, simple_reasons = _eval_simple_signals(text)
    complex_score, complex_reasons = _eval_complex_signals(text)

    # ── 复杂信号污染惩罚 ──
    # 当存在任何复杂信号时，simple_score 按比例衰减。
    # 使用 reasons 数量（而非 net score）作为污染度，避免减法归零掩盖真实信号。
    pollution_count = len(complex_reasons)
    if pollution_count > 0:
        pollution_penalty = min(0.70, pollution_count * 0.20)
        simple_score = max(0.0, simple_score - pollution_penalty)
        simple_reasons.append(f"complex_pollution:count={pollution_count} penalty=-{pollution_penalty:.2f}")

    if simple_score >= _SIMPLE_CONFIDENT_THRESHOLD and complex_score < 0.20:
        return ("simple", simple_score)

    if complex_score >= _COMPLEX_CONFIDENT_THRESHOLD:
        return ("complex", complex_score)

    # 边界情况：medium
    # 置信度取 middle ground
    confidence = 0.50 + (complex_score - simple_score) * 0.30
    return ("medium", max(0.30, min(0.75, confidence)))


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def build_plan(claims: list[Claim]) -> VerificationPlan:
    """
    对声明列表计算 suspicion_score、判断复杂度，按分数降序排列，返回 VerificationPlan。
    """
    for claim in claims:
        claim.suspicion_score = _compute_score(claim.text)
        claim.complexity, claim.complexity_confidence = classify_complexity(claim.text)

    sorted_claims = sorted(claims, key=lambda c: c.suspicion_score, reverse=True)

    return VerificationPlan(
        claims=sorted_claims,
        status={c.id: "pending" for c in sorted_claims},
    )
