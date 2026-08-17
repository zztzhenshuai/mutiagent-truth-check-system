"""
agent/agents/medical.py

MedicalAgent — 循证医学领域专家 Agent。

覆盖：
  - 系统 prompt：循证医学 persona + GRADE 证据等级
  - Challenger prompt：质疑证据等级、样本量、相关vs因果
  - Judge prompt：判决优先采纳 RCT/系统综述
  - 策略融合：medium 也启用 Challenger（涉及健康需双重检查）
  - 校准系数：对无工具调用惩罚更重
"""

from __future__ import annotations

from dataclasses import replace

from .base import DomainAgent


class MedicalAgent(DomainAgent):
    """循证医学领域事实核查专家。"""

    # ── 系统 prompt（核心覆盖）────────────────────────────

    def build_system_prompt(self, overlays=None, disabled_note_tools=(), tool_required: bool = True) -> str:
        """构建完整的医学核查系统 prompt。"""
        has_tools = bool(self.skill.allowed_tools)
        from ..tools.registry import TOOL_REGISTRY

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
            "你是一名循证医学（Evidence-Based Medicine）事实核查专家。"
            "你精通临床流行病学、生物统计学和 GRADE 证据质量分级体系。\n\n"
            "## 你的核查原则\n\n"
            "### 证据等级体系（GRADE）\n"
            "- **高等级证据**：系统综述/Meta 分析、大规模 RCT（随机对照试验）— 优先采纳\n"
            "- **中等证据**：队列研究、病例对照研究 — 可信但仍需交叉验证\n"
            "- **低等级证据**：病例报告、专家意见、体外实验 — 标记为低置信度支撑\n\n"
            "### 核心判断准则\n"
            "1. 区分「膳食建议」与「临床疗效」：食物/营养素的健康益处即使以通俗修辞表达，"
            "只要方向正确、对人体无害且不宣称替代药物，应判定为无明显错误。\n"
            "2. 区分「相关」与「因果」：观察性研究中 A 与 B 存在统计关联，不等于 A 导致 B。"
            "除非有 RCT 或明确的生物学机制支撑，对因果断言保持审慎。\n"
            "3. 经验医学的合理留白：生姜止呕、蜂蜜润喉等广泛认知的日常护理（Home Remedies），"
            "有辅助缓解作用且无禁忌时，不因缺乏国际临床指南而判错。\n"
            "4. **零容忍红线**（直接判定 factual_error）：\n"
            "   - 宣称食物/偏方可「替代处方药」「根治」慢性病（高血压、糖尿病、癌症）\n"
            "   - 捏造/篡改科研证据（虚构论文、夸大疗效数字）\n"
            "   - 严重毒副作用隐瞒（将肝肾毒性偏方称为「绝对安全无副作用」）\n"
        )

    # ── Challenger prompt ─────────────────────────────────────

    def build_challenger_prompt(self, claim_text: str, skill_name: str, verifier_record) -> str:
        tool_context = "\n".join(
            f"- {call.tool_name}: {call.tool_input} => {call.tool_output}"
            for call in verifier_record.tool_calls
        ) or "- 无工具调用记录"
        evidence = verifier_record.annotation.evidence_urls or []

        return (
            "你是循证医学领域的 Challenger Agent。"
            "你的职责不是简单否定，而是从医学证据学角度审视 Verifier 的结论是否严谨。\n\n"
            "请从以下维度质疑：\n"
            "1. **证据等级**：Verifier 所依据的证据属于 GRADE 哪一级？等级过低时置信度不应过高。\n"
            "2. **相关 vs 因果**：Verifier 是否把观察性研究的统计关联误判为因果关系？\n"
            "3. **样本量与外推性**：研究人群是否能外推到声明所指的人群（如亚洲人 vs 欧美人）？\n"
            "4. **利益冲突**：工具返回的信息是否来自药企赞助的研究或商业推广文章？\n"
            "5. **危险信号遗漏**：Verifier 是否遗漏了声明的潜在危害（如延误正规治疗）？\n\n"
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
            '"reasoning":"从医学证据学角度的质疑或支持（中文一句话）",'
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
            "你是循证医学领域的 Judge Agent，负责对 Verifier 和 Challenger 的辩论做最终裁决。\n\n"
            "## 判决标准\n"
            "1. **证据权重**：优先采纳系统综述/RCT 的证据；个案报告/专家意见仅作辅助参考。\n"
            "2. **安全性优先**：当证据不充分但涉及生命安全时，默认更保守（倾向高置信度 factual_error）。\n"
            "3. **生存偏差警惕**：Challenger 指出的证据等级不足、样本量小等问题若合理，应降低采纳置信度。\n"
            "4. **养生修辞豁免**：不因民间修辞（如「血管清道夫」）或不严谨的因果表述（如「吃A防癌」）"
            "而判 false，除非触及零容忍红线（替代治疗、虚构证据、隐瞒毒性）。\n\n"
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
        # 医学领域：涉及人体健康，所有声明默认更严格
        if claim_complexity == "medium":
            return replace(base, enable_challenger=True, max_react_steps=4)
        if claim_complexity == "complex":
            return replace(base, high_confidence_threshold=0.92)
        return base

    # ── 校准系数 ──────────────────────────────────────────────

    def get_calibration_multipliers(self) -> dict[str, float]:
        """医学领域：无工具调用惩罚更重，涉及健康必须查证。"""
        return {
            "no_tool": 0.70,          # 医学声明不调工具 → 重罚（默认 0.80）
            "tool_error": 0.80,        # 工具错误也重罚（默认 0.85）
            "no_evidence_url": 0.85,   # 无证据 URL 惩罚更重（默认 0.90）
        }

    # ── 高置信度快速通道覆盖 ─────────────────────────────────

    def should_skip_debate(self, verifier_record, strategy) -> bool:
        """医学领域：即使高置信度，若有 nil 错误但有零容忍信号也不跳过。"""
        if (
            verifier_record.annotation.confidence >= strategy.high_confidence_threshold
            and not verifier_record.errors
        ):
            # 额外检查：医学声明即使高置信度，如果声称无错误(null)
            # 但未调用任何工具，不应跳过（需要至少一次外部查证）
            if verifier_record.annotation.error_type is None and not verifier_record.tool_calls:
                return False
            return True
        return False
