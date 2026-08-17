"""
agent/workflow/nodes/scan.py

ScanNode — 包装 agent.scanner.scan_article()，提取文章中的可疑声明。
"""

from __future__ import annotations

import logging

from ..node import NodeOutput, WorkflowContext, WorkflowNode

logger = logging.getLogger("agent.workflow.nodes.scan")


class ScanNode(WorkflowNode):
    """扫描文章，提取待核查声明。

    ctx 输入键：
      - article_text: str
      - _llm: BaseLLMClient

    ctx 输出键：
      - claims: list[Claim]
    """

    name = "scan"
    description = "扫描文章提取可疑声明"
    input_keys = ("article_text", "_llm")
    output_keys = ("claims",)

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        from agent.models import ErrorEvent, StatusEvent
        from agent.scanner import scan_article

        article_text = ctx["article_text"]
        llm = ctx["_llm"]

        events: list = [
            StatusEvent(
                stage="scan",
                message="开始扫描文章，提取可疑声明",
                details={"article_length": len(article_text)},
            )
        ]

        try:
            claims = await scan_article(article_text, llm)
        except Exception as exc:
            logger.exception("文章扫描失败")
            events.append(
                ErrorEvent(claim_id=None, message=f"文章扫描失败：{exc}")
            )
            return NodeOutput(
                data={"claims": []},
                events=events,
                next_node="summary",  # 扫描失败直接跳到总结
            )

        logger.info("扫描完成：提取到 %d 条候选声明", len(claims))
        events.append(
            StatusEvent(
                stage="scan",
                message=f"文章扫描完成，提取到 {len(claims)} 条候选声明",
                details={"claim_candidates": len(claims)},
            )
        )

        return NodeOutput(
            data={"claims": claims},
            events=events,
        )
