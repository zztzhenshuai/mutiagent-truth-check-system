"""
agent/agents/news_policy.py

NewsAgent — 时政新闻与政策法规领域专家 Agent。

覆盖：
  - 系统 prompt：新闻调查编辑 persona + 辟谣平台优先 + 官方口径
  - Challenger prompt：质疑信源权威性、数字一致性、官方通报匹配
  - Judge prompt：判决优先采纳官方通报和辟谣平台记录
  - 策略融合：medium 声明启用 Challenger（政策误读后果严重）
  - 校准系数：对无证据 URL 惩罚更重
"""

from __future__ import annotations

from dataclasses import replace

from .base import DomainAgent


class NewsAgent(DomainAgent):
    """时政新闻与政策法规领域事实核查专家。"""

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
            "你是一名时政新闻与政策法规领域的事实核查编辑。"
            "你精通政府公文解读、国际关系基本常识、辟谣平台交叉验证方法论。\n\n"
            "## 你的核查原则\n\n"
            "### 信源优先级\n"
            "1. **最高优先级**：官方辟谣平台（fact_check_domestic / fact_check_global 直接命中 → 判 factual_error）\n"
            "2. **高优先级**：政府官网声明、官方通讯社一手报道\n"
            "3. **中优先级**：权威媒体（新华社、人民日报、BBC、Reuters）交叉报道\n"
            "4. **低优先级**：自媒体、社交平台截图、匿名信源 — 仅作参考，不作为裁决依据\n\n"
            "### 核心判断准则\n"
            "1. **剥离修辞外壳，只核核心事实**：「135 国共同提案，史无前例」→ 核心事实为 135 国数字是否属实，"
            "「史无前例」是修辞。只要数量属实，不因修辞无法量化而判错。\n"
            "2. **允许外交辞令与政治定性**：带阵营立场的词汇（「挑衅行为」「破坏稳定」）属于政治叙事语境，"
            "不因其主观色彩判为 logical_fallacy。\n"
            "3. **孤证克制原则**：无法证实也无法证伪的「民间奇闻」「无聊琐事」→ 判 unsupported_claim，"
            "confidence 限制在 0.4~0.6。\n"
            "4. **零容忍红线**（直接判定 factual_error）：\n"
            "   - 官方辟谣库直接命中（定性为「谣言」「伪造」）\n"
            "   - 常识性时空/物理错乱（泰山说成在黑龙江、二战结束说成 2005 年）\n"
            "   - 凭空捏造不存在的突发事件（全网无任何记录的重大灾害/事故）\n"
        )

    # ── Challenger prompt ─────────────────────────────────────

    def build_challenger_prompt(self, claim_text: str, skill_name: str, verifier_record) -> str:
        tool_context = "\n".join(
            f"- {call.tool_name}: {call.tool_input} => {call.tool_output}"
            for call in verifier_record.tool_calls
        ) or "- 无工具调用记录"
        evidence = verifier_record.annotation.evidence_urls or []

        return (
            "你是时政新闻与政策法规领域的 Challenger Agent。"
            "你的职责是从信源可信度与逻辑一致性角度审视 Verifier 的结论。\n\n"
            "请从以下维度质疑：\n"
            "1. **信源权威性**：Verifier 引用的证据来自官方通报还是自媒体二手引用？\n"
            "2. **辟谣平台匹配**：声明是否已在 fact_check_domestic / fact_check_global 中有记录？Verifier 是否未查？\n"
            "3. **数字一致性**：声明中的具体数字（人数、金额、日期）是否与官方数据吻合？\n"
            "4. **政策状态**：Verifier 是否混淆了「审议中」「拟实行」「已生效」等政策阶段？\n"
            "5. **地理/职务匹配**：声明中的地理位置、官员职务、机构名称是否吻合官方记录？\n\n"
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
            '"reasoning":"从信源可信度与逻辑一致性角度的质疑或支持（中文一句话）",'
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
            "你是时政新闻领域的 Judge Agent，负责对 Verifier 和 Challenger 的辩论做最终裁决。\n\n"
            "## 判决标准\n"
            "1. **辟谣平台一票否决**：若声明被 fact_check_domestic / fact_check_global 直接命中并定性为谣言，"
            "直接判 factual_error，不再考量其他因素。\n"
            "2. **官方口径优先**：政府/机构官方通报的数字 > 媒体报道 > 社交平台信息。\n"
            "3. **修辞豁免**：外交辞令、政治定性词汇不构成判定依据，只核底层物理事实。\n"
            "4. **政策阶段精确匹配**：「审议中」「已通过」「已生效」「已废止」的状态错误属于 factual_error。\n"
            "5. **孤证原则**：全网只有一个不可靠来源的信息 → 判 unsupported_claim，confidence ≤ 0.5。\n\n"
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
        # 新闻/政策领域：medium 声明也启用 Challenger（政策误读后果严重）
        if claim_complexity == "medium":
            return replace(base, enable_challenger=True, max_react_steps=4)
        # complex：提高门槛
        if claim_complexity == "complex":
            return replace(base, high_confidence_threshold=0.92)
        return base

    # ── 校准系数 ──────────────────────────────────────────────

    def get_calibration_multipliers(self) -> dict[str, float]:
        """新闻/政策领域：无证据 URL 惩罚更重，政策声明必须有官方来源。"""
        return {
            "no_evidence_url": 0.78,   # 必须有来源链接（默认 0.90）
            "no_tool": 0.75,           # 不调工具重罚（默认 0.80）
        }
