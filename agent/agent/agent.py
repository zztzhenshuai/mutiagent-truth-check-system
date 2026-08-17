"""
agent/agent.py

成员 A 在迭代三的主改动：
- 单 Agent ReAct 升级为 Verifier / Challenger / Judge 的主控辩论流
- 新增 debate / summary 事件
- 输出可持久化的 claim 结果，并预留 claim 级重验证入口

迭代四（方向1：复杂度自适应路由）改动：
- 新增 _STRATEGY_MAP：simple/medium/complex 三级策略参数
- _debate_claim 策略驱动分流：simple→快速通道，medium→无Challenger，complex→完整辩论+Reflexion
- _react_loop 动态步数上限（由 strategy.max_react_steps 控制）
- 新增 Reflexion 反思机制（complex 专属，低置信度灰色地带触发）
- run() 输出复杂度分布统计

迭代四（方向5：领域专家 Agent 池）改动：
- Agent.run() 变为协调器：route_skill → get_domain_agent → 委托 DomainAgent
- _debate_claim 委托 DomainAgent 构建 system/challenger/judge/reflexion prompt
- _react_loop 新增 system_prompt 可选参数（领域 persona 注入）
- _calibrate_annotation 合并 DomainAgent 的领域校准系数

方向9（DAG 工作流引擎）改动：
- Agent.run() 简化为 WorkflowContext 构建 + WorkflowEngine.run() 委托
- 管线步骤（scan/plan/context/route/debate/summary）拆为独立 WorkflowNode
- 保留所有私有方法以支持 DebateNode 调用
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any, AsyncGenerator

from .debate import (
    ChallengeRecord,
    ClaimDebateRecord,
    VerificationRecord,
    build_challenger_prompt,
    build_judge_prompt,
    build_rebuttal_instruction,
    build_reflexion_prompt,
    parse_challenger_response,
    parse_judge_response,
    parse_reflexion_response,
)
from .llm.base import BaseLLMClient
from .models import (
    AgentState,
    AnnotationEvent,
    ComplexityLevel,
    DebateEvent,
    ErrorEvent,
    ThinkingEvent,
    ToolCallEvent,
    VerificationStrategy,
)
from .skills import Skill, load_skills
from .tools.registry import TOOL_REGISTRY

logger = logging.getLogger("agent.core")

# ── 全局约束 ──
_MIN_FINAL_CONFIDENCE = 0.60      # 最终裁决置信度低于此值 → 忽略该 claim（设为无错误）

# ── Reflexion 灰色地带（仅 complex 策略触发）──
_REFLEXION_LOW = 0.60   # 低于此值：过于不确定，反思意义不大
_REFLEXION_HIGH = 0.80  # 高于此值：已足够确定，无需反思
# 灰色地带 [0.60, 0.80)：置信度不够高又不过分低 → 触发反思

# ── 复杂度 → 策略映射（迭代四·方向1）──

_STRATEGY_MAP: dict[ComplexityLevel, VerificationStrategy] = {
    "simple": VerificationStrategy(
        level="simple",
        max_react_steps=2,
        enable_challenger=False,
        enable_judge=False,
        enable_rebuttal=False,
        enable_reflexion=False,
        tool_required=False,
        high_confidence_threshold=0.0,
    ),
    "medium": VerificationStrategy(
        level="medium",
        max_react_steps=3,
        enable_challenger=False,
        enable_judge=True,
        enable_rebuttal=False,
        enable_reflexion=False,
        tool_required=True,
        high_confidence_threshold=0.85,
    ),
    "complex": VerificationStrategy(
        level="complex",
        max_react_steps=6,
        enable_challenger=True,
        enable_judge=True,
        enable_rebuttal=True,
        enable_reflexion=True,
        tool_required=True,
        high_confidence_threshold=0.90,
    ),
}
"""
各复杂度对应的验证策略：

