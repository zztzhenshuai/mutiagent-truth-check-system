"""
agent/workflow/engine.py

WorkflowEngine — 读取 DAG 配置，动态遍历节点图，汇聚 SSE 事件流。

执行语义：
  - 从 entry_node 开始，沿边动态前进（非静态拓扑展开）
  - 每个节点执行后，resolve_next() 根据 ctx + 条件边决定下一节点
  - can_skip() → 跳过 execute()，直接走边
  - next_node 覆盖 → 跳过 DAG 边，强制跳转到指定节点
  - 扇出（一条边 → 多个下游）→ 并发执行（第二期特性，当前未启用）
  - 防环路：同一节点不会在同一次 run 中被执行两次
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

from .config import DAGConfig, load_dag_config
from .node import NodeOutput, WorkflowContext

logger = logging.getLogger("agent.workflow.engine")


class WorkflowEngine:
    """DAG 工作流引擎。

    用法:
        config = load_dag_config("agent/workflow/default_dag.yaml")
        engine = WorkflowEngine(config)
        async for event in engine.run(ctx):
            yield event
    """

    def __init__(self, config: DAGConfig | str | Path):
        """初始化引擎。

        Args:
            config: DAGConfig 实例或 YAML 配置文件路径。
        """
        if isinstance(config, (str, Path)):
            self._config = load_dag_config(config)
        else:
            self._config = config
            # 对于手动构造的 DAGConfig，仅计算 entry_node（不跑完整 validate，
            # 因为测试可能故意构造有环/有问题的 DAG）
            if not self._config.entry_node:
                self._config.entry_node = self._resolve_entry_node()

        logger.info(
            "WorkflowEngine 初始化完成：%d 个节点，入口=%s",
            len(self._config.nodes),
            self._config.entry_node,
        )

    @property
    def config(self) -> DAGConfig:
        return self._config

    def _resolve_entry_node(self) -> str:
        """从 DAG 节点和边中计算入度为 0 的入口节点。

        仅做简单计算，不跑完整 validate()，兼容测试中故意构造的非标准 DAG。
        若找不到唯一入口，回退到节点列表的第一个（尽力而为）。
        """
        all_names = set(self._config.nodes)
        if not all_names:
            return ""

        in_degree: dict[str, int] = {n: 0 for n in all_names}
        for from_n, to_list in self._config.edges.items():
            for to_n in to_list:
                if to_n in in_degree:
                    in_degree[to_n] += 1
        for ce in self._config.conditional_edges:
            if ce.to_if_true in in_degree:
                in_degree[ce.to_if_true] += 1
            if ce.to_if_false is not None and ce.to_if_false in in_degree:
                in_degree[ce.to_if_false] += 1

        zero_in = [n for n, d in in_degree.items() if d == 0]
        if len(zero_in) == 1:
            return zero_in[0]
        # 多入口或无入口时回退到第一个注册节点
        return next(iter(all_names)) if all_names else ""

    async def run(
        self,
        ctx: WorkflowContext,
    ) -> AsyncGenerator[Any, None]:
        """主入口：从 entry_node 开始，沿 DAG 边动态遍历执行节点。

        流程（状态机式）：
        1. current = entry_node
        2. 执行 current：can_skip? → execute → yield events → update ctx
        3. resolve_next(current, ctx) → 下一节点（考虑条件边）
        4. 若 next_node 覆盖 → 使用覆盖值
        5. current = next，循环直到 current is None
        6. 防环路守卫：同一节点不执行两次
        """
        current = self._config.entry_node
        executed: set[str] = set()

        while current is not None:
            # 防环路守卫
            if current in executed:
                logger.warning(
                    "检测到环路：节点 %s 已被执行过，终止遍历（已执行=%s）",
                    current, executed,
                )
                break
            executed.add(current)

            node = self._config.nodes.get(current)
            if node is None:
                logger.error("节点 %s 不在已注册节点中，终止遍历", current)
                break

            # ── 检查是否可跳过 ──
            if node.can_skip(ctx):
                logger.info("⏭ 跳过节点 %s（can_skip=True）", current)
                # 走边进入下一节点（不执行 execute）
                current = self._config.resolve_next(current, ctx)
                continue

            # ── 执行节点 ──
            logger.info("▶ 执行节点 %s", current)
            output: NodeOutput = NodeOutput()
            try:
                # 流式执行：节点把事件实时 put 进 queue，引擎并行 drain
                event_queue: asyncio.Queue = asyncio.Queue()
                node_task = asyncio.create_task(
                    node.execute_streaming(ctx, event_queue)
                )

                while True:
                    # 等待事件或任务完成
                    if node_task.done():
                        # 任务异常 → 向上抛
                        exc = node_task.exception()
                        if exc:
                            raise exc
                        # 正常完成 → drain 剩余事件后退出
                        while not event_queue.empty():
                            item = event_queue.get_nowait()
                            if isinstance(item, NodeOutput):
                                output = item
                            else:
                                yield item
                        break

                    try:
                        item = await asyncio.wait_for(
                            event_queue.get(), timeout=0.1
                        )
                    except asyncio.TimeoutError:
                        continue  # 任务未完成，继续轮询

                    if isinstance(item, NodeOutput):
                        output = item
                        # NodeOutput 是终止信号，但可能后面还有残余事件
                        # 等任务完成后再 drain（上面的 node_task.done() 分支）
                    else:
                        yield item  # ★ 实时转发 SSE 事件

            except Exception as exc:
                logger.exception("节点 %s 执行失败", current)
                from agent.models import ErrorEvent
                yield ErrorEvent(
                    claim_id=None,
                    message=f"工作流节点 {current} 执行失败：{exc}",
                )
                # 执行失败时尝试走边继续（避免整个管线卡死）
                current = self._config.resolve_next(current, ctx)
                continue

            # ── 更新上下文 ──
            if output.data:
                ctx.update(output.data)
                logger.debug(
                    "节点 %s 更新 ctx 键：%s",
                    current, list(output.data.keys()),
                )

            # ── 决定下一节点 ──
            if output.next_node is not None:
                logger.info(
                    "节点 %s 指定下一节点=%s（覆盖 DAG 边）",
                    current, output.next_node,
                )
                current = output.next_node
            else:
                current = self._config.resolve_next(current, ctx)
                if current is not None:
                    logger.debug("边：%s → %s", current, current)

        logger.info(
            "WorkflowEngine 执行完毕，遍历节点=%s",
            " → ".join(executed),
        )
