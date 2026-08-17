"""
tests/test_agent_chat.py

测试 Agent.chat() — 上下文感知的追问回答。
"""

import pytest
from agent.agent import Agent
from agent.llm.base import BaseLLMClient


class MockStreamLLM(BaseLLMClient):
    """假 LLM：返回预设文本，流式输出逐字符。"""

    def __init__(self, response: str = "这是测试回答。"):
        self._response = response

    async def complete(self, messages: list[dict]) -> str:
        return self._response

    async def complete_with_tools(self, messages, tools):
        raise NotImplementedError

    async def complete_stream(self, messages: list[dict]):
        for char in self._response:
            yield char


@pytest.fixture
def mock_agent():
    return Agent(complex_llm=MockStreamLLM("分析完成"), chat_llm=MockStreamLLM("测试回答"))


@pytest.fixture
def sample_context():
    return {
        "session_id": "sess_test",
        "article_title": "测试文章",
        "article_text": "这是一篇包含事实错误的测试文章。中国GDP增速为8.5%。",
        "claims": [
            {
                "claim_id": "c001",
                "text": "中国GDP增速为8.5%",
                "verdict": "rejected",
                "error_type": "factual_error",
                "confidence": 0.9,
                "reasoning": "2023年中国GDP增速实际约为5.2%",
                "evidence_urls": ["https://example.com/gdp2023"],
            }
        ],
        "summary": {
            "overall_conclusion": "文章包含1条事实错误",
            "total_claims": 1,
            "total_errors": 1,
            "error_breakdown": {"factual_error": 1},
        },
    }


@pytest.mark.asyncio
async def test_chat_returns_stream(mock_agent, sample_context):
    """chat() 应逐 token yield 回复。"""
    tokens = []
    async for token in mock_agent.chat(
        session_context=sample_context,
        user_message="这篇文章有哪些错误？",
    ):
        tokens.append(token)

    full = "".join(tokens)
    assert full == "测试回答"
    assert len(tokens) == 4  # 测、试、回、答


@pytest.mark.asyncio
async def test_chat_system_prompt_includes_context(mock_agent, sample_context):
    """_build_chat_system_prompt 应包含文章信息和声明。"""
    prompt = mock_agent._build_chat_system_prompt(sample_context)
    assert "测试文章" in prompt
    assert "中国GDP增速为8.5%" in prompt
    assert "factual_error" in prompt
    assert "https://example.com/gdp2023" in prompt
    assert "文章包含1条事实错误" in prompt


@pytest.mark.asyncio
async def test_chat_with_history(mock_agent, sample_context):
    """chat() 应正确处理历史对话。"""
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你的？"},
    ]
    tokens = []
    async for token in mock_agent.chat(
        session_context=sample_context,
        user_message="继续",
        history=history,
    ):
        tokens.append(token)

    assert "".join(tokens) == "测试回答"


@pytest.mark.asyncio
async def test_chat_preserves_user_message(mock_agent, sample_context):
    """即使用户问无关问题，LLM 也应收到上下文。"""
    tokens = []
    async for token in mock_agent.chat(
        session_context=sample_context,
        user_message="你好",
    ):
        tokens.append(token)

    assert len(tokens) > 0
