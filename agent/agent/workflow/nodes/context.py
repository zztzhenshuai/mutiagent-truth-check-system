"""
agent/workflow/nodes/context.py

CrossReferenceNode — 包装 agent.tools.cross_reference.prepare_cross_reference_context()。
"""

from __future__ import annotations

import logging

from ..node import NodeOutput, WorkflowContext, WorkflowNode

logger = logging.getLogger("agent.workflow.nodes.context")


class CrossReferenceNode(WorkflowNode):
    """准备交叉引用上下文（句向量预热）。

    ctx 输入键：
      - article_text: str
      - _llm: BaseLLMClient
      - plan: VerificationPlan（用于动态跳过判断）

    ctx 输出键：
      - context_ready: bool
    """

    name = "context"
    description = "准备交叉引用上下文（句向量预热）"
    input_keys = ("article_text", "_llm")
    output_keys = ("context_ready",)

    def can_skip(self, ctx: WorkflowContext) -> bool:
        """动态跳过：扫描结果 < 3 条声明时可跳过交叉引用预热。"""
        plan = ctx.get("plan")
        if plan is not None and len(plan.claims) < 3:
            logger.info("声明数=%d < 3，跳过交叉引用预热", len(plan.claims))
            return True
        return False

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        from agent.models import StatusEvent
        from agent.tools.cross_reference import prepare_cross_reference_context

        article_text = ctx["article_text"]
        llm = ctx["_llm"]

        events: list = [
            StatusEvent(stage="context", message="准备交叉引用上下文")
        ]

        try:
            await prepare_cross_reference_context(article_text, llm=llm)
            logger.info("交叉引用上下文准备完成")
            events.append(
                StatusEvent(stage="context", message="交叉引用上下文准备完成")
            )
            return NodeOutput(
                data={"context_ready": True},
                events=events,
            )
        except Exception as exc:
            logger.warning("交叉引用上下文准备失败，已跳过：%s", exc)
            events.append(
                StatusEvent(
                    stage="context",
                    message=f"交叉引用上下文准备失败，已跳过：{exc}",
                )
            )
            return NodeOutput(
                data={"context_ready": False, "context_skipped": True},
                events=events,
            )
