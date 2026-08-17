"""
agent/workflow/nodes/route.py

RouteNode — 领域路由 + DomainAgent 实例化 + 工具禁用处理。
包装 agent.skills.router.route_skill() + agent.agents.registry.get_domain_agent()。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..node import NodeOutput, WorkflowContext, WorkflowNode

logger = logging.getLogger("agent.workflow.nodes.route")


class RouteNode(WorkflowNode):
    """领域路由 + DomainAgent 实例化。

    ctx 输入键：
      - article_text: str
      - _skills: dict[str, Skill]
      - _router_llm: BaseLLMClient
      - _llm: BaseLLMClient（传给 DomainAgent）
      - _domain_agent_cache: dict[str, DomainAgent]
      - disabled_tools: list[str] | None
      - overlays: list[dict] | None

    ctx 输出键：
      - skill: Skill
      - effective_skill: Skill
      - domain_agent: DomainAgent
      - removed_tools: tuple[str, ...]
      - active_overlays: list[Skill]
    """

    name = "route"
    description = "领域路由 + DomainAgent 实例化 + 工具禁用处理"
    input_keys = ("article_text", "_skills", "_router_llm", "_llm")
    output_keys = ("skill", "effective_skill", "domain_agent", "removed_tools", "active_overlays")

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        from agent.agents import get_domain_agent
        from agent.models import StatusEvent
        from agent.skills import build_overlay_skill, route_skill
        from agent.tools.registry import TOOL_REGISTRY

        article_text = ctx["article_text"]
        skills = ctx["_skills"]
        router_llm = ctx["_router_llm"]
        llm = ctx["_llm"]
        disabled_tools_raw = ctx.get("disabled_tools") or []
        overlays_raw = ctx.get("overlays") or []
        domain_agent_cache: dict = ctx.setdefault("_domain_agent_cache", {})

        events: list = []

        # ── 领域路由 ──
        skill = await route_skill(article_text, skills, router_llm)
        logger.info(
            "领域路由结果：skill=%s allowed_tools=%s",
            skill.name, list(skill.allowed_tools),
        )

        # ── 工具禁用处理 ──
        disabled = self._normalize_disabled(disabled_tools_raw)
        removed = tuple(t for t in skill.allowed_tools if t in disabled)
        effective = tuple(t for t in skill.allowed_tools if t not in disabled)
        if removed:
            logger.info(
                "用户禁用工具 %s；领域 %s 剩余可用工具 %s",
                list(removed), skill.name, list(effective),
            )
        effective_skill = replace(skill, allowed_tools=effective)
        if not effective:
            events.append(
                StatusEvent(
                    stage="route",
                    message=f"领域 {skill.name} 的工具已被全部禁用，核查能力受限，将基于已有信息保守判定",
                    details={"skill": skill.name, "disabled_tools": ", ".join(removed)},
                )
            )

        # ── Overlay 处理 ──
        active_overlays = []
        for raw_overlay in overlays_raw:
            try:
                active_overlays.append(build_overlay_skill(raw_overlay))
            except (ValueError, TypeError) as exc:
                logger.warning("跳过无效的附加视角 overlay：%s", exc)
                events.append(
                    StatusEvent(
                        stage="route",
                        message=f"已跳过无效的附加视角：{exc}",
                    )
                )
        if active_overlays:
            logger.info(
                "启用 %d 个 overlay：%s",
                len(active_overlays),
                [o.name for o in active_overlays],
            )

        events.append(
            StatusEvent(
                stage="route",
                message=f"匹配领域 skill：{skill.name}",
                details={
                    "skill": skill.name,
                    "allowed_tools": ", ".join(effective),
                    "overlays": ", ".join(overlay.name for overlay in active_overlays),
                    "disabled_tools": ", ".join(removed),
                    "effective_tools": ", ".join(effective),
                },
            )
        )

        # ── 领域专家 Agent 实例化 ──
        domain_agent = domain_agent_cache.get(skill.name)
        if domain_agent is None:
            domain_agent = get_domain_agent(skill.name, effective_skill, llm)
            domain_agent_cache[skill.name] = domain_agent
            logger.info(
                "领域专家 Agent 已实例化：%s（类型=%s）",
                skill.name, type(domain_agent).__name__,
            )

        return NodeOutput(
            data={
                "skill": skill,
                "effective_skill": effective_skill,
                "domain_agent": domain_agent,
                "removed_tools": removed,
                "active_overlays": active_overlays,
            },
            events=events,
        )

    @staticmethod
    def _normalize_disabled(disabled_tools: list[str] | None) -> frozenset[str]:
        from agent.tools.registry import TOOL_REGISTRY

        if not disabled_tools:
            return frozenset()
        return frozenset(
            str(t).strip()
            for t in disabled_tools
            if isinstance(t, str) and str(t).strip() in TOOL_REGISTRY
        )