simple  → 单轮 LLM，不强制调工具，无辩论，直接输出（适合纯数字/日期核验）
medium  → ≤3 步 ReAct，跳过 Challenger，仅低置信度时走 Judge
complex → ≤6 步 ReAct + 完整 V→C→J 辩论 + 灰色地带 Reflexion 反思
"""


class Agent:

    def __init__(
        self,
        complex_llm: BaseLLMClient,
        router_llm: BaseLLMClient | None = None,
        chat_llm: BaseLLMClient | None = None,
    ) -> None:
        self._llm = complex_llm
        self._router_llm = router_llm or complex_llm
        self._chat_llm = chat_llm or complex_llm
        self._skills = load_skills()
        # 方向5：惰性缓存，同一领域只实例化一次 DomainAgent
        self._domain_agent_cache: dict[str, object] = {}

    @staticmethod
    def _normalize_disabled(disabled_tools: list[str] | None) -> frozenset[str]:
        """把用户传入的禁用工具名规整为集合：只保留 TOOL_REGISTRY 中存在的名字（未知名静默忽略，前向兼容）。"""
        if not disabled_tools:
            return frozenset()
        return frozenset(
            str(t).strip()
            for t in disabled_tools
            if isinstance(t, str) and str(t).strip() in TOOL_REGISTRY
        )

    async def run(
        self,
        article_text: str,
        overlays: list[dict] | None = None,
        disabled_tools: list[str] | None = None,
    ) -> AsyncGenerator[AgentState, None]:
        """主入口（方向9重构）：构建 WorkflowContext，委托 WorkflowEngine 执行 DAG。

        管线步骤（由 default_dag.yaml 定义）：
          scan → plan → context → route → debate → summary → Done
        """
        from pathlib import Path

        from .workflow import WorkflowContext, WorkflowEngine

        logger.info(
            "run() 开始（DAG 引擎模式）：article_length=%d overlays=%d disabled_tools=%d",
            len(article_text), len(overlays or []), len(disabled_tools or []),
        )

        # 构建共享上下文（注入引擎所需的所有依赖）
        ctx = WorkflowContext(
            article_text=article_text,
            overlays=overlays,
            disabled_tools=disabled_tools,
            _llm=self._llm,
            _router_llm=self._router_llm,
            _skills=self._skills,
            _domain_agent_cache=self._domain_agent_cache,
            _agent=self,  # DebateNode 需要调用 Agent 的私有方法
        )

        # 加载默认 DAG 配置
        dag_path = Path(__file__).resolve().parent / "workflow" / "default_dag.yaml"
        engine = WorkflowEngine(dag_path)

        # 按 DAG 顺序执行节点，逐事件产出
        async for event in engine.run(ctx):
            yield event

    def _build_system_prompt(
        self,
        skill: Skill,
        overlays: list[Skill] | None = None,
        disabled_note_tools: tuple[str, ...] = (),
        tool_required: bool = True,
    ) -> str:
        has_tools = bool(skill.allowed_tools)
        tool_lines = "\n".join(
            f"- {TOOL_REGISTRY[name].name}：{TOOL_REGISTRY[name].description}"
            for name in skill.allowed_tools
            if name in TOOL_REGISTRY
        )
        if not tool_lines:
            tool_lines = "（所有工具已被禁用）"

        skill_section = f"## 领域核查要点（{skill.name}）\n\n{skill.prompt}\n\n" if skill.prompt else ""
        overlay_section = "".join(
            f"## 附加关注点（{overlay.name}）\n\n{overlay.prompt}\n\n"
            for overlay in (overlays or [])
            if overlay.prompt
        )
        # 用户禁用覆盖说明：不改写领域正文，而是显式告知哪些上文可能提到的工具已不可用。
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
            "你是 Verifier Agent，负责核查声明是否存在错误，并给出结构化结论。\n"
            "你的任务是验证给定声明是否存在错误。\n\n"
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
    def _parse_react_response(text: str) -> dict[str, Any]:
        """解析 LLM 的 ReAct 格式回复，兼容各种常见不合规变体。"""
        # ── 1. 去除 markdown 代码围栏 ──
        # LLM 常把整个回复包在 ``` 或 ```json 里
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        else:
            # 去掉孤立的开头/结尾 ```（不成对的围栏碎片）
            cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
            cleaned = re.sub(r"\s*```$", "", cleaned)

        # ── 2. 去除常见 markdown 装饰 ──
        cleaned = cleaned.replace("**", "").replace("__", "")

        # ── 3. 去除行首的列表/编号/引用/标题符号 ──
        # 包括：*, -, #, >, 数字+点（如 1. 2.），以及更多空白
        cleaned = re.sub(
            r"(?m)^[\s>#*\-]*\d*\.?\s*(?=(?:Thought|Action Input|Action|Final Answer)\s*[:：])",
            "",
            cleaned,
        )

        # ── 4. 截取第一个有效关键字之后的内容，丢弃前置废话 ──
        # LLM 常在结构化输出前写 "好的，以下是我的分析：" 之类的客套话
        first_keyword = re.search(
            r"(?:Thought|Action Input|Action|Final Answer)\s*[:：]", cleaned
        )
        if first_keyword:
            cleaned = cleaned[first_keyword.start():]

        # ── 5. 提取 Thought ──
        thought_match = re.search(
            r"Thought\s*[:：]\s*(.+?)(?=\n(?:Action|Final Answer)\s*[:：]|$)", cleaned, re.S
        )
        thought = thought_match.group(1).strip() if thought_match else ""

        # ── 6. 提取 Final Answer（用平衡括号，不用贪婪 .+） ──
        final_keyword = re.search(r"Final Answer\s*[:：]", cleaned)
        if final_keyword:
            json_start_pos = final_keyword.end()
            # 从 Final Answer 之后找第一个 {
            brace_start = cleaned.find("{", json_start_pos)
            if brace_start != -1:
                # 平衡括号计数，找到匹配的 }
                depth = 0
                brace_end = -1
                for i, ch in enumerate(cleaned[brace_start:], start=brace_start):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            brace_end = i
                            break
                if brace_end != -1:
                    json_str = cleaned[brace_start:brace_end + 1]
                    from json_repair import json_repair
                    try:
                        answer = json_repair.loads(json_str)
                        return {"type": "final", "thought": thought, "answer": answer}
                    except Exception:
                        pass  # 回退到下面的 action 解析

        # ── 7. 提取 Action + Action Input ──
        action_match = re.search(r"Action\s*[:：]\s*[`'\"]*([A-Za-z_][A-Za-z0-9_]*)", cleaned)
        input_match = re.search(r"Action Input\s*[:：]\s*(.+?)(?:\n(?:Observation|Thought|Action|Final Answer)\s*[:：]|\n?$)", cleaned, re.S)
        if action_match:
            action_input = input_match.group(1).strip() if input_match else ""
            action_input = action_input.strip("`'\" ")
            return {
                "type": "action",
                "thought": thought,
                "action": action_match.group(1).strip(),
                "action_input": action_input,
            }

        # ── 8. 完全无法解析，返回 unknown 并附带原始文本供上层诊断 ──
        return {"type": "unknown", "raw": text}

    async def _reformat_response(self, raw_response: str, claim_text: str) -> str | None:
        """尝试将格式错误的 LLM 输出重新格式化为正确的 ReAct 格式。

        使用一个独立的、目的单一的 LLM 请求做格式转换。
        这是一个「修复」步骤而非推理步骤——不消耗 ReAct 步数。

        返回:
            格式化后的字符串，如果格式化失败则返回 None。
        """
        prompt = (
            "你是一个格式化助手。下面是一个 AI 助手的原始回复，它应该遵循特定的 ReAct 格式，"
            "但输出格式不正确。请将其内容重新组织为正确的格式。\n\n"
            "## 允许的两种格式\n\n"
            "格式一（调用工具）：\n"
            "Thought: <推理过程，一句话>\n"
            "Action: <工具名>\n"
            "Action Input: <工具输入>\n\n"
            "格式二（给出结论）：\n"
            "Thought: <基于证据的最终推理，一句话>\n"
            'Final Answer: {{"error_type": "factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null, "confidence": 0.0~1.0, "reasoning": "中文推理摘要", "evidence_urls": ["url1", "url2"]}}\n\n'
            "## 原始声明（上下文参考）\n"
            f"{claim_text}\n\n"
            "## 原始回复（需要重新格式化）\n"
            f"{raw_response}\n\n"
            "## 要求\n"
            "- 保留原始回复中的所有关键信息（推理、结论、工具调用等）\n"
            "- 根据原始回复的意图选择正确的格式\n"
            "- 如果原始回复暗示需要调用工具，使用格式一\n"
            "- 如果原始回复给出了最终结论，使用格式二\n"
            "- 不要添加任何额外文字或解释，只输出格式化后的内容\n"
            "- 不要用 markdown 代码块包裹输出"
        )
        try:
            reformatted = await self._llm.complete([{"role": "user", "content": prompt}])
        except Exception as exc:
            logger.warning("Reformatter LLM 调用失败：%s", exc)
            return None

        # 验证 reformatted 是否可以解析
        parsed = self._parse_react_response(reformatted)
        if parsed["type"] != "unknown":
            logger.info(
                "Reformatter 成功将格式异常输出（%d字符）转换为类型=%s（%d字符）",
                len(raw_response), parsed["type"], len(reformatted),
            )
            return reformatted

        logger.warning(
            "Reformatter 输出仍无法解析（类型=%s），原始=%r，reformatted=%r",
            parsed["type"], raw_response[:200], reformatted[:200],
        )
        return None

    async def _repair_tool_name(
        self,
        wrong_name: str,
        tool_input: str,
        claim_text: str,
        candidate_names: list[str],
    ) -> str | None:
        """尝试将错误的工具名修复为正确的工具名。

        使用独立的单轮 LLM 请求，从候选工具列表中匹配最接近的工具。
        这是一个轻量级的「名称修复」任务——不做推理，只做匹配。

        Args:
            wrong_name: LLM 输出的错误工具名（如 "WebSearch"）
            tool_input: LLM 原本要传给工具的输入
            claim_text: 正在核查的声明（上下文参考）
            candidate_names: 可用的正确工具名列表

        Returns:
            修复后的工具名，如果修复失败则返回 None。
        """
        candidates_str = "\n".join(f"- {n}" for n in sorted(candidate_names))
        logger.info(
            "🔧 _repair_tool_name 尝试修复 '%s' → 从 %d 个候选工具中匹配…",
            wrong_name, len(candidate_names),
        )
        prompt = (
            "你是一个工具名修复助手。一个 AI 核查系统尝试调用一个不存在或不可用的工具，"
            "请从可用工具列表中选择最匹配的工具名。只考虑语义匹配（大小写、下划线、同义词），"
            "不要考虑工具是否真的适合该任务。\n\n"
            f"## 原始声明\n{claim_text}\n\n"
            f"## 错误的工具名\n{wrong_name}\n\n"
            f"## 工具输入\n{tool_input}\n\n"
            f"## 可用工具列表\n{candidates_str}\n\n"
            "只返回正确的工具名（从上述列表中精确复制），不要解释、不要 markdown、不要额外文字。"
        )
        try:
            repaired = await self._llm.complete([{"role": "user", "content": prompt}])
        except Exception as exc:
            logger.warning("_repair_tool_name LLM 调用失败：%s", exc)
            return None

        repaired = repaired.strip().strip('"').strip("'").strip()
        if repaired in candidate_names:
            logger.info(
                "_repair_tool_name 将 '%s' 修复为 '%s'",
                wrong_name, repaired,
            )
            return repaired

        logger.warning(
            "⚠ _repair_tool_name 无法修复 '%s'：LLM 返回 '%s'（不在 %d 个候选工具中）",
            wrong_name, repaired, len(candidate_names),
        )
        return None

    async def _handle_tool_action(
        self,
        claim,
        skill,
        disabled_note_tools: tuple[str, ...],
        tool_name: str,
        tool_input: str,
        thought: str,
        result_sink: dict | None,
        step: int,
        messages: list[dict],
        max_steps: int = 6,
    ) -> AsyncGenerator:
        """处理工具调用：校验工具名、尝试修复、执行工具、产出事件。

        这是一个 async generator，产出 ThinkingEvent / ErrorEvent / ToolCallEvent。
        调用方应 async-for 转发每个事件，然后总是 continue 到下一轮 ReAct 循环。

        工具名校验修复流程：
        1. 工具不在 TOOL_REGISTRY → 尝试用全部注册工具名修复
        2. 工具被用户禁用 → 不修复（用户明确选择）
        3. 工具不在领域白名单 → 尝试用领域允许的工具名修复
        """
        from agent.models import ThinkingEvent, ToolCallEvent, ErrorEvent
        from agent.tools.registry import TOOL_REGISTRY

        # ── 1. 产出思考事件 ──
        if thought:
            thinking_event = ThinkingEvent(claim_id=claim.id, thought=thought)
            if result_sink is not None:
                result_sink["thoughts"].append(thought)
            yield thinking_event

        # ── 2. 工具名校验 & 修复 ──
        repaired_name: str | None = None

        if tool_name not in TOOL_REGISTRY:
            logger.warning("[%s] 模型请求了未注册的工具：%s", claim.id, tool_name)
            # 尝试用全部注册工具名修复
            repaired_name = await self._repair_tool_name(
                wrong_name=tool_name,
                tool_input=tool_input,
                claim_text=claim.text,
                candidate_names=list(TOOL_REGISTRY.keys()),
            )
            if repaired_name is not None and repaired_name != tool_name:
                logger.info(
                    "[%s] ✅ 工具名已修复：%s → %s（来自全部注册工具）",
                    claim.id, tool_name, repaired_name,
                )
                tool_name = repaired_name
            else:
                logger.warning(
                    "[%s] ⚠ 工具名修复失败：'%s' 无匹配的注册工具",
                    claim.id, tool_name,
                )

        if tool_name in disabled_note_tools:
            logger.warning("[%s] 模型请求了被用户禁用的工具：%s", claim.id, tool_name)
            allowed = "、".join(skill.allowed_tools) or "（无）"
            tool_output = (
                f"工具 \"{tool_name}\" 已被用户禁用，不可使用。"
                f"请改用其他可用工具（{allowed}）或直接给出结论。"
            )
            error = ErrorEvent(claim_id=claim.id, message=f"工具 {tool_name} 已被用户禁用")
            if result_sink is not None:
                result_sink["errors"].append(error.message)
            yield error

        elif tool_name not in skill.allowed_tools:
            logger.warning(
                "[%s] 工具 %s 不在 skill %s 白名单内", claim.id, tool_name, skill.name,
            )
            # 尝试用领域允许的工具名修复
            allowed_names = list(skill.allowed_tools)
            repaired_name = await self._repair_tool_name(
                wrong_name=tool_name,
                tool_input=tool_input,
                claim_text=claim.text,
                candidate_names=allowed_names,
            )
            if repaired_name is not None and repaired_name != tool_name:
                logger.info(
                    "[%s] 工具名已修复：%s → %s（来自领域 %s 白名单）",
                    claim.id, tool_name, repaired_name, skill.name,
                )
                tool_name = repaired_name
                # 重新检查修复后的工具名
                if tool_name in disabled_note_tools:
                    allowed = "、".join(skill.allowed_tools) or "（无）"
                    tool_output = (
                        f"工具 \"{tool_name}\" 已被用户禁用，不可使用。"
                        f"请改用其他可用工具（{allowed}）或直接给出结论。"
                    )
                    error = ErrorEvent(claim_id=claim.id, message=f"工具 {tool_name} 已被用户禁用")
                    if result_sink is not None:
                        result_sink["errors"].append(error.message)
                    yield error
                elif tool_name in TOOL_REGISTRY and tool_name in skill.allowed_tools:
                    # 修复成功且工具可用，执行
                    try:
                        tool_output = await TOOL_REGISTRY[tool_name].func(tool_input)
                        logger.info(
                            "[%s] 工具 %s 返回 %d 字符（名修复后执行）",
                            claim.id, tool_name, len(tool_output),
                        )
                    except Exception as exc:
                        logger.warning("[%s] 工具 %s 执行异常：%s", claim.id, tool_name, exc)
                        tool_output = f"工具调用失败：{exc}"
                        error = ErrorEvent(
                            claim_id=claim.id, message=f"工具 {tool_name} 异常：{exc}",
                        )
                        if result_sink is not None:
                            result_sink["errors"].append(error.message)
                        yield error
                else:
                    # 修复了但仍不可用（极端情况）
                    allowed = "、".join(skill.allowed_tools) or "（无）"
                    tool_output = (
                        f"当前领域（{skill.name}）不允许使用工具 \"{tool_name}\"。"
                        f"可用工具：{allowed}。"
                    )
                    error = ErrorEvent(
                        claim_id=claim.id,
                        message=f"工具 {tool_name} 不在 skill {skill.name} 白名单内",
                    )
                    if result_sink is not None:
                        result_sink["errors"].append(error.message)
                    yield error
            else:
                # 修复失败，使用原有错误信息
                allowed = "、".join(skill.allowed_tools) or "（无）"
                tool_output = (
                    f"当前领域（{skill.name}）不允许使用工具 \"{tool_name}\"。"
                    f"可用工具：{allowed}。"
                )
                error = ErrorEvent(
                    claim_id=claim.id,
                    message=f"工具 {tool_name} 不在 skill {skill.name} 白名单内",
                )
                if result_sink is not None:
                    result_sink["errors"].append(error.message)
                yield error

        else:
            # 工具名合法，直接执行
            try:
                tool_output = await TOOL_REGISTRY[tool_name].func(tool_input)
                logger.info(
                    "[%s] 工具 %s 返回 %d 字符", claim.id, tool_name, len(tool_output),
                )
            except Exception as exc:
                logger.warning("[%s] 工具 %s 执行异常：%s", claim.id, tool_name, exc)
                tool_output = f"工具调用失败：{exc}"
                error = ErrorEvent(claim_id=claim.id, message=f"工具 {tool_name} 异常：{exc}")
                if result_sink is not None:
                    result_sink["errors"].append(error.message)
                yield error

        # ── 3. 产出工具调用事件 & 记录 Observation ──
        display_output = tool_output[:500] + ("…" if len(tool_output) > 500 else "")
        tool_event = ToolCallEvent(
            claim_id=claim.id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=display_output,
        )
        if result_sink is not None:
            result_sink["tool_calls"].append(tool_event)
        yield tool_event
        observation = f"Observation: {tool_output}"
        # 最后一步提示：下一轮循环即将因 max_steps 退出，必须在本轮回复中给出结论。
        # 注意：此提示随当前 Observation 一起发给 LLM，LLM 在**下一轮**（step+1）生成回复时读到。
        # 若 step+1 == max_steps，下一轮是最后一轮；若 step+1 > max_steps，循环直接退出，
        # 届时由 _react_loop 末尾的强制结论调用兜底。
        if step + 1 == max_steps:
            observation += (
                f"\n\n⚠️ 最后一轮！收到此消息后你必须立即用 Final Answer 格式输出最终核查结论，"
                f"不得再调用任何工具、不得输出自然语言。"
            )
        elif step + 1 > max_steps:
            # 极端情况：max_steps 被动态缩减到当前步数以下（如工具修复消耗了额外步骤）。
            # Observation 本身就是"最后一轮"，LLM 会读到它。
            observation += (
                f"\n\n⚠️ 紧急！你必须在本轮立即用 Final Answer 格式输出结论，没有下一轮。"
            )
        messages.append({"role": "user", "content": observation})

    async def _debate_claim(
        self,
        claim,
        skill: Skill,
        overlays: list[Skill] | None,
        record_sink: dict[str, ClaimDebateRecord | None],
        disabled_note_tools: tuple[str, ...] = (),
        strategy: VerificationStrategy | None = None,
        domain_agent: object | None = None,
    ) -> AsyncGenerator[AgentState, None]:
        """
        策略驱动的辩论流（迭代四·方向1重构 + 方向5 领域专家池）：

        simple  → 单轮 Verifier（不强制工具），无辩论，直接输出
        medium  → ≤3 步 Verifier，跳过 Challenger，仅低置信度走 Judge
        complex → ≤6 步 Verifier + 完整 V→C→J + 可选 Reflexion

        方向5：通过 domain_agent 委托领域特化的 prompt 构建、策略微调、校准系数。
        为保持向后兼容，domain_agent 为 None 时行为与当前完全一致。
        """
        if strategy is None:
            strategy = _STRATEGY_MAP["complex"]

        yield DebateEvent(
            claim_id=claim.id,
            round=1,
            phase="started",
            role="coordinator",
            message=f"Coordinator 已发起核查（策略：{strategy.label}）",
            details={
                "skill": skill.name,
                "strategy": strategy.level,
                "max_steps": strategy.max_react_steps,
                "overlays": [overlay.name for overlay in overlays or []],
                "can_reverify": strategy.enable_rebuttal,
            },
        )

        # ── 方向5：构建领域专属 system prompt ──
        system_prompt: str | None
        if domain_agent is not None:
            system_prompt = domain_agent.build_system_prompt(
                overlays, disabled_note_tools,
                tool_required=strategy.tool_required,
            )
        else:
            system_prompt = None  # _react_loop 回退到 self._build_system_prompt()

        # ── 阶段 1：Verifier ReAct（所有策略共用）──
        verifier_sink: dict[str, Any] = {}
        async for event in self._react_loop(
            claim,
            skill,
            overlays,
            max_steps=strategy.max_react_steps,
            tool_required=strategy.tool_required,
            emit_annotation=False,
            result_sink=verifier_sink,
            disabled_note_tools=disabled_note_tools,
            system_prompt=system_prompt,
        ):
            yield event
        verifier_record = self._build_verification_record(claim, "verifier", verifier_sink)
        logger.info(
            "[%s] Verifier 初判：error_type=%s confidence=%.2f tool_calls=%d strategy=%s",
            claim.id,
            verifier_record.annotation.error_type,
            verifier_record.annotation.confidence,
            len(verifier_record.tool_calls),
            strategy.label,
        )

        yield DebateEvent(
            claim_id=claim.id,
            round=1,
            phase="argument",
            role="verifier",
            message=verifier_record.annotation.reasoning,
            stance="support" if verifier_record.annotation.error_type is None else "challenge",
            confidence=verifier_record.annotation.confidence,
            evidence_urls=list(verifier_record.annotation.evidence_urls),
            details={
                "error_type": verifier_record.annotation.error_type,
                "tool_calls": len(verifier_record.tool_calls),
                "strategy": strategy.level,
            },
        )

        # ═══════════════════════════════════════════════════════════
        # 阶段 2：按策略分流
        # ═══════════════════════════════════════════════════════════

        # ── simple 快速通道：直接采纳 Verifier 结论 ──
        if strategy.level == "simple":
            logger.info(
                "[%s] ★ simple 快速通道（%s）：跳过辩论直接输出，error_type=%s confidence=%.2f",
                claim.id, type(domain_agent).__name__,
                verifier_record.annotation.error_type,
                verifier_record.annotation.confidence,
            )
            pre_error_type = verifier_record.annotation.error_type
            calibrated = self._calibrate_annotation(verifier_record.annotation, verifier_record, domain_agent=domain_agent)
            simple_dropped = (pre_error_type is not None and calibrated.error_type is None)
            if simple_dropped:
                yield DebateEvent(
                    claim_id=claim.id,
                    round=1,
                    phase="argument",
                    role="coordinator",
                    message=f"⚠ 置信度不足（校准后 {calibrated.confidence:.0%} < {_MIN_FINAL_CONFIDENCE:.0%}），"
                            f"放弃「{pre_error_type}」判定，按无错误处理",
                    stance="neutral",
                )
            simple_result_msg = (
                f"[快速核验] 结论：证据不足，无错误（"
                f"{Agent._drop_reason(verifier_record)}"
                f"）"
                if simple_dropped else
                f"[快速核验] {calibrated.reasoning}"
            )
            yield DebateEvent(
                claim_id=claim.id,
                round=1,
                phase="result",
                role="verifier",
                message=simple_result_msg,
                stance="support" if calibrated.error_type is None else "challenge",
                confidence=calibrated.confidence,
                evidence_urls=list(calibrated.evidence_urls),
                details={
                    "final_error_type": calibrated.error_type,
                    "fast_path": True,
                    "strategy": "simple",
                    "reverify_target": claim.id,
                },
            )
            yield calibrated
            record_sink["record"] = ClaimDebateRecord(
                claim=claim,
                skill_name=skill.name,
                verifier=verifier_record,
                challenger=ChallengeRecord(
                    stance="support",
                    confidence=0.0,
                    reasoning="简单声明，跳过辩论。",
                ),
                judge_annotation=calibrated,
            )
            return

        # ── medium / complex：检查高置信度快速通道 ──
        should_skip = (
            domain_agent.should_skip_debate(verifier_record, strategy)
            if domain_agent is not None
            else (
                verifier_record.annotation.confidence >= strategy.high_confidence_threshold
                and not verifier_record.errors
            )
        )
        if should_skip:
            logger.info(
                "[%s] ★ %s-高置信度快速通道（%s）：confidence=%.2f >= %.2f errors=%d，跳过后续辩论",
                claim.id, type(domain_agent).__name__, strategy.label,
                verifier_record.annotation.confidence,
                strategy.high_confidence_threshold,
                len(verifier_record.errors),
            )
            pre_ht_error_type = verifier_record.annotation.error_type
            calibrated_annotation = self._calibrate_annotation(
                verifier_record.annotation, verifier_record, domain_agent=domain_agent,
            )
            dropped = (pre_ht_error_type is not None and calibrated_annotation.error_type is None)
            if dropped:
                yield DebateEvent(
                    claim_id=claim.id,
                    round=1,
                    phase="argument",
                    role="coordinator",
                    message=f"⚠ 置信度不足（校准后 {calibrated_annotation.confidence:.0%} < {_MIN_FINAL_CONFIDENCE:.0%}），"
                            f"放弃「{pre_ht_error_type}」判定，按无错误处理",
                    stance="neutral",
                )
            result_msg = (
                f"核查结论：证据不足，无错误（{Agent._drop_reason(verifier_record)}）"
                if dropped else
                f"Verifier 结论置信度 {verifier_record.annotation.confidence:.0%}"
                f"（≥{strategy.high_confidence_threshold:.0%}）且无工具错误，直接采纳。"
                f"{verifier_record.annotation.reasoning}"
            )
            yield DebateEvent(
                claim_id=claim.id,
                round=1,
                phase="result",
                role="verifier",
                message=result_msg,
                stance="support" if calibrated_annotation.error_type is None else "challenge",
                confidence=calibrated_annotation.confidence,
                evidence_urls=list(calibrated_annotation.evidence_urls),
                details={
                    "final_error_type": calibrated_annotation.error_type,
                    "fast_path": True,
                    "strategy": strategy.level,
                    "reverify_target": claim.id,
                },
            )
            yield calibrated_annotation
            record_sink["record"] = ClaimDebateRecord(
                claim=claim,
                skill_name=skill.name,
                verifier=verifier_record,
                challenger=ChallengeRecord(
                    stance="support",
                    confidence=0.0,
                    reasoning="高置信度快速通道，未执行挑战。",
                ),
                judge_annotation=calibrated_annotation,
            )
            return

        # ── medium 策略：默认跳过 Challenger，直接走 Judge。
        # 方向5：若领域 Agent 覆盖了 enable_challenger=True（如 MedicalAgent），则升级为完整辩论。
        if strategy.level == "medium" and not strategy.enable_challenger:
            logger.info("[%s] ★ medium 标准验证（%s）：跳过 Challenger，直接走 Judge 终裁", claim.id, type(domain_agent).__name__)
            challenge = ChallengeRecord(
                stance="support",
                confidence=0.0,
                reasoning="标准验证策略，未执行挑战环节。",
            )
            fallback_annotation = verifier_record.annotation
            judge_annotation, debate_summary = await self._run_judge(
                claim,
                skill.name,
                verifier_record,
                challenge,
                None,  # 无 rebuttal
                fallback_annotation,
                domain_agent=domain_agent,
            )
            pre_md_error_type = judge_annotation.error_type
            calibrated = self._calibrate_annotation(
                judge_annotation, verifier_record, domain_agent=domain_agent,
            )
            md_dropped = (pre_md_error_type is not None and calibrated.error_type is None)
            if md_dropped:
                yield DebateEvent(
                    claim_id=claim.id,
                    round=1,
                    phase="argument",
                    role="coordinator",
                    message=f"⚠ 置信度不足（校准后 {calibrated.confidence:.0%} < {_MIN_FINAL_CONFIDENCE:.0%}），"
                            f"放弃「{pre_md_error_type}」判定，按无错误处理",
                    stance="neutral",
                )
            logger.info(
                "[%s] Judge 终裁（medium）：error_type=%s confidence=%.2f",
                claim.id, calibrated.error_type, calibrated.confidence,
            )
            md_result_msg = (
                f"核查结论：证据不足，无错误（{Agent._drop_reason(verifier_record)}）"
                if md_dropped else debate_summary
            )
            yield DebateEvent(
                claim_id=claim.id,
                round=1,
                phase="result",
                role="judge",
                message=md_result_msg,
                stance="support" if calibrated.error_type is None else "challenge",
                confidence=calibrated.confidence,
                evidence_urls=list(calibrated.evidence_urls),
                details={
                    "final_error_type": calibrated.error_type,
                    "strategy": "medium",
                    "reverify_target": claim.id,
                },
            )
            yield calibrated
            record_sink["record"] = ClaimDebateRecord(
                claim=claim,
                skill_name=skill.name,
                verifier=verifier_record,
                challenger=challenge,
                judge_annotation=calibrated,
            )
            return

        # ── complex 策略：完整 V→C→J 辩论流（当前行为）──
        logger.info(
            "[%s] ★ %s 辩论（%s）：开始完整 V→C→J 流程（confidence=%.2f < %.2f 或有错误）",
            claim.id, strategy.level, type(domain_agent).__name__,
            verifier_record.annotation.confidence,
            strategy.high_confidence_threshold,
        )
        challenge = await self._run_challenger(claim.text, skill.name, verifier_record, domain_agent=domain_agent)
        logger.info(
            "[%s] Challenger 立场：stance=%s confidence=%.2f",
            claim.id, challenge.stance, challenge.confidence,
        )
        yield DebateEvent(
            claim_id=claim.id,
            round=1,
            phase="argument",
            role="challenger",
            message=challenge.reasoning,
            stance=challenge.stance,
            confidence=challenge.confidence,
            details={
                "missing_evidence": challenge.missing_evidence,
                "suggested_queries": challenge.suggested_queries,
            },
        )

        rebuttal_record: VerificationRecord | None = None
        if challenge.stance == "challenge" and strategy.enable_rebuttal:
            logger.info("[%s] Challenger 提出异议，触发第 2 轮重验证", claim.id)
            yield DebateEvent(
                claim_id=claim.id,
                round=2,
                phase="started",
                role="coordinator",
                message="Challenger 提出异议，Coordinator 已触发重验证",
                details={"reverify_target": claim.id},
            )
            rebuttal_sink: dict[str, Any] = {}
            async for event in self._react_loop(
                claim,
                skill,
                overlays,
                max_steps=strategy.max_react_steps,
                tool_required=strategy.tool_required,
                extra_instruction=build_rebuttal_instruction(challenge),
                emit_annotation=False,
                result_sink=rebuttal_sink,
                disabled_note_tools=disabled_note_tools,
                system_prompt=system_prompt,
            ):
                yield event
            rebuttal_record = self._build_verification_record(claim, "verifier_rebuttal", rebuttal_sink)
            logger.info(
                "[%s] 重验证结论：error_type=%s confidence=%.2f tool_calls=%d",
                claim.id,
                rebuttal_record.annotation.error_type,
                rebuttal_record.annotation.confidence,
                len(rebuttal_record.tool_calls),
            )
            yield DebateEvent(
                claim_id=claim.id,
                round=2,
                phase="argument",
                role="verifier",
                message=rebuttal_record.annotation.reasoning,
                stance="support" if rebuttal_record.annotation.error_type is None else "challenge",
                confidence=rebuttal_record.annotation.confidence,
                evidence_urls=list(rebuttal_record.annotation.evidence_urls),
                details={
                    "error_type": rebuttal_record.annotation.error_type,
                    "tool_calls": len(rebuttal_record.tool_calls),
                    "reverify_target": claim.id,
                },
            )

        fallback_annotation = (
            rebuttal_record.annotation if rebuttal_record is not None else verifier_record.annotation
        )
        judge_annotation, debate_summary = await self._run_judge(
            claim,
            skill.name,
            verifier_record,
            challenge,
            rebuttal_record,
            fallback_annotation,
            domain_agent=domain_agent,
        )

        # ── Reflexion 反思（complex 专属，迭代四新增）──
        if strategy.enable_reflexion and judge_annotation.error_type is not None:
            raw_conf = judge_annotation.confidence
            if _REFLEXION_LOW <= raw_conf < _REFLEXION_HIGH:
                logger.info(
                    "[%s] 触发 Reflexion（%s）：confidence=%.2f 处于灰色地带 [%.2f, %.2f)",
                    type(domain_agent).__name__,
                    claim.id, raw_conf, _REFLEXION_LOW, _REFLEXION_HIGH,
                )
                yield DebateEvent(
                    claim_id=claim.id,
                    round=2 if rebuttal_record is not None else 1,
                    phase="started",
                    role="coordinator",
                    message="置信度处于灰色地带，Coordinator 启动反思审查",
                    details={"reflexion_trigger": True, "pre_reflexion_confidence": raw_conf},
                )
                try:
                    judge_annotation = await self._run_reflexion(
                        claim, judge_annotation, verifier_record, rebuttal_record,
                        domain_agent=domain_agent,
                    )
                except Exception as exc:
                    logger.warning("[%s] Reflexion 执行异常，沿用原结论：%s", claim.id, exc)

        # 置信度校准：根据客观信号（工具调用、错误、证据链）调整 Judge 终裁
        pre_cx_error_type = judge_annotation.error_type
        calibrated_annotation = self._calibrate_annotation(
            judge_annotation, verifier_record, rebuttal_record, domain_agent=domain_agent,
        )
        cx_dropped = (pre_cx_error_type is not None and calibrated_annotation.error_type is None)

        if cx_dropped:
            yield DebateEvent(
                claim_id=claim.id,
                round=2 if rebuttal_record is not None else 1,
                phase="argument",
                role="coordinator",
                message=f"⚠ 置信度不足（校准后 {calibrated_annotation.confidence:.0%} < {_MIN_FINAL_CONFIDENCE:.0%}），"
                        f"放弃「{pre_cx_error_type}」判定，按无错误处理",
                stance="neutral",
            )

        logger.info(
            "[%s] Judge 终裁：error_type=%s confidence=%.2f revised=%s",
            claim.id,
            calibrated_annotation.error_type,
            calibrated_annotation.confidence,
            calibrated_annotation.error_type != verifier_record.annotation.error_type,
        )

        cx_result_msg = (
            f"核查结论：证据不足，无错误（{Agent._drop_reason(verifier_record, rebuttal_record)}）"
            if cx_dropped else debate_summary
        )
        yield DebateEvent(
            claim_id=claim.id,
            round=2 if rebuttal_record is not None else 1,
            phase="result",
            role="judge",
            message=cx_result_msg,
            stance="support" if calibrated_annotation.error_type is None else "challenge",
            confidence=calibrated_annotation.confidence,
            evidence_urls=list(calibrated_annotation.evidence_urls),
            details={
                "final_error_type": calibrated_annotation.error_type,
                "strategy": strategy.level,
                "reverify_target": claim.id,
            },
        )
        yield calibrated_annotation

        record_sink["record"] = ClaimDebateRecord(
            claim=claim,
            skill_name=skill.name,
            verifier=verifier_record,
            challenger=challenge,
            rebuttal=rebuttal_record,
            judge_annotation=calibrated_annotation,
        )

    async def _run_challenger(
        self,
        claim_text: str,
        skill_name: str,
        verifier_record: VerificationRecord,
        domain_agent: object | None = None,
    ):
        # 方向5：委托 DomainAgent 构建领域专属 Challenger prompt
        if domain_agent is not None:
            prompt = domain_agent.build_challenger_prompt(claim_text, skill_name, verifier_record)
        else:
            prompt = build_challenger_prompt(claim_text, skill_name, verifier_record)
        try:
            raw = await self._llm.complete([{"role": "user", "content": prompt}])
        except Exception:
            logger.warning("Challenger LLM 调用失败，维持初判", exc_info=True)
            return parse_challenger_response(
                '{"stance":"support","confidence":0.3,"reasoning":"挑战环节调用失败，维持初判。"}'
            )
        return parse_challenger_response(raw)

    async def _run_judge(
        self,
        claim,
        skill_name: str,
        verifier_record: VerificationRecord,
        challenge,
        rebuttal_record: VerificationRecord | None,
        fallback_annotation: AnnotationEvent,
        domain_agent: object | None = None,
    ) -> tuple[AnnotationEvent, str]:
        # 方向5：委托 DomainAgent 构建领域专属 Judge prompt
        if domain_agent is not None:
            prompt = domain_agent.build_judge_prompt(
                claim_text=claim.text,
                skill_name=skill_name,
                verifier=verifier_record,
                challenger=challenge,
                rebuttal=rebuttal_record,
            )
        else:
            prompt = build_judge_prompt(
                claim_text=claim.text,
                skill_name=skill_name,
                verifier=verifier_record,
                challenger=challenge,
                rebuttal=rebuttal_record,
            )
        try:
            raw = await self._llm.complete([{"role": "user", "content": prompt}])
        except Exception:
            logger.warning("[%s] Judge LLM 调用失败，回退到兜底结论", claim.id, exc_info=True)
            return fallback_annotation, fallback_annotation.reasoning
        return parse_judge_response(raw, claim, fallback_annotation)

    def _build_verification_record(
        self,
        claim,
        role: str,
        sink: dict[str, Any],
    ) -> VerificationRecord:
        annotation = sink.get("annotation")
        if not isinstance(annotation, AnnotationEvent):
            annotation = self._fallback_annotation(
                claim,
                reasoning="达到最大推理步数，未能得出稳定结论",
            )
        return VerificationRecord(
            role=role,
            annotation=annotation,
            tool_calls=list(sink.get("tool_calls", [])),
            thoughts=list(sink.get("thoughts", [])),
            errors=list(sink.get("errors", [])),
        )

    def _fallback_annotation(self, claim, reasoning: str) -> AnnotationEvent:
        return AnnotationEvent(
            claim_id=claim.id,
            text=claim.text,
            start_offset=claim.position[0],
            end_offset=claim.position[1],
            error_type=None,
            confidence=0.0,
            reasoning=reasoning,
            evidence_urls=[],
        )

    def _build_failed_claim_record(self, claim, skill_name: str, error_message: str) -> ClaimDebateRecord:
        # 崩溃的声明没有真正完成验证 → error_type=None, confidence=0
        annotation = AnnotationEvent(
            claim_id=claim.id,
            text=claim.text,
            start_offset=claim.position[0],
            end_offset=claim.position[1],
            error_type=None,
            confidence=0.0,
            reasoning=f"辩论流程异常终止：{error_message}（此声明未被完成验证）",
            evidence_urls=[],
        )
        verifier = VerificationRecord(role="verifier", annotation=annotation, errors=[error_message])
        challenger = ChallengeRecord(
            stance="support",
            confidence=0.0,
            reasoning="挑战流程未执行，保留兜底结论。",
        )
        return ClaimDebateRecord(
            claim=claim,
            skill_name=skill_name,
            verifier=verifier,
            challenger=challenger,
            judge_annotation=annotation,
        )

    async def analyze_claims(
        self,
        claims: list[str],
        skill_name: str | None = None,
        overlays: list[dict] | None = None,
        disabled_tools: list[str] | None = None,
        disable_skill_routing: bool = False,
        debate_mode: str = "full",
    ) -> dict[str, Any]:
        """离线实验入口：直接验证 claim 列表，跳过文章扫描/规划 DAG。

        debate_mode:
          - full: 复用现有完整辩论管线（Verifier / Challenger / Judge）
          - verifier_only: 仅运行 Verifier ReAct
          - verifier_challenger: 运行 Verifier + Challenger，最终采纳 Verifier 结论
        """
        from .agents import get_domain_agent
        from .debate import build_claim_result, build_summary_event
        from .models import Claim
        from .skills import build_overlay_skill, route_skill
        from .skills.base import GENERAL_SKILL_NAME

        if debate_mode not in {"full", "verifier_only", "verifier_challenger"}:
            raise ValueError(f"不支持的 debate_mode：{debate_mode}")

        active_overlays = []
        for raw_overlay in overlays or []:
            try:
                active_overlays.append(build_overlay_skill(raw_overlay))
            except (TypeError, ValueError) as exc:
                logger.warning("跳过无效 overlay：%s", exc)

        disabled = self._normalize_disabled(disabled_tools)
        records: list[ClaimDebateRecord] = []
        skill_map: dict[str, str] = {}

        async def _resolve_skill(claim_text: str):
            if disable_skill_routing:
                return self._skills[GENERAL_SKILL_NAME]
            if skill_name:
                if skill_name not in self._skills:
                    raise ValueError(
                        f"未知 skill：{skill_name}；可选值：{sorted(self._skills)}"
                    )
                return self._skills[skill_name]
            return await route_skill(claim_text, self._skills, self._router_llm)

        for idx, text in enumerate(claims, start=1):
            claim_text = str(text).strip()
            claim = Claim(
                id=f"c{idx:03d}",
                text=claim_text,
                position=(0, len(claim_text)),
                suspicion_score=1.0,
                complexity="complex",
                complexity_confidence=1.0,
            )

            try:
                skill = await _resolve_skill(claim.text)
                removed = tuple(t for t in skill.allowed_tools if t in disabled)
                effective_skill = replace(
                    skill,
                    allowed_tools=tuple(t for t in skill.allowed_tools if t not in disabled),
                )
                domain_agent = self._domain_agent_cache.get(effective_skill.name)
                if domain_agent is None:
                    domain_agent = get_domain_agent(effective_skill.name, effective_skill, self._llm)
                    self._domain_agent_cache[effective_skill.name] = domain_agent

                skill_map[claim.id] = effective_skill.name
                strategy = domain_agent.merge_strategy("complex")

                if debate_mode == "full":
                    record_sink: dict[str, ClaimDebateRecord | None] = {"record": None}
                    async for _event in self._debate_claim(
                        claim,
                        effective_skill,
                        active_overlays,
                        record_sink,
                        removed,
                        strategy=strategy,
                        domain_agent=domain_agent,
                    ):
                        pass
                    record = record_sink.get("record")
                    if record is None:
                        record = self._build_failed_claim_record(
                            claim,
                            effective_skill.name,
                            "完整辩论流程未返回记录",
                        )
                elif debate_mode == "verifier_only":
                    record = await self._analyze_claim_verifier_only(
                        claim,
                        effective_skill,
                        active_overlays,
                        removed,
                        strategy,
                        domain_agent,
                    )
                else:
                    record = await self._analyze_claim_verifier_challenger(
                        claim,
                        effective_skill,
                        active_overlays,
                        removed,
                        strategy,
                        domain_agent,
                    )
                records.append(record)
            except Exception as exc:
                logger.exception("[%s] 离线 claim 分析失败", claim.id)
                fallback_skill = skill_name or GENERAL_SKILL_NAME
                skill_map[claim.id] = fallback_skill
                records.append(self._build_failed_claim_record(claim, fallback_skill, str(exc)))

        summary_event = build_summary_event(records)
        return {
            "skills": skill_map,
            "config": {
                "disable_skill_routing": disable_skill_routing,
                "debate_mode": debate_mode,
                "skill": skill_name,
                "overlays": [overlay.name for overlay in active_overlays],
                "disabled_tools": sorted(disabled),
            },
            "summary": summary_event.model_dump(),
            "claim_results": [build_claim_result(record) for record in records],
        }

    async def _analyze_claim_verifier_only(
        self,
        claim,
        skill: Skill,
        overlays: list[Skill] | None,
        removed_tools: tuple[str, ...],
        strategy: VerificationStrategy,
        domain_agent: object | None,
    ) -> ClaimDebateRecord:
        system_prompt = (
            domain_agent.build_system_prompt(
                overlays,
                removed_tools,
                tool_required=strategy.tool_required,
            )
            if domain_agent is not None else None
        )
        verifier_sink: dict[str, Any] = {}
        async for _event in self._react_loop(
            claim,
            skill,
            overlays,
            max_steps=strategy.max_react_steps,
            tool_required=strategy.tool_required,
            emit_annotation=False,
            result_sink=verifier_sink,
            disabled_note_tools=removed_tools,
            system_prompt=system_prompt,
        ):
            pass

        verifier_record = self._build_verification_record(claim, "verifier", verifier_sink)
        final_annotation = self._calibrate_annotation(
            verifier_record.annotation,
            verifier_record,
            domain_agent=domain_agent,
        )
        return ClaimDebateRecord(
            claim=claim,
            skill_name=skill.name,
            verifier=verifier_record,
            challenger=ChallengeRecord(
                stance="support",
                confidence=0.0,
                reasoning="消融实验：仅运行 Verifier，未执行 Challenger/Judge。",
            ),
            judge_annotation=final_annotation,
        )

    async def _analyze_claim_verifier_challenger(
        self,
        claim,
        skill: Skill,
        overlays: list[Skill] | None,
        removed_tools: tuple[str, ...],
        strategy: VerificationStrategy,
        domain_agent: object | None,
    ) -> ClaimDebateRecord:
        system_prompt = (
            domain_agent.build_system_prompt(
                overlays,
                removed_tools,
                tool_required=strategy.tool_required,
            )
            if domain_agent is not None else None
        )
        verifier_sink: dict[str, Any] = {}
        async for _event in self._react_loop(
            claim,
            skill,
            overlays,
            max_steps=strategy.max_react_steps,
            tool_required=strategy.tool_required,
            emit_annotation=False,
            result_sink=verifier_sink,
            disabled_note_tools=removed_tools,
            system_prompt=system_prompt,
        ):
            pass

        verifier_record = self._build_verification_record(claim, "verifier", verifier_sink)
        challenge = await self._run_challenger(
            claim.text,
            skill.name,
            verifier_record,
            domain_agent=domain_agent,
        )
        final_annotation = self._calibrate_annotation(
            verifier_record.annotation,
            verifier_record,
            domain_agent=domain_agent,
        )
        return ClaimDebateRecord(
            claim=claim,
            skill_name=skill.name,
            verifier=verifier_record,
            challenger=challenge,
            judge_annotation=final_annotation,
        )

    async def _run_reflexion(
        self,
        claim,
        judge_annotation: AnnotationEvent,
        verifier_record: VerificationRecord,
        rebuttal_record: VerificationRecord | None = None,
        domain_agent: object | None = None,
    ) -> AnnotationEvent:
        """
        反思审查（迭代四·方向1，complex 专属 + 方向5 领域特化）。

        当 Judge 终裁置信度处于灰色地带 [REFLEXION_LOW, REFLEXION_HIGH) 时触发：
        Verifier 回顾自己的推理链，识别可能的遗漏或逻辑缺陷，
        基于反思结果修正结论（可能修正 error_type 或调整 confidence）。
        """
        tool_summaries: list[str] = []
        for tc in verifier_record.tool_calls:
            tool_summaries.append(f"[{tc.tool_name}]({tc.tool_input[:100]}) → {tc.tool_output[:200]}")
        if rebuttal_record:
            for tc in rebuttal_record.tool_calls:
                tool_summaries.append(f"[{tc.tool_name}]({tc.tool_input[:100]}) → {tc.tool_output[:200]}")

        errors = verifier_record.errors + (rebuttal_record.errors if rebuttal_record else [])

        tool_summary_text = "\n".join(tool_summaries) if tool_summaries else "（无可用的工具调用记录）"
        error_summary_text = "\n".join(errors) if errors else "（无工具错误）"

        # 方向5：委托 DomainAgent 构建领域专属 Reflexion prompt
        if domain_agent is not None:
            prompt = domain_agent.build_reflexion_prompt(
                claim_text=claim.text,
                original_error_type=judge_annotation.error_type or "无",
                original_confidence=judge_annotation.confidence,
                original_reasoning=judge_annotation.reasoning,
                tool_calls_summary=tool_summary_text,
                errors_summary=error_summary_text,
            )
        else:
            prompt = build_reflexion_prompt(
                claim_text=claim.text,
                original_error_type=judge_annotation.error_type or "无",
                original_confidence=judge_annotation.confidence,
                original_reasoning=judge_annotation.reasoning,
                tool_calls_summary=tool_summary_text,
                errors_summary=error_summary_text,
            )
        try:
            raw = await self._llm.complete([{"role": "user", "content": prompt}])
        except Exception as exc:
            logger.warning("[%s] Reflexion LLM 调用失败，沿用原结论：%s", claim.id, exc)
            return judge_annotation

        revised = parse_reflexion_response(raw, claim, judge_annotation)
        logger.info(
            "[%s] Reflexion 修正：error_type %s→%s confidence %.2f→%.2f",
            claim.id,
            judge_annotation.error_type, revised.error_type,
            judge_annotation.confidence, revised.confidence,
        )
        return revised

    @staticmethod
    def _drop_reason(
        verifier_record: VerificationRecord,
        rebuttal_record: VerificationRecord | None = None,
        domain_agent: object | None = None,
    ) -> str:
        """生成置信度不足的具体原因说明。"""
        reasons: list[str] = []
        all_tool_calls = list(verifier_record.tool_calls)
        all_errors = list(verifier_record.errors)
        if rebuttal_record is not None:
            all_tool_calls.extend(rebuttal_record.tool_calls)
            all_errors.extend(rebuttal_record.errors)

        if not all_tool_calls:
            reasons.append("未调用任何核查工具")
        if all_errors:
            reasons.append(f"有 {len(all_errors)} 个工具执行错误")
        if not verifier_record.annotation.evidence_urls:
            reasons.append("无证据来源 URL")
        if len(verifier_record.thoughts) >= 5:
            reasons.append("推理步数过多")

        if not reasons:
            reasons.append("客观信号不足")
        return "；".join(reasons)

    @staticmethod
    def _calibrate_annotation(
        annotation: AnnotationEvent,
        verifier_record: VerificationRecord,
        rebuttal_record: VerificationRecord | None = None,
        domain_agent: object | None = None,
    ) -> AnnotationEvent:
        """对终裁置信度做客观信号修正，低于阈值则忽略该 claim。

        步骤：
        1. 根据核查过程客观信号调整 LLM 自评置信度（乘法系数）
        2. 合并 DomainAgent 的领域校准系数偏置（方向5）
        3. 调整后若 < _MIN_FINAL_CONFIDENCE（默认 60%），
           且原结论声称有错误，则将 error_type 置为 None（忽略该发现）
        """
        raw = annotation.confidence
        multiplier = 1.0

        # ── 方向5：获取领域特定校准系数 ──
        domain_multipliers: dict[str, float] = {}
        if domain_agent is not None:
            domain_multipliers = domain_agent.get_calibration_multipliers()

        # 汇总两轮核查的工具调用、错误、步数
        all_tool_calls = list(verifier_record.tool_calls)
        all_errors = list(verifier_record.errors)
        all_thoughts = list(verifier_record.thoughts)
        if rebuttal_record is not None:
            all_tool_calls.extend(rebuttal_record.tool_calls)
            all_errors.extend(rebuttal_record.errors)
            all_thoughts.extend(rebuttal_record.thoughts)

        # 默认校准系数（领域可通过 domain_multipliers 覆盖）
        _no_tool = domain_multipliers.get("no_tool", 0.80)
        _tool_error = domain_multipliers.get("tool_error", 0.85)
        _no_evidence_url = domain_multipliers.get("no_evidence_url", 0.90)
        _too_many_steps = domain_multipliers.get("too_many_steps", 0.90)
        _diverse_tools = domain_multipliers.get("diverse_tools", 1.10)

        if not all_tool_calls:
            multiplier *= _no_tool       # 未调工具：轻微惩罚
        if all_errors:
            multiplier *= _tool_error    # 有工具错误：轻微惩罚
        if not annotation.evidence_urls:
            multiplier *= _no_evidence_url  # 无证据 URL：轻微惩罚
        if len(all_thoughts) >= 5:
            multiplier *= _too_many_steps    # 步数过多：轻微惩罚

        distinct_tools = {tc.tool_name for tc in all_tool_calls}
        if len(distinct_tools) >= 2:
            multiplier *= _diverse_tools

        calibrated = max(0.0, min(1.0, raw * multiplier))

        # ── 核心规则：低于阈值 → 忽略该发现 ──
        if calibrated < _MIN_FINAL_CONFIDENCE and annotation.error_type is not None:
            logger.info(
                "[%s] 终裁置信度 %.0f%% < %.0f%%，忽略该 claim（error_type %s → None）",
                annotation.claim_id,
                calibrated,
                _MIN_FINAL_CONFIDENCE,
                annotation.error_type,
            )
            return AnnotationEvent(
                claim_id=annotation.claim_id,
                text=annotation.text,
                start_offset=annotation.start_offset,
                end_offset=annotation.end_offset,
                error_type=None,
                confidence=round(calibrated, 4),
                reasoning=(
                    f"校准后置信度仅 {calibrated:.0%}（阈值 {_MIN_FINAL_CONFIDENCE:.0%}），"
                    f"证据不足以支持「{annotation.error_type}」判定，按无错误处理。"
                    f"原因：{Agent._drop_reason(verifier_record, rebuttal_record)}"
                ),
                evidence_urls=annotation.evidence_urls,
            )

        # 无实质变化则原样返回
        if abs(calibrated - raw) < 0.01:
            return annotation

        logger.info(
            "[%s] 置信度校准：%.0f%% → %.0f%%（系数 %.2f）",
            annotation.claim_id, raw, calibrated, multiplier,
        )
        return annotation.model_copy(update={
            "confidence": round(calibrated, 4),
            "reasoning": f"{annotation.reasoning} [置信度校准：{raw:.0%}→{calibrated:.0%}]",
        })

    async def _react_loop(
        self,
        claim,
        skill: Skill,
        overlays: list[Skill] | None = None,
        max_steps: int = 6,
        tool_required: bool = True,
        extra_instruction: str | None = None,
        emit_annotation: bool = True,
        result_sink: dict[str, Any] | None = None,
        disabled_note_tools: tuple[str, ...] = (),
        system_prompt: str | None = None,
    ) -> AsyncGenerator[AgentState, None]:
        # 空集降级：无可用工具时不再强制调用工具，允许直接给出保守结论。
        # 迭代四：tool_required 参数允许 simple 策略不强制调工具
        # 方向5：system_prompt 可选参数，传入时直接使用（领域 persona），否则回退默认
        if skill.allowed_tools and tool_required:
            tool_requirement = "要求：你必须至少调用一次工具进行外部核实，不得直接给出 Final Answer。"
        elif not skill.allowed_tools:
            tool_requirement = "要求：当前无可用工具，请基于声明本身与常识保守判断，可直接给出 Final Answer。"
        else:
            tool_requirement = "你可以根据需要调用工具，也可直接给出 Final Answer。"
        user_prompt = f"请验证以下声明是否存在错误：\n\n{claim.text}\n\n{tool_requirement}"
        if extra_instruction:
            user_prompt = f"{user_prompt}\n\n补充要求：{extra_instruction}"

        messages: list[dict] = [
            {"role": "system", "content": system_prompt or self._build_system_prompt(
                skill, overlays, disabled_note_tools, tool_required=tool_required,
            )},
            {"role": "user", "content": user_prompt},
        ]

        if result_sink is not None:
            result_sink.setdefault("tool_calls", [])
            result_sink.setdefault("thoughts", [])
            result_sink.setdefault("errors", [])

        logger.debug("[%s] ReAct 循环开始（rebuttal=%s max_steps=%d）", claim.id, extra_instruction is not None, max_steps)
        step = 0
        while step < max_steps:
            try:
                raw_response = await self._llm.complete(messages)
            except Exception as exc:
                logger.warning("[%s] ReAct 步骤 %d LLM 调用失败：%s", claim.id, step + 1, exc)
                error = ErrorEvent(claim_id=claim.id, message=f"LLM 调用失败（步骤{step + 1}）：{exc}")
                if result_sink is not None:
                    result_sink["errors"].append(error.message)
                yield error
                break

            messages.append({"role": "assistant", "content": raw_response})
            # 记录每步 LLM 原始回复（用 %r 保留换行/markdown 等真实字符），便于排查格式异常
            logger.info("[%s] ReAct 步骤 %d LLM 原始回复：%r", claim.id, step + 1, raw_response)
            parsed = self._parse_react_response(raw_response)
            logger.debug("[%s] ReAct 步骤 %d 解析类型=%s", claim.id, step + 1, parsed["type"])

            if parsed["type"] == "final":
                thought = parsed["thought"]
                answer = parsed["answer"]
                if thought:
                    thinking_event = ThinkingEvent(claim_id=claim.id, thought=thought)
                    if result_sink is not None:
                        result_sink["thoughts"].append(thought)
                    yield thinking_event

                error_type = answer.get("error_type")
                if error_type not in {
                    "factual_error",
                    "logical_fallacy",
                    "contradiction",
                    "unsupported_claim",
                    None,
                }:
                    error_type = "unsupported_claim"

                annotation = AnnotationEvent(
                    claim_id=claim.id,
                    text=claim.text,
                    start_offset=claim.position[0],
                    end_offset=claim.position[1],
                    error_type=error_type,
                    confidence=float(answer.get("confidence", 0.5)),
                    reasoning=str(answer.get("reasoning", "")).strip(),
                    evidence_urls=answer.get("evidence_urls", []),
                )
                logger.info(
                    "[%s] ReAct 在第 %d 步给出 Final Answer：error_type=%s confidence=%.2f",
                    claim.id, step + 1, error_type, annotation.confidence,
                )
                if result_sink is not None:
                    result_sink["annotation"] = annotation
                if emit_annotation:
                    yield annotation
                return

            if parsed["type"] == "action":
                logger.info(
                    "[%s] ReAct 步骤 %d 调用工具 %s，输入=%r",
                    claim.id, step + 1, parsed["action"], parsed["action_input"],
                )
                async for evt in self._handle_tool_action(
                    claim=claim, skill=skill, disabled_note_tools=disabled_note_tools,
                    tool_name=parsed["action"], tool_input=parsed["action_input"],
                    thought=parsed["thought"], result_sink=result_sink,
                    step=step, messages=messages, max_steps=max_steps,
                ):
                    yield evt
                step += 1
                continue

            logger.warning(
                "[%s] ReAct 步骤 %d 输出格式异常（解析类型=%s），原始回复：%r",
                claim.id, step + 1, parsed["type"], raw_response,
            )

            # ── 尝试自动修复格式：单次独立 LLM 请求做格式转换 ──
            logger.info(
                "[%s] 🔄 格式异常，启动 Reformatter 自动修复（%d字符）…",
                claim.id, len(raw_response),
            )
            reformatted = await self._reformat_response(raw_response, claim.text)
            if reformatted is not None and reformatted != raw_response:
                logger.info(
                    "[%s] Reformatter 将格式异常输出转换为正确格式",
                    claim.id,
                )
                # 替换 messages 中最后一条 assistant 消息，用 reformatted 版
                messages[-1]["content"] = reformatted
                reparsed = self._parse_react_response(reformatted)

                if reparsed["type"] == "final":
                    # 格式化成功后直接按 final 分支处理
                    thought = reparsed["thought"]
                    answer = reparsed["answer"]
                    if thought:
                        thinking_event = ThinkingEvent(claim_id=claim.id, thought=thought)
                        if result_sink is not None:
                            result_sink["thoughts"].append(thought)
                        yield thinking_event
                    error_type = answer.get("error_type")
                    if error_type not in {
                        "factual_error", "logical_fallacy", "contradiction",
                        "unsupported_claim", None,
                    }:
                        error_type = "unsupported_claim"
                    annotation = AnnotationEvent(
                        claim_id=claim.id,
                        text=claim.text,
                        start_offset=claim.position[0],
                        end_offset=claim.position[1],
                        error_type=error_type,
                        confidence=float(answer.get("confidence", 0.5)),
                        reasoning=str(answer.get("reasoning", "")).strip(),
                        evidence_urls=answer.get("evidence_urls", []),
                    )
                    logger.info(
                        "[%s] ReAct 在第 %d 步给出 Final Answer（reformatter 修正后）：error_type=%s confidence=%.2f",
                        claim.id, step + 1, error_type, annotation.confidence,
                    )
                    if result_sink is not None:
                        result_sink["annotation"] = annotation
                    if emit_annotation:
                        yield annotation
                    return

                if reparsed["type"] == "action":
                    # 格式化成功后直接按 action 分支处理
                    logger.info(
                        "[%s] ReAct 步骤 %d 调用工具 %s（reformatter 修正后），输入=%r",
                        claim.id, step + 1, reparsed["action"], reparsed["action_input"],
                    )
                    async for evt in self._handle_tool_action(
                        claim=claim, skill=skill, disabled_note_tools=disabled_note_tools,
                        tool_name=reparsed["action"], tool_input=reparsed["action_input"],
                        thought=reparsed["thought"], result_sink=result_sink,
                        step=step, messages=messages, max_steps=max_steps,
                    ):
                        yield evt
                    continue

            # reformatter 失败，回退到现有纠错反馈机制
            error = ErrorEvent(claim_id=claim.id, message=f"LLM 输出格式异常（步骤{step + 1}），已尝试自动修复")
            if result_sink is not None:
                result_sink["errors"].append(error.message)
            yield error

            raw_snippet = raw_response[:200].replace("\n", "\\n")
            correction_hint = (
                f"你的上一轮输出格式不符合要求。你的输出是：\n{raw_snippet}\n\n"
                "请严格按以下两种格式之一输出，不要添加任何额外文字、markdown 代码块或编号：\n\n"
                "格式一（调用工具）：\n"
                "Thought: <推理>\n"
                "Action: <工具名>\n"
                "Action Input: <输入>\n\n"
                "格式二（给出结论）：\n"
                "Thought: <推理>\n"
                'Final Answer: {"error_type": "factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null, "confidence": 0.0~1.0, "reasoning": "中文摘要", "evidence_urls": [...]}'
            )
            messages.append({"role": "user", "content": correction_hint})
            step += 1

        # ── 强制结论调用 ──
        # 循环耗尽步数但未产出 Final Answer（常见于 simple 策略 2 步内调了工具但未及结论）。
        # 追加一次纯结论 LLM 请求：不允许调工具，强制基于已有证据输出 Final Answer。
        tc_names = {tc.tool_name for tc in result_sink.get("tool_calls", [])} if result_sink else set()
        if tc_names:
            logger.info(
                "[%s] ReAct 达到最大步数 %d 仍未给出结论（已调用工具：%s），启动强制结论调用",
                claim.id, max_steps, ", ".join(sorted(tc_names)),
            )
            tool_summaries = "\n".join(
                f"[{tc.tool_name}] 输入: {tc.tool_input[:200]}\n  → 输出: {tc.tool_output[:500]}"
                for tc in result_sink["tool_calls"]
            )
            force_prompt = (
                "你已经完成了所有核查工具调用。以下是工具返回结果汇总：\n\n"
                f"{tool_summaries}\n\n"
                "⚠️ 现在请基于以上所有证据，立即用 Final Answer 格式给出你的最终核查结论。\n"
                "你**绝对不能**再调用任何工具（不要输出 Action/Action Input），必须直接输出：\n"
                "Thought: <基于证据的最终推理，一句话>\n"
                'Final Answer: {"error_type": "factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null, '
                '"confidence": 0.0~1.0, "reasoning": "中文推理摘要", "evidence_urls": ["url1", "url2"]}'
            )
            messages.append({"role": "user", "content": force_prompt})
            try:
                raw_response = await self._llm.complete(messages)
                parsed = self._parse_react_response(raw_response)
                if parsed["type"] == "final":
                    thought = parsed["thought"]
                    answer = parsed["answer"]
                    if thought:
                        thinking_event = ThinkingEvent(claim_id=claim.id, thought=thought)
                        if result_sink is not None:
                            result_sink["thoughts"].append(thought)
                        yield thinking_event
                    error_type = answer.get("error_type")
                    if error_type not in {"factual_error", "logical_fallacy", "contradiction", "unsupported_claim", None}:
                        error_type = "unsupported_claim"
                    annotation = AnnotationEvent(
                        claim_id=claim.id, text=claim.text,
                        start_offset=claim.position[0], end_offset=claim.position[1],
                        error_type=error_type,
                        confidence=float(answer.get("confidence", 0.5)),
                        reasoning=str(answer.get("reasoning", "")).strip(),
                        evidence_urls=answer.get("evidence_urls", []),
                    )
                    logger.info(
                        "[%s] 强制结论调用成功：error_type=%s confidence=%.2f reasoning=%s",
                        claim.id, error_type, annotation.confidence,
                        annotation.reasoning[:120],
                    )
                    if result_sink is not None:
                        result_sink["annotation"] = annotation
                    if emit_annotation:
                        yield annotation
                    return
                else:
                    logger.warning(
                        "[%s] 强制结论调用产出非 final 输出（类型=%s），回退兜底",
                        claim.id, parsed["type"],
                    )
            except Exception as exc:
                logger.warning("[%s] 强制结论调用失败：%s，回退兜底", claim.id, exc)

        # ── 最终兜底（强制结论调用也失败 / 没有任何工具调用）──
        if tc_names:
            fallback_reason = (
                f"ReAct 循环在 {max_steps} 步内未给出 Final Answer。"
                f"已调用工具：{', '.join(sorted(tc_names))}，但未在步数限制内输出最终结论。"
            )
        else:
            fallback_reason = f"ReAct 循环在 {max_steps} 步内未调用任何工具也未给出结论。"
        logger.warning("[%s] ReAct 达到最大步数 %d 仍未给出结论，使用兜底标注", claim.id, max_steps)
        fallback = self._fallback_annotation(claim, fallback_reason)
        if result_sink is not None:
            result_sink["annotation"] = fallback
        if emit_annotation:
            yield fallback

    # ------------------------------------------------------------------
    # 聊天功能：基于会话上下文的追问回答
    # ------------------------------------------------------------------

    @staticmethod
    def _build_chat_system_prompt(session_context: dict) -> str:
        """根据会话上下文构建聊天 system prompt。"""
        article_title = session_context.get("article_title") or "未知文章"
        article_text = session_context.get("article_text") or ""
        article_summary = session_context.get("article_summary") or ""
        claims = session_context.get("claims") or []
        summary = session_context.get("summary") or {}
        rag_context = session_context.get("rag_context") or ""

        # 文章内容：优先使用 LLM 摘要，否则回退到原文截断
        if article_summary:
            article_section = f"## 文章摘要\n{article_summary}"
        else:
            article_snippet = article_text[:3000]
            if len(article_text) > 3000:
                article_snippet += "\n…（文章过长，已截断）"
            article_section = f"## 文章正文（部分）\n{article_snippet}"

        # 构建声明摘要
        claims_lines: list[str] = []
        for c in claims:
            verdict_icon = {
                "verified": "✅",
                "rejected": "❌",
                "pending": "⏳",
            }.get(c.get("verdict", "pending"), "❓")
            err = c.get("error_type") or "无"
            conf = c.get("confidence")
            conf_str = f"{conf:.0%}" if conf is not None else "N/A"
            reasoning = (c.get("reasoning") or "")[:200]
            evidence = c.get("evidence_urls") or []
            ev_str = ", ".join(evidence[:3]) if evidence else "无"

            claims_lines.append(
                f"- [{c.get('claim_id', '?')}] {verdict_icon} {c.get('text', '')}\n"
                f"  判定: {verdict_icon} | 错误类型: {err} | 置信度: {conf_str}\n"
                f"  推理: {reasoning}\n"
                f"  证据: {ev_str}"
            )

        claims_text = "\n\n".join(claims_lines) if claims_lines else "（无声明记录）"

        overall = summary.get("overall_conclusion") or "尚无总结"

        # RAG 参考文档段落
        rag_section = ""
        if rag_context:
            rag_section = (
                f"\n\n## 用户上传的参考文档（RAG）\n"
                f"以下是从用户上传的文档中检索到的相关片段，请优先参考这些内容回答问题：\n\n"
                f"{rag_context}\n"
            )

        return (
            "你是一个专业的事实核查助手。用户正在查看一篇你已完成分析的文章，"
            "现在向你提出追问。请基于以下上下文（文章内容、声明验证结果、总结"
            f"{'、用户上传的参考文档' if rag_context else ''}）"
            "回答用户的问题。\n\n"
            "## 回答原则\n"
            "- 引用上下文中的具体声明 ID 和证据 URL，增强可信度\n"
            "- 如果用户上传了参考文档，优先从文档中查找答案\n"
            "- 如果问题超出上下文范围，诚实说明你无法回答\n"
            "- 回答简洁、准确，避免冗长的推理过程复述\n"
            "- 使用中文回答\n\n"
            f"## 文章信息\n标题: {article_title}\n\n"
            f"{article_section}\n\n"
            f"## 声明验证结果\n{claims_text}\n\n"
            f"## 分析总结\n{overall}"
            f"{rag_section}\n"
        )

    async def chat(
        self,
        session_context: dict,
        user_message: str,
        history: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        基于会话上下文回答用户追问，逐 token 流式 yield 回复。

        参数：
          session_context: build_chat_context() 的返回值
          user_message:    用户当前追问文本
          history:         历史对话消息列表 [{"role":"user","content":"..."}, ...]
        """
        logger.info(
            "chat() 收到追问：msg_len=%d history=%d claims=%d",
            len(user_message), len(history or []), len(session_context.get("claims") or []),
        )
        system_prompt = self._build_chat_system_prompt(session_context)

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]

        # 注入历史对话
        if history:
            for msg in history[-20:]:  # 最近 20 条
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant"):
                    messages.append({"role": role, "content": content})

        # 当前用户追问
        messages.append({"role": "user", "content": user_message})

        # 流式生成
        async for token in self._chat_llm.complete_stream(messages):
            yield token
