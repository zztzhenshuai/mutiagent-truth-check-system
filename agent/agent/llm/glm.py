"""
agent/llm/glm.py

智谱 GLM-4-Flash 客户端，用于简单任务（规划器可疑度排序）。
GLM 提供 OpenAI 兼容接口，无需 tool use。
需要环境变量：GLM_API_KEY、GLM_BASE_URL
"""

import os
from typing import AsyncGenerator, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from .base import BaseLLMClient

MODEL = "glm-4-flash"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"


class GLMClient(BaseLLMClient):

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=os.environ["GLM_API_KEY"],
            base_url=os.environ.get("GLM_BASE_URL", DEFAULT_BASE_URL),
        )

    async def complete(self, messages: list[dict]) -> str:
        response = await self._client.chat.completions.create(
            model=MODEL,
            messages=cast(list[ChatCompletionMessageParam], messages),
            max_tokens=2048,
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else ""

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> dict:
        raise NotImplementedError("GLMClient 不支持原生 tool use，请使用 ReAct 文本协议或支持 tool use 的客户端")

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
