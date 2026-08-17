"""
agent/llm/deepseek.py

DeepSeek API 客户端（DeepSeek-V3），OpenAI 兼容接口，支持 tool use。
需要环境变量：DEEPSEEK_API_KEY
可选环境变量：DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）
"""

import json
import os
from typing import Any, AsyncGenerator, cast

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from .base import BaseLLMClient

MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekClient(BaseLLMClient):

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        )

    async def complete(self, messages: list[dict]) -> str:
        response = await self._client.chat.completions.create(
            model=MODEL,
            messages=cast(list[ChatCompletionMessageParam], messages),
            max_tokens=4096,
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else ""

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> dict:
        """
        tools 格式（OpenAI function calling schema，会自动转换 Anthropic 风格输入）：
        [{"name": "...", "description": "...", "input_schema": {...}}]

        返回：
        {"content": str | None, "tool_use": {"name": str, "input": dict} | None}
        """
        # 将 Anthropic 风格的 tools 转换为 OpenAI 风格
        openai_tools: list[ChatCompletionToolParam] = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })

        response = await self._client.chat.completions.create(
            model=MODEL,
            messages=cast(list[ChatCompletionMessageParam], messages),
            max_tokens=4096,
            tools=openai_tools,
        )

        choice = response.choices[0]
        msg = choice.message

        text_content: str | None = msg.content if msg.content else None
        tool_use: dict[str, Any] | None = None

        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.type == "function":
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {"raw": tc.function.arguments}
                    tool_use = {"name": tc.function.name, "input": args}
                    break  # 只取第一个 tool call

        return {"content": text_content, "tool_use": tool_use}

    async def complete_stream(
        self, messages: list[dict]
    ) -> AsyncGenerator[str, None]:
        """使用 OpenAI 兼容 streaming API 逐 token yield 回复。"""
        response = await self._client.chat.completions.create(
            model=MODEL,
            messages=cast(list[ChatCompletionMessageParam], messages),
            max_tokens=4096,
            stream=True,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content
