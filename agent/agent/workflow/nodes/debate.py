"""
agent/workflow/nodes/debate.py

DebateNode — 并行处理所有声明（Verifier/Challenger/Judge 辩论流）。

这是整个管线中最复杂的节点。它完整迁移 Agent.run() 中的
并发声明处理逻辑（asyncio.Queue + Semaphore + _debate_claim）。

关键设计：覆盖 execute_streaming() 而非 execute()。
  - execute_streaming() 逐条 put 事件到 queue → 引擎实时转发给前端
  - 对比批量模式（execute）：15 个 claim 无需等全部完成才出第一个结果
  - Semaphore(3) 控制并发，内部 event_queue 保证事件按到达顺序产出

ctx 输入键：
  - _agent: Agent 实例（用于调用 _debate_claim 等私有方法）
  - plan: VerificationPlan
  - effective_skill: Skill
  - active_overlays: list[Skill]
  - domain_agent: DomainAgent
  - removed_tools: tuple[str, ...]

ctx 输出键：
  - claim_records: list[ClaimDebateRecord]
  - annotation_count: int
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..node import NodeOutput, WorkflowContext, WorkflowNode

logger = logging.getLogger("agent.workflow.nodes.debate")

_MAX_CONCURRENT_CLAIMS = 3
_SENTINEL = object()


class DebateNode(WorkflowNode):
    """并行处理所有声明，执行 Verifier→Challenger→Judge 辩论流。"""

    name = "debate"
    description = "并行处理所有声明（Verifier/Challenger/Judge 辩论流）"
    input_keys = ("plan", "effective_skill", "domain_agent", "removed_tools")
    output_keys = ("claim_records", "annotation_count")

    def can_skip(self, ctx: WorkflowContext) -> bool:
        plan = ctx.get("plan")
        if plan is None or len(plan.claims) == 0:
            logger.info("无可验证声明，跳过辩论节点")
            return True
        return False

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        """批量模式（兼容旧路径）：收集所有事件再返回。"""
        result_sink: dict[str, NodeOutput | None] = {"output": None}
        queue: asyncio.Queue = asyncio.Queue()

        async def _collector():
            events: list = []
            node_task = asyncio.create_task(
                self.execute_streaming(ctx, queue)
            )
            while True:
                if node_task.done():
                    exc = node_task.exception()
                    if exc:
                        raise exc
                    while not queue.empty():
                        item = queue.get_nowait()
                        if isinstance(item, NodeOutput):
                            result_sink["output"] = item
                        else:
                            events.append(item)
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if isinstance(item, NodeOutput):
                    result_sink["output"] = item
                else:
                    events.append(item)
            output = result_sink["output"] or NodeOutput()
            output.events = events
            return output

        return await _collector()

    async def execute_streaming(
        self, ctx: WorkflowContext, engine_queue: asyncio.Queue
    ) -> None:
        """流式执行辩论：逐条 put 事件到 engine_queue，引擎实时转发。

        核心流程（与原 Agent.run() 第 264-354 行完全一致）：
        1. 为每个 claim 创建 _process_one_claim 协程（Semaphore(3) 并发）
        2. 协程把事件 put 进内部 event_queue
        3. 主循环从内部 queue get 事件 → 立即 put 到 engine_queue
        4. 全部 claim 完成后 put NodeOutput(data=...) 作为终止信号
        """
        from agent.models import AnnotationEvent, ErrorEvent, StatusEvent

        agent = ctx["_agent"]
        plan = ctx["plan"]
        effective_skill = ctx["effective_skill"]
        active_overlays = ctx.get("active_overlays") or []
        domain_agent = ctx["domain_agent"]
        removed_tools = ctx.get("removed_tools", ())

        internal_queue: asyncio.Queue = asyncio.Queue()
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CLAIMS)

        annotation_count = 0
        claim_records: list = []

        async def _process_one_claim(claim) -> None:
            nonlocal annotation_count
            async with semaphore:
                strategy = domain_agent.merge_strategy(claim.complexity)

                from agent.agent import _STRATEGY_MAP as _SM
                _base = _SM[claim.complexity]
                if strategy != _base:
                    diffs = []
                    for fld in (
                        'max_react_steps', 'enable_challenger', 'enable_judge',
                        'enable_rebuttal', 'enable_reflexion', 'high_confidence_threshold',
                    ):
                        bv = getattr(_base, fld)
                        sv = getattr(strategy, fld)
                        if bv != sv:
                            diffs.append(f'{fld}:{bv}→{sv}')
                    logger.info(
                        "[%s] 领域 Agent %s 策略覆盖 (complexity=%s)：%s",
                        claim.id, type(domain_agent).__name__,
                        claim.complexity, ', '.join(diffs),
                    )

                plan.status[claim.id] = "running"
                preview = claim.text if len(claim.text) <= 80 else claim.text[:80] + "..."
                logger.info(
                    "[%s] 开始验证声明 score=%.2f complexity=%s strategy=%s domain=%s：%s",
                    claim.id, claim.suspicion_score, claim.complexity,
                    strategy.label, type(domain_agent).__name__, preview,
                )
                internal_queue.put_nowait(
                    StatusEvent(
                        stage="verify",
                        claim_id=claim.id,
                        message=f"[{type(domain_agent).__name__} · {strategy.label}] {preview}",
                        details={
                            "complexity": claim.complexity,
                            "max_steps": strategy.max_react_steps,
                            "domain_agent": type(domain_agent).__name__,
                        },
                    )
                )

                record_sink: dict[str, None] = {"record": None}
                try:
                    async for event in agent._debate_claim(
                        claim, effective_skill, active_overlays, record_sink,
                        removed_tools,
                        strategy=strategy,
                        domain_agent=domain_agent,
                    ):
                        internal_queue.put_nowait(event)
                        if isinstance(event, AnnotationEvent) and event.error_type is not None:
                            annotation_count += 1
                except Exception as exc:
                    plan.status[claim.id] = "error"
                    logger.exception("[%s] 验证流程异常终止", claim.id)
                    internal_queue.put_nowait(
                        ErrorEvent(claim_id=claim.id, message=f"验证失败：{exc}")
                    )
                    claim_records.append(
                        agent._build_failed_claim_record(claim, effective_skill.name, str(exc))
                    )
                else:
                    plan.status[claim.id] = "done"
                    logger.info("[%s] 验证完成", claim.id)
                    if record_sink["record"] is not None:
                        claim_records.append(record_sink["record"])
                finally:
                    internal_queue.put_nowait(_SENTINEL)

        tasks = [
            asyncio.create_task(_process_one_claim(claim))
            for claim in plan.claims
        ]

        completed = 0
        total = len(plan.claims)
        while completed < total:
            item = await internal_queue.get()
            if item is _SENTINEL:
                completed += 1
            else:
                # ★ 关键：直接 put 到引擎 queue，实时转发
                engine_queue.put_nowait(item)

        # 防御性清理
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "辩论节点完成：claims=%d annotations=%d",
            len(plan.claims), annotation_count,
        )

        # 终止信号：不含 events 的 NodeOutput（事件已逐条发出）
        engine_queue.put_nowait(
            NodeOutput(
                data={
                    "claim_records": claim_records,
                    "annotation_count": annotation_count,
                },
            )
        )
