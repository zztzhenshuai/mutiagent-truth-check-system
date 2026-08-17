"""
agent/agents/finance.py

FinanceAgent — 财经金融领域专家 Agent。

覆盖：
  - 系统 prompt：财经数据分析师 persona + 时效性规则
  - Challenger prompt：质疑数据时效性、口径张冠李戴、财报捏造
  - Judge prompt：判决考虑数据时效性、区分预测与事实
  - 策略融合：medium 也启用 Challenger（财务数字需双重确认）
  - 校准系数：对无证据 URL 惩罚更重（财经数据必须有源）
"""

from __future__ import annotations

from dataclasses import replace

from .base import DomainAgent


class FinanceAgent(DomainAgent):
    """财经金融领域事实核查专家。"""

    # ── 系统 prompt（核心覆盖）────────────────────────────

    def build_system_prompt(self, overlays=None, disabled_note_tools=(), tool_required: bool = True) -> str:
        from ..tools.registry import TOOL_REGISTRY

        has_tools = bool(self.skill.allowed_tools)
        tool_lines = "\n".join(
            f"- {TOOL_REGISTRY[name].name}：{TOOL_REGISTRY[name].description}"
            for name in self.skill.allowed_tools
            if name in TOOL_REGISTRY
        )
        if not tool_lines:
            tool_lines = "（所有工具已被禁用）"

        persona = getattr(self.skill, "persona", None) or self._default_persona()
        skill_section = (
            f"## 领域核查要点（{self.skill.name}）\n\n{self.skill.prompt}\n\n"
            if self.skill.prompt else ""
        )

        overlay_section = "".join(
            f"## 附加关注点（{overlay.name}）\n\n{overlay.prompt}\n\n"
            for overlay in (overlays or [])
            if overlay.prompt
        )

        disabled_section = (
            "## 已禁用工具（不可使用）\n\n"
            f"以下工具已被用户禁用，即使上文核查要点提到也不得调用：{', '.join(disabled_note_tools)}\n\n"
            if disabled_note_tools else ""
        )

        final_instruction = (
            "你必须至少调用一次工具，再给出 Final Answer。"
            if has_tools else
            "当前无可用工具，请基于声明本身与常识保守判断，"
            "倾向 unsupported_claim 并给较低 confidence，可直接输出 Final Answer。"
        )

        return (
            f"{persona}\n\n"
            f"{skill_section}"
            f"{overlay_section}"
            f"{disabled_section}"
            f"可用工具：\n{tool_lines}\n\n"
            "## 输出格式规则（必须严格遵守）\n\n"
            "每次输出只能是以下两种格式之一，不得包含任何额外文字、解释或 markdown：\n\n"
            "【格式一：调用工具】\n"
            "Thought: <你的推理过程，一句话>\n"
            "Action: <工具名，必须完全匹配可用工具名称之一>\n"
            "Action Input: <工具输入，纯字符串，不得换行>\n\n"
            "【格式二：给出最终结论】\n"
            "Thought: <基于已有证据的最终推理，一句话>\n"
            'Final Answer: {"error_type": "factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null, '
            '"confidence": 0.0~1.0, "reasoning": "中文推理摘要", "evidence_urls": ["url1", "url2"]}\n\n'
            f"{final_instruction}"
        )

    @staticmethod
    def _default_persona() -> str:
        return (
            "你是一名财经数据分析师与金融合规审计专家。"
            "你精通财务报表分析、宏观经济指标解读、证券市场规则和数据时效性管理。\n\n"
            "## 你的核查原则\n\n"
            "### 数据时效性规则\n"
            "- 财经数据高度时效敏感：GDP 数据按季度发布，CPI 按月发布，股价按秒变动。\n"
            "- 核查时注意声明的发布时间与数据的可用时间窗是否匹配。\n"
            "- 若声明引用的是旧数据但未标注时间，需在结论中指出时效性问题。\n\n"
            "### 核心判断准则\n"
            "1. **分离「叙事色彩」与「数据造假」**：财经媒体常使用「狂飙」「蒸发」「断崖式下跌」等情绪化动词。"
            "只要底层数字方向大体相符，必须放过这些修辞。\n"
            "2. **区分「预测」与「事实」**：分析师目标价、技术面分析、未来盈利预测属于 Forward-looking Statements，"
            "不是可核查的事实——除非文章将其伪装成已发生的既成事实。\n"
            "3. **数字口径匹配**：注意同比 vs 环比、名义 vs 实际、营收 vs 净利润的区分。"
            "张冠李戴属于 factul_error。\n"
            "4. **零容忍红线**（直接判定 factual_error）：\n"
            "   - 数字与口径张冠李戴（同比写成环比、营收写成净利润）\n"
            "   - 财务报表事实捏造（盈利 10 亿写成亏损 10 亿）\n"
            "   - 将高风险投机行为宣称为「国家兜底」「稳赚不赔」\n"
        )

    # ── Challenger prompt ─────────────────────────────────────

    def build_challenger_prompt(self, claim_text: str, skill_name: str, verifier_record) -> str:
        tool_context = "\n".join(
            f"- {call.tool_name}: {call.tool_input} => {call.tool_output}"
            for call in verifier_record.tool_calls
        ) or "- 无工具调用记录"
        evidence = verifier_record.annotation.evidence_urls or []

        return (
            "你是财经金融领域的 Challenger Agent。"
            "你的职责是从财务审计与数据合规角度审视 Verifier 的结论是否有盲点。\n\n"
            "请从以下维度质疑：\n"
            "1. **数据时效性**：Verifier 引用的数据时间窗口是否与声明匹配？是否用了过期数据？\n"
            "2. **口径一致性**：Verifier 是否检查了同比/环比、名义/实际、营收/净利润的对齐？\n"
            "3. **信源权威性**：工具返回的数据来自官方统计机构（如国家统计局、SEC）还是自媒体二手引用？\n"
            "4. **预测伪装**：声明是否把分析师预测包装成了已发生的事实？\n"
            "5. **数字一致性**：声明中前后提到的数字是否自洽（可通过 cross_reference 发现）？\n\n"
            f"领域 skill: {skill_name}\n"
            f"声明: {claim_text}\n"
            f"Verifier 结论 error_type: {verifier_record.annotation.error_type}\n"
            f"Verifier confidence: {verifier_record.annotation.confidence}\n"
            f"Verifier reasoning: {verifier_record.annotation.reasoning}\n"
            f"Verifier evidence_urls: {evidence}\n"
            "Verifier 工具记录:\n"
            f"{tool_context}\n\n"
            "请只返回 JSON，不要解释，不要 markdown：\n"
            '{"stance":"support"|"challenge","confidence":0.0,'
            '"reasoning":"从财务审计与数据合规角度的质疑或支持（中文一句话）",'
            '"missing_evidence":["最多3条"],"suggested_queries":["最多3条"]}'
        )

    # ── Judge prompt ──────────────────────────────────────────

    def build_judge_prompt(self, claim_text, skill_name, verifier, challenger, rebuttal) -> str:
        rebuttal_section = ""
        if rebuttal is not None:
            rebuttal_section = (
                f"Rebuttal error_type: {rebuttal.annotation.error_type}\n"
                f"Rebuttal confidence: {rebuttal.annotation.confidence}\n"
                f"Rebuttal reasoning: {rebuttal.annotation.reasoning}\n"
                f"Rebuttal evidence_urls: {rebuttal.annotation.evidence_urls}\n"
            )
        return (
            "你是财经金融领域的 Judge Agent，负责对 Verifier 和 Challenger 的辩论做最终裁决。\n\n"
            "## 判决标准\n"
            "1. **数据时效性优先**：如果 Challenger 指出时效性问题且 Verifier 确实未核实时间窗，"
            "降低最终采纳置信度至少 0.10。\n"
            "2. **区分预测与事实**：Forward-looking 类的声明（目标价、预期增速）即使是假的也不判 error，"
            "属于市场情绪而非可核查事实。\n"
            "3. **口径对齐**：Challenger 指出的同比/环比混淆若属实，直接采纳 Challenger 的纠正。\n"
            "4. **市场修辞豁免**：「狂飙」「惨跌」哪怕略带夸张，只要底层核心数据趋势正确，"
            "判定为无明显错误。\n\n"
            "必须输出可直接持久化的最终结论，不能输出其他解释。\n\n"
            f"领域 skill: {skill_name}\n"
            f"声明: {claim_text}\n"
            f"Verifier error_type: {verifier.annotation.error_type}\n"
            f"Verifier confidence: {verifier.annotation.confidence}\n"
            f"Verifier reasoning: {verifier.annotation.reasoning}\n"
            f"Verifier evidence_urls: {verifier.annotation.evidence_urls}\n"
            f"Challenger stance: {challenger.stance}\n"
            f"Challenger confidence: {challenger.confidence}\n"
            f"Challenger reasoning: {challenger.reasoning}\n"
            f"Challenger missing_evidence: {challenger.missing_evidence}\n"
            f"{rebuttal_section}\n"
            "请只返回 JSON，不要 markdown：\n"
            '{"error_type":"factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null,'
            '"confidence":0.0,'
            '"reasoning":"中文推理摘要",'
            '"evidence_urls":["url1"],'
            '"debate_summary":"一句话说明最终裁决依据"}'
        )

    # ── 策略融合 ──────────────────────────────────────────────

    def merge_strategy(self, claim_complexity):
        from ..agent import _STRATEGY_MAP

        base = _STRATEGY_MAP[claim_complexity]
        # 财经领域：涉及数字的声明需要更充分的工具调用
        if claim_complexity == "medium":
            return replace(base, enable_challenger=True, max_react_steps=4)
        if claim_complexity == "complex":
            return replace(base, high_confidence_threshold=0.92)
        return base

    # ── 校准系数 ──────────────────────────────────────────────

    def get_calibration_multipliers(self) -> dict[str, float]:
        """财经领域：无证据 URL 惩罚更重，财经数据必须有出处。"""
        return {
            "no_evidence_url": 0.80,   # 财经声明必须有来源 URL → 重罚（默认 0.90）
            "no_tool": 0.75,           # 财经数字不调工具 → 重罚（默认 0.80）
            "tool_error": 0.80,        # 工具错误 → 重罚（默认 0.85）
        }
