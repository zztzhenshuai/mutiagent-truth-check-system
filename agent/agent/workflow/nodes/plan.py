"""
agent/workflow/nodes/plan.py

PlanNode — 包装 agent.planner.build_plan()，计算声明分数 + 复杂度分类。
"""

from __future__ import annotations

import logging

from ..node import NodeOutput, WorkflowContext, WorkflowNode

logger = logging.getLogger("agent.workflow.nodes.plan")


class PlanNode(WorkflowNode):
    """对声明列表计算 suspicion_score、判断复杂度，生成验证计划。

    ctx 输入键：
      - claims: list[Claim]

    ctx 输出键：
      - plan: VerificationPlan

    注意：即使 claims 为空也照常执行，PlanEvent(total=0) 是前端必需的信号。
    空声明的快速路径由 DAG 条件边处理（plan → summary_empty）。
    """

    name = "plan"
    description = "计算 suspicion_score + 复杂度分类，生成验证计划"
    input_keys = ("claims",)
    output_keys = ("plan",)

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        from agent.models import PlanEvent
        from agent.planner import build_plan

        claims = ctx["claims"]
        plan = build_plan(claims)

        logger.info(
            "规划完成：%d 条声明，按可疑度排序 -> %s",
            len(plan.claims),
            [(c.id, round(c.suspicion_score, 2)) for c in plan.claims],
        )

        # 复杂度分布统计
        from agent.models import ComplexityLevel
        dist: dict[ComplexityLevel, int] = {"simple": 0, "medium": 0, "complex": 0}
        for c in plan.claims:
            dist[c.complexity] = dist.get(c.complexity, 0) + 1
        logger.info(
            "复杂度分布：simple=%d medium=%d complex=%d",
            dist["simple"], dist["medium"], dist["complex"],
        )

        plan_event = PlanEvent(
            total=len(plan.claims),
            claims=[
                {
                    "id": claim.id,
                    "text": claim.text,
                    "suspicion_score": claim.suspicion_score,
                    "complexity": claim.complexity,
                    "complexity_confidence": round(claim.complexity_confidence, 3),
                }
                for claim in plan.claims
            ],
        )

        return NodeOutput(
            data={"plan": plan},
            events=[plan_event],
        )
