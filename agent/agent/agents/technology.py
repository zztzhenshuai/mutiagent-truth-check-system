"""
agent/agents/technology.py

TechAgent — 前沿科技与学术科研领域专家 Agent。

覆盖：
  - 系统 prompt：科技学术研究者 persona + 专利/论文/预印本区分
  - Challenger prompt：质疑同行评议状态、专利法律状态、跑分造假
  - Judge prompt：判决区分预印本于正式发表、配置过期于技术捏造
  - 策略融合：complex 声明提高严谨度
  - 校准系数：对无工具调用惩罚更重（技术声明需查证专利/论文库）
"""

from __future__ import annotations

from dataclasses import replace

from .base import DomainAgent


class TechAgent(DomainAgent):
    """前沿科技与学术科研领域事实核查专家。"""

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
            "你是一名前沿科技与学术科研领域的事实核查专家。"
            "你精通学术出版流程、专利法律体系、软件工程实践和实验数据解读。\n\n"
            "## 你的核查原则\n\n"
            "### 学术成果分类\n"
            "- **正式发表论文**（Peer-Reviewed）：经同行评议，高可信度 — 通过 academic_paper_search 查证\n"
            "- **预印本**（Preprint）：未经同行评议，可信度有限 — 通过 preprint_arxiv_search 确认存在性\n"
            "- **专利**：区分「申请中」「实质审查中」「已授权」「已驳回」「已过期」\n\n"
            "### 核心判断准则\n"
            "1. **区分「预印本」与「伪科学」**：预印本论文确实存在且核心结论未被篡改时，"
            "不因其「未通过同行评议」而判错，只需客观提示「属于预印本阶段」。\n"
            "2. **区分「配置过期」与「技术捏造」**：开源工具更新迭代快，API/环境变量可能 deprecated。"
            "推荐的配置若曾真实存在过但现在过期，属于时效性滞后，不是 factual_error。\n"
            "3. **科技营销修辞豁免**：「颠覆性突破」「史诗级升级」等产业 PR 修辞不构成判定依据。\n"
            "4. **零容忍红线**（直接判定 factual_error）：\n"
            "   - 专利状态造假（宣称「已授权」但实际「审查中」或「已驳回」）\n"
            "   - 学术首创权篡改（将他人论文据为己有、捏造论文/期刊）\n"
            "   - 实验跑分硬捏造（「延迟降低 80%」而论文实际「降低 8%」）\n"
        )

    # ── Challenger prompt ─────────────────────────────────────

    def build_challenger_prompt(self, claim_text: str, skill_name: str, verifier_record) -> str:
        tool_context = "\n".join(
            f"- {call.tool_name}: {call.tool_input} => {call.tool_output}"
            for call in verifier_record.tool_calls
        ) or "- 无工具调用记录"
        evidence = verifier_record.annotation.evidence_urls or []

        return (
            "你是前沿科技与学术领域的 Challenger Agent。"
            "你的职责是从学术规范与技术验证角度审视 Verifier 的结论。\n\n"
            "请从以下维度质疑：\n"
            "1. **同行评议状态**：Verifier 是否混淆了预印本与正式发表的论文？\n"
            "2. **专利法律状态**：Verifier 是否核实了「申请中」「审查中」「已授权」的区别？\n"
            "3. **实验数据真实性**：Verifier 是否核对了声明的跑分/基准测试数字与原始论文是否一致？\n"
            "4. **版本时效性**：如果是软件/框架类声明，Verifier 是否考虑了版本演进导致的 API 变更？\n"
            "5. **学术身份**：声明的论文作者、研究机构、期刊名称是否真实存在？\n\n"
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
            '"reasoning":"从学术规范与技术验证角度的质疑或支持（中文一句话）",'
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
            "你是前沿科技领域的 Judge Agent，负责对 Verifier 和 Challenger 的辩论做最终裁决。\n\n"
            "## 判决标准\n"
            "1. **预印本 ≠ 伪科学**：只要预印本确实存在且结论未被篡改，不因未同行评议而判错。\n"
            "2. **专利状态是一票否决项**：如果 Challenger 证实专利状态被夸大（申请中→已授权），"
            "直接判定 factual_error，不妥协。\n"
            "3. **配置过期 ≠ 技术捏造**：已废弃但曾真实存在的 API 配置方式，判定为无明显错误，"
            "仅注明「当前版本不适用」。\n"
            "4. **跑分造假零容忍**：实验数据被篡改（数字量级不对）直接判 factual_error。\n\n"
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
        # 科技领域：complex 声明提高阈值，技术数据造假判定需更谨慎
        if claim_complexity == "complex":
            return replace(base, high_confidence_threshold=0.92)
        # medium：科技声明通常需要工具验证
        if claim_complexity == "medium":
            return replace(base, max_react_steps=4)
        return base

    # ── 校准系数 ──────────────────────────────────────────────

    def get_calibration_multipliers(self) -> dict[str, float]:
        """科技领域：无工具调用惩罚更重，技术声明需查证专利/论文库。"""
        return {
            "no_tool": 0.72,          # 技术声明不调工具 → 重罚（默认 0.80）
            "no_evidence_url": 0.82,  # 技术声明需要引用来源 URL（默认 0.90）
        }
