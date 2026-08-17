"""
agent/agents/base.py

DomainAgent 基类 — 封装领域差异（系统 prompt、辩论 prompt、策略映射、校准偏好）。
默认行为 = 当前 GeneralAgent 行为（完全向后兼容）。子类覆盖部分方法即可实现领域特化。

方向5（领域专家 Agent 池）+ 方向1（复杂度自适应路由）融合：
  - merge_strategy() 对 _STRATEGY_MAP 做领域微调
  - build_system_prompt() 是核心覆盖点（persona + 知识注入）
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ..debate import (
    build_challenger_prompt,
    build_judge_prompt,
    build_reflexion_prompt,
)
from ..tools.registry import TOOL_REGISTRY

if TYPE_CHECKING:
    from ..models import ComplexityLevel, VerificationStrategy
    from ..skills import Skill


class DomainAgent:
    """领域专家 Agent 基类。

    封装：系统 prompt 构建、Challenger/Judge/Reflexion prompt 构建、
          策略映射微调、置信度校准偏好。

    默认行为 = 当前的 GeneralAgent 行为（完全向后兼容）。
    子类覆盖部分方法即可实现领域特化。

    设计要点：
      - 不继承 Agent，不持有 Agent 引用（组合而非继承）
      - 只封装"有领域差异"的行为，共享机制（_react_loop）留在 Agent
      - 所有方法都有默认实现，未覆盖 = 不回退
    """

    def __init__(self, skill: Skill, llm) -> None:
        self.skill = skill
        self._llm = llm

    # ── 属性代理 ──
    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self.skill.allowed_tools

    # ── 系统 prompt（核心覆盖点）────────────────────────────

    def build_system_prompt(
        self,
        overlays: list[Skill] | None = None,
        disabled_note_tools: tuple[str, ...] = (),
        tool_required: bool = True,
    ) -> str:
        """构建完整 Verifier 系统 prompt（人格 + 工具 + 核查标准）。

        默认行为 = 当前 agent.py 的 _build_system_prompt()。
        若 skill 有 persona 字段，优先使用 persona 作为角色描述；
        否则使用通用前缀 + skill.prompt 后缀。
        """
        has_tools = bool(self.skill.allowed_tools)
        tool_lines = "\n".join(
            f"- {TOOL_REGISTRY[name].name}：{TOOL_REGISTRY[name].description}"
            for name in self.skill.allowed_tools
            if name in TOOL_REGISTRY
        )
        if not tool_lines:
            tool_lines = "（所有工具已被禁用）"

        # persona 优先（方向5 新增）
        persona = getattr(self.skill, "persona", None) or ""
        if persona:
            role_section = f"{persona}\n\n"
        else:
            role_section = "你是 Verifier Agent，负责核查声明是否存在错误，并给出结构化结论。\n你的任务是验证给定声明是否存在错误。\n\n"

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

        # ★ 三态 final_instruction：与 _react_loop 的 tool_requirement 保持一致，避免 system/user prompt 矛盾
        if has_tools and tool_required:
            final_instruction = "你必须至少调用一次工具，再给出 Final Answer。"
        elif not has_tools:
            final_instruction = "当前无可用工具，请基于声明本身与常识保守判断，倾向 unsupported_claim 并给较低 confidence，可直接输出 Final Answer。"
        else:
            final_instruction = "你可以根据需要调用工具，也可直接给出 Final Answer。"

        return (
            f"{role_section}"
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

    # ── 辩论阶段 prompt（可选覆盖）────────────────────────

    def build_challenger_prompt(self, claim_text: str, skill_name: str, verifier_record) -> str:
        """构建 Challenger 质疑 prompt。默认使用 debate.py 的全局实现。"""
        return build_challenger_prompt(claim_text, skill_name, verifier_record)

    def build_judge_prompt(
        self, claim_text: str, skill_name: str, verifier, challenger, rebuttal
    ) -> str:
        """构建 Judge 终裁 prompt。默认使用 debate.py 的全局实现。"""
        return build_judge_prompt(claim_text, skill_name, verifier, challenger, rebuttal)

    def build_reflexion_prompt(
        self,
        claim_text: str,
        original_error_type: str,
        original_confidence: float,
        original_reasoning: str,
        tool_calls_summary: str,
        errors_summary: str,
    ) -> str:
        """构建 Reflexion 反思 prompt。默认使用 debate.py 的全局实现。"""
        return build_reflexion_prompt(
            claim_text=claim_text,
            original_error_type=original_error_type,
            original_confidence=original_confidence,
            original_reasoning=original_reasoning,
            tool_calls_summary=tool_calls_summary,
            errors_summary=errors_summary,
        )

    # ── 策略融合（与方向1对接）────────────────────────

    def merge_strategy(self, claim_complexity: ComplexityLevel) -> VerificationStrategy:
        """基于领域偏好 + 声明复杂度，返回最终策略。

        默认：直接返回 _STRATEGY_MAP[claim_complexity]。
        子类可覆盖以调整阈值（如医学提高所有 complexity 的严格度）。
        """
        from ..agent import _STRATEGY_MAP
        return _STRATEGY_MAP[claim_complexity]

    # ── 置信度校准偏好（可选覆盖）─────────────────────

    def get_calibration_multipliers(self) -> dict[str, float]:
        """返回领域特定的校准系数偏置。

        键名语义：
          - no_tool: 未调工具惩罚（默认 0.80）
          - tool_error: 工具错误惩罚（默认 0.85）
          - no_evidence_url: 无证据 URL 惩罚（默认 0.90）
          - too_many_steps: 步数过多惩罚（默认 0.90）
          - diverse_tools: 多工具奖励（默认 1.10）

        返回的 dict 中只包含需要偏置的键；未包含的键使用 agent.py 硬编码默认值。
        """
        return {}

    # ── 高置信度快速通道（可选覆盖）──────────────────

    def should_skip_debate(self, verifier_record, strategy: VerificationStrategy) -> bool:
        """判断是否应跳过后续辩论（高置信度快速通道）。

        默认行为：confidence >= threshold 且无 tool errors。
        子类可覆盖以自定义逻辑（如医学需要额外检查证据等级）。
        """
        return (
            verifier_record.annotation.confidence >= strategy.high_confidence_threshold
            and not verifier_record.errors
        )
