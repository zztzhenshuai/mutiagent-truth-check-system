"""
agent/workflow/node.py

DAG 工作流节点的抽象基类和数据模型。

设计原则：
  - WorkflowNode 是纯协议：name + execute(ctx) → NodeOutput
  - 节点不持有 Agent 引用，所需依赖通过 ctx 注入
  - FunctionalNode 提供便捷包装，避免为每个小节点写类

流式输出协议：
  需要实时产出事件的节点（如 DebateNode）覆盖 execute_streaming(ctx, queue)。
  queue 是 asyncio.Queue，节点把事件 put 进去，引擎实时 drain 并 yield。
  最后把 NodeOutput 也 put 进 queue，引擎识别后停止 drain。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class NodeOutput:
    """节点执行结果。

    Attributes:
        next_node: 强制指定下一节点名（覆盖 DAG 边），None = 走边
        data: 写入 WorkflowContext 的键值对
        events: 本节点产出的 SSE 事件列表
    """
    next_node: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    events: list[Any] = field(default_factory=list)


class WorkflowContext(dict):
    """DAG 工作流共享上下文。

    继承 dict，节点通过 ctx[key] 读写共享状态。

    约定键名（渐进添加）：
      article_text, overlays, disabled_tools,
      claims, plan, skill, effective_skill, domain_agent,
      removed_tools, claim_records, annotation_count,
      context_ready, context_skipped, summary_event, done_event,
      _llm, _router_llm, _skills, _domain_agent_cache,
    """
    pass


class WorkflowNode(ABC):
    """DAG 节点抽象基类。

    每个子类必须定义 name 类属性。节点是无状态的，
    只依赖 ctx 参数。节点内部不应修改非本节点产出的 ctx 键。

    Class Attributes:
        name: 唯一标识（如 "scan"、"plan"），与 DAG 配置中的节点名对应
        description: 节点用途说明（文档用）
        input_keys: 必需的 ctx 键（文档用，非强制校验）
        output_keys: 写入 ctx 的键（文档用，非强制校验）
    """

    name: str = ""
    description: str = ""
    input_keys: tuple[str, ...] = ()
    output_keys: tuple[str, ...] = ()

    @abstractmethod
    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        """执行节点逻辑（批量模式）。

        子类实现此方法完成节点工作。引擎调用 execute() 后，
        通过 NodeOutput.events 获取所有事件并逐条 yield。

        Args:
            ctx: 工作流共享上下文，可读写。

        Returns:
            NodeOutput: 包含写入 ctx 的数据、产出事件、下一步指示。
        """
        ...

    async def execute_streaming(
        self, ctx: WorkflowContext, queue: asyncio.Queue
    ) -> None:
        """执行节点逻辑（流式模式，可选覆盖）。

        与 execute() 的批量模式不同，此方法将事件实时 put 进 queue，
        引擎并行 drain queue 并 yield，避免长尾阻塞。

        调用约定：
          1. 处理过程中：queue.put_nowait(event)  逐条放入事件
          2. 处理完成后：queue.put_nowait(output)  放入 NodeOutput 作终止信号

        默认实现：调用 execute() → put 所有 events → put NodeOutput。
        需要流式输出的节点（如 DebateNode）覆盖此方法。
        """
        output = await self.execute(ctx)
        for event in output.events:
            queue.put_nowait(event)
        queue.put_nowait(output)

    def can_skip(self, ctx: WorkflowContext) -> bool:
        """是否可跳过此节点（子类可选覆盖，用于动态调度）。

        引擎在调用 execute() 前检查 can_skip()：
          - 返回 True → 跳过 execute()，节点输出空 NodeOutput
          - 返回 False → 正常执行

        子类覆盖此方法实现动态跳过逻辑。
        """
        return False


class FunctionalNode(WorkflowNode):
    """将 async 函数包装为 WorkflowNode。

    用于快速把现有函数（如 scan_article、build_plan）适配为节点，
    无需为每个简单节点写类。
    """

    def __init__(
        self,
        name: str,
        fn: Callable[[WorkflowContext], Any],
        *,
        description: str = "",
        input_keys: tuple[str, ...] = (),
        output_keys: tuple[str, ...] = (),
        can_skip_fn: Callable[[WorkflowContext], bool] | None = None,
    ):
        object.__setattr__(self, 'name', name)
        self.description = description
        self.input_keys = input_keys
        self.output_keys = output_keys
        self._fn = fn
        self._can_skip = can_skip_fn

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        result = await self._fn(ctx)
        if isinstance(result, NodeOutput):
            return result
        # 如果 fn 返回了非 NodeOutput 的东西（如 dict），
        # 则自动包装为 NodeOutput.data
        if isinstance(result, dict):
            return NodeOutput(data=result)
        return NodeOutput()

    def can_skip(self, ctx: WorkflowContext) -> bool:
        if self._can_skip:
            return self._can_skip(ctx)
        return False
