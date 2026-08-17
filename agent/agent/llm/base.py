"""
agent/llm/base.py

LLM 客户端抽象基类。所有 LLM 实现必须继承此类。
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator


class BaseLLMClient(ABC):

    @abstractmethod
    async def complete(self, messages: list[dict]) -> str:
        """
        发送消息列表，返回 LLM 的文本回复。
        messages 格式遵循 OpenAI convention：
            [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        """
        ...

    @abstractmethod
    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> dict:
        """
        支持 tool use 的调用，返回原始响应 dict，包含：
            {"content": str | None, "tool_use": {"name": str, "input": dict} | None}
        不支持 tool use 的实现（如 GLMClient）应抛出 NotImplementedError。
        """
        ...

    async def complete_stream(
        self, messages: list[dict]
    ) -> AsyncGenerator[str, None]:
        """
        流式发送消息列表，逐 token yield 文本。
        默认实现：调用 complete() 后模拟逐字符 yield（非真正流式）。
        子类应覆盖此方法提供原生流式支持。
        """
        full = await self.complete(messages)
        for char in full:
            yield char
