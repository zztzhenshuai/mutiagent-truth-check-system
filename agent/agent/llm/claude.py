"""
agent/llm/claude.py

Claude API 客户端（Sonnet 4.6），用于复杂推理任务和 tool use。
需要环境变量：ANTHROPIC_API_KEY
"""

import logging
import os
from typing import Any, AsyncGenerator, cast

import anthropic
from anthropic.types import MessageParam, TextBlock, ToolParam, ToolUseBlock

from .base import BaseLLMClient

logger = logging.getLogger("agent.llm.claude")

MODEL = "claude-sonnet-4-6"


def _split_system_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system_parts: list[str] = []
    chat_messages: list[dict] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            if content:
                system_parts.append(str(content))
            continue
        chat_messages.append(message)

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return system_prompt, chat_messages


class ClaudeClient(BaseLLMClient):

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            base_url=os.environ["ANTHROPIC_BASE_URL"],
        )

    async def complete(self, messages: list[dict]) -> str:
        system_prompt, chat_messages = _split_system_messages(messages)
        logger.debug("complete() -> %s，%d 条消息", MODEL, len(chat_messages))
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=cast(list[MessageParam], chat_messages),
            )
        except Exception:
            logger.warning("Claude complete() 调用失败", exc_info=True)
            raise
        logger.debug(
            "complete() 完成：stop_reason=%s usage=%s",
            response.stop_reason, getattr(response, "usage", None),
        )
        for block in response.content:
            if isinstance(block, TextBlock):
                return block.text
        return ""

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> dict:
        """
        tools 格式（Anthropic tool use schema）：
        [{"name": "...", "description": "...", "input_schema": {...}}]

        返回：
        {"content": str | None, "tool_use": {"name": str, "input": dict} | None}
        """
        system_prompt, chat_messages = _split_system_messages(messages)
        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=cast(list[ToolParam], tools),
            messages=cast(list[MessageParam], chat_messages),
        )

        text_content: str | None = None
        tool_use: dict[str, Any] | None = None

        for block in response.content:
            if isinstance(block, TextBlock):
                text_content = block.text
            elif isinstance(block, ToolUseBlock):
                tool_use = {"name": block.name, "input": block.input}

        return {"content": text_content, "tool_use": tool_use}

    async def complete_stream(
        self, messages: list[dict]
    ) -> AsyncGenerator[str, None]:
        """使用 Anthropic streaming API 逐 token yield 回复。"""
        system_prompt, chat_messages = _split_system_messages(messages)
        async with self._client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=cast(list[MessageParam], chat_messages),
        ) as stream:
            async for text in stream.text_stream:
                yield text
