"""
agent/workflow/nodes/summary.py

SummaryNode + EmptySummaryNode — 生成最终总结和 DoneEvent。
"""

from __future__ import annotations

import logging

from ..node import NodeOutput, WorkflowContext, WorkflowNode

logger = logging.getLogger("agent.workflow.nodes.summary")


class SummaryNode(WorkflowNode):
    """生成总结事件 + DoneEvent。

    ctx 输入键：
      - claim_records: list[ClaimDebateRecord]
      - annotation_count: int
      - plan: VerificationPlan

    ctx 输出键：
      - summary_event: SummaryEvent
      - done_event: DoneEvent
    """

    name = "summary"
    description = "生成总结 + DoneEvent"
    input_keys = ("claim_records", "annotation_count", "plan")
    output_keys = ("summary_event", "done_event")

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        from agent.debate import build_claim_result, build_summary_event
        from agent.models import DoneEvent, StatusEvent

        claim_records = ctx.get("claim_records", [])
        annotation_count = ctx.get("annotation_count", 0)
        plan = ctx.get("plan")

        total_claims = len(plan.claims) if plan else 0

        summary_event = build_summary_event(claim_records)
        logger.info(
            "全部声明处理完毕：claims=%d annotations=%d",
            total_claims, annotation_count,
        )

        status_event = StatusEvent(
            stage="complete",
            message=f"分析完成，共输出 {annotation_count} 条错误标注",
            details={
                "total_annotations": annotation_count,
                "summary_available": True,
                "reverify_supported": True,
            },
        )

        done_event = DoneEvent(
            total_annotations=annotation_count,
            total_claims=total_claims,
            summary_available=True,
            reverify_supported=True,
            claim_results=[build_claim_result(record) for record in claim_records],
        )

        return NodeOutput(
            data={
                "summary_event": summary_event,
                "done_event": done_event,
            },
            events=[summary_event, status_event, done_event],
        )


class EmptySummaryNode(WorkflowNode):
    """零声明时的快速结束节点。

    ctx 输出键：
      - summary_event: SummaryEvent
      - done_event: DoneEvent
    """

    name = "summary_empty"
    description = "零声明快速结束"

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        from agent.debate import build_summary_event
        from agent.models import DoneEvent, StatusEvent

        logger.info("零声明，直接结束")

        # 构建零声明总结，与原始 run() 行为一致
        summary_event = build_summary_event([])
        status_event = StatusEvent(
            stage="complete",
            message="分析完成，共输出 0 条错误标注",
            details={
                "total_annotations": 0,
                "summary_available": True,
                "reverify_supported": True,
            },
        )
        done_event = DoneEvent(
            total_annotations=0,
            total_claims=0,
            summary_available=True,
            reverify_supported=True,
            claim_results=[],
        )

        return NodeOutput(
            data={"summary_event": summary_event, "done_event": done_event},
            events=[summary_event, status_event, done_event],
        )
